#!/usr/bin/env python3
"""PaperRead 閱讀同步服務（跑在常開的主機上，只用標準函式庫）。

只聽 127.0.0.1，由 `tailscale serve --set-path /paperread-api` 掛到 tailnet 網域下。
每篇論文存一份狀態：只含 kind=highlight 的 notes（刪除留 tombstone）與 progress。
合併規則跟 PaperRead easyread/store.py 的 apply_ops 一樣是「較新的 updated / at 勝」，
差別是這裡用解析後的時間比較，並把時間戳一律正規化成 PaperRead 的格式
（YYYY-MM-DDTHH:MM:SS.mmm+08:00），讓 Mac 端 apply_ops 的字串比較結果一致。

API（前綴 /paperread-api 有沒有被 tailscale 剝掉都能用）：
  GET  /health                     -> {ok, seq, papers, time}
  GET  /state[?since=N]            -> {seq, papers: {pid: {notes, progress}}}（since：只回 seq > N 的變更）
  GET  /state/<pid>[?since=N]      -> {seq, pid, notes, progress}
  POST /ops  {ops:[{pid, op, ...}], client}
       op = note      {note:{id, kind:"highlight", updated, ...}}
            note_del  {id, at}
            progress  {block, off, ratio, at}
       -> {seq, applied:[...], rejected:[...]}

    python server.py [--host 127.0.0.1] [--port 8791] [--data DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PREFIX = "/paperread-api"
TZ = timezone(timedelta(hours=8))          # 台灣沒有日光節約，固定 +08:00 跟 PaperRead 產生的格式一致
PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_BODY = 2 * 1024 * 1024
MAX_NOTE = 64 * 1024
FUTURE_SLACK = timedelta(minutes=5)

LOCK = threading.Lock()
DATA: Path = Path(".")
STATE: dict = {}          # pid -> {"notes": {}, "progress": {}, "seqs": {"n:<id>": seq, "progress": seq}}
META = {"seq": 0}
LOG_FILE: Path | None = None


# --------------------------------------------------------------------------- 工具
def log(msg: str) -> None:
    line = f"{datetime.now(TZ).isoformat(timespec='seconds')} {msg}"
    if LOG_FILE:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:   # 簡單輪替，避免無限長
                os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    else:
        print(line, flush=True)


def fmt(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+08:00"


def parse_ts(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def norm_ts(s) -> str | None:
    """解析並正規化；超過現在 5 分鐘以上的未來時間（裝置時鐘錯）截成現在，免得永遠贏。"""
    dt = parse_ts(s)
    if dt is None:
        return None
    now = datetime.now(TZ)
    if dt > now + FUTURE_SLACK:
        dt = now
    return fmt(dt)


def newer(a: str | None, b: str | None) -> bool:
    """a 是否嚴格比 b 新（b 缺 = 最舊）。"""
    da, db = parse_ts(a), parse_ts(b)
    if da is None:
        return False
    return db is None or da > db


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:            # Windows：別的行程正在讀，稍等重試
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- 狀態
def load_all() -> None:
    (DATA / "papers").mkdir(parents=True, exist_ok=True)
    meta = DATA / "meta.json"
    if meta.is_file():
        META.update(json.loads(meta.read_text("utf-8")))
    for f in (DATA / "papers").glob("*.json"):
        if PID_RE.match(f.stem):
            st = json.loads(f.read_text("utf-8"))
            st.setdefault("notes", {})
            st.setdefault("progress", {})
            st.setdefault("seqs", {})
            STATE[f.stem] = st
            META["seq"] = max([META["seq"], *st["seqs"].values()] or [0])


def paper(pid: str) -> dict:
    return STATE.setdefault(pid, {"notes": {}, "progress": {}, "seqs": {}})


def save(pids: set[str]) -> None:
    for pid in pids:
        write_json_atomic(DATA / "papers" / f"{pid}.json", STATE[pid])
    write_json_atomic(DATA / "meta.json", META)


def bump(st: dict, key: str) -> None:
    META["seq"] += 1
    st["seqs"][key] = META["seq"]


def view(pid: str, since: int) -> dict:
    st = STATE.get(pid) or {"notes": {}, "progress": {}, "seqs": {}}
    seqs = st["seqs"]
    notes = {nid: n for nid, n in st["notes"].items() if seqs.get("n:" + nid, 0) > since}
    prog = st["progress"] if seqs.get("progress", 0) > since else {}
    return {"notes": notes, "progress": prog}


def apply_one(op: dict) -> str:
    """套用一個 op；回傳 'applied:<描述>' 或拋 ValueError（格式錯）或回 'skip:<描述>'（較舊）。"""
    pid = op.get("pid")
    if not isinstance(pid, str) or not PID_RE.match(pid):
        raise ValueError("bad pid")
    kind = op.get("op")
    if kind == "note":
        note = op.get("note")
        if not isinstance(note, dict):
            raise ValueError("note must be object")
        if len(json.dumps(note, ensure_ascii=False)) > MAX_NOTE:
            raise ValueError("note too large")
        nid = note.get("id")
        if not isinstance(nid, str) or not NID_RE.match(nid):
            raise ValueError("bad note id")
        if note.get("kind") != "highlight":
            raise ValueError("only kind=highlight notes are synced")
        if not isinstance(note.get("anchor"), str) or not isinstance(note.get("quote"), str):
            raise ValueError("note needs anchor and quote")
        upd = norm_ts(note.get("updated") or op.get("at"))
        if not upd:
            raise ValueError("bad note.updated")
        note = {k: v for k, v in note.items() if not k.startswith("_")}
        note["updated"] = upd
        if note.get("created"):
            note["created"] = norm_ts(note["created"]) or upd
        if note.get("deleted") is not True:
            note.pop("deleted", None)
        st = paper(pid)
        cur = st["notes"].get(nid)
        if cur and not newer(upd, cur.get("updated")):
            return f"skip:note:{pid}:{nid}"
        st["notes"][nid] = note
        bump(st, "n:" + nid)
        return f"applied:note:{pid}:{nid}"
    if kind == "note_del":
        nid = op.get("id")
        if not isinstance(nid, str) or not NID_RE.match(nid):
            raise ValueError("bad note id")
        at = norm_ts(op.get("at"))
        if not at:
            raise ValueError("bad at")
        st = paper(pid)
        cur = st["notes"].get(nid)
        if cur:
            ok = newer(at, cur.get("updated")) or (not cur.get("deleted") and at == cur.get("updated"))
            if not ok:
                return f"skip:note_del:{pid}:{nid}"
        tomb = {"id": nid, "kind": "highlight", "deleted": True, "updated": at}
        if cur and cur.get("anchor"):
            tomb["anchor"] = cur["anchor"]
        st["notes"][nid] = tomb
        bump(st, "n:" + nid)
        return f"applied:note_del:{pid}:{nid}"
    if kind == "progress":
        block = op.get("block")
        if not isinstance(block, str) or not 0 < len(block) <= 200:
            raise ValueError("bad block")
        at = norm_ts(op.get("at"))
        if not at:
            raise ValueError("bad at")
        st = paper(pid)
        if not newer(at, st["progress"].get("at")):
            return f"skip:progress:{pid}"
        prog = {"block": block, "at": at}
        for key in ("ratio", "off"):
            v = op.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
                prog[key] = round(min(1.0, max(0.0, float(v))), 4)
        st["progress"] = prog
        bump(st, "progress")
        return f"applied:progress:{pid}"
    raise ValueError(f"unknown op {kind!r}")


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PaperReadSync/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt_, *args):   # 預設寫 stderr；改寫到 log 檔（只記錯誤與寫入，見下）
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        u = urlsplit(self.path)
        path = u.path
        if path == PREFIX or path.startswith(PREFIX + "/"):   # tailscale 沒剝前綴時也能用
            path = path[len(PREFIX):]
        path = "/" + path.strip("/")
        q = parse_qs(u.query)
        return path, q

    def do_GET(self):
        path, q = self._route()
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            return self._send(400, {"error": "bad since"})
        with LOCK:
            if path in ("/", "/health"):
                return self._send(200, {"ok": True, "seq": META["seq"], "papers": len(STATE),
                                        "time": fmt(datetime.now(TZ))})
            if path == "/state":
                papers = {}
                for pid in STATE:
                    v = view(pid, since)
                    if v["notes"] or v["progress"]:
                        papers[pid] = v
                return self._send(200, {"seq": META["seq"], "papers": papers})
            m = re.fullmatch(r"/state/([^/]+)", path)
            if m and PID_RE.match(m.group(1)):
                return self._send(200, {"seq": META["seq"], "pid": m.group(1), **view(m.group(1), since)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path, _ = self._route()
        if path != "/ops":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            return self._send(413 if n > MAX_BODY else 400, {"error": "bad body size"})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            ops = body.get("ops")
            if not isinstance(ops, list) or len(ops) > 5000:
                raise ValueError("ops must be a list")
        except (ValueError, AttributeError) as e:
            return self._send(400, {"error": f"bad json: {e}"})
        client = str(body.get("client") or "")[:64]
        applied, rejected, touched = [], [], set()
        with LOCK:
            for i, op in enumerate(ops):
                try:
                    if not isinstance(op, dict):
                        raise ValueError("op must be object")
                    r = apply_one(op)
                except (ValueError, TypeError, KeyError) as e:
                    rejected.append({"index": i, "error": str(e)})
                    continue
                if r.startswith("applied:"):
                    applied.append(r[8:])
                    touched.add(op["pid"])
            if touched:
                save(touched)
            seq = META["seq"]
        if applied or rejected:
            log(f"ops client={client} applied={applied} rejected={rejected}")
        self._send(200, {"seq": seq, "applied": applied, "rejected": rejected})


def main():
    global DATA, LOG_FILE
    ap = argparse.ArgumentParser(description="PaperRead 閱讀同步服務")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--log", default="", help="log 檔（預設 <data>/../server.log；- 代表 stdout）")
    args = ap.parse_args()
    DATA = Path(args.data).resolve()
    if args.log != "-":
        LOG_FILE = Path(args.log) if args.log else DATA.parent / "server.log"
    load_all()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    log(f"start pid={os.getpid()} listen={args.host}:{args.port} data={DATA} seq={META['seq']} papers={len(STATE)} py={sys.version.split()[0]}")
    try:
        httpd.serve_forever()
    finally:
        log("stop")


if __name__ == "__main__":
    main()
