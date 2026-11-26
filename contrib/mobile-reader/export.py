#!/usr/bin/env python3
"""PaperRead 文獻庫 -> iPad 唯讀靜態網站（PWA）。

    uv run export.py [--library ~/PaperRead/library] [--out site] [--force]

只「讀」文獻庫（先 rsync 一份快照到 .work/library 再處理），不寫入、不修改原資料夾；
排版用 vendor/easyread 鎖版複本（見 PATCHES.md），與主程式完全獨立。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / ".work"

# 讓 vendored easyread 不碰使用者的 ~/PaperRead（config / prefs 都落在工作區）
os.environ["EASYREAD_HOME"] = str(WORK / "home")
os.environ["EASYREAD_LANG"] = "zh-TW"
(WORK / "home").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(HERE / "vendor"))
sys.path.insert(0, str(HERE))

import figcrop  # noqa: E402
from easyread.build import build  # noqa: E402
from easyread.store import Workspace  # noqa: E402
from PIL import Image  # noqa: E402

SNAPSHOT_EXCLUDES = [".easyread-lock.json", "history/", "extract/"]
PARTS = ("paper.json", "reader.json", "layout.json", "discussion.json", "item.json")
MARK = HERE / "vendor" / "easyread" / "web" / "paperread-mark.png"
SYNC_JS = HERE / "web" / "prsync.js"      # 畫重點＋進度同步（ipad-reader 自己的腳本，不改 vendor）
API_PATH = "/paperread-api"               # 同步服務（與網站同網域，例如 tailscale serve 掛的路徑）
BG = (246, 243, 236)  # PaperRead 淺色底 #f6f3ec


# --------------------------------------------------------------------------- 快照
def snapshot(library: Path, dest: Path) -> list[str]:
    """rsync 唯讀複製；回傳論文 id 列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete"]
    for ex in SNAPSHOT_EXCLUDES:
        cmd += ["--exclude", ex]
    subprocess.run(cmd + [f"{library}/", f"{dest}/"], check=True)
    return sorted(p.name for p in dest.iterdir() if (p / "paper.json").is_file())


