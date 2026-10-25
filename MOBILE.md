# 手機安裝版（PWA）

手機版是 **可安裝的漸進式網頁應用程式（PWA）**，與桌面 Streamlit 共用 Python PDF 拆分、暫存管理及串流傳輸核心。它不是 APK／IPA，也不是手機離線 PDF 引擎。

另已加入 Android APK 專案、簽章驗證與 CI 產物，安裝方式及驗證邊界見 [ANDROID.md](ANDROID.md)。Android APK 同樣需要可連線的後端；PWA 不受影響。

## 只產生摘要

[開啟網頁試用站](https://pdf-split-app.onrender.com)，上傳 PDF 後，在「處理方式」選「只產生摘要（不拆分）」。選擇頁碼範圍、提供 Gemini API Key 並勾選同意傳送文字，再按「產生文件摘要」。既有 APK 會載入同一套摘要介面；原生 Markdown 儲存需要 1.1.0 以上 APK，舊版可先用手機瀏覽器。

每次最多 50 頁／480,000 字元，逐段處理每段最多 24,000 字元，最後整合；超過整次上限會停止，不只摘要前面部分。精簡／標準／詳細上限為 500／1,500／3,000 字元，預設詳細。單段 1 次、多段為段數 + 1 次 Gemini 請求（最多 21 次基本請求，另有整個操作最多 2 次服務錯誤自動重試），勾選同意前會顯示這項費用估算規則。結果列出無文字及擷取失敗頁面，不執行 OCR。

可複製結果，或下載 UTF-8 `.md`，包含整合摘要、各段筆記、來源範圍及限制；工作到期前可重複下載。只摘要不建立章節 PDF 或 ZIP，原始 PDF 在擷取後就刪除。預設失敗即清除段落原文，只留下有上限的部分摘要供下載；處理前可選擇保留未完成文字供接續，首次暫停後約 10 分鐘到期，背景於 30 秒內清理，不因重試延長。已完成段落不重送；接續需再次同意可能計費並提供金鑰，亦可直接清除。取消需等待目前請求結束，不再開始下一段；已送出的請求仍可能計費。

## 本機啟動

```bash
pip install -r requirements.txt
python mobile_server.py
```

以瀏覽器開啟 `http://localhost:8765`。這個入口同時提供手機介面、manifest、service worker、分塊上傳 API 和 ZIP 串流下載，只有一個 HTTP 埠。原本桌面入口仍是 `streamlit run app.py`。兩個入口預設使用不同暫存目錄；若同時執行，需為其中一個設定不同連接埠。

`mobile_server.py` 使用 `PORT`（預設 `8765`），預設只監聽 `127.0.0.1`。雲端部署設定 `PDF_TRANSFER_HOST=0.0.0.0`，並由託管平台或反向代理提供受信任的 HTTPS。

## 手機安裝條件

必須先把 Python 後端部署在手機能連線的 **HTTPS 網址**。GitHub Repository 與 Actions 的原始碼 ZIP 不是可直接安裝的 app；GitHub Pages 也無法執行 Python 後端。電腦的 localhost 不等於手機的 localhost，區網 HTTP 通常也不符合 PWA 安裝條件。

- **Android／Chrome：**開啟部署網址，點頁面「安裝」，或 Chrome 選單 →「安裝應用程式」／「加到主畫面」。瀏覽器版本不同，選單名稱與安裝提示時間可能不同。
- **iPhone／Safari：**開啟部署網址，分享 →「加入主畫面」→「加入」。iOS 不使用 Android 的安裝提示 API。
- 安裝後會以獨立視窗啟動。離線可以開啟已快取的介面，但上傳、拆分、Gemini 和下載都需要連線。

## 已完成與未驗證

- 已提供 `manifest.webmanifest`、192／512 像素圖示、`standalone` 顯示、根目錄 service worker 範圍、安裝提示與 iPhone 操作說明。
- 自動化瀏覽器測試以 390 × 844 觸控視窗驗證介面、manifest、Chromium 安裝條件、上傳、拆分、ZIP 下載、伺服器清理、離線開啟。
- service worker 只快取六個公開介面資源；不快取 PDF、ZIP、API 回應、權杖、摘要或 API Key。Session 權杖只保存在該分頁的 `sessionStorage`；金鑰只保留在表單／當次請求，不寫入 localStorage。
- PWA 尚未在實體 Android／iPhone 安裝測試。另提供 Android APK 建置流程，詳見 ANDROID.md；未產出 IPA 或進行商店上架。
- 已有公開 HTTPS 免費試用入口：<https://pdf-split-app.onrender.com>。免費主機會休眠，請先用小型文件測試；1GB 雲端／手機處理尚未驗證。下方付費 Blueprint 仍只是另一個部署提案，不是試用站的目前方案。

## 大檔案限制

仍以每次 4MiB 上傳並在伺服器磁碟處理，最大 1GiB。先前 1GiB／1,023 頁壓測驗證的是本機 HTTP 與伺服器流程，**不是實體手機、行動網路或特定雲端方案的保證**。手機瀏覽器切換背景、鎖定螢幕、省電或記憶體回收都可能中止上傳。請保持前景與穩定連線；上傳中斷需重新上傳。下載中斷可在原保留期限內重試，串流完整送出後伺服器會移除 ZIP。

伺服器建議至少 2GiB RAM 與約 8GiB 可用暫存空間，實際需求視 PDF 而定；僅允許一個拆分工作同時執行。不能只靠免費方案名稱推定它能穩定處理 1GiB。重新部署、程序終止或暫存磁碟重建可能使尚未下載的工作失效。

## 已準備的 Render 部署提案

`render.yaml` 定義一個公開 Python Web Service：新加坡區域、單一程序、`1c-2g`（1 CPU／2GiB RAM）付費運算方案、`/health` 健康檢查。介面與 API 共用同一個 HTTPS 網域，無需原本桌面版的雙埠代理。自動部署關閉；不建立資料庫或持久磁碟，不設定任何 Gemini 金鑰。2026-10-25 查閱 [Render 官方價格](https://render.com/pricing)，此 Web Service 的運算費為 **US$25／月**；流量超額及其他平台費用另計，最終以建立服務時報價為準。部署前須確認費用、暫存磁碟容量及是否需要額外存取限制。

[開啟 Render 部署設定](https://render.com/deploy?repo=https://github.com/foxicall/PDF_Split_App)

這個連結會讓你檢視並建立服務；Repository 中有 Blueprint 不代表服務已經部署。若部署公開服務，預設任何人可建立自己的上傳工作，但需要自己的隨機 Session 憑證才能取得對應結果。不要在未加存取控制的公開服務設定供所有人共用的付費 Gemini Key。

## 重現手機瀏覽器檢查

```bash
pip install pytest requests playwright
python -m playwright install chromium
python -m tests.mobile_browser_check
```

CI 執行相同檢查，並上傳 `pdf-splitter-mobile-source` 原始碼套件。此套件需在伺服器執行，不能當作 APK 安裝。
