# vendor/easyread 修補紀錄

基底：PaperRead upstream tag `v1.5.3` 的 `easyread/` 套件（`git archive`，唯讀取出）。
以下每項修補在原始碼裡都有 `[ipad-reader patch]` 註解，可 `grep -rn "ipad-reader patch" vendor/` 找到。

| # | 檔案 | 改了什麼 | 為什麼 |
|---|---|---|---|
| 1 | `web/js/reader/render.js` | 新增 `srcMediaHtml(b)`；`figure(b)`／`table(b)` 在 `b.src` 有對應圖檔時輸出 `<img>`（包在 `.figure-media.figure-crop`），沒有才走原本的「圖表保留於原文」佔位框。表格有圖時題注預設在上。 | 原本 JS 渲染完全不看 `src`，圖表永遠是佔位框；iPad 跑的是 JS 渲染版。`figure-zoom.js` 的選擇器 `#paper .figure-media img` 因此自動接上點圖放大。 |
| 2 | `build.py`（`_prerender_paper` 的 table 分支） | table 也讀 `src`，有圖就輸出 `<img>`。 | 靜態預渲染（無 JS 時看到的內容）與 JS 版一致。 |
| 3 | `web/js/reader/figure-zoom.js` | 放大檢視加入雙指捏合縮放（pointer events；原本單指拖曳平移保留）。 | 觸控裝置原本只有 +/- 按鈕，iPad 上用捏合更自然。 |
| 4 | `web/js/reader/store.js`、`web/js/reader/main.js`、`web/css/reader-mobile.css` | 匯出資料可帶 `backHref`；靜態模式有 `backHref` 時顯示左上角返回鈕（回書架），手機版 CSS 不再隱藏 `#backBtn`。 | 原本靜態版一律隱藏返回鈕。 |
| 5 | `web/css/reader-mobile.css`（檔尾追加） | `.figure-crop img { max-width:100%; height:auto; ... }`。 | `.blk-table` 沒有 `img max-width` 規則，寬表會超出版心。 |
| 6 | `web/js/common/util.js`（`PR.api`）、`web/js/common/presence.js`、`web/js/reader/panels.js` | 頁面含內嵌資料（`#pr-data`，即靜態版）時，不呼叫 `/api/*`、不連 `/api/presence` WebSocket、「最近讀過」直接視為空。 | 靜態網站沒有後端，不修的話 console 會有 404／WebSocket 錯誤。 |

未改動：`i18n.py`、KaTeX 預渲染流程（`build.py::_find_node` 用 PATH 上的 `node`，找不到就退回純文字公式）、其他所有檔案。

匯出器（`export.py`）自己在產生的 HTML 上做的後處理（不屬於 vendor 修補）：
manifest／apple-touch-icon／theme-color meta、`viewport-fit=cover`、service worker 註冊、
把 `/web/paperread-mark.png`（絕對路徑，子路徑部署會壞）改成 `../../icons/paperread-mark.png`。

畫重點與進度同步（2026-11-26，同樣不屬於 vendor 修補、vendor 原碼一行未改）：
- `export.py::inject_sync` 在內嵌的 `mobile.js` 之前插入 `<script>PR._baseCommit=PR.commit</script>`，保存 store.js 原版 commit
  （mobile.js 會把它包成只放行 progress）；頁尾內嵌 `web/prsync.js` 與 `window.PRSYNC={pid, api}`，head 加按鈕樣式。
- `prsync.js` 在執行期覆寫 `PR.noteMarkClick`（mobile.js 設成 no-op）改為「刪除畫線」選單、包一層 `PR.commit` 轉送 progress、
  包一層 `PR.watchProgress` 取得啟動完成時機；畫線一律經 `PR._baseCommit` 進 `PR.state.reader`，由原本的 `applyMarks` 定位。
- vendor 升級時要確認：`mobile.js` 仍含 `document.body.classList.add("mobile-read")`（插入點）、`PR.fullText/zhOfEl/isEnEl/markNote/applyMarks/noteMarkClick/watchProgress/readingBlock/readingLine` 仍存在。
- `mac_sync.py` 用 vendored `easyread.store.Workspace.apply_reader_ops` 寫 Mac；vendored `store.py` 與 v1.5.4 逐位元相同（sha256 06ceb098…），mac_sync 會檢查。

## 升級 vendor 的方式
1. `git -C <upstream> archive <新 tag> easyread | tar -x -C vendor`（先備份舊的 vendor/easyread）。
2. `grep -rn "ipad-reader patch"` 舊版，逐項把修補重做到新版；跑 `uv run export.py --force`；看 `verify/` 的截圖對照。
