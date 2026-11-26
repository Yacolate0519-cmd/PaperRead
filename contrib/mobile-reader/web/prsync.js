/* ipad-reader 自己的同步腳本（不是 vendor/easyread 的原碼；由 export.py 內嵌進每篇論文頁）。
   - 選字 → 「畫重點」→ 黃色 highlight note；點畫線 → 「刪除畫線」。
   - 載入時向同步服務（/paperread-api）取這篇的 highlight 與進度，畫出來；別台裝置進度較新就跳過去。
   - 捲動停約 2 秒回報進度。
   - 所有修改先進本機佇列（localStorage），連不到服務就留著，恢復連線再送。
   狀態一律經 PaperRead 自己的 store.commit（mobile.js 包裝前的原版，存在 PR._baseCommit）套進
   PR.state.reader，畫線由 PaperRead 的 applyMarks（quote + prefix/suffix 定位）負責，所以跟 Mac App 的定位規則一致。 */
(function (PR) {
  "use strict";
  const CFG = window.PRSYNC || {};
  const PID = CFG.pid, API = (CFG.api || "/paperread-api").replace(/\/$/, "");
  if (!PR || !PID || !PR._baseCommit) { console.warn("prsync: 缺少設定或 PR._baseCommit，停用同步"); return; }
  const S = PR.state;
  const base = PR._baseCommit;
  const QKEY = "prsync-q-" + PID;
  const client = "web-" + (/iPhone/.test(navigator.userAgent) ? "iphone" : /iPad|Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1 ? "ipad" : "browser") + "-" + PR.uid("c").slice(1, 9);
  const ts = (s) => { const t = Date.parse(s || ""); return isNaN(t) ? 0 : t; };
  let queue = PR.ls.get(QKEY, []);
  if (!Array.isArray(queue)) queue = [];

  /* ---------------------------------------------------------------- 佇列與送出 */
  let flushT = null, retryMs = 2000, sending = false;
  function saveQueue() { PR.ls.set(QKEY, queue); }
  function enqueue(op) {
    op = Object.assign({ pid: PID }, op);
    if (op.op === "progress") queue = queue.filter((o) => o.op !== "progress");
    if (op.op === "note") queue = queue.filter((o) => !(o.op === "note" && o.note.id === op.note.id));
    queue.push(op);
    saveQueue();
    schedule(op.op === "progress" ? 2000 : 250);
  }
  function schedule(ms) { clearTimeout(flushT); flushT = setTimeout(flush, ms); }
  async function flush() {
    if (sending || !queue.length) return;
    sending = true;
    const batch = queue.slice();
    try {
      const r = await fetch(API + "/ops", {
        method: "POST", cache: "no-store", keepalive: batch.length < 50,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops: batch, client }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const res = await r.json();
      (res.rejected || []).forEach((x) => console.warn("prsync: 服務拒收", batch[x.index], x.error));
      queue = queue.filter((o) => !batch.includes(o));
      saveQueue();
      retryMs = 2000;
      setBadge();
      if (queue.length) schedule(50);
    } catch (e) {
      setBadge();
      schedule(retryMs);
      retryMs = Math.min(retryMs * 2, 60000);
    } finally {
      sending = false;
    }
  }
  window.addEventListener("online", () => { retryMs = 2000; flush(); pull(false); });

  /* 待送數量的小標示（離線時才看得到） */
  let badge = null;
  function setBadge() {
    if (!badge) { badge = document.createElement("div"); badge.id = "prs-badge"; document.body.appendChild(badge); }
    const n = queue.filter((o) => o.op !== "progress").length;
    badge.textContent = n ? n + " 條畫線待同步" : "";
    badge.classList.toggle("on", !!n);
  }

  /* ---------------------------------------------------------------- 進度：捲動停 2 秒回報 */
  const mobileCommit = PR.commit;           // mobile.js 的包裝：只放行 progress
  PR.commit = function (op) {
    mobileCommit(op);
    if (op && op.op === "progress" && op.block) enqueue({ op: "progress", block: op.block, off: op.off, ratio: op.ratio, at: op.at || PR.nowIso() });
  };

  function targetTop(p) {
    const el = p.block && document.getElementById("b-" + p.block);
    if (el) {
      const box = el.getBoundingClientRect();
      if (box.height > 0) return Math.max(0, Math.round(scrollY + box.top + box.height * (p.off || 0) - PR.readingLine()));
    }
    const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
    return p.ratio != null ? Math.round(max * p.ratio) : null;
  }
  function jumpTo(p) {
    const to = targetTop(p);
    if (to == null || Math.abs(to - scrollY) < innerHeight * 0.25) return;
    const back = scrollY;
    let touched = false;
    const stop = () => { touched = true; };
    ["touchstart", "wheel", "keydown", "mousedown"].forEach((n) => window.addEventListener(n, stop, { once: true, passive: true }));
    window.scrollTo({ top: to, behavior: "auto" });
    [600, 1600].forEach((ms) => setTimeout(() => { if (!touched) { const t = targetTop(p); if (t != null && Math.abs(t - scrollY) > 2) window.scrollTo({ top: t, behavior: "auto" }); } }, ms));
    const pct = p.ratio != null ? "（" + Math.round(p.ratio * 100) + "%）" : "";
    PR.toast("已跳到其他裝置最新的閱讀位置" + pct, { label: "回到原處", fn: () => window.scrollTo({ top: back, behavior: "auto" }) }, 8000);
  }

  /* ---------------------------------------------------------------- 從服務拉狀態 */
  let lastPull = 0, pulls = 0;
  async function pull(allowJump) {
    let res;
    try {
      const r = await fetch(API + "/state/" + encodeURIComponent(PID), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      res = await r.json();
    } catch (e) { return false; }
    lastPull = Date.now();
    pulls++;
    const mine = S.reader.notes || {};
    for (const [id, sn] of Object.entries(res.notes || {})) {
      const ln = mine[id];
      if (sn.deleted) {
        if (ln && !ln.deleted && ts(sn.updated) >= ts(ln.updated)) { base({ op: "note_del", id, at: sn.updated }); if (ln.anchor) PR.markNote(ln); }
      } else if (!ln || ts(sn.updated) > ts(ln.updated)) {
        base({ op: "note", note: sn, at: sn.updated });
      }
    }
    const sp = res.progress || {}, lp = S.reader.progress || {};
    if (sp.block && ts(sp.at) > ts(lp.at)) {
      base({ op: "progress", block: sp.block, off: sp.off, ratio: sp.ratio, at: sp.at });   // 記下來，不再回報
      if (allowJump && PR.readingBlock() !== sp.block) jumpTo(sp);
    }
    return true;
  }
  let hiddenAt = 0;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { hiddenAt = Date.now(); if (PR.captureProgress) PR.captureProgress(); flush(); return; }
    flush();
    pull(hiddenAt && Date.now() - hiddenAt > 30000);   // 從背景回來超過 30 秒，視同重新打開
  });
  setInterval(() => { if (!document.hidden && Date.now() - lastPull > 55000) pull(false); }, 60000);

  /* ---------------------------------------------------------------- 選字 → 畫重點 */
  const SKIP = ".katex-mathml, .stale-tag, button, .en-title, .num";   // 與 anchor.js 的 textNodes 相同
  function textNodes(root) {
    const out = [];
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let n; (n = w.nextNode());) out.push(n);
    return out;
  }
  function offsetOf(root, container, offset) {   // 與 anchor.js 的 offsetOf 相同
    let pos = 0;
    for (const t of textNodes(root)) {
      if (t === container) return pos + offset;
      const r = document.createRange();
      r.selectNodeContents(t);
      if (r.comparePoint(container, offset) < 0) return pos;
      pos += t.data.length;
    }
    return pos;
  }
  /* 讀選取範圍；跨段選取只取「第一段」被選到的部分（PaperRead 的跨段 parts 格式在手機上不好確認範圍，故不做） */
  function readSel() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const startEl = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    const el = startEl && startEl.closest("#paper .zh, #paper .en");
    if (!el || el.querySelector("textarea")) return null;
    const blk = el.closest(".blk"), zh = PR.zhOfEl(el);
    if (!blk || !zh || !zh.dataset.key) return null;
    const text = PR.fullText(el);
    const crossed = !el.contains(range.endContainer);
    let s = offsetOf(el, range.startContainer, range.startOffset);
    let e = crossed ? text.length : offsetOf(el, range.endContainer, range.endOffset);
    if (e < s) [s, e] = [e, s];
    while (s < e && /\s/.test(text[s])) s++;
    while (e > s && /\s/.test(text[e - 1])) e--;
    const quote = text.slice(s, e);
    if (!quote) return null;
    const out = { anchor: blk.dataset.id, key: zh.dataset.key, quote, prefix: text.slice(Math.max(0, s - 32), s), suffix: text.slice(e, e + 32), crossed };
    if (PR.isEnEl(el)) out.side = "en";
    out.rect = range.getBoundingClientRect();
    return out;
  }

  const btn = document.createElement("button");
  btn.id = "prs-hl"; btn.type = "button"; btn.textContent = "畫重點";
  document.body.appendChild(btn);
  let pending = null, hideT = null;
  function showBtn(p) {
    pending = p;
    clearTimeout(hideT);
    btn.classList.add("on");
    const r = p.rect, w = btn.offsetWidth || 96, h = btn.offsetHeight || 44;
    const x = Math.min(innerWidth - w - 10, Math.max(10, r.left + r.width / 2 - w / 2));
    // 放在選取範圍上方（使用者實測 iOS 原生選單出現在下方），太靠頂就改放下方並避開原生選單
    let y = r.top - h - 12;
    if (y < 10) y = Math.min(innerHeight - h - 10, r.bottom + 64);
    btn.style.left = x + "px"; btn.style.top = y + "px";
  }
  function hideBtn(now) {
    clearTimeout(hideT);
    hideT = setTimeout(() => { btn.classList.remove("on"); pending = null; }, now ? 0 : 400);
  }
  const check = PR.debounce(() => { const p = readSel(); if (p) showBtn(p); else hideBtn(false); }, 180);
  document.addEventListener("selectionchange", check);
  document.addEventListener("mouseup", () => setTimeout(check, 10));
  document.addEventListener("touchend", () => setTimeout(check, 30), { passive: true });
  window.addEventListener("scroll", () => { if (pending && btn.classList.contains("on")) { const p = readSel(); if (p) showBtn(p); } }, { passive: true });

  function createHighlight() {
    const p = pending || readSel();
    hideBtn(true);
    if (!p) return;
    const now = PR.nowIso();
    const note = { id: PR.uid("n"), anchor: p.anchor, key: p.key, quote: p.quote, prefix: p.prefix, suffix: p.suffix,
      kind: "highlight", color: "yellow", body: "", created: now, updated: now };
    if (p.side) note.side = p.side;
    try { window.getSelection().removeAllRanges(); } catch (e) { /* ignore */ }
    base({ op: "note", note, at: now });
    PR.markNote(note);
    enqueue({ op: "note", note, at: now });
    PR.toast(p.crossed ? "已畫重點（跨段選取只畫第一段）" : "已畫重點", { label: "復原", fn: () => removeHighlight(note) }, 3000);
  }
  // 觸控：pointerdown 時選取還在，立刻動作；之後的 click 忽略
  let firedAt = 0;
  btn.addEventListener("pointerdown", (e) => { e.preventDefault(); firedAt = Date.now(); createHighlight(); });
  btn.addEventListener("mousedown", (e) => e.preventDefault());
  btn.addEventListener("click", (e) => { e.preventDefault(); if (Date.now() - firedAt > 800) createHighlight(); });

  /* ---------------------------------------------------------------- 點畫線 → 刪除 */
  function removeHighlight(n) {
    const at = PR.nowIso();
    base({ op: "note_del", id: n.id, at });
    PR.markNote(n);
    enqueue({ op: "note_del", id: n.id, at });
  }
  const pop = document.createElement("div");
  pop.id = "prs-pop";
  pop.innerHTML = '<button type="button" data-act="del">刪除畫線</button><button type="button" data-act="cancel">取消</button>';
  document.body.appendChild(pop);
  let popNote = null;
  function closePop() { pop.classList.remove("on"); popNote = null; }
  pop.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    e.stopPropagation();
    const n = popNote; closePop();
    if (b.dataset.act === "del" && n) { removeHighlight(n); PR.toast("已刪除畫線", { label: "復原", fn: () => restore(n) }, 3000); }
  });
  function restore(n) {
    const now = PR.nowIso();
    const note = Object.assign({}, n, { updated: now });
    delete note.deleted;
    base({ op: "note", note, at: now });
    PR.markNote(note);
    enqueue({ op: "note", note, at: now });
  }
  // 點到別處關閉選單；同一次點擊若是點在畫線上（anchor.js 剛打開選單），不要關
  let openedAt = 0;
  document.addEventListener("click", (e) => { if (popNote && Date.now() - openedAt > 350 && !e.target.closest("#prs-pop")) closePop(); }, true);
  window.addEventListener("scroll", () => { if (popNote) closePop(); }, { passive: true });
  /* anchor.js 點到 mark.hl 會呼叫 PR.noteMarkClick（mobile.js 設成 no-op）；這裡換成刪除選單。只處理 highlight，其他筆記維持唯讀 */
  PR.noteMarkClick = function (m, n) {
    if (!n || n.kind !== "highlight") return;
    popNote = n;
    openedAt = Date.now();
    pop.classList.add("on");
    const r = m.getBoundingClientRect(), w = pop.offsetWidth || 200, h = pop.offsetHeight || 48;
    const x = Math.min(innerWidth - w - 10, Math.max(10, r.left + r.width / 2 - w / 2));
    let y = r.bottom + 10;
    if (y + h > innerHeight - 10) y = Math.max(10, r.top - h - 10);
    pop.style.left = x + "px"; pop.style.top = y + "px";
  };

  /* ---------------------------------------------------------------- 啟動 */
  let started = false;
  function start() {
    if (started) return;
    started = true;
    setBadge();
    flush();
    pull(true);
  }
  const watch = PR.watchProgress;            // main.js 的 boot() 最後才呼叫：版面與上次位置都就緒
  PR.watchProgress = function () { if (watch) watch.apply(this, arguments); setTimeout(start, 300); };
  setTimeout(() => { if (!started && document.querySelector("#paper .blk")) start(); }, 4000);

  PR.prsync = { flush, pull, get queue() { return queue.slice(); }, get pulls() { return pulls; }, client };
})(window.PR);
