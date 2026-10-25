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
_PDF_LOCK = threading.RLock()  # PyMuPDF must not run concurrently in different threads.


def _serialized_pdf(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _PDF_LOCK:
            return function(*args, **kwargs)
    return call


class PDFError(Exception):
    """An error safe to display to the user."""


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
                        summary = summarize(text) if text else "無可擷取文字（掃描頁面未執行 OCR）"
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
