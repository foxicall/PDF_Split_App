package com.foxicall.pdfsplitter;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.espresso.intent.Intents;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.uiautomator.By;
import androidx.test.uiautomator.UiDevice;
import androidx.test.uiautomator.UiObject2;
import androidx.test.uiautomator.Until;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.zip.ZipInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import static androidx.test.espresso.intent.Intents.intending;
import static androidx.test.espresso.intent.matcher.IntentMatchers.hasAction;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class MainActivityTest {
    private WebView findWeb(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) {
                WebView found = findWeb(group.getChildAt(i)); if (found != null) return found;
            }
        }
        return null;
    }
    private String js(ActivityScenario<MainActivity> activity, String code) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<>();
        activity.onActivity(a -> findWeb(a.getWindow().getDecorView()).evaluateJavascript(code, value -> {
            result.set(value); done.countDown();
        }));
        assertTrue("WebView callback timeout", done.await(10, TimeUnit.SECONDS));
        return result.get();
    }
    private void waitJs(ActivityScenario<MainActivity> activity, String condition) throws Exception {
        long deadline = System.currentTimeMillis() + 30000;
        while (System.currentTimeMillis() < deadline) {
            if ("true".equals(js(activity, condition))) return;
            Thread.sleep(200);
        }
        fail("WebView condition timed out: " + condition);
    }
    private void click(UiDevice device, String label) {
        UiObject2 item = device.wait(Until.findObject(By.text(label)), 10000);
        assertNotNull("Missing control: " + label, item); item.click();
    }
    private void assertDocumentPicker(UiDevice device) {
        assertTrue("Android document picker did not open",
                device.wait(Until.hasObject(By.pkg("com.google.android.documentsui")), 10000)
                || device.wait(Until.hasObject(By.pkg("com.android.documentsui")), 5000));
    }
    private void clickFilePicker(ActivityScenario<MainActivity> activity, UiDevice device) throws Exception {
        waitJs(activity, "!!document.querySelector('label[for=file]')");
        // The introduction can wrap on small screens. Keep a real user tap on
        // the visible label so the test still exercises WebView's file chooser.
        js(activity, "document.querySelector('label[for=file]').scrollIntoView({block:'center'})");
        click(device, "選擇 PDF 檔案");
    }

    @Test public void installedAppConnectsUploadsAndStreamsZip() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        context.getSharedPreferences("backend_settings", Context.MODE_PRIVATE).edit()
                .putString("backend", "http://127.0.0.1:8765/").commit();
        UiDevice device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation());
        try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
            waitJs(activity, "!!document.getElementById('file')");
            clickFilePicker(activity, device);
            assertDocumentPicker(device); device.pressBack();
            waitJs(activity, "!!document.getElementById('file')");
            // Android's actual picker opens above. Supply a generated content URI through the
            // real Activity result callback to test upload independent of provider localization.
            Intents.init();
            try {
                intending(hasAction(Intent.ACTION_OPEN_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(
                        Activity.RESULT_OK, new Intent().setData(FixtureProvider.INPUT)
                                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)));
                clickFilePicker(activity, device);
                waitJs(activity, "document.getElementById('file').files.length === 1");
                js(activity, "document.getElementById('upload').click()");
                waitJs(activity, "!document.getElementById('settings').hidden");
                js(activity, "document.getElementById('pages').value=1;document.getElementById('split').click()");
                waitJs(activity, "!document.getElementById('result').hidden && !document.getElementById('download').hidden");
            } finally { Intents.release(); }
            js(activity, "document.getElementById('download').scrollIntoView({block:'center'})");
            click(device, "下載章節 ZIP");
            assertDocumentPicker(device); device.pressBack();
            // Cancelling the save dialog must leave the one-shot ZIP available.
            assertTrue(device.wait(Until.hasObject(By.text("已取消儲存；ZIP 尚未開始下載。")), 10000));
            waitJs(activity, "!document.getElementById('download').hidden");
            Intents.init();
            try {
                intending(hasAction(Intent.ACTION_CREATE_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(
                        Activity.RESULT_OK, new Intent().setData(FixtureProvider.OUTPUT)
                                .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)));
                click(device, "下載章節 ZIP");
                assertTrue(device.wait(Until.hasObject(By.text("ZIP 已儲存至你選擇的位置。")), 30000));
            } finally { Intents.release(); }
            int count = 0;
            try (ZipInputStream zip = new ZipInputStream(context.getContentResolver().openInputStream(FixtureProvider.OUTPUT))) {
                byte[] buffer = new byte[4096];
                while (zip.getNextEntry() != null) { while (zip.read(buffer) != -1) {} count++; }
            }
            assertEquals(2, count);
            waitJs(activity, "document.getElementById('status').textContent.includes('伺服器檔案已清除')");
        }
    }

    @Test public void summaryMarkdownCanBeCancelledAndSavedAsUtf8() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        context.getSharedPreferences("backend_settings", Context.MODE_PRIVATE).edit()
                .putString("backend", "http://127.0.0.1:8765/").commit();
        UiDevice device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation());
        try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
            waitJs(activity, "!!document.getElementById('file')");
            Intents.init();
            try {
                intending(hasAction(Intent.ACTION_OPEN_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(
                        Activity.RESULT_OK, new Intent().setData(FixtureProvider.BLANK)
                                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)));
                clickFilePicker(activity, device);
                waitJs(activity, "document.getElementById('file').files.length === 1");
                js(activity, "document.getElementById('upload').click()");
                waitJs(activity, "!document.getElementById('settings').hidden");
                // Generated blank pages never send a Gemini request; the dummy key is not real.
                js(activity, "document.getElementById('operation').value='summary';"
                        + "document.getElementById('operation').dispatchEvent(new Event('change'));"
                        + "document.getElementById('gemini').checked=true;"
                        + "document.getElementById('api-key').value='dummy-empty-pages-no-api';"
                        + "document.getElementById('split').click()");
                waitJs(activity, "!document.getElementById('document-result').hidden");
            } finally { Intents.release(); }
            js(activity, "document.getElementById('download-summary').scrollIntoView({block:'center'})");
            click(device, "下載 Markdown 摘要（.md）");
            assertDocumentPicker(device); device.pressBack();
            assertTrue(device.wait(Until.hasObject(By.text("已取消儲存；Markdown 尚未開始下載。")), 10000));
            for (int attempt = 0; attempt < 2; attempt++) {
                Intents.init();
                try {
                    intending(hasAction(Intent.ACTION_CREATE_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(
                            Activity.RESULT_OK, new Intent().setData(FixtureProvider.MARKDOWN)
                                    .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)));
                    click(device, "下載 Markdown 摘要（.md）");
                    assertTrue(device.wait(Until.hasObject(By.text("Markdown 已儲存至你選擇的位置。")), 30000));
                } finally { Intents.release(); }
                try (InputStream input = context.getContentResolver().openInputStream(FixtureProvider.MARKDOWN);
                     ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[4096]; int size;
                    while ((size = input.read(buffer)) != -1) output.write(buffer, 0, size);
                    String text = new String(output.toByteArray(), StandardCharsets.UTF_8);
                    assertTrue(text.contains("# PDF 文件摘要") && text.contains("所選頁面無可擷取文字"));
                    assertFalse(text.contains("dummy-empty-pages-no-api"));
                }
            }
        }
    }
}