def tool_fingerprint() -> str:
    """工具本身（裁圖、匯出、vendored 排版）有變動就要全部重建。"""
    h = hashlib.sha256()
    for f in [HERE / "figcrop.py", Path(__file__)] + sorted((HERE / "vendor").rglob("*")) + sorted((HERE / "web").rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            st = f.stat()
            h.update(f"{f.relative_to(HERE)}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def input_hash(root: Path, tool: str) -> str:
    h = hashlib.sha256(tool.encode())
    for n in PARTS:
        f = root / n
        h.update(f.read_bytes() if f.is_file() else b"-")
    st = (root / "source.pdf").stat()
    h.update(f"{st.st_size}".encode())
    return h.hexdigest()


def dir_hash(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            h.update(str(f.relative_to(d)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------- 單篇
def head_extra(depth_up: str) -> str:
    return (
        f'<link rel="manifest" href="{depth_up}manifest.webmanifest">\n'
        f'<link rel="apple-touch-icon" href="{depth_up}icons/apple-touch-icon.png">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-title" content="PaperRead">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
        '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f6f3ec">\n'
        '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#1a1917">\n'
    )


def sw_register(depth_up: str) -> str:
    return ("<script>if('serviceWorker' in navigator){addEventListener('load',function(){"
            f"navigator.serviceWorker.register('{depth_up}sw.js').catch(function(e){{console.warn('sw',e)}})}})}}</script>\n")


SYNC_CSS = """<style id="prsync-css">
#prs-hl{position:fixed;z-index:9000;display:none;min-width:96px;height:44px;padding:0 18px;border:0;border-radius:22px;
  background:#f2c94c;color:#2b2410;font:600 16px/44px -apple-system,BlinkMacSystemFont,"PingFang TC",sans-serif;
  box-shadow:0 4px 14px rgba(0,0,0,.22);-webkit-tap-highlight-color:transparent;touch-action:manipulation;cursor:pointer}
#prs-hl.on{display:block}
#prs-pop{position:fixed;z-index:9001;display:none;gap:8px;padding:8px;border-radius:14px;background:var(--surface,#fbfaf6);
  border:1px solid var(--rule,#ddd7ca);box-shadow:0 6px 20px rgba(0,0,0,.2)}
#prs-pop.on{display:flex}
#prs-pop button{min-width:88px;height:44px;padding:0 14px;border-radius:10px;border:1px solid var(--rule,#ddd7ca);
  background:transparent;color:var(--ink,#23211d);font:inherit;font-size:15px;touch-action:manipulation;cursor:pointer}
#prs-pop button[data-act=del]{background:#c0392b;border-color:#c0392b;color:#fff;font-weight:600}
#prs-badge{position:fixed;z-index:8999;left:50%;transform:translateX(-50%);bottom:calc(14px + env(safe-area-inset-bottom,0px));
  display:none;padding:5px 12px;border-radius:99px;background:rgba(40,38,34,.82);color:#fff;font-size:12.5px;pointer-events:none}
#prs-badge.on{display:block}
</style>
"""


def inject_sync(page: str, pid: str) -> str:
    """畫重點與進度同步：
    1) mobile.js 會把 PR.commit 包成「只放行 progress」，在它之前先存下 store.js 的原版（PR._baseCommit）；
    2) 頁尾內嵌 prsync.js（離線時跟著頁面一起被 service worker 快取）。"""
    i = page.find('document.body.classList.add("mobile-read")')
    k = page.rfind("<script>", 0, i)
    if i < 0 or k < 0:
        raise RuntimeError("找不到 mobile.js（vendor 改版了？），無法掛上同步腳本")
    page = page[:k] + "<script>window.PR&&(PR._baseCommit=PR.commit);</script>\n" + page[k:]
    page = page.replace("</head>", SYNC_CSS + "</head>", 1)
    cfg = json.dumps({"pid": pid, "api": API_PATH})
    js = SYNC_JS.read_text("utf-8").replace("</script", "<\\/script")
    k = page.rindex("</body>")
    return page[:k] + f"<script>window.PRSYNC={cfg};</script>\n<script>{js}</script>\n" + page[k:]


def export_paper(snap_root: Path, out_site: Path, tool: str, force: bool) -> dict:
    pid = snap_root.name
    out_dir = out_site / "papers" / pid
    stamp = out_dir / ".input-hash"
    ih = input_hash(snap_root, tool)
    paper0 = json.loads((snap_root / "paper.json").read_text("utf-8"))
    item = json.loads((snap_root / "item.json").read_text("utf-8")) if (snap_root / "item.json").is_file() else {}
    meta = paper0.get("meta", {})
    fig_blocks = [b for b in paper0.get("blocks", []) if b.get("type") in ("figure", "table")]
    info = {"id": pid, "n_fig": sum(b["type"] == "figure" for b in fig_blocks),
            "n_tab": sum(b["type"] == "table" for b in fig_blocks), "cropped": 0, "status": "built"}

    if not force and stamp.is_file() and stamp.read_text() == ih and (out_dir / "index.html").is_file():
        info["status"] = "skipped(unchanged)"
        info["cropped"] = len(list((out_dir / "assets").glob("figs-*.webp"))) if (out_dir / "assets").is_dir() else 0
    else:
        layout = json.loads((snap_root / "layout.json").read_text("utf-8")) if (snap_root / "layout.json").is_file() else {}
        crops = figcrop.crop_paper(snap_root, paper0, layout, snap_root)       # 寫進「快照」的 figs/
        for b in paper0.get("blocks", []):
            if b.get("id") in crops:
                b["src"] = crops[b["id"]] or ""
        info["cropped"] = sum(1 for v in crops.values() if v)
        info["failed"] = [k for k, v in crops.items() if not v]
        (snap_root / "paper.json").write_text(json.dumps(paper0, ensure_ascii=False), "utf-8")   # 只改快照

        if out_dir.exists():
            shutil.rmtree(out_dir)
        (out_dir / "assets").mkdir(parents=True)
        ws = Workspace(snap_root)
        build(ws, out=out_dir / "index.html", assets=out_dir / "assets",
              extra={"backHref": "../../"}, lang="zh-TW", mobile=True)
        page = (out_dir / "index.html").read_text("utf-8")
        page = page.replace('<meta name="viewport" content="width=device-width, initial-scale=1">',
                            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">', 1)
        page = page.replace("/web/paperread-mark.png", "../../icons/paperread-mark.png")
        page = page.replace("</head>", head_extra("../../") + "</head>", 1)
        page = inject_sync(page, pid)
        k = page.rindex("</body>")   # 內嵌的 JS 字串裡也有 "</body>"，要插在最後一個
        page = page[:k] + sw_register("../../") + page[k:]
        (out_dir / "index.html").write_text(page, "utf-8")
        stamp.write_text(ih)

    files = []
    for f in sorted(out_dir.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            rel = f.relative_to(out_dir).as_posix()
            files.append([f"papers/{pid}/" + ("" if rel == "index.html" else rel), f.stat().st_size])  # 資料夾網址，避免伺服器把 index.html 轉址
    info.update({
        "title_zh": meta.get("title_zh") or meta.get("title_en") or pid,
        "title_en": meta.get("title_en") or "",
        "short_zh": meta.get("short_zh") or "",
        "authors": meta.get("authors") or "",
        "kicker": " · ".join(x for x in [meta.get("arxiv"), meta.get("venue"), meta.get("date")] if x),
        "pages": len(meta.get("pages", [])),
        "tags": item.get("tags") or [],
        "starred": bool(item.get("starred")),
        "added": item.get("added") or "",
        "opened": item.get("last_opened") or "",
        "hash": dir_hash(out_dir),
        "size": sum(s for _, s in files),
        "files": files,
        "url": f"papers/{pid}/",
    })
    return info


# --------------------------------------------------------------------------- 站台外殼
def make_icons(out_site: Path):
    d = out_site / "icons"
    d.mkdir(parents=True, exist_ok=True)
    mark = Image.open(MARK).convert("RGBA")
    bb = mark.getbbox()
    mark = mark.crop(bb)

    def tile(size: int, pad: float) -> Image.Image:
        canvas = Image.new("RGB", (size, size), BG)
        inner = int(size * (1 - 2 * pad))
        m = mark.copy()
        m.thumbnail((inner, inner), Image.LANCZOS)
        canvas.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
        return canvas

    tile(180, 0.14).save(d / "apple-touch-icon.png")
    tile(192, 0.14).save(d / "icon-192.png")
    tile(512, 0.14).save(d / "icon-512.png")
    tile(512, 0.24).save(d / "icon-maskable-512.png")   # maskable 安全區要留更多邊
    tile(32, 0.08).save(d / "favicon-32.png")
    small = mark.copy()
    small.thumbnail((128, 128), Image.LANCZOS)
    small.save(d / "paperread-mark.png")   # 閱讀頁左上角的 logo（原本是 /web/ 絕對路徑，子路徑部署會壞）


def write_manifest(out_site: Path):
    m = {
        "name": "PaperRead 閱讀", "short_name": "PaperRead",
        "description": "PaperRead 已翻譯論文的唯讀閱讀器（可離線）",
        "lang": "zh-TW", "start_url": "./", "scope": "./", "id": "./",
        "display": "standalone", "orientation": "any",
        "background_color": "#f6f3ec", "theme_color": "#f6f3ec",
        "icons": [
            {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (out_site / "manifest.webmanifest").write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")


SW_TEMPLATE = r"""/* PaperRead 閱讀：service worker（由 export.py 產生，build __BUILD__） */
const BUILD = '__BUILD__';
const PAPERS = __PAPERS__;                     // { 論文 id: 內容雜湊 }，論文有變動雜湊就變
const SHELL = 'pr-shell-' + BUILD;
const SHELL_URLS = ['./', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png', 'icons/paperread-mark.png'];
const paperCache = (id) => 'pr-paper-' + id + '-' + PAPERS[id];
const ROOT = new URL('./', self.location).pathname;

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  const keep = new Set([SHELL, ...Object.keys(PAPERS).map(paperCache)]);
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('pr-') && !keep.has(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function cacheFirst(req, name) {
  const cache = await caches.open(name);
  const hit = await cache.match(req, { ignoreSearch: true });
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok && !res.redirected) cache.put(req, res.clone());
  return res;
}

async function networkFirst(req) {
  const cache = await caches.open(SHELL);
  try {
    const res = await fetch(req);
    if (res.ok && !res.redirected) cache.put(req, res.clone());
    return res;
  } catch (err) {
    let hit = await cache.match(req, { ignoreSearch: true });
    if (!hit && req.mode === 'navigate') {
      const u = new URL(req.url);
      // 一律用資料夾網址（不帶 index.html）：有些伺服器（tailscale serve）會把 index.html 301 轉址，
      // Safari 不接受 service worker 回傳轉址過的導覽回應
      hit = await cache.match(new URL('./', self.location).href);
    }
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // 同步服務（/paperread-api/）不在 ROOT 底下，這行就會直接放行走網路、不快取
  if (url.origin !== location.origin || !url.pathname.startsWith(ROOT)) return;
  const rel = url.pathname.slice(ROOT.length);
  const m = rel.match(/^papers\/([^/]+)\//);
  if (m && PAPERS[m[1]]) e.respondWith(cacheFirst(req, paperCache(m[1])));
  else e.respondWith(networkFirst(req));
});
"""


SHELF_TEMPLATE = r"""<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>PaperRead 閱讀</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icons/favicon-32.png" type="image/png">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="PaperRead">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f6f3ec">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#1a1917">
<style>
:root{--bg:#f6f3ec;--surface:#fbfaf6;--surface-2:#efebe2;--ink:#23211d;--ink-2:#5a554c;--ink-3:#8c8579;--rule:#ddd7ca;--accent:#2d6173;--accent-soft:rgba(45,97,115,.09);--ok:#3d7a4f;--shadow:0 1px 2px rgba(35,33,29,.06),0 6px 18px rgba(35,33,29,.05);color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#1a1917;--surface:#22211e;--surface-2:#2a2825;--ink:#ddd7cb;--ink-2:#b2aa9c;--ink-3:#7d766b;--rule:#38352f;--accent:#86b8c9;--accent-soft:rgba(134,184,201,.12);--ok:#7fc093;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased;padding:max(20px,env(safe-area-inset-top)) max(20px,env(safe-area-inset-right)) max(32px,env(safe-area-inset-bottom)) max(20px,env(safe-area-inset-left))}
main{max-width:1180px;margin:0 auto}
header.top{display:flex;align-items:center;gap:14px;margin:6px 0 18px}
header.top img{width:44px;height:44px;border-radius:10px;flex:none}
header.top h1{font-size:26px;line-height:1.2;margin:0;font-weight:650;letter-spacing:.01em}
header.top p{margin:2px 0 0;color:var(--ink-3);font-size:14px}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.search{flex:1 1 260px;min-width:0;height:46px;border:1px solid var(--rule);border-radius:12px;background:var(--surface);color:var(--ink);padding:0 14px;font:inherit;font-size:16px;outline:none}
.search:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.btn{height:46px;padding:0 18px;border:1px solid var(--accent);border-radius:12px;background:var(--accent);color:var(--bg);font:inherit;font-size:15px;font-weight:600;cursor:pointer;-webkit-tap-highlight-color:transparent}
.btn[disabled]{opacity:.6}
.btn.ghost{background:transparent;color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
.chip{min-height:40px;padding:0 15px;border:1px solid var(--rule);border-radius:999px;background:var(--surface);color:var(--ink-2);font:inherit;font-size:14.5px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.chip small{opacity:.7;margin-left:4px}
.dl{display:none;margin:12px 0 4px;padding:12px 14px;border:1px solid var(--rule);border-radius:12px;background:var(--surface);font-size:14px;color:var(--ink-2)}
.dl.on{display:block}
.bar{height:8px;border-radius:99px;background:var(--surface-2);overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
h2.group{font-size:15px;color:var(--ink-3);font-weight:600;letter-spacing:.06em;margin:26px 2px 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,330px),1fr));gap:14px}
.card{display:flex;flex-direction:column;gap:6px;padding:16px 18px 14px;border:1px solid var(--rule);border-radius:16px;background:var(--surface);color:inherit;text-decoration:none;box-shadow:var(--shadow);min-height:150px;-webkit-tap-highlight-color:transparent}
.card:active{transform:scale(.995);background:var(--surface-2)}
.card h3{margin:0;font-size:18px;line-height:1.4;font-weight:650}
.card .en{margin:0;font-size:13.5px;line-height:1.45;color:var(--ink-3);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .meta{margin-top:auto;padding-top:8px;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;font-size:13px;color:var(--ink-2)}
.tag{padding:1px 9px;border-radius:99px;background:var(--accent-soft);color:var(--accent);font-size:12.5px}
.off{margin-left:auto;font-size:12.5px;color:var(--ink-3)}
.off.ok{color:var(--ok)}
.prog{font-size:12.5px;color:var(--accent);font-weight:600}
.pbar{height:4px;border-radius:99px;background:var(--surface-2);overflow:hidden;margin-top:6px}
.pbar>i{display:block;height:100%;background:var(--accent)}
.empty{padding:40px 0;text-align:center;color:var(--ink-3)}
footer{margin-top:34px;color:var(--ink-3);font-size:12.5px;line-height:1.7}
@media (max-width:520px){header.top h1{font-size:22px}.btn{flex:1 1 100%}}
</style>
</head>
<body>
<main>
  <header class="top">
    <img src="icons/icon-192.png" alt="">
    <div><h1>PaperRead 閱讀</h1><p id="sub"></p></div>
  </header>
  <div class="tools">
    <input id="q" class="search" type="search" inputmode="search" placeholder="搜尋標題、標籤、作者" aria-label="搜尋論文" autocomplete="off">
    <button id="dlAll" class="btn" type="button">下載全部以供離線閱讀</button>
  </div>
  <div id="dl" class="dl" role="status" aria-live="polite"><div id="dlText"></div><div class="bar"><i id="dlBar"></i></div></div>
  <div id="chips" class="chips" role="group" aria-label="標籤篩選"></div>
  <div id="list"></div>
  <footer id="foot"></footer>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
  'use strict';
  var D = JSON.parse(document.getElementById('data').textContent);
  var papers = D.papers, state = {tag: '', q: ''}, cached = {}, sync = {};
  try { sync = JSON.parse(localStorage.getItem('prsync-shelf') || '{}') || {}; } catch(e) {}
  var $ = function(s){return document.querySelector(s)};
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
  function mb(n){return (n/1048576).toFixed(n>=10485760?0:1)+' MB'}
  function cname(p){return 'pr-paper-'+p.id+'-'+p.hash}

  $('#sub').textContent = papers.length + ' 篇已翻譯論文';
  $('#foot').innerHTML = '匯出於 ' + esc(D.built_at) + ' · build ' + esc(D.build) + '<br>看譯文、切換中英對照、點圖放大；選取文字可畫重點，畫線與閱讀進度會與其他裝置及 Mac 同步。要更新譯文內容請在電腦上重新匯出。';

  function tagsOf(p){return p.tags.length ? p.tags : ['未分類']}
  var allTags = {};
  papers.forEach(function(p){tagsOf(p).forEach(function(t){allTags[t]=(allTags[t]||0)+1})});

  function renderChips(){
    var h = '<button class="chip" type="button" data-tag="" aria-pressed="'+(state.tag===''?'true':'false')+'">全部<small>'+papers.length+'</small></button>';
    Object.keys(allTags).sort().forEach(function(t){
      h += '<button class="chip" type="button" data-tag="'+esc(t)+'" aria-pressed="'+(state.tag===t?'true':'false')+'">'+esc(t)+'<small>'+allTags[t]+'</small></button>';
    });
    $('#chips').innerHTML = h;
  }
  function match(p){
    if(state.tag && tagsOf(p).indexOf(state.tag) < 0) return false;
    if(!state.q) return true;
    var hay = [p.title_zh,p.title_en,p.short_zh,p.authors,p.kicker,p.tags.join(' ')].join(' ').toLowerCase();
    return state.q.toLowerCase().split(/\s+/).every(function(w){return hay.indexOf(w) >= 0});
  }
  function card(p){
    var st = cached[p.id];
    var off = st === 'full' ? '<span class="off ok">✓ 已可離線</span>' : st === 'part' ? '<span class="off">部分離線</span>' : '';
    var sy = sync[p.id] || {}, pct = sy.ratio != null ? Math.round(sy.ratio * 100) : null;
    var prog = (pct != null ? '<span class="prog">讀到 ' + pct + '%</span>' : '') + (sy.hl ? '<span>' + sy.hl + ' 條重點</span>' : '');
    return '<a class="card" href="'+esc(p.url)+'"><h3>'+esc(p.title_zh)+'</h3>'
      + (p.title_en ? '<p class="en" lang="en">'+esc(p.title_en)+'</p>' : '')
      + '<div class="meta">'+p.tags.map(function(t){return '<span class="tag">'+esc(t)+'</span>'}).join('')
      + prog + '<span>'+p.pages+' 頁</span><span>'+(p.n_fig+p.n_tab)+' 張圖表</span><span>'+mb(p.size)+'</span>'+off+'</div>'
      + (pct != null ? '<div class="pbar"><i style="width:'+pct+'%"></i></div>' : '') + '</a>';
  }
  function renderList(){
    var shown = papers.filter(match), h = '';
    if(!shown.length){ $('#list').innerHTML = '<div class="empty">沒有符合的論文</div>'; return; }
    if(state.tag || state.q){
      h = '<div class="grid">'+shown.map(card).join('')+'</div>';
    } else {
      var groups = {};
      shown.forEach(function(p){tagsOf(p).forEach(function(t){(groups[t]=groups[t]||[]).push(p)})});
      Object.keys(groups).sort().forEach(function(t){
        h += '<h2 class="group">'+esc(t)+'（'+groups[t].length+'）</h2><div class="grid">'+groups[t].map(card).join('')+'</div>';
      });
    }
    $('#list').innerHTML = h;
  }
  $('#chips').addEventListener('click', function(e){
    var b = e.target.closest('.chip'); if(!b) return;
    state.tag = b.dataset.tag; renderChips(); renderList();
  });
  $('#q').addEventListener('input', function(e){state.q = e.target.value.trim(); renderList()});

  /* ---- 離線狀態 ---- */
  async function checkCached(){
    if(!('caches' in window)) return;
    for (var i=0;i<papers.length;i++){
      var p = papers[i];
      try{
        if(!(await caches.has(cname(p)))){ cached[p.id] = ''; continue; }
        var c = await caches.open(cname(p)), keys = await c.keys();
        cached[p.id] = keys.length >= p.files.length ? 'full' : (keys.length ? 'part' : '');
      }catch(e){ cached[p.id] = ''; }
    }
    renderList();
  }

  /* ---- 下載全部 ---- */
  var busy = false;
  $('#dlAll').addEventListener('click', async function(){
    if(busy) return;
    if(!('caches' in window)){ alert('這個瀏覽器不支援離線儲存。'); return; }
    busy = true; this.disabled = true;
    try{ if(navigator.storage && navigator.storage.persist) navigator.storage.persist(); }catch(e){}
    var todo = [], total = 0;
    for (var i=0;i<papers.length;i++){
      var p = papers[i], c = await caches.open(cname(p)), have = new Set((await c.keys()).map(function(r){return new URL(r.url).pathname}));
      p.files.forEach(function(f){
        var abs = new URL(f[0], location.href).pathname;
        if(!have.has(abs)){ todo.push({p:p, c:c, url:f[0], size:f[1]}); total += f[1]; }
      });
    }
    var box = $('#dl'), bar = $('#dlBar'), txt = $('#dlText');
    box.classList.add('on');
    if(!todo.length){ txt.textContent = '全部論文都已可離線閱讀。'; bar.style.width = '100%'; busy = false; this.disabled = false; return; }
    var done = 0, bytes = 0, failed = 0, idx = 0;
    function upd(){
      txt.textContent = '下載中… ' + done + ' / ' + todo.length + ' 個檔案 · ' + mb(bytes) + ' / ' + mb(total);
      bar.style.width = (total ? Math.min(100, bytes/total*100) : 100) + '%';
    }
    upd();
    async function worker(){
      while(idx < todo.length){
        var t = todo[idx++];
        try{
          var res = await fetch(new Request(t.url, {cache:'reload'}));
          if(!res.ok || res.redirected) throw new Error(res.status);
          await t.c.put(new Request(t.url), res);
        }catch(e){ failed++; }
        done++; bytes += t.size; upd();
      }
    }
    await Promise.all([worker(),worker(),worker(),worker(),worker()]);
    txt.textContent = failed ? ('完成，但有 ' + failed + ' 個檔案失敗，請連上網路後再按一次。') : ('完成：已下載 ' + mb(total) + '，全部論文可離線閱讀。');
    bar.style.width = '100%';
    busy = false; this.disabled = false;
    checkCached();
  });

  /* ---- 閱讀進度與重點數（同步服務；連不到就用上次記下的） ---- */
  async function loadSync(){
    try{
      var r = await fetch(D.api + '/state', {cache:'no-store'});
      if(!r.ok) return;
      var res = await r.json(), out = {};
      Object.keys(res.papers || {}).forEach(function(pid){
        var st = res.papers[pid], n = 0;
        Object.keys(st.notes || {}).forEach(function(k){ if(!st.notes[k].deleted) n++; });
        out[pid] = {ratio: st.progress && st.progress.ratio != null ? st.progress.ratio : null, hl: n};
      });
      sync = out;
      try { localStorage.setItem('prsync-shelf', JSON.stringify(out)); } catch(e) {}
      renderList();
    }catch(e){}
  }
  document.addEventListener('visibilitychange', function(){ if(!document.hidden) loadSync(); });

  renderChips(); renderList(); checkCached(); loadSync();
  if('serviceWorker' in navigator){ addEventListener('load', function(){ navigator.serviceWorker.register('sw.js').catch(function(e){console.warn('sw',e)}) }); }
})();
</script>
</body>
</html>
"""


def write_shell(out_site: Path, infos: list[dict]):
    now = datetime.now()
    shell_h = hashlib.sha256((SW_TEMPLATE + SHELF_TEMPLATE).encode()).hexdigest()
    build_id = now.strftime("%Y%m%d-%H%M%S-") + hashlib.sha256((shell_h + "".join(i["hash"] for i in infos)).encode()).hexdigest()[:6]
    papers = sorted(infos, key=lambda i: (i["added"] or ""), reverse=True)
    keep = ("id", "title_zh", "title_en", "short_zh", "authors", "kicker", "pages", "tags", "starred", "hash",
            "size", "files", "url", "n_fig", "n_tab")
    data = {"build": build_id, "built_at": now.strftime("%Y-%m-%d %H:%M"), "api": API_PATH,
            "papers": [{k: i[k] for k in keep} for i in papers]}
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    (out_site / "index.html").write_text(SHELF_TEMPLATE.replace("__DATA__", payload), "utf-8")
    (out_site / "sw.js").write_text(
        SW_TEMPLATE.replace("__BUILD__", build_id).replace("__PAPERS__", json.dumps({i["id"]: i["hash"] for i in infos})), "utf-8")
    make_icons(out_site)
    write_manifest(out_site)
    return build_id


def dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def main():
    ap = argparse.ArgumentParser(description="PaperRead 文獻庫 -> iPad 唯讀靜態網站")
    ap.add_argument("--library", default=str(Path.home() / "PaperRead" / "library"), help="PaperRead 文獻庫（只讀）")
    ap.add_argument("--out", default=str(HERE / "site"), help="輸出資料夾（預設 ./site）")
    ap.add_argument("--force", action="store_true", help="忽略增量，全部重建")
    args = ap.parse_args()

    library = Path(args.library).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not library.is_dir():
        sys.exit(f"找不到文獻庫：{library}")
    t0 = time.time()
    snap = WORK / "library"
    ids = snapshot(library, snap)
    if not ids:
        sys.exit("文獻庫裡沒有論文")
    print(f"快照 {len(ids)} 篇 -> {snap}")

    (out / "papers").mkdir(parents=True, exist_ok=True)
    tool = tool_fingerprint()
    infos = []
    for pid in ids:
        r = export_paper(snap / pid, out, tool, args.force)
        infos.append(r)
        n_all = r["n_fig"] + r["n_tab"]
        print(f"  {pid}  {r['short_zh'] or r['title_zh'][:20]:<24} 圖{r['n_fig']:>3} 表{r['n_tab']:>3}  裁圖 {r['cropped']}/{n_all}"
              f"  {r['size']/1048576:6.1f} MB  [{r['status']}]" + (f"  失敗:{r['failed']}" if r.get("failed") else ""))
    # 刪掉文獻庫已不存在的論文輸出
    for d in (out / "papers").iterdir():
        if d.is_dir() and d.name not in ids:
            shutil.rmtree(d)
            print(f"  移除已不在文獻庫的輸出：{d.name}")
    build_id = write_shell(out, infos)
    total = dir_size(out)
    print(f"完成 build {build_id}：{len(infos)} 篇，site = {total/1048576:.1f} MB，用時 {time.time()-t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
