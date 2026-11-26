#!/usr/bin/env python3
"""Mac PaperRead <-> 同步服務 的雙向同步（highlight 與閱讀進度）。

    uv run mac_sync.py [--dry-run] [--only PID] [--no-progress] [--api URL]

規則（每篇論文、每個物件各自比較，較新的 updated / at 勝）：
  服務端較新 -> 轉成 PaperRead op（note / note_del / progress），只經 Workspace.apply_reader_ops 寫入
               （有 dir_lock、journal、snapshot；與 PaperRead v1.5.4 的 easyread/store.py 同一份程式碼，見 vendor/）。
               絕不直接覆寫 reader.json。
  Mac 較新   -> 推到服務端（kind=highlight 的 note；Mac 刪掉的 highlight 推 tombstone；Mac 把 highlight
               改成筆記/提問時，對服務端推 tombstone，閱讀器就不再當畫線顯示）。
服務連不到時安靜結束（exit 0，log 記一行）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("EASYREAD_HOME", str(HERE / ".work" / "home"))   # vendored easyread 不碰 ~/PaperRead 的設定
(HERE / ".work" / "home").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(HERE / "vendor"))
from easyread.store import Workspace  # noqa: E402

LIBRARY = Path.home() / "PaperRead" / "library"
API = os.environ.get("PAPERREAD_SYNC_API", "http://127.0.0.1:8791")
APP_PLIST = Path("/Applications/PaperRead.app/Contents/Info.plist")
# vendor/easyread/store.py 與上游 v1.5.4 的 easyread/store.py 逐位元相同（2026-11-26 比對）；
# 被改動就停止寫入，避免鎖或合併規則跟 App 不一致
STORE_SHA256 = "06ceb0984e635fd85e217cfb91fabbf3a7fcb11372ced434e6f2653116065ff2"
BACKUPS = HERE / "backups"
CLIENT = "ipad-reader-mac_sync"


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def ts(s) -> float:
    if not isinstance(s, str) or not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def http(method: str, url: str, body=None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def writes_allowed() -> tuple[bool, str]:
    store = HERE / "vendor" / "easyread" / "store.py"
    h = hashlib.sha256(store.read_bytes()).hexdigest()
    if h != STORE_SHA256:
        return False, f"vendor store.py 雜湊不符（{h[:12]}），停止寫入"
    try:
        ver = plistlib.loads(APP_PLIST.read_bytes()).get("CFBundleShortVersionString", "")
    except OSError:
        return False, "讀不到 PaperRead.app 版本"
    if not ver.startswith("1.5."):
        return False, f"PaperRead 版本 {ver} 未驗證過（只驗證 1.5.x），停止寫入；請重新比對 easyread/store.py 後更新 mac_sync.py"
    return True, ver


def backup_all(force: bool = False) -> Path | None:
    """寫入前把所有 reader.json 打包；6 小時內已有備份就不重複，最多留 30 份。"""
    BACKUPS.mkdir(exist_ok=True)
    olds = sorted(BACKUPS.glob("reader-*.tar.gz"))
    if olds and not force and time.time() - olds[-1].stat().st_mtime < 6 * 3600:
        return None
    out = BACKUPS / f"reader-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for f in sorted(LIBRARY.glob("*/reader.json")):
            tar.add(f, arcname=f"{f.parent.name}/reader.json")
    for f in sorted(BACKUPS.glob("reader-*.tar.gz"))[:-30]:
        f.unlink()
    return out


def plan(pid: str, mac: dict, srv: dict, with_progress: bool) -> tuple[list, list]:
    """回傳 (要寫進 Mac 的 PaperRead ops, 要推到服務端的 ops)。"""
    to_mac, to_srv = [], []
    mnotes, snotes = mac.get("notes") or {}, srv.get("notes") or {}
    # 服務端 -> Mac
    for nid, sn in snotes.items():
        mn = mnotes.get(nid)
        su = sn.get("updated") or ""
        if sn.get("deleted"):
            if mn and not mn.get("deleted") and mn.get("kind") == "highlight" and ts(su) > ts(mn.get("updated")):
                if su >= (mn.get("updated") or ""):           # apply_ops 用字串比較，兩者要一致才送
                    to_mac.append({"op": "note_del", "id": nid, "at": su})
        else:
            if sn.get("kind") != "highlight" or not isinstance(sn.get("anchor"), str) or not isinstance(sn.get("quote"), str):
                continue
            if mn is None or ts(su) > ts(mn.get("updated")):
                if mn is None or su >= (mn.get("updated") or ""):
                    to_mac.append({"op": "note", "note": sn, "at": su})
    # Mac -> 服務端
    for nid, mn in mnotes.items():
        sn = snotes.get(nid)
        mu = mn.get("updated") or mn.get("created") or ""
        if not mu:
            continue
        if sn is not None and ts(mu) <= ts(sn.get("updated")):
            continue
        if mn.get("kind") == "highlight":
            if mn.get("deleted"):
                to_srv.append({"pid": pid, "op": "note_del", "id": nid, "at": mu})
            elif isinstance(mn.get("anchor"), str) and isinstance(mn.get("quote"), str):
                to_srv.append({"pid": pid, "op": "note", "note": {**mn, "updated": mu}, "at": mu})
        elif sn is not None and not sn.get("deleted"):        # 在 Mac 上被改成筆記／提問
            to_srv.append({"pid": pid, "op": "note_del", "id": nid, "at": mu})
    if with_progress:
        mp, sp = mac.get("progress") or {}, srv.get("progress") or {}
        if sp.get("block") and ts(sp.get("at")) > ts(mp.get("at")) and sp["at"] >= (mp.get("at") or ""):
            op = {"op": "progress", "block": sp["block"], "at": sp["at"]}
            for k in ("ratio", "off"):
                if sp.get(k) is not None:
                    op[k] = sp[k]
            to_mac.append(op)
        elif mp.get("block") and ts(mp.get("at")) > ts(sp.get("at")):
            to_srv.append({"pid": pid, "op": "progress", "block": mp["block"], "at": mp["at"],
                           **{k: mp[k] for k in ("ratio", "off") if mp.get(k) is not None}})
    return to_mac, to_srv


def main() -> int:
    ap = argparse.ArgumentParser(description="PaperRead Mac <-> 同步服務")
    ap.add_argument("--api", default=API)
    ap.add_argument("--only", help="只處理這篇（論文 id）")
    ap.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不寫入也不推送")
    ap.add_argument("--no-progress", action="store_true", help="不同步閱讀進度（只同步畫線）")
    args = ap.parse_args()
    api = args.api.rstrip("/")

    try:
        state = http("GET", api + "/state")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log(f"SKIP 服務連不到：{getattr(e, 'reason', e)}")
        return 0
    papers = state.get("papers") or {}
    ok, why = writes_allowed()
    if not ok:
        log(f"WARN {why}（這次只推不寫）")

    pulled = pushed = 0
    all_push, details = [], []
    backed = False
    for root in sorted(LIBRARY.iterdir()):
        pid = root.name
        if args.only and pid != args.only:
            continue
        if not (root / "paper.json").is_file():
            continue
        ws = Workspace(root)
        try:
            mac = ws.load("reader")
        except Exception as e:  # noqa: BLE001  讀不到就跳過這篇，別覆寫
            log(f"WARN {pid} reader.json 讀不到：{e}")
            continue
        if int(mac.get("schema", 2)) != 2:
            log(f"WARN {pid} reader.json schema={mac.get('schema')}，跳過")
            continue
        to_mac, to_srv = plan(pid, mac, papers.get(pid) or {}, not args.no_progress)
        if to_mac:
            desc = [o["op"] + ":" + (o.get("id") or (o.get("note") or {}).get("id") or o.get("block", "")) for o in to_mac]
            if args.dry_run or not ok:
                details.append(f"{pid} 會寫入 Mac {desc}" + ("" if ok else "（略過：不允許寫入）"))
            else:
                if not backed:
                    b = backup_all()
                    if b:
                        log(f"備份 reader.json -> {b}")
                    backed = True
                res = ws.apply_reader_ops(to_mac, client=CLIENT)
                pulled += len(res.get("applied") or [])
                details.append(f"{pid} 寫入 Mac rev={res.get('rev')} applied={res.get('applied')}")
        all_push += to_srv
    if all_push:
        if args.dry_run:
            details.append(f"會推到服務端 {len(all_push)} 個 op：" + ", ".join(f"{o['pid']}:{o['op']}:{o.get('id') or (o.get('note') or {}).get('id') or o.get('block')}" for o in all_push))
        else:
            try:
                res = http("POST", api + "/ops", {"ops": all_push, "client": CLIENT}, timeout=20)
                pushed = len(res.get("applied") or [])
                if res.get("rejected"):
                    log(f"WARN 服務拒收 {res['rejected']}")
                details.append(f"推到服務端 applied={res.get('applied')}")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                log(f"SKIP 推送失敗：{getattr(e, 'reason', e)}")
    for d in details:
        log("  " + d)
    log(f"OK{' (dry-run)' if args.dry_run else ''} 服務 seq={state.get('seq')} 寫入 Mac {pulled} 個、推上服務 {pushed} 個")
    return 0


if __name__ == "__main__":
    sys.exit(main())
