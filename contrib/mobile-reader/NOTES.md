# Mobile reader（iPad / iPhone 閱讀）— 開發紀錄

> 狀態：個人使用中的原型，放在 fork 的 `feat/mobile-reader` 分支保存，**尚未整理成可 PR 的形式**。
> 原始工作目錄在作者 Mac 的 `PaperRead-tools/ipad-reader/`（這裡是去掉個人主機設定後的副本）。

## 解決什麼問題
在 Mac 翻譯好的論文，想在 iPad / iPhone 上讀（含圖表），並且：
- 可以離線讀（PWA，加到主畫面）
- 觸控選字畫重點（黃色 highlight）、點畫線刪除
- 閱讀進度與畫重點在 iPad、iPhone、桌面 App 三端同步

## 架構
```
Mac PaperRead (reader.json) ──每 5 分鐘 mac_sync.py──┐
                                                     ├─ sync_server/server.py（常開主機，127.0.0.1:8766，經 tailscale serve 提供 HTTPS）
iPad / iPhone PWA（export.py 產生的靜態網站＋web/prsync.js）─┘
```
| 檔案 | 作用 |
|---|---|
| `export.py` | 用 `easyread/build.py` 把文獻庫匯出成靜態網站：書架、每篇單檔閱讀頁、manifest、service worker（離線快取） |
| `figcrop.py` | 用 pypdfium2 依圖說文字位置從 source.pdf 裁出圖表（paper.json 的 figure `src` 是空的） |
| `web/prsync.js` | 注入閱讀頁：選字畫線、刪除、畫出既有 highlight（quote＋prefix/suffix 定位）、回報進度、開啟時跳到最新進度、離線 op 佇列 |
| `sync_server/server.py` | 標準函式庫 HTTP 服務；每篇只存 kind=highlight 的 notes（刪除為 tombstone）＋progress；較新的 `updated`/`at` 勝 |
| `mac_sync.py` | 雙向同步：服務端較新的 → 轉成 reader op，經 `Workspace.apply_reader_ops`（有 `dir_lock`）寫入；本機較新的 → 推上服務端 |

## 踩過的坑
- Windows 版 `tailscale serve` 會把 `index.html` 301 轉到資料夾網址；Safari 的 service worker 不接受轉址過的導覽回應 → 一律用資料夾網址、不快取 redirected 回應。
- `tailscale serve --set-path` 會剝掉路徑前綴，服務要兩種路徑都接受。
- 常開主機要 `tailscale set --unattended`，否則沒登入 Windows 時連不上。
- iOS 原生選字選單出現在選取範圍下方 → 「畫重點」按鈕放在選取範圍上方。
- PaperRead 刪除 highlight 是留下 `deleted:true` 的 tombstone，不會真的移除。
- 桌面 App 前端每 2.5 秒輪詢 reader 版本，外部經 `apply_reader_ops` 寫入會自動顯示。

## 若要 PR 給上游，建議的做法（與原型不同）
原型為了「不改 App」而另外做了匯出與同步服務；真正併入上游時應該重用現有機制：
1. **觸控畫線**：直接讓現有 reader 頁支援觸控選字（目前畫線 UI 以滑鼠為主），按鈕避開 iOS 選單。
2. **行動版排版**：reader 頁加窄螢幕（≤430px）樣式。
3. **同步**：不需要另一個服務——Docker 瀏覽器版本來就有 `/api/.../ops`，op 本身冪等、帶時間戳。行動裝置直接連 Docker 版即可；離線佇列可沿用前端既有的重送邏輯。
4. **離線閱讀（選配）**：給 reader 頁加 service worker／「下載此篇」，這是上游目前沒有的。
5. 先開 issue 問作者是否接受「行動版＋離線」方向，再拆成小 PR（1、2 最容易被接受）。
