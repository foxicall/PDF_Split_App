package com.foxicall.pdfsplitter;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.graphics.pdf.PdfDocument;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;

/** Present only in the instrumentation APK; serves generated test documents. */
public class FixtureProvider extends ContentProvider {
    public static final Uri INPUT = Uri.parse("content://com.foxicall.pdfsplitter.test.fixture/fixture.pdf");
    public static final Uri OUTPUT = Uri.parse("content://com.foxicall.pdfsplitter.test.fixture/result.zip");
    public static final Uri BLANK = Uri.parse("content://com.foxicall.pdfsplitter.test.fixture/blank.pdf");
    public static final Uri MARKDOWN = Uri.parse("content://com.foxicall.pdfsplitter.test.fixture/summary.md");

    @Override public boolean onCreate() {
        PdfDocument document = new PdfDocument();
        try {
            for (int i = 1; i <= 2; i++) {
                PdfDocument.Page page = document.startPage(new PdfDocument.PageInfo.Builder(300, 400, i).create());
                page.getCanvas().drawText("Test page " + i, 30, 50, new android.graphics.Paint());
                document.finishPage(page);
            }
            try (FileOutputStream stream = new FileOutputStream(new File(getContext().getCacheDir(), "fixture.pdf"))) {
                document.writeTo(stream);
            }
            PdfDocument blank = new PdfDocument();
            try {
                blank.finishPage(blank.startPage(new PdfDocument.PageInfo.Builder(300, 400, 1).create()));
                try (FileOutputStream stream = new FileOutputStream(new File(getContext().getCacheDir(), "blank.pdf"))) {
                    blank.writeTo(stream);
                }
            } finally { blank.close(); }
            return true;
        } catch (Exception error) { throw new IllegalStateException(error); }
        finally { document.close(); }
    }

    private File file(Uri uri) throws FileNotFoundException {
        String name = uri.getLastPathSegment();
        if (!"fixture.pdf".equals(name) && !"result.zip".equals(name)
                && !"blank.pdf".equals(name) && !"summary.md".equals(name)) throw new FileNotFoundException();
        return new File(getContext().getCacheDir(), name);
    }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        return ParcelFileDescriptor.open(file(uri), mode.contains("w")
                ? ParcelFileDescriptor.MODE_WRITE_ONLY | ParcelFileDescriptor.MODE_CREATE | ParcelFileDescriptor.MODE_TRUNCATE
                : ParcelFileDescriptor.MODE_READ_ONLY);
    }
    @Override public String getType(Uri uri) {
        return uri.equals(MARKDOWN) ? "text/markdown" : uri.equals(OUTPUT) ? "application/zip" : "application/pdf";
    }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
        try {
            MatrixCursor cursor = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
            cursor.addRow(new Object[]{file(uri).getName(), file(uri).length()}); return cursor;
        } catch (FileNotFoundException error) { return null; }
    }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String selection, String[] args) { try { return file(uri).delete() ? 1 : 0; } catch (Exception error) { return 0; } }
}
