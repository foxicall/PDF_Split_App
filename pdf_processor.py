"""Disk-backed PDF processing. Page numbers in public objects are one-based."""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Callable

import fitz

MIB = 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * MIB
MAX_OUTPUT_BYTES = 3 * MAX_UPLOAD_BYTES
CHUNK_BYTES = 4 * MIB
DISK_RESERVE_BYTES = 256 * MIB
PAGE_BATCH_SIZE = 8
MAX_CHAPTERS = 10_000
MAX_SUMMARY_PAGES = 50
MAX_SUMMARY_CHARS = 24_000  # Per request, not per document.
MAX_SUMMARY_CHUNKS = 20
_PDF_LOCK = threading.RLock()  # PyMuPDF must not run concurrently in different threads.


def _serialized_pdf(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _PDF_LOCK:
            return function(*args, **kwargs)
    return call


class PDFError(Exception):
    """An error safe to display to the user."""


class PDFCancelled(PDFError):
    """The user cancelled an in-flight summary."""


def ensure_space(directory: Path, needed: int = CHUNK_BYTES) -> None:
    if shutil.disk_usage(directory).free < needed + DISK_RESERVE_BYTES:
        raise PDFError("暫存磁碟空間不足；請清除其他工作或擴充磁碟後重試。")


class TemporaryWorkspace:
    """Owned by a session; explicit cleanup plus TemporaryDirectory finalization."""

    def __init__(self, root: Path):
        ensure_space(root)
        self._temp = tempfile.TemporaryDirectory(prefix="job-", dir=root)
        self.path = Path(self._temp.name)
        self.touch()

    def touch(self) -> None:
        (self.path / ".lease").touch()

    def cleanup(self) -> None:
        self._temp.cleanup()

    def __enter__(self) -> "TemporaryWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self.cleanup()


@dataclass(frozen=True)
class Bookmark:
    level: int
    title: str
    page: int


@dataclass(frozen=True)
class PDFInfo:
    page_count: int
    bookmarks: tuple[Bookmark, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Chapter:
    title: str
    start_page: int
    end_page: int


@dataclass
class SplitResult:
    archive: Path
    rows: list[dict] = field(default_factory=list)


def validate_summary_range(page_count: int, start_page: int, end_page: int) -> None:
    if (type(start_page) is not int or type(end_page) is not int
            or not 1 <= start_page <= end_page <= page_count):
        raise PDFError("摘要頁碼範圍無效，請輸入文件內的起始頁與結束頁。")
    if end_page - start_page + 1 > MAX_SUMMARY_PAGES:
        raise PDFError(f"每次摘要最多 {MAX_SUMMARY_PAGES} 頁，請縮小頁碼範圍。")


def summarize_pdf(source_path: Path, workspace: TemporaryWorkspace, start_page: int,
                  end_page: int, summarize: Callable[[str], str], progress=None,
                  cancelled: Callable[[], bool] | None = None, combine=None,
                  summary_style: str = "detailed", allow_multiple_requests: bool = True) -> dict:
    """Spool bounded chunks, summarize every extracted page, then synthesize once.

    Preflight the entire range before API calls. Oversize ranges fail explicitly;
    no prefix of a document is silently substituted for the requested range.
    """
    def check_cancel():
        if cancelled and cancelled():
            raise PDFCancelled("摘要已取消；原始暫存檔已清除。")

    chunks, buffer, used, first, last = [], "", 0, start_page, start_page
    blank_pages, unreadable_pages = [], []

    def flush():
        nonlocal buffer
        path = workspace.path / f"summary-{len(chunks):02d}.txt"
        ensure_space(workspace.path, len(buffer.encode("utf-8")))
        path.write_text(buffer, encoding="utf-8")
        chunks.append((path, first, last))
        buffer = ""

    try:
        check_cancel()
        with _PDF_LOCK, fitz.open(source_path) as source:
            if source.needs_pass or not source.is_pdf:
                raise PDFError("PDF 無法讀取，請先解除加密並確認格式。")
            validate_summary_range(source.page_count, start_page, end_page)
            for number in range(start_page, end_page + 1):
                check_cancel()
                page = source.load_page(number - 1)
                try:
                    text = page.get_text("text").strip()
                except (RuntimeError, ValueError):
                    text = ""
                    unreadable_pages.append(number)
                finally:
                    del page
                if not text and number not in unreadable_pages:
                    blank_pages.append(number)
                if text:
                    text = ("\n" if used else "") + text
                    if used + len(text) > MAX_SUMMARY_CHARS * MAX_SUMMARY_CHUNKS:
                        raise PDFError("所選範圍超過 480,000 字元（20 段）上限；尚未呼叫 Gemini。"
                                       "請重新上傳並縮小頁碼範圍，不會只摘要前面部分。")
                    used += len(text)
                    offset = 0
                    while offset < len(text):
                        check_cancel()
                        if not buffer:
                            first = number
                        take = min(MAX_SUMMARY_CHARS - len(buffer), len(text) - offset)
                        buffer += text[offset:offset + take]
                        last = number
                        offset += take
                        if len(buffer) == MAX_SUMMARY_CHARS:
                            flush()
                workspace.touch()
                fitz.TOOLS.store_shrink(100)
                if progress:
                    progress(number - start_page + 1, end_page - start_page + 1, "extracting")
            if buffer:
                flush()
        # The PDF is closed before generation; only one bounded chunk is read at a time.
        check_cancel()
        if len(chunks) > 1 and not allow_multiple_requests:
            raise PDFError(f"此範圍需要 {len(chunks) + 1} 次 Gemini 請求；尚未呼叫 API。"
                           "請重新整理介面、重新上傳並勾選同意分段與整合請求。")
        parts = []
        final = combine or summarize
        if not chunks:
            summary = "所選頁面無可擷取文字（可能為圖片或空白頁；未執行 OCR）"
        elif len(chunks) == 1:
            if progress:
                progress(0, 1, "summarizing")
            summary = final(chunks[0][0].read_text(encoding="utf-8"))
            check_cancel()
        else:
            for index, (path, first_page, last_page) in enumerate(chunks, 1):
                check_cancel()
                if progress:
                    progress(index - 1, len(chunks) + 1, "summarizing")
                part = summarize(path.read_text(encoding="utf-8"))
                check_cancel()
                parts.append({"start_page": first_page, "end_page": last_page, "text": part})
                workspace.touch()
            notes = "\n\n".join(f"第 {i} 段（第 {p['start_page']}–{p['end_page']} 頁）\n{p['text']}"
                                  for i, p in enumerate(parts, 1))
            check_cancel()
            if progress:
                progress(len(chunks), len(chunks) + 1, "integrating")
            summary = final(notes)
            check_cancel()
        return {"text": summary, "start_page": start_page, "end_page": end_page,
                "requested_end_page": end_page, "text_chars": used, "truncated": False,
                "unreadable_pages": len(unreadable_pages), "unreadable_page_numbers": unreadable_pages,
                "blank_pages": blank_pages, "chunk_count": len(chunks), "parts": parts,
                "summary_style": summary_style,
                "api_requests": len(chunks) + (1 if len(chunks) > 1 else 0)}
    finally:
        try:
            workspace.cleanup()
        finally:
            with _PDF_LOCK:
                fitz.TOOLS.store_shrink(100)


@_serialized_pdf
def inspect_pdf(path: Path) -> PDFInfo:
    try:
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise PDFError("檔案內容不是有效的 PDF，請重新選擇檔案。")
        with fitz.open(path) as doc:
            if not doc.is_pdf:
                raise PDFError("僅支援 PDF 格式。")
            if doc.needs_pass:
                raise PDFError("此 PDF 已加密，請先解除密碼保護再上傳。")
            if doc.page_count < 1:
                raise PDFError("PDF 沒有可供拆分的頁面。")
            toc = doc.get_toc(simple=True)
            valid = tuple(
                Bookmark(int(level), str(title).strip()[:500] or "未命名章節", int(page))
                for level, title, page in toc
                if level >= 1 and 1 <= page <= doc.page_count
            )
            warnings = []
            if len(valid) != len(toc):
                warnings.append("已略過外部連結、無頁碼或超出文件頁數的書籤。")
            if any(a.page > b.page for a, b in zip(valid, valid[1:])):
                warnings.append("書籤順序與實際頁序不同，拆分時依頁碼排序。")
            if len({b.page for b in valid}) != len(valid):
                warnings.append("位於同一頁的選定書籤將合併為一個章節。")
            return PDFInfo(doc.page_count, valid, tuple(warnings))
    except PDFError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PDFError("無法讀取 PDF，檔案可能已損毀或格式不受支援。") from exc


def bookmark_chapters(info: PDFInfo, depth: int = 1) -> list[Chapter]:
    if depth < 1:
        raise PDFError("書籤層級必須大於零。")
    boundaries: dict[int, list[str]] = {}
    for item in info.bookmarks:
        if item.level <= depth:
            names = boundaries.setdefault(item.page, [])
            if item.title not in names:
                names.append(item.title)
    if not boundaries:
        raise PDFError("所選層級沒有有效書籤，請選擇更深層級或改用固定頁數拆分。")
    if min(boundaries) > 1:
        boundaries[1] = ["前言與封面"]
    pages = sorted(boundaries)
    chapters = [
        Chapter(" / ".join(boundaries[page])[:500], page,
                pages[index + 1] - 1 if index + 1 < len(pages) else info.page_count)
        for index, page in enumerate(pages)
    ]
    validate_chapters(chapters, info.page_count)
    return chapters


def fixed_chapters(page_count: int, pages_per_chapter: int) -> list[Chapter]:
    if page_count < 1 or pages_per_chapter < 1:
        raise PDFError("頁數必須大於零。")
    if (page_count + pages_per_chapter - 1) // pages_per_chapter > MAX_CHAPTERS:
        raise PDFError(f"章節數超過 {MAX_CHAPTERS:,}，請增加每個檔案的頁數。")
    return [
        Chapter(f"第 {start}–{min(start + pages_per_chapter - 1, page_count)} 頁",
                start, min(start + pages_per_chapter - 1, page_count))
        for start in range(1, page_count + 1, pages_per_chapter)
    ]


def validate_chapters(chapters: list[Chapter], page_count: int) -> None:
    if not chapters or len(chapters) > MAX_CHAPTERS:
        raise PDFError(f"章節數必須介於 1 與 {MAX_CHAPTERS:,} 之間。")
    next_page = 1
    for chapter in chapters:
        if chapter.start_page != next_page or not chapter.start_page <= chapter.end_page <= page_count:
            raise PDFError("章節頁碼必須連續、不得重疊且不得超出 PDF 範圍。")
        next_page = chapter.end_page + 1
    if next_page != page_count + 1:
        raise PDFError("章節範圍未完整涵蓋 PDF。")


def safe_filename(index: int, title: str) -> str:
    text = unicodedata.normalize("NFKC", title)
    text = "".join("_" if unicodedata.category(c).startswith("C") else c for c in text)
    text = re.sub(r'[<>:"/\\|?*]', "_", text).strip(" ._") or "未命名章節"
    # Bound UTF-8 bytes, not just character count, for common 255-byte filesystems.
    text = text.encode("utf-8")[:150].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{index:02d}_{text}.pdf"


def _write_chapter(source: fitz.Document, chapter: Chapter, path: Path,
                   heartbeat: Callable[[], None]) -> None:
    """Close/reopen the destination every eight pages; never materialize PDF bytes."""
    for first in range(chapter.start_page - 1, chapter.end_page, PAGE_BATCH_SIZE):
        ensure_space(path.parent)
        existing = path.exists()
        with fitz.open(path) if existing else fitz.open() as output:
            output.insert_pdf(source, from_page=first,
                              to_page=min(first + PAGE_BATCH_SIZE - 1, chapter.end_page - 1))
            if existing:
                output.saveIncr()
            else:
                output.save(path, garbage=0, deflate=False)
        fitz.TOOLS.store_shrink(100)
        if path.stat().st_size > MAX_OUTPUT_BYTES:
            raise PDFError("單一章節超過 3GB 輸出上限，請選擇更細的拆分方式。")
        heartbeat()


@_serialized_pdf
def split_pdf(source_path: Path, workspace: TemporaryWorkspace, chapters: list[Chapter],
              summarize: Callable[[str], str] | None = None,
              progress: Callable[[int, int], None] | None = None) -> SplitResult:
    """Consume the source; on success only the ZIP remains, on failure no PDF/ZIP remains."""
    archive_path = workspace.path / "chapters.zip"
    chapter_path = workspace.path / "chapter.pdf"
    succeeded = False
    rows: list[dict] = []
    try:
        with fitz.open(source_path) as source:
            if source.needs_pass or not source.is_pdf:
                raise PDFError("PDF 無法拆分，請先解除加密並確認格式。")
            validate_chapters(chapters, source.page_count)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                for index, chapter in enumerate(chapters, 1):
                    filename = safe_filename(index, chapter.title)
                    try:
                        _write_chapter(source, chapter, chapter_path, workspace.touch)
                        with chapter_path.open("rb") as incoming, archive.open(
                            filename, "w", force_zip64=True
                        ) as outgoing:
                            while block := incoming.read(CHUNK_BYTES):
                                ensure_space(workspace.path, len(block))
                                outgoing.write(block)
                                if archive.fp.tell() > MAX_OUTPUT_BYTES:
                                    raise PDFError("ZIP 超過 3GB 輸出上限，請改用更小的來源 PDF。")
                                workspace.touch()
                    finally:
                        chapter_path.unlink(missing_ok=True)
                    summary = "未啟用"
                    if summarize is not None:
                        page = source.load_page(chapter.start_page - 1)
                        try:
                            text = page.get_text("text")[:8000].strip()
                        except (RuntimeError, ValueError):
                            text = ""
                        finally:
                            del page
                        summary = summarize(text) if text else "章節首頁無可擷取文字（可能為圖片或空白頁；未執行 OCR）"
                    rows.append({"檔名": filename, "章節": chapter.title,
                                 "起始頁": chapter.start_page, "結束頁": chapter.end_page,
                                 "頁數": chapter.end_page - chapter.start_page + 1, "摘要": summary})
                    workspace.touch()
                    if progress:
                        progress(index, len(chapters))
        if archive_path.stat().st_size > MAX_OUTPUT_BYTES:
            raise PDFError("ZIP 超過 3GB 輸出上限。")
        succeeded = True
        return SplitResult(archive_path, rows)
    except PDFError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise PDFError("拆分失敗，可能是 PDF 損毀或磁碟空間不足；請重新上傳後重試。") from exc
    finally:
        source_path.unlink(missing_ok=True)
        chapter_path.unlink(missing_ok=True)
        if not succeeded:
            archive_path.unlink(missing_ok=True)
        fitz.TOOLS.store_shrink(100)
