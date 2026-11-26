"""從 source.pdf 裁出 figure / table 圖片。

PaperRead 自己不裁圖（layout.json 只存位置，且表格的 box 常常只是題注附近的估算）。
這裡的策略：
  graphic  -> layout box 本身就是 PDF 圖形的範圍，直接用。
  caption  -> 用 PDF 文字找到題注的實際位置，往上/下找「看得見的像素」，
              遇到正文、其他圖表、或過大的空白就停；只取圖表本體（不含題注）。
  between  -> 在 layout box 內收邊到有內容的像素。
全部以 pypdfium2 渲染 source.pdf（約 3x），不依賴 PaperRead 的任何執行環境。
"""
from __future__ import annotations

import re
import unicodedata
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops

SCALE = 3.0
WHITE = 245
PAD = 0.007
Y_MIN, Y_MAX = 0.062, 0.945      # 頁首（running head）與頁碼的帶狀區
BLOCK_GAP = 0.012                # 小於這個空白的像素列併成同一塊
GAP = {"figure": 0.034, "table": 0.020}   # 塊與塊之間容許的最大空白（頁高比例）
LEAD_GAP = 0.09                  # 題注到圖表本體之間容許的空白
TABLE_LIKE_H = 0.045             # 圖往上掃、已收進圖形後，遇到比這更高的非圖形塊就停（表格或正文）
MIN_H = 0.02
MAX_EDGE = 2000
QUALITY = 85


def _norm(s: str):
    out, idx = [], []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if "a" <= c <= "z" or "0" <= c <= "9":
                out.append(c)
                idx.append(i)
    return "".join(out), idx


def _clean_caption(cap: str) -> str:
    cap = re.sub(r"\$[^$]*\$", " ", cap or "")
    return cap.replace("*", "").replace("_", " ")


def _page_geom(page):
    l, b, r, t = page.get_cropbox()
    return l, b, r - l, t - b


