package com.foxicall.pdfsplitter;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final int PICK_PDF = 100, SAVE_ZIP = 101;
    private static final long MAX_ZIP_BYTES = 3L * 1024 * 1024 * 1024;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService downloads = Executors.newSingleThreadExecutor();
    private WebView web;
    private TextView status;
    private String backend = "", pendingDownload = "";
    private ValueCallback<Uri[]> fileCallback;
    private volatile boolean downloading;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(243, 247, 245));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        LinearLayout bar = new LinearLayout(this);
        bar.setPadding(dp(12), dp(4), dp(12), dp(4));
        TextView title = new TextView(this);
        title.setText("PDF 章節拆分"); title.setTextSize(18); title.setTextColor(Color.rgb(19, 124, 116));
        bar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        Button settings = new Button(this); settings.setText("伺服器設定");
        settings.setOnClickListener(view -> configureServer()); bar.addView(settings);
        root.addView(bar);
        status = new TextView(this); status.setTextSize(13); status.setPadding(dp(16), dp(4), dp(16), dp(8));
        root.addView(status);
        web = new WebView(this); web.setId(View.generateViewId());
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1)); setContentView(root);
        WebSettings preferences = web.getSettings();
        preferences.setJavaScriptEnabled(true); preferences.setDomStorageEnabled(true);
        preferences.setAllowFileAccess(false); preferences.setAllowContentAccess(true);
        preferences.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        preferences.setJavaScriptCanOpenWindowsAutomatically(false); preferences.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (BackendUrl.sameOrigin(backend, request.getUrl().toString())) return false;
                if (request.hasGesture() && "https".equalsIgnoreCase(request.getUrl().getScheme())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl())); }
                    catch (ActivityNotFoundException ignored) { message("找不到可開啟連結的瀏覽器。"); }
                }
                return true;
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel(); message("伺服器的 HTTPS 憑證無效，已停止連線。");
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) message("無法連線。請確認網路與伺服器網址，再從設定重新連線。");
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("application/pdf");
                try { startActivityForResult(intent, PICK_PDF); }
                catch (ActivityNotFoundException ignored) { fileCallback.onReceiveValue(null); fileCallback = null; message("此裝置沒有 PDF 檔案選擇器。"); }
                return true;
            }
        });
        web.setDownloadListener((url, agent, disposition, mime, length) -> chooseDownload(url));
        String saved = getSharedPreferences("backend_settings", MODE_PRIVATE).getString("backend", "");
        if (saved.isEmpty()) configureServer(); else connect(saved);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private void message(String text) { ui.post(() -> { if (!isDestroyed()) status.setText(text); }); }

    private void configureServer() {
        if (downloading || !pendingDownload.isEmpty()) { message("請先完成或取消目前的 ZIP 儲存作業。"); return; }
        LinearLayout form = new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL); form.setPadding(dp(24), dp(8), dp(24), dp(8));
        TextView help = new TextView(this);
        help.setText("此 APK 需要已部署的 PDF 拆分伺服器，不能離線拆分。請填入 HTTPS 網址；付費主機不會由 App 自動建立。");
        form.addView(help);
        EditText input = new EditText(this); input.setSingleLine(true); input.setHint("https://你的伺服器網址"); input.setText(backend);
        input.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        form.addView(input);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("連線到 PDF 伺服器").setView(form)
                .setPositiveButton("連線", null).setNegativeButton("稍後設定", (d, which) -> {
                    if (backend.isEmpty()) message("尚未設定伺服器。點選右上方「伺服器設定」即可連線。");
                }).create();
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
            try { connect(BackendUrl.normalize(input.getText().toString(), BuildConfig.DEBUG)); dialog.dismiss(); }
            catch (IllegalArgumentException error) { input.setError(error.getMessage()); }
        }));
        dialog.show();
    }

    private void connect(String value) {
        backend = BackendUrl.normalize(value, BuildConfig.DEBUG);
        web.stopLoading(); web.clearCache(true); WebStorage.getInstance().deleteAllData();
        getSharedPreferences("backend_settings", MODE_PRIVATE).edit().putString("backend", backend).apply();
        message("保持網路連線與前景畫面，避免大型上傳或下載中斷。");
        web.loadUrl(backend);
    }

    private void chooseDownload(String url) {
        if (downloading || !pendingDownload.isEmpty()) { message("已有下載正在處理。"); return; }
        if (!BackendUrl.isDownload(backend, url)) { message("已拒絕非目前伺服器的下載連結。"); return; }
        pendingDownload = url;
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType("application/zip").putExtra(Intent.EXTRA_TITLE, "chapters.zip");
        try { startActivityForResult(intent, SAVE_ZIP); }
        catch (ActivityNotFoundException ignored) { pendingDownload = ""; message("此裝置沒有可選擇儲存位置的檔案管理器。"); }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        Uri uri = result == RESULT_OK && data != null ? data.getData() : null;
        if (request == PICK_PDF && fileCallback != null) {
            fileCallback.onReceiveValue(uri == null ? null : new Uri[]{uri}); fileCallback = null;
        } else if (request == SAVE_ZIP) {
            String url = pendingDownload; pendingDownload = "";
            if (uri != null && !url.isEmpty()) download(url, uri);
            else message("已取消儲存；ZIP 尚未開始下載。");
        }
    }

    private void download(String url, Uri destination) {
        downloading = true; message("正在下載 ZIP，請保持 App 開啟…");
        downloads.execute(() -> {
            HttpURLConnection connection = null;
            boolean complete = false;
            try {
                connection = (HttpURLConnection) new URL(url).openConnection();
                connection.setInstanceFollowRedirects(false); connection.setConnectTimeout(30000); connection.setReadTimeout(30000);
                String cookies = CookieManager.getInstance().getCookie(url);
                if (cookies != null) connection.setRequestProperty("Cookie", cookies);
                if (connection.getResponseCode() != 200 || connection.getContentType() == null
                        || !connection.getContentType().startsWith("application/zip") || connection.getContentLengthLong() > MAX_ZIP_BYTES) {
                    throw new java.io.IOException("Download unavailable");
                }
                long expected = connection.getContentLengthLong(), received = 0, lastUpdate = 0;
                try (InputStream input = connection.getInputStream(); OutputStream output = getContentResolver().openOutputStream(destination, "w")) {
                    if (output == null) throw new java.io.IOException("Destination unavailable");
                    byte[] buffer = new byte[64 * 1024]; int count;
                    while ((count = input.read(buffer)) != -1) {
                        if (Thread.currentThread().isInterrupted() || received + count > MAX_ZIP_BYTES) throw new java.io.IOException("Interrupted");
                        output.write(buffer, 0, count); received += count;
                        if (received - lastUpdate >= 1024 * 1024) { message("ZIP 已接收 " + (received / 1024 / 1024) + " MB…"); lastUpdate = received; }
                    }
                    if (expected >= 0 && received != expected) throw new java.io.IOException("Incomplete response");
                    output.flush();
                }
                complete = true; message("ZIP 已儲存至你選擇的位置。");
            } catch (Exception ignored) {
                message("下載未完成。請確認網路與儲存空間；若連結仍有效可重試，否則需重新上傳 PDF。");
            } finally {
                if (connection != null) connection.disconnect();
                if (!complete) { try { DocumentsContract.deleteDocument(getContentResolver(), destination); } catch (Exception ignored) {} }
                downloading = false;
            }
        });
    }

    @Override public void onBackPressed() {
        if (downloading) {
            Toast.makeText(this, "下載中，請保持 App 開啟直到儲存完成。", Toast.LENGTH_SHORT).show();
        } else if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        downloads.shutdownNow(); web.destroy(); super.onDestroy();
    }
}
