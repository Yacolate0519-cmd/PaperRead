# Mobile reader：iPad / iPhone 閱讀原型

在 iPad、iPhone 上閱讀 PaperRead 已翻譯好的論文，可以觸控畫重點，閱讀進度和畫線會與桌面版 App 雙向同步。

這是一個**不修改 PaperRead 本身**的原型：另外匯出一個靜態網站給行動裝置，再用一個小型同步服務交換畫線和進度。目的是驗證這個方向可行，不是可以直接合併的實作。

## 功能

- 書架與單篇閱讀頁，包含譯文和論文裡的圖表（依圖說位置從 PDF 裁出）
- 可「加入主畫面」當作 App 使用（PWA），下載後可離線閱讀
- 選取文字後畫黃色重點，點畫線可刪除
- 開啟論文時跳到其他裝置最後讀到的位置，可一鍵回到原處
- 離線時的畫線與進度會先存在裝置上，恢復連線後補送

## 運作方式

```
 桌面版 PaperRead                     自己的常開主機                    iPad / iPhone
 ~/PaperRead/library  ── export.py ─► 靜態網站（PWA） ── HTTPS ──►   Safari 加入主畫面
   reader.json  ◄── mac_sync.py ──►  sync_server  ◄──── prsync.js ───┘
                    （定期執行）       （畫線＋進度）
```

連線透過 Tailscale 私人網路，論文與筆記只存在自己的電腦上，不經過任何雲端服務。

## 檔案

| 檔案 | 作用 |
|---|---|
| `export.py` | 讀取文獻庫（只讀），用 `easyread/build.py` 產生書架、每篇閱讀頁、manifest 和 service worker |
| `figcrop.py` | 依圖說文字在 `source.pdf` 上的位置裁出圖表（`paper.json` 裡圖表的 `src` 是空的） |
| `web/prsync.js` | 注入閱讀頁：觸控畫線與刪除、回報與還原閱讀進度、離線佇列 |
| `sync_server/server.py` | 同步服務，只用 Python 標準函式庫；每篇論文保存畫線與進度 |
| `mac_sync.py` | 桌面端雙向同步：把服務端較新的變更寫進 `reader.json`，把本機較新的推上服務端 |
| `PATCHES.md` | 匯出時對 `easyread/` 前端做的修改（主要是靜態版顯示圖表、觸控縮放） |

## 設計考量

- **資料留在自己手上**：同步服務跑在使用者自己的機器上，只聽 `127.0.0.1`，再由 Tailscale 提供私人網路內的 HTTPS。
- **衝突處理沿用 PaperRead 既有規則**：`reader.json` 的 op 本來就帶時間戳、可重送，同一物件以較新的為準。服務端用同樣的規則合併；桌面端只透過 `Workspace.apply_reader_ops` 寫入，有原本的跨行程鎖、journal 與快照，App 開著也不會互相覆蓋，App 前端輪詢到變更後會自動重畫。
- **刪除**：與 App 相同，刪除畫線是留下 `deleted: true` 的紀錄，讓刪除也能同步到其他裝置。
- **iOS 的細節**：iOS 原生選字選單出現在選取範圍下方，所以「畫重點」按鈕放在上方；Safari 的 service worker 不接受被轉址過的頁面回應，所以一律使用資料夾網址，也不快取轉址回應。

## 試用方式

> 這個分支只保存原型程式碼。原型開發時使用 `easyread/` v1.5.3 的鎖版複本（放在 `vendor/easyread/`，修改見 `PATCHES.md`），沒有一併放進來。

1. 把 `easyread/` 套件複製到 `contrib/mobile-reader/vendor/easyread/`，套用 `PATCHES.md` 的修改。
2. 匯出網站：`uv run export.py --out site`，把 `site/` 放到任何 HTTPS 靜態伺服器（例如 `tailscale serve --set-path /paperread <site 路徑>`）。
3. 啟動同步服務：`uv run sync_server/server.py --port 8791`，並掛到同一個網域的 `/paperread-api`。
4. 桌面端同步：`PAPERREAD_SYNC_API=https://<主機>/paperread-api uv run mac_sync.py`（可加 `--dry-run` 先看會做什麼），之後可用 launchd 或排程定期執行。

## 已知限制

- 只同步畫線（`kind=highlight`）與閱讀進度，筆記與提問不會同步。
- 存取控制完全依賴 Tailscale 私人網路，服務本身沒有登入機制。
- 衝突以時間較新者為準，依賴各裝置時鐘大致正確。
- 匯出、裁圖與前端修改都在 App 之外進行；若要併入 PaperRead，比較合理的做法是讓現有閱讀頁直接支援觸控與窄螢幕，同步改用既有的 `/api/.../ops`。