class PageCtx:
    def __init__(self, doc, n: int):
        self.page = doc[n - 1]
        self.x0, self.y0, self.W, self.H = _page_geom(self.page)
        self.tp = self.page.get_textpage()
        self.text = self.tp.get_text_range()
        self.nt, self.idx = _norm(self.text)
        with closing(self.page.render(scale=SCALE, draw_annots=False)) as bm:
            self.img = bm.to_pil().convert("RGB")
        r, g, b = self.img.split()
        darkest = ImageChops.darker(ImageChops.darker(r, g), b)
        self.mask = darkest.point(lambda v: 255 if v < WHITE else 0)
        self.gfx, self.paths, self.rules = [], [], []
        for o in self.page.get_objects(max_depth=1):
            if o.type not in (2, 3, 4, 5):
                continue
            l, bt, r, t = o.get_bounds()
            box = [(l - self.x0) / self.W, 1 - (t - self.y0) / self.H, (r - self.x0) / self.W, 1 - (bt - self.y0) / self.H]
            if o.type == 2:
                if t - bt < 2.5 and r - l > 0.05 * self.W:
                    self.rules.append(box)
                if r - l > 2 and t - bt > 2 and (r - l) * (t - bt) < 40000:
                    self.paths.append(box)
            elif not (box[2] - box[0] > 0.95 and box[3] - box[1] > 0.9):   # 整頁底圖不算
                self.gfx.append(box)

    def is_graphic(self, x0, x1, y0, y1) -> bool:
        """這個範圍內是不是圖（點陣圖、嵌入的向量圖、或大量向量路徑），而不是表格／文字。"""
        for g in self.gfx:
            inter = min(g[3], y1) - max(g[1], y0)
            if inter > 0.5 * min(g[3] - g[1], y1 - y0) and _overlap_x(g, x0, x1):
                return True
        n = sum(1 for p in self.paths if y0 <= (p[1] + p[3]) / 2 <= y1 and x0 <= (p[0] + p[2]) / 2 <= x1)
        return n >= 6

    def close(self):
        self.img.close(); self.mask.close()
        self.tp.close(); self.page.close()

    def charbox(self, i):
        l, b, r, t = self.tp.get_charbox(i)
        return [(l - self.x0) / self.W, 1 - (t - self.y0) / self.H, (r - self.x0) / self.W, 1 - (b - self.y0) / self.H]

    def find_caption(self, block: dict):
        """題注在頁面上的實際範圍（頁面比例座標）；找不到回傳 None。"""
        raw = block.get("caption_en") or ""
        cap = re.sub(r"\$[^$]*\$", "\x00", raw).replace("*", "").replace("_", " ")
        key, _ = _norm(cap.replace("\x00", " "))
        if len(key) < 8:
            return None
        first, _ = _norm(re.split(r"(?<=[.!?])\s+", cap.replace("\x00", " ").strip())[0])
        a = self.nt.find(first[:28]) if len(first) >= 8 else self.nt.find(key[:28])
        if a < 0:
            # 子題注（例如「Figure 9 (b): Ablation of ...」）：去掉編號前綴再找
            rest = re.sub(r"^\s*(figure|fig\.?|table|tab\.?)\s*[\w.\-]*\s*(\([a-z]\))?\s*[:.]\s*", "", cap, flags=re.I)
            rk, _ = _norm(rest.replace("\x00", " "))
            if len(rk) >= 14:
                a = self.nt.find(rk[:28])
        if a < 0:
            num = _norm(str(block.get("num") or ""))[0]
            label = ("figure", "fig") if block.get("type") == "figure" else ("table", "tab")
            for pfx in (label if num else ()):
                tok = pfx + num
                p = self.nt.find(tok)
                while p >= 0:
                    j = self.idx[p + len(tok) - 1] + 1
                    if j < len(self.text) and self.text[j] in ":.":
                        a = p
                        break
                    p = self.nt.find(tok, p + 1)
                if a >= 0:
                    break
        if a < 0:
            return None
        # 結尾：由後往前，用每個「不含數學式」的句子尾端去找
        end = -1
        pieces = []
        for seg in cap.split("\x00"):
            pieces.extend(re.split(r"(?<=[.!?])\s+", seg))
        for piece in reversed(pieces):
            pk, _ = _norm(piece)
            if len(pk) >= 14:
                tail = pk[-20:]
                p = self.nt.find(tail, a)
                if 0 <= p - a <= len(key) * 1.3 + 60:
                    end = p + len(tail) - 1
                    break
        if end < 0:
            end = min(a + len(key), len(self.idx) - 1)
        boxes = []
        for i in range(self.idx[a], self.idx[end] + 1):
            if not self.text[i].strip():      # 換行、空白的字元框不可靠
                continue
            bx = self.charbox(i)
            if bx[2] > bx[0] and bx[3] > bx[1]:
                boxes.append(bx)
        if not boxes:
            return None
        # 字元流裡混進別處的字（例如標籤「Table 1」排在頁尾）：只留字數最多的那一群（y 相連的行）
        boxes.sort(key=lambda b: b[1] + b[3])
        groups = [[boxes[0]]]
        for bx in boxes[1:]:
            last = groups[-1][-1]
            if (bx[1] + bx[3]) / 2 - (last[1] + last[3]) / 2 > 0.04:
                groups.append([bx])
            else:
                groups[-1].append(bx)
        boxes = max(groups, key=len)
        return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]

    # ---- 像素 ----
    def rows(self, x0, x1, y0, y1):
        """區域內每個像素列是否有內容；回傳 (起始 y 像素, [0/1...])。"""
        w, h = self.mask.size
        box = (int(x0 * w), int(y0 * h), max(int(x0 * w) + 1, int(x1 * w)), max(int(y0 * h) + 1, int(y1 * h)))
        part = self.mask.crop(box)
        return box[1], part.getprojection()[1]

    def ink_bbox(self, x0, x1, y0, y1):
        w, h = self.mask.size
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        bb = self.mask.crop(box).getbbox()
        if not bb:
            return None
        return [(box[0] + bb[0]) / w, (box[1] + bb[1]) / h, (box[0] + bb[2]) / w, (box[1] + bb[3]) / h]


def _overlap_x(b, x0, x1):
    return min(b[2], x1) - max(b[0], x0) > 0.03


