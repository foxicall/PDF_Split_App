package com.foxicall.pdfsplitter;

import java.net.URI;
import java.util.Locale;

/** Shared validation for navigation and download destinations. */
public final class BackendUrl {
    private BackendUrl() {}

    public static String normalize(String value, boolean allowLocalHttp) {
        try {
            URI uri = new URI(value.trim());
            String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
            String host = uri.getHost();
            boolean local = "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "10.0.2.2".equals(host);
            if (host == null || !(scheme.equals("https") || (allowLocalHttp && local && scheme.equals("http")))
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null
                    || !(uri.getRawPath() == null || uri.getRawPath().isEmpty() || uri.getRawPath().equals("/"))
                    || uri.getPort() == 0 || uri.getPort() > 65535) {
                throw new IllegalArgumentException();
            }
            return new URI(scheme, null, host.toLowerCase(Locale.ROOT), uri.getPort(), "/", null, null).toASCIIString();
        } catch (Exception error) {
            throw new IllegalArgumentException("請輸入伺服器的 HTTPS 根網址，不可包含帳密、子路徑或查詢參數。");
        }
    }

    public static boolean sameOrigin(String backend, String target) {
        try {
            URI a = new URI(backend), b = new URI(target);
            return b.getRawUserInfo() == null && a.getScheme().equalsIgnoreCase(b.getScheme())
                    && a.getHost().equalsIgnoreCase(b.getHost()) && port(a) == port(b);
        } catch (Exception ignored) { return false; }
    }

    public static boolean isDownload(String backend, String target) {
        try {
            URI uri = new URI(target);
            return sameOrigin(backend, target) && uri.getRawQuery() == null && uri.getRawFragment() == null
                    && uri.getRawPath().matches("/[A-Za-z0-9_-]{43}/download");
        } catch (Exception ignored) { return false; }
    }

    private static int port(URI uri) {
        return uri.getPort() == -1 ? ("https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80) : uri.getPort();
    }
}
