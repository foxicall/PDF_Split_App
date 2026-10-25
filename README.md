# PDF Bookmark Splitter App

繁體中文 Streamlit PDF 拆分工具：支援 **1GB（1,073,741,824 bytes／1024MB）** PDF、書籤層級選擇、固定頁數拆分、ZIP 下載、選用 Gemini 章節首頁摘要，以及**不拆分 PDF 的文件摘要模式**。

[線上免費試用](https://pdf-split-app.onrender.com)（閒置後會休眠，先以小型文件測試；免費主機的 1GB 實務容量尚未驗證）。

另提供手機 PWA：執行 `python mobile_server.py`，本機開啟 `http://localhost:8765`。部署至可連線的 HTTPS 後，Android／iPhone 可加入主畫面；需要伺服器，不能離線拆分。詳見 [手機安裝與部署說明](MOBILE.md)。

Android APK：見 [安裝、建置與測試說明](ANDROID.md)，下載 GitHub Actions 的 `pdf-splitter-android-apk` 產物。APK 為需要伺服器的 Android 客戶端，首次開啟需填入後端網址。

## 啟動

需要 Python 3.10+。在專案目錄執行：

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

開啟 [http://localhost:8501](http://localhost:8501)。程式自動在 `127.0.0.1:8765` 啟動分塊傳輸服務，無須另外啟動程序。請從本專案目錄啟動，讓 Streamlit 載入 `.streamlit/config.toml`。

1. 選擇 PDF，按「上傳 PDF」，等待檢查完成。
2. 按「讀取上傳狀態／更新結果」，選擇書籤深度或每 N 頁拆分。
3. 檢查章節預覽；如需摘要，在側欄輸入 API Key 並勾選啟用。
4. 按「開始拆分並打包 ZIP」，完成後按上方「下載章節 ZIP」。

## 只產生摘要，不拆分 PDF

1. 上傳 PDF；Streamlit 版另按「讀取上傳狀態／更新結果」。
2. 在「處理方式」選 **只產生摘要（不拆分）**，選擇起始與結束頁、摘要詳細程度（預設詳細）。
3. 輸入 Gemini API Key，或使用伺服器已設定的金鑰；勾選 **啟用 Gemini 摘要並同意傳送文字**。
4. 按 **產生文件摘要**。網頁／APK 自動顯示進度，Streamlit 可按更新結果；結果可選取複製，或按 **下載 Markdown 摘要（.md）**。

此模式不需要書籤，也不建立章節 PDF 或 ZIP。使用 Gemini 3.8 Flash，提供精簡（最多 **500 字元**）、標準（**1,500 字元**）、詳細（**3,000 字元**）三種整合摘要長度，預設詳細；這是輸出上限，不保證模型一定寫滿。Markdown 保留小標題、條列、換行。

每次最多 **50 頁、480,000 字元**。先逐頁檢查所選範圍，將可擷取文字分成最多 20 段、每段最多 **24,000 字元**，寫入工作暫存目錄；PDF 指標在 Gemini 呼叫前關閉，每次僅載入一段。字元跨段不丟失，同一頁可能跨段；不再達到 24,000 字就略過後續頁面。若超過整次上限，會在任何生成請求前停止並明確提示縮小範圍，**不傳送截短內容**。

勾選傳送同意也包含分段與整合請求：1 段只需 1 次，多段需「段數 + 1」次（例如 3 段共 4 次），最多 **21 次基本請求**；只摘要模式可針對暫時性 500／502／503／504，整個操作額外重試最多 **2 次**（總計最多 **23 次**請求嘗試）。各段筆記最多 1,000 字元，再一次整合為所選詳細程度；時間與 API 費用隨文字量增加。進度顯示實際預計請求數。遇到無法恢復的失敗會停止後續段落；已完成的段落可下載明確標為「未完成／部分結果」的 Markdown，不會標為完整摘要。

結果顯示檢查頁碼、文字字元數、段數、請求數，以及無文字／擷取失敗頁面。**只摘要可擷取的文字，不代表選取範圍內所有圖片、表格或掃描內容，也不是全文轉錄**。不執行 OCR；整個範圍無文字時不呼叫 Gemini。

Markdown 為 UTF-8，檔名以來源文件名稱清理後加 `_摘要.md`；含來源、頁碼、限制說明、整合摘要與各段筆記，便於查閱被整合壓縮的細節。不包含 API Key 或工作權杖。HTTP 匯出需目前工作權杖、禁止快取，直到清除／重新上傳／到期前可重複下載；它不會消耗 ZIP，也不產生持久 Markdown 實體檔。Streamlit 僅將這份有上限的小型文字交給下載按鈕，不將 PDF／ZIP 放入記憶體。Android 儲存 Markdown 需要 1.1.0 以上 APK；舊版可先使用手機瀏覽器。

### 暫時故障、接續與隱私

`GEMINI_UNAVAILABLE` 表示 SDK 回報 HTTP 500／502／503／504 的安全分類，不代表 API Key 錯誤，也不能只憑它斷定 Google 全面故障。自動重試採 2、4 秒退避加最多 0.5 秒抖動，共用兩次額外預算，不按每段重新計算。遵守 `Retry-After` 秒數或 HTTP 日期；若要求等待超過 30 秒，停止自動等待並提示稍後處理。SDK 每個請求設 20 秒逾時，支援的 SDK 關閉內建重試。金鑰、權限、模型、輸入錯誤不自動重試；429 配額與逾時／連線錯誤也不自動重送，以免盲目重複計費。日誌只記錄固定診斷代碼與數字 HTTP 狀態，不記錄原始錯誤、金鑰、文件文字或 URL。

**預設不保留原文供接續。** 所選文字擷取完成、PDF 指標關閉後，原始 PDF 就會刪除；成功或失敗後清除段落原文。若已有成功段落，僅保留有字數上限的摘要結果至 Session 到期，供下載部分 Markdown；再次摘要需重新上傳。

可在處理前明確勾選 **暫時故障時保留未完成文字供接續（選用，最長 10 分鐘）**。此時暫時性錯誤／逾時／配額不足會暫停，保留最多 480,000 字元的未完成段落文字及最多 20,000 字元的已完成筆記；已完成段落原文立即刪除，原始 PDF、API Key 都不保留。從首次暫停起的 10 分鐘期限不因重試延長，也不超過服務 Session TTL；到期後背景清理器於 30 秒內刪除。重新部署／重啟無法恢復工作，殘留段落文字的到期標記由背景清理器回收。

按 **接續重試** 必須重新同意傳送及可能再次計費，並重新輸入金鑰或使用伺服器金鑰；只重送未完成段落，若只剩整合則只重試整合。每次操作有獨立的最多兩次額外自動重試預算，已完成段落不會重跑。回應中的 `Retry-After` 也限制手動接續，等待時間超過保留期限時需重新上傳。逾時不代表 Google 未處理原請求；重試可能再次計費。介面與 Markdown 顯示累計請求嘗試數（含重試）。

取消、清除或重新上傳會刪除保留資料。處理中的取消會等待當下頁面或 Google 請求完成，退避等待可立即取消；不開始下一次請求，也不顯示遲到回應。進行中的處理／下載不會被清理器刪除，完成後清理；沒有無限重試、背景自動接續或原始 PDF 持久儲存。

## 檔案架構

```text
.streamlit/config.toml  # server.maxUploadSize = 1200
app.py                 # Streamlit UI、工作狀態、拆分與摘要串接
pdf_processor.py       # 書籤解析、頁碼規劃、磁碟拆分、ZIP、TemporaryDirectory
transfer_server.py     # 分塊 HTTP 上傳、串流下載、Session 追蹤、背景清理器
upload_widget.html     # 瀏覽器端分塊上傳與原生下載按鈕
gemini_helper.py       # google-genai / gemini-3.8-flash
summary_markdown.py     # UTF-8 摘要匯出、來源範圍與清理後檔名
requirements.txt
tests/                 # 核心、傳輸、清理與摘要的測試
README.md
mobile/                # 手機 PWA、manifest、圖示、service worker
mobile_server.py       # 單一 HTTP 入口的手機版服務
MOBILE.md              # 安裝條件、驗證範圍與部署提案
render.yaml            # 待確認的 Render 付費部署設定
.github/workflows/ci.yml # Python 測試與手機瀏覽器檢查
```

## 大檔案與記憶體

設定檔明確設為 `server.maxUploadSize = 1200`，應用程式另行強制檢查 **1024MB** 上限，包含宣告大小與實際分塊累計大小。

**Streamlit 原生 `st.file_uploader` 和 `st.download_button` 會將檔案放入記憶體。** 因此本程式採額外的輕量 HTTP 傳輸服務：瀏覽器以 `File.slice()` 每次傳送 4MiB，伺服器逐塊寫入磁碟；ZIP 以原生下載連結每次讀取 4MiB 送出，未使用 `getvalue()`、整檔 `read()`、`BytesIO` 或下載用 `fetch(...).blob()`。

PyMuPDF 從磁碟路徑開啟來源；每個章節每批插入 8 頁、直接存檔並關閉／重新開啟輸出文件，以增量寫入降低同時駐留的物件數。每個章節寫入 ZIP 後立即刪除。ZIP 採 `ZIP_STORED`（PDF 通常已壓縮）並啟用 ZIP64。

這避免整份 1GB 原檔或 ZIP 的 Python 記憶體副本，**不代表固定 4MiB RAM 或對任何 PDF 保證不會 OOM**。PyMuPDF 仍需要交叉參照表、頁面物件及原生快取；極端的單頁影像、損毀文件及大量頁數仍可能耗用大量記憶體。增量寫入可能重複共用資源而增加輸出大小。單章／ZIP 上限為 3GiB，超出即失敗並清理。建議至少 2–4GiB RAM，預留約 8GiB 可用磁碟並依實際文件壓測；同時只允許一個拆分工作，HTTP 同時請求最多 16 個。

## 書籤規則與防呆

- 使用 PDF 實體頁碼（從 1 起算），不使用印刷頁碼／頁面標籤。
- 選擇第 N 層時，以第 1 至 N 層全部有效書籤作為切點，依頁碼排序。
- 同頁書籤合併成一個章節；下一個切點的前一頁為結束頁，末章涵蓋最後一頁。
- 第一個書籤前的內容另存為「前言與封面」，不遺漏頁面。
- 外部連結、無頁碼與越界書籤略過；無有效書籤時提示改用固定頁數。
- 檔名格式 `01_章節名稱.pdf`；清理控制字元、路徑分隔符及不合法字元，限制 UTF-8 檔名字節數，序號防止撞名。
- 拒絕空檔案、非 PDF、加密及無頁面 PDF；最多 10,000 個章節。
- 輸出保留頁面內容；原 PDF 的數位簽章、文件附件、原始目錄等文件層級功能不保證移植。不同章節間的超連結可能失效。

## 暫存生命週期

每個上傳使用獨立的 `tempfile.TemporaryDirectory()`，由 Session 持有並明確呼叫 `cleanup()`；API Key 不寫入磁碟。暫存根目錄預設為系統暫存目錄內的 `pdf-bookmark-splitter`，可用 `PDF_TEMP_ROOT` 指定。

| 事件 | 清理行為 |
| --- | --- |
| 開始上傳新 PDF／按清除暫存 | 立即移除上一個上傳、ZIP 及摘要結果；正在處理或下載時拒絕替換 |
| 章節加入 ZIP | 立即刪除該章節中間 PDF |
| 拆分成功 | 關閉來源指標並刪除原始 PDF，只保留 ZIP 等待下載 |
| 拆分例外或中止 | `try/finally` 清理原檔、中間檔與不完整 ZIP，Session 再清除整個目錄 |
| 只摘要成功、取消或未勾選保留時失敗 | 原始 PDF 先刪除，再清除段落原文與工作目錄；可保留有上限的完整／部分摘要供下載 |
| ZIP 串流完整送出 | 關閉檔案後刪除 ZIP 及整個工作目錄；表格仍可查看 |
| 下載連線中斷 | 暫時保留 ZIP，可於原保留期限內重試；不支援 Range 續傳 |
| 關閉分頁、未完成上傳或沒有下載 | 預設閒置 30 分鐘到期，背景每 30 秒清理；UI 輪詢不延長期限 |
| 正常程序退出 | `atexit` 清理 |
| 強制終止／斷電 | 下次啟動及背景掃描清理超過期限的孤兒目錄 |

瀏覽器沒有可靠 API 告知伺服器「檔案已存入使用者磁碟」。本程式以 **HTTP 回應串流完整寫出** 作為自動清理點；傳輸完成後連結失效，需重傳原始 PDF 才能重建 ZIP。關閉分頁也沒有可靠的即時事件，透過 TTL 保證服務仍運行時回收。程序完全停止期間無法執行 Python 清理；若主機可能長期不再啟動，請由系統定期清理專用暫存目錄，且避免清理仍運行的工作。

寫入前檢查磁碟可用空間，保留 256MiB 安全餘量；PDF 原生寫入以批次前後檢查，檔案系統硬配額仍是防止其他程序或極端單頁耗盡磁碟的最後保障。最多保留 32 個 Session。根目錄持有跨程序鎖，避免不同程序互刪正在使用的檔案；部署採單一 Streamlit 程序，勿讓多個實例共用暫存目錄。

## Gemini（可選）

```bash
export GEMINI_API_KEY='你的 API Key'
streamlit run app.py
```

也可在側欄輸入密碼欄位，優先於環境變數。必須勾選「啟用 Gemini 摘要並同意傳送文字」才會呼叫 Google。拆分模式每章只送首頁純文字最多 8,000 字元，輸出最多 50 字；只摘要模式的範圍與限制見上節。兩者都不上傳整份 PDF 到 Google。模型使用 **Gemini 3.8 Flash**（`gemini-3.8-flash`），輸出繁體中文。2026-10-25 已透過 Google Models API 的清單與模型資料確認此型號支援 `generateContent`；這項唯讀檢查不傳送 PDF，也不執行摘要生成。實際權限仍取決於使用者提供的金鑰及專案，程式不會自動降回舊模型。無文字／掃描頁面會顯示提示，不執行 OCR。SDK 呼叫設定 20 秒逾時；支援重試設定的 SDK 會關閉自動重試。任何 API 錯誤不影響拆分模式的 ZIP，該次作業的後續摘要會停用以避免重複費用；錯誤訊息不包含 API Key 或文件內容。

摘要失敗時，畫面會顯示固定的安全診斷代碼；伺服器只記錄代碼，不記錄例外原文、金鑰、網址或文件文字。舊版的「請確認 API Key、配額與網路」不能用來判定實際原因，也無法回溯該次錯誤。修正設定後，先保存現有結果（拆分模式請下載 ZIP），再重新上傳文件以重試摘要。

| 診斷代碼 | 檢查方向 |
| --- | --- |
| `GEMINI_KEY_INVALID` | 在 [Google AI Studio](https://aistudio.google.com/api-keys) 確認金鑰有效，並重新輸入；不要貼到聊天或 GitHub。 |
| `GEMINI_KEY_RESTRICTED` / `GEMINI_PERMISSION` / `GEMINI_API_DISABLED` | 檢查金鑰所屬專案、API 啟用狀態及伺服器端使用限制。 |
| `GEMINI_QUOTA` | 檢查 AI Studio 的速率／配額；等待恢復或調整已授權的方案。重試本身不會增加配額。 |
| `GEMINI_TIMEOUT` / `GEMINI_NETWORK` / `GEMINI_UNAVAILABLE` | Google 回應逾時、網路連線或服務暫時異常，稍後重試。 |
| `GEMINI_MODEL` / `GEMINI_REQUEST` / `GEMINI_BILLING` | 檢查專案的模型、服務地區與帳務可用性；程式不會自動開通付費方案。 |
| `GEMINI_SDK` / `GEMINI_UNKNOWN` | 提供代碼給維護者檢查，無須提供金鑰或原始文件。 |

「章節首頁無可擷取文字」只代表該章第一頁無文字層，可能為圖片或空白頁，不代表整章都沒有文字。此工具不會自動將文件送至 OCR。

## 遠端／雲端部署

上傳 iframe 和 Streamlit 是兩個 HTTP 入口。瀏覽器必須能存取傳輸服務；只有開放 8501 的平台不能直接使用預設設定。建議用同一個 HTTPS 反向代理提供兩個路徑，並將 `/transfer/` 前綴去除後轉送至 8765。

```bash
export PDF_TRANSFER_PUBLIC_URL=/transfer
streamlit run app.py
```

Nginx 範例（放入現有 HTTPS `server` 區段；TLS 憑證依部署環境配置）：

```nginx
location /transfer/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_request_buffering off;
    proxy_buffering off;
    client_max_body_size 5m;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    access_log off; # 避免記錄網址中的 Session 憑證
}
location / {
    proxy_pass http://127.0.0.1:8501;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}
```

也可指定完整外部 HTTPS 網址（例如 `PDF_TRANSFER_PUBLIC_URL=https://files.example.com`）。同網域路徑方案可避免混合內容問題。只在確實需要讓反向代理／容器網路存取時設定 `PDF_TRANSFER_HOST=0.0.0.0`。預設傳輸服務綁定 localhost。

| 環境變數 | 預設 | 用途 |
| --- | --- | --- |
| `PDF_TRANSFER_HOST` | `127.0.0.1` | 傳輸服務監聽位址 |
| `PDF_TRANSFER_PORT` | `8765` | 傳輸服務埠 |
| `PDF_TRANSFER_PUBLIC_URL` | 瀏覽器目前主機的 8765 埠 | 瀏覽器可存取的傳輸網址或 `/transfer` |
| `PDF_TEMP_ROOT` | 系統暫存目錄/pdf-bookmark-splitter | 專用暫存根目錄 |
| `PDF_RETENTION_SECONDS` | `1800` | 上傳及結果閒置保留秒數 |
| `GEMINI_API_KEY` | 無 | 選用摘要金鑰 |

每個 Session 使用不可猜測的 256-bit 權杖，檔案路徑不由使用者輸入；回應禁止快取且不記錄權杖網址。權杖網址等同暫時存取憑證，不應分享。對外開放時請在代理加上所需的登入及流量限制，本工具沒有帳號系統。

## 測試

```bash
pip install pytest requests
python -m pytest -q
```

一般測試使用即時產生的小型 PDF 與模擬 Gemini 回應，不消耗 API 配額。

2026-10-25 驗證結果（Linux／Python 3.12.14）：

- 102 項 pytest 測試通過，包含 Streamlit AppTest、手機 API、HTTP 實際傳輸、下載中斷重試、只摘要的頁面與字數上限、取消、錯誤分類及暫存清理。另驗證 Session 檔案隔離、權杖到期、路徑穿越拒絕，以及下載期間拒絕第二個下載、清除及重新上傳。
- Chromium 實際完成選檔、上傳、書籤預覽、拆分、原生 ZIP 下載，並確認下載後工作目錄刪除。
- 1GiB 測試 PDF 含 1,023 頁，每頁有獨立的未壓縮 1MiB 灰階影像。實際經 HTTP 上傳 1,073,741,824 bytes，拆成三份各 341 頁 PDF，下載的 ZIP 為 1,073,325,838 bytes，CRC、頁數與清理檢查均通過。該次程序峰值 RSS 約 **83.3MiB**，總耗時約 **12.4 秒**；此量測包含測試 HTTP 客戶端與傳輸／拆分服務，不包含 Streamlit 與瀏覽器程序，也不代表所有文件或硬體的效能。
- 只摘要的手機 Chromium 測試使用模擬 Gemini，涵蓋同意傳送、頁碼選擇、複製結果、跨段整合、UTF-8 Markdown 重複下載、不產生 ZIP、取消及清理。執行方式：`python -m tests.summary_browser_check`。
- Gemini 生成使用模擬回應驗證，未執行付費 API 呼叫。既有 Render HTTPS 試用站已驗證上傳／拆分／下載；不代表所有金鑰、實體手機或免費主機的大檔處理都已驗證。

如需重現大型測試，預留至少約 5GiB 可用磁碟，再執行以下指令（完成或拋出例外後會清除測試檔；強制終止程序除外）：

```bash
python -m tests.manual_large_file
```

大型測試的 RSS 量測使用 Unix `resource` 模組。它驗證一種實際 1GiB 輸入與約 1GiB 輸出流程，不能替代針對使用者文件的壓測。
