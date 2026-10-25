# PDF Bookmark Splitter App

繁體中文 Streamlit PDF 拆分工具：支援 **1GB（1,073,741,824 bytes／1024MB）** PDF、書籤層級選擇、固定頁數拆分、ZIP 下載，以及選用 Gemini 首頁摘要。

另提供手機 PWA：執行 `python mobile_server.py`，本機開啟 `http://localhost:8765`。部署至可連線的 HTTPS 後，Android／iPhone 可加入主畫面；需要伺服器，不能離線拆分。詳見 [手機安裝與部署說明](MOBILE.md)。

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

## 檔案架構

```text
.streamlit/config.toml  # server.maxUploadSize = 1200
app.py                 # Streamlit UI、工作狀態、拆分與摘要串接
pdf_processor.py       # 書籤解析、頁碼規劃、磁碟拆分、ZIP、TemporaryDirectory
transfer_server.py     # 分塊 HTTP 上傳、串流下載、Session 追蹤、背景清理器
upload_widget.html     # 瀏覽器端分塊上傳與原生下載按鈕
gemini_helper.py       # google-genai / gemini-2.5-flash
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
| 開始上傳新 PDF／按清除暫存 | 立即移除上一個上傳及 ZIP；正在拆分或下載時拒絕替換 |
| 章節加入 ZIP | 立即刪除該章節中間 PDF |
| 拆分成功 | 關閉來源指標並刪除原始 PDF，只保留 ZIP 等待下載 |
| 拆分例外或中止 | `try/finally` 清理原檔、中間檔與不完整 ZIP，Session 再清除整個目錄 |
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

也可在側欄輸入密碼欄位，優先於環境變數。必須勾選「啟用 Gemini 章節摘要」才會呼叫 Google；每章只送首頁純文字最多 8,000 字元，不上傳整份 PDF。模型固定 `gemini-2.5-flash`，輸出繁體中文並在程式端限制 50 個字元。無文字／掃描頁面會顯示提示，不執行 OCR。SDK 呼叫設定 20 秒逾時；支援重試設定的 SDK 會關閉自動重試。任何 API 錯誤不影響 ZIP，該次作業的後續摘要會停用以避免重複費用；錯誤訊息不包含 API Key 或文件內容。

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

- 42 項 pytest 測試通過，包含 Streamlit AppTest、手機 API、HTTP 實際傳輸、下載中斷重試與暫存清理。另驗證 Session 檔案隔離、權杖到期、路徑穿越拒絕，以及下載期間拒絕第二個下載、清除及重新上傳。
- Chromium 實際完成選檔、上傳、書籤預覽、拆分、原生 ZIP 下載，並確認下載後工作目錄刪除。
- 1GiB 測試 PDF 含 1,023 頁，每頁有獨立的未壓縮 1MiB 灰階影像。實際經 HTTP 上傳 1,073,741,824 bytes，拆成三份各 341 頁 PDF，下載的 ZIP 為 1,073,325,838 bytes，CRC、頁數與清理檢查均通過。該次程序峰值 RSS 約 **83.3MiB**，總耗時約 **12.4 秒**；此量測包含測試 HTTP 客戶端與傳輸／拆分服務，不包含 Streamlit 與瀏覽器程序，也不代表所有文件或硬體的效能。
- Gemini 僅使用模擬回應驗證，未執行付費 API 呼叫；未測試正式雲端代理部署。

如需重現大型測試，預留至少約 5GiB 可用磁碟，再執行以下指令（完成或拋出例外後會清除測試檔；強制終止程序除外）：

```bash
python -m tests.manual_large_file
```

大型測試的 RSS 量測使用 Unix `resource` 模組。它驗證一種實際 1GiB 輸入與約 1GiB 輸出流程，不能替代針對使用者文件的壓測。