def _scan(ctx: PageCtx, x0, x1, ya, yb, going_down: bool, kind: str):
    """從 ya（靠近題注）往 yb 掃，把像素列併成塊，依規則決定收幾塊。回傳 (y0, y1) 或 None。"""
    top, bot = (ya, yb) if going_down else (yb, ya)
    if bot - top <= 0.002:
        return None
    start_px, proj = ctx.rows(x0, x1, top, bot)
    h = ctx.mask.size[1]
    runs, i, n = [], 0, len(proj)
    while i < n:
        if proj[i]:
            j = i
            while j < n and proj[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    blocks = []
    for r in runs:
        if blocks and (r[0] - blocks[-1][1]) / h < BLOCK_GAP:
            blocks[-1][1] = r[1]
        else:
            blocks.append(list(r))
    if not blocks:
        return None
    if not going_down:
        blocks = [[b[0], b[1]] for b in reversed(blocks)]
    gap_lim = GAP[kind]
    chosen = []
    prev_edge = None
    for b in blocks:
        lo, hi = b
        near = (lo if going_down else hi)
        if prev_edge is None:
            gap = (near - (0 if going_down else n)) if False else (lo if going_down else n - hi)
            if gap / h > LEAD_GAP:
                break
        else:
            gap = (lo - prev_edge) if going_down else (prev_edge - hi)
            if gap / h > gap_lim:
                break
        y0 = (start_px + lo) / h
        y1 = (start_px + hi) / h
        gfx = ctx.is_graphic(x0, x1, y0, y1)
        if kind == "table":
            if chosen and gfx:
                break
        else:
            if any(c[2] for c in chosen) and not gfx and (y1 - y0) >= TABLE_LIKE_H:
                break
        chosen.append(b + [gfx])
        prev_edge = hi if going_down else lo
    if not chosen:
        return None
    a = min(c[0] for c in chosen)
    z = max(c[1] for c in chosen)
    return (start_px + a) / h, (start_px + z) / h


def locate(ctx: PageCtx, block: dict, loc: dict, barriers: list, cap: list | None, style: dict, others: list):
    """回傳 (要裁的頁面比例框, (y 下限, y 上限))，或 None。others = 同頁其他題注的框。"""
    src = loc.get("src")
    box = loc["box"]
    if cap is None:
        x0, y0, x1, y1 = box
        y0, y1 = max(y0, Y_MIN), min(y1, Y_MAX)
        bb = ctx.ink_bbox(max(0, x0 - 0.02), min(1, x1 + 0.02), y0, y1)
        return (bb, (y0, y1)) if bb else None
    cx0, cy0, cx1, cy1 = cap
    # ---- 水平範圍 ----
    xc = (cx0 + cx1) / 2
    mid = (cy0 + cy1) / 2
    row = [o for o in others if abs((o[1] + o[3]) / 2 - mid) < 0.05 and (o[2] <= cx0 + 0.01 or o[0] >= cx1 - 0.01)]
    left = [o for o in row if o[2] <= cx0 + 0.01]
    right = [o for o in row if o[0] >= cx1 - 0.01]
    if style["two_col"]:
        xa, xb = (cx0, cx1) if cx1 - cx0 >= 0.55 else (max(0.02, cx0 - 0.012), min(0.98, cx1 + 0.012))
    else:
        xa = (max(o[2] for o in left) + cx0) / 2 if left else (max(0.02, cx0 - 0.03) if right else max(0.02, min(cx0, style["L"] - 0.06)))
        xb = (min(o[0] for o in right) + cx1) / 2 if right else (min(0.98, cx1 + 0.03) if left else min(0.98, max(cx1, style["R"] + 0.06)))
    ups = [b[3] for b in barriers if _overlap_x(b, xa, xb) and b[3] <= cy0 + 0.004]
    # 緊貼在題注旁的其他題注（子表格的 (b)、(c) 標題）算同一組，不當邊界
    downs = [b[1] for b in barriers if _overlap_x(b, xa, xb) and b[1] >= cy1 - 0.004 and not (len(b) == 5 and b[1] - cy1 < 0.02)]
    up_lim = max([Y_MIN] + ups)
    dn_lim = min([Y_MAX] + downs)
    kind = block["type"]
    res = {
        "up": _scan(ctx, xa, xb, cy0 - 0.002, up_lim, False, kind),
        "down": _scan(ctx, xa, xb, cy1 + 0.002, dn_lim, True, kind),
    }
    order = ("down", "up") if block["type"] == "table" and src != "between" else ("up", "down")
    cands = [d for d in order if res[d] and res[d][1] - res[d][0] >= MIN_H]
    if not cands:
        return None

    def span_items(d):
        ya_, yb_ = res[d]
        if kind == "table":
            return [r for r in ctx.rules if ya_ - 0.005 <= r[1] <= yb_ + 0.005 and min(r[2], xb) - max(r[0], xa) > 0.4 * (xb - xa)]
        return [g for g in ctx.gfx if min(g[3], yb_) - max(g[1], ya_) > 0.02 and min(g[2], xb) - max(g[0], xa) > 0.4 * (xb - xa)]

    pick = next((d for d in cands if span_items(d)), cands[0])
    ya, yb = res[pick]
    # 表格／圖比題注寬（雙欄論文裡跨欄的表）：照規則線、圖形的實際寬度放寬
    for r in span_items(pick):
        if r[2] - r[0] > xb - xa:
            xa, xb = min(xa, r[0] - 0.004), max(xb, r[2] + 0.004)
    bb = ctx.ink_bbox(max(0.0, xa), min(1.0, xb), ya, yb)
    if not bb:
        return None
    lim = (max(up_lim, cy1 + 0.001) if pick == "down" else up_lim, dn_lim if pick == "down" else min(dn_lim, cy0 - 0.001))
    return bb, lim


def _save(ctx: PageCtx, box, lim, dst: Path):
    w, h = ctx.img.size
    x0, y0, x1, y1 = box
    x0, x1 = max(0.0, x0 - PAD), min(1.0, x1 + PAD)
    y0, y1 = max(lim[0], y0 - PAD), min(lim[1], y1 + PAD)   # 留白不能蓋到相鄰的題注／正文
    part = ctx.img.crop((int(x0 * w), int(y0 * h), int(round(x1 * w)), int(round(y1 * h))))
    if max(part.size) > MAX_EDGE:
        r = MAX_EDGE / max(part.size)
        part = part.resize((max(1, round(part.width * r)), max(1, round(part.height * r))), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part.save(dst, "WEBP", quality=QUALITY, method=6)
    return part.size


def _style(layout: dict, by_id: dict) -> dict:
    boxes = [bx for bid, l in layout.items() if by_id.get(bid, {}).get("type") not in ("figure", "table")
             for bx in (l.get("boxes") or [l["box"]]) if l.get("src") in ("text", "head")]
    wide = [b for b in boxes if b[2] - b[0] > 0.6]
    narrow = [b for b in boxes if b[2] - b[0] < 0.5]
    two = len(narrow) > 0.3 * max(1, len(boxes)) and len(narrow) >= 6
    if wide:
        L = sorted(b[0] for b in wide)[len(wide) // 10]
        R = sorted(b[2] for b in wide)[-1 - len(wide) // 10]
    else:
        L, R = 0.1, 0.9
    return {"two_col": two, "L": L, "R": R}


def crop_paper(root: Path, paper: dict, layout: dict, out_dir: Path) -> dict:
    """裁出一篇論文所有 figure/table。回傳 {block id: 相對於 root 的路徑 或 None}。"""
    blocks = paper.get("blocks", [])
    by_id = {b.get("id"): b for b in blocks if isinstance(b, dict)}
    targets = [b for b in blocks if b.get("type") in ("figure", "table") and b.get("id") in layout]
    pages = {}
    for b in targets:
        pages.setdefault(layout[b["id"]]["page"], []).append(b)
    result = {b["id"]: None for b in blocks if b.get("type") in ("figure", "table")}
    style = _style(layout, by_id)
    with closing(pdfium.PdfDocument(str(root / "source.pdf"))) as doc:
        for n, bl in sorted(pages.items()):
            if not 1 <= n <= len(doc):
                continue
            ctx = PageCtx(doc, n)
            try:
                caps = {b["id"]: ctx.find_caption(b) for b in bl}
                saved = {}
                # 「圖裡的表格」(tab-figNN，沒有自己的題注)排在後面：從該圖的裁切範圍切出表格那一塊
                bl = sorted(bl, key=lambda b: bool(re.match(r"tab-fig\d+", b["id"])) and not caps[b["id"]])
                for b in bl:
                    m = re.match(r"tab-(fig\d+)", b["id"])
                    if m and not caps[b["id"]] and m.group(1) in saved:
                        fx0, fy0, fx1, fy1 = saved[m.group(1)]
                        blocked = sorted((max(fx0, g[0]), min(fx1, g[2])) for g in ctx.gfx
                                         if min(g[3], fy1) - max(g[1], fy0) > 0.04 and g[2] > fx0 and g[0] < fx1)
                        free, cur = [], fx0
                        for u, v in blocked:
                            if u - cur > 0: free.append((cur, u))
                            cur = max(cur, v)
                        if fx1 - cur > 0: free.append((cur, fx1))
                        span = max(free, key=lambda f: f[1] - f[0]) if free else (fx0, fx1)
                        bb = ctx.ink_bbox(span[0], span[1], fy0, fy1)
                        if bb:
                            rel = f"figs/{b['id']}.webp"
                            _save(ctx, bb, (fy0, fy1), out_dir / rel)
                            result[b["id"]] = rel
                            continue
                    loc = layout[b["id"]]
                    barriers = []
                    for bid, l in layout.items():
                        if l.get("page") != n or bid == b["id"]:
                            continue
                        o = by_id.get(bid)
                        if o and o.get("type") in ("figure", "table"):
                            if caps.get(bid):
                                barriers.append(list(caps[bid]) + ["cap"])
                        elif l.get("src") == "text":      # head 是估算的、between 是缺口，邊界不可靠
                            barriers.extend(l.get("boxes") or [l["box"]])
                    others = [c for bid, c in caps.items() if c and bid != b["id"]]
                    got = locate(ctx, b, loc, barriers, caps.get(b["id"]), style, others)
                    if not got:
                        continue
                    box, lim = got
                    saved[b["id"]] = box
                    rel = f"figs/{b['id']}.webp"
                    _save(ctx, box, lim, out_dir / rel)
                    result[b["id"]] = rel
            finally:
                ctx.close()
    return result
