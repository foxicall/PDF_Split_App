package com.foxicall.pdfsplitter;

import org.junit.Test;
import static org.junit.Assert.*;

public class BackendUrlTest {
    @Test public void acceptsHttpsRootAndNormalizes() {
        assertEquals("https://example.com/", BackendUrl.normalize(" HTTPS://EXAMPLE.COM ", false));
        assertEquals("https://example.com:8443/", BackendUrl.normalize("https://example.com:8443/", false));
    }
    @Test public void rejectsUnsafeOrAmbiguousUrls() {
        for (String url : new String[]{"http://example.com", "javascript:alert(1)", "https://user:pass@example.com",
                "https://example.com/app", "https://example.com/?token=secret", "https://example.com/#fragment",
                "https://example.com:0", "https://example.com:70000", "file:///tmp/a", "", "https://"}) {
            try { BackendUrl.normalize(url, false); fail(url); } catch (IllegalArgumentException expected) {}
        }
    }
    @Test public void httpOnlyForLoopbackDuringDevelopment() {
        assertEquals("http://127.0.0.1:8765/", BackendUrl.normalize("http://127.0.0.1:8765", true));
        for (boolean debug : new boolean[]{true, false}) {
            try { BackendUrl.normalize("http://example.com", debug); fail(); } catch (IllegalArgumentException expected) {}
        }
        try { BackendUrl.normalize("http://localhost", false); fail(); } catch (IllegalArgumentException expected) {}
    }
    @Test public void downloadsMustUseSameOriginAndExactCapabilityPath() {
        String root = "https://example.com/", token = "a".repeat(43);
        assertTrue(BackendUrl.isDownload(root, root + token + "/download"));
        assertTrue(BackendUrl.isDownload(root, root + token + "/summary.md"));
        for (String url : new String[]{"https://evil.example/" + token + "/download", root + token + "/download?file=x",
                root + "../download", root + token + "/summaryXmd", root + token + "/summary.md?token=x",
                "https://evil.example/" + token + "/summary.md", root + token + "/summary.md/../download", "http://example.com/" + token + "/download", root + token + "/download#x",
                "https://user@example.com/" + token + "/download", "https://example.com:444/" + token + "/download"}) {
            assertFalse(url, BackendUrl.isDownload(root, url));
        }
    }
}
