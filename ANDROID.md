# Android APK

Android 8.0（API 26）以上可使用的 **Android WebView 客戶端**，與手機 PWA／桌面 Streamlit 共用伺服器。APK 是實際 Android 應用程式套件，但 PDF 拆分仍在後端執行，不能離線拆分，也未包含硬編碼的公開伺服器。

## 取得與安裝

在 [GitHub Actions 的 Android APK 工作流程](https://github.com/foxicall/PDF_Split_App/actions/workflows/android.yml) 選擇已成功的執行，下載 `pdf-splitter-android-apk` artifact（GitHub 可能要求登入）。解壓縮後取得 `app-debug.apk`、`SHA256SUMS` 與簽章資訊 `signature.txt`。

1. 將 `app-debug.apk` 儲存到 Android 手機並開啟。
2. 若 Android 要求，僅針對你使用的瀏覽器／檔案管理器允許安裝此來源的應用程式。
3. 開啟「PDF 拆分（測試版）」並輸入已部署的 HTTPS 根網址，例如自己的 Render 網址。不能填 GitHub Repository 網址；它不會執行後端。
4. 選擇 PDF，上傳並拆分；點「下載章節 ZIP」後，使用 Android 系統介面選擇儲存位置。

尚未有公開 HTTPS 後端時，APK 可以安裝與開啟設定，但無法完成線上拆分。APK 不會自動購買或部署主機。Render 付費部署仍需另行確認，方案與費用見 [MOBILE.md](MOBILE.md)。

## 使用與安全邊界

- 一般伺服器只接受 HTTPS，拒絕含帳密、查詢參數、片段或子路徑的設定，並使用 Android 的正常憑證驗證。不提供忽略 SSL 錯誤的選項。
- PDF 選擇使用 Android Storage Access Framework；只存取使用者選取的檔案，不要求全檔案存取或廣泛儲存權限。
- ZIP 先選擇目的地，確認後才開始 HTTP GET。取消儲存視窗不會消耗一次性下載。原生下載以 64KiB 緩衝區寫入選定位置，不將整份 ZIP 載入 RAM。
- 下載只接受目前伺服器的有效工作 URL，不跟隨 HTTP 重新導向。失敗時嘗試刪除不完整輸出，若連結仍有效可重試；HTTP 完整送出後伺服器可能已清理，必要時須重新上傳。
- APK 僅儲存後端網址。API Key 仍由手機網頁明確選用，不硬編碼在 APK；不把文件上傳到 GitHub。
- 請保持 App 在前景，避免鎖定螢幕或系統回收程序。沒有實作長時間下載的前景服務，也沒有保證背景上傳不中斷。1GiB 支援以先前伺服器壓測為依據，不能推定所有實體手機與行動網路都能穩定處理。

## 測試版簽章

目前提供 `debug` 測試版，套件 ID 為 `com.foxicall.pdfsplitter.debug`，使用建置時產生的開發簽章。金鑰只存在該次建置的忽略目錄，不提交到 GitHub，也不包含在 artifact。這不是正式商店發行簽章，未上架 Google Play。

不同 CI 執行會產生不同開發金鑰，因此更新時若出現簽章不符，需先移除舊版再安裝（會清除已儲存的伺服器網址）。正式發行需由專案擁有者保管持久簽章金鑰，另外配置 Release signing；本專案不自動產生或上傳正式發行憑證。

僅 Debug 版本為模擬器測試允許 `http://127.0.0.1`、`http://localhost`、`http://10.0.2.2`；其他 HTTP 網站仍被拒絕。Release 設定不包含這個網路例外。

## 建置與驗證

需 JDK 17+、Android SDK Platform 35、Build Tools 35.0.0。Gradle Wrapper 固定 8.13 並驗證 distribution SHA-256。

```bash
cd android
./gradlew assembleDebug testDebugUnitTest assembleDebugAndroidTest lintDebug
"$ANDROID_HOME/build-tools/35.0.0/apksigner" verify --verbose --print-certs app/build/outputs/apk/debug/app-debug.apk
```

APK 位於 `android/app/build/outputs/apk/debug/app-debug.apk`。Windows 可用 `gradlew.bat`。正式 `release` 變體預設不設定簽章，避免將測試金鑰誤作正式金鑰。

## Android 模擬器測試

CI 另外啟動隔離的本機測試後端與 Android 35 模擬器，使用 `adb reverse tcp:8765 tcp:8765` 連線。Instrumentation 測試涵蓋：安裝／啟動、實際 Android 選檔視窗開啟與取消、透過 Activity 回傳的測試 content URI 上傳、拆分、儲存視窗取消不消耗 ZIP，以及原生串流寫入並檢查 ZIP。為避免不同檔案管理器介面的文字／路徑差異，選定的測試文件與儲存位置透過 Espresso Intents 回傳；這不代表已手動測遍各品牌手機的檔案管理器。

測試用 ContentProvider 僅在 instrumentation APK 中，提供程式產生的測試文件，不會包含在交付的 `app-debug.apk`。測試結果以該提交的 GitHub Actions 與 Android reports artifact 為準。實體手機相容性、正式 HTTPS 部署及 1GiB 實機上傳仍需後續驗證。
