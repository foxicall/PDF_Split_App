import zipfile
from pathlib import Path

import fitz
import pytest
from pypdf import PdfReader

import pdf_processor as processor
from pdf_processor import (
    Bookmark, Chapter, PDFError, PDFInfo, TemporaryWorkspace, bookmark_chapters,
    fixed_chapters, inspect_pdf, safe_filename, split_pdf,
)


def test_nested_bookmarks_cover_all_pages_and_merge_same_page(make_pdf):
    path = make_pdf(toc=[[1, "第一章", 3], [2, "介紹", 3], [2, "細節", 6],
                         [1, "第二章", 12], [1, "附錄", 20]])
    info = inspect_pdf(path)
    assert [(c.start_page, c.end_page) for c in bookmark_chapters(info)] == [(1, 2), (3, 11), (12, 19), (20, 20)]
    deep = bookmark_chapters(info, 2)
    assert [(c.start_page, c.end_page) for c in deep] == [(1, 2), (3, 5), (6, 11), (12, 19), (20, 20)]
    assert deep[1].title == "第一章 / 介紹"
    assert any("同一頁" in warning for warning in info.warnings)


def test_unsorted_and_external_bookmarks(make_pdf):
    path = make_pdf(toc=[[1, "後段", 10], [1, "外部", -1], [1, "前段", 2]])
    info = inspect_pdf(path)
    assert len(info.bookmarks) == 2
    assert len(info.warnings) == 2
    assert [(c.start_page, c.end_page) for c in bookmark_chapters(info)] == [(1, 1), (2, 9), (10, 20)]


def test_no_bookmarks_fixed_fallback(make_pdf):
    info = inspect_pdf(make_pdf(pages=7))
    assert not info.bookmarks
    with pytest.raises(PDFError):
        bookmark_chapters(info)
    assert [(c.start_page, c.end_page) for c in fixed_chapters(7, 3)] == [(1, 3), (4, 6), (7, 7)]


@pytest.mark.parametrize("title", ["../../CON", 'a<>:"/\\|?*b', "\x00\n\t", "中文" * 200, "..", "CON"])
def test_safe_unique_filenames(title):
    name = safe_filename(1, title)
    assert Path(name).name == name
    assert not any(c in name for c in '<>:"/\\|?*\x00\n\t')
    assert len(name.encode("utf-8")) < 200
    assert name != safe_filename(2, title)


@pytest.mark.parametrize("encrypted", [False, True])
def test_invalid_and_encrypted_pdf_rejected(tmp_path, make_pdf, encrypted):
    if encrypted:
        path = make_pdf(encrypted=True)
    else:
        path = tmp_path / "bad.pdf"
        path.write_bytes(b"not a PDF")
    with pytest.raises(PDFError):
        inspect_pdf(path)


def test_zip_page_contents_incremental_batches_and_cleanup(tmp_path, make_pdf):
    source = make_pdf(toc=[[1, "a/b", 1], [1, "a\\b", 18]])
    info = inspect_pdf(source)
    with TemporaryWorkspace(tmp_path) as work:
        owned_source = work.path / "source.pdf"
        source.replace(owned_source)
        summary_inputs = []
        result = split_pdf(owned_source, work, bookmark_chapters(info),
                           summarize=lambda text: summary_inputs.append(text) or "測試摘要")
        assert not owned_source.exists()
        assert not (work.path / "chapter.pdf").exists()
        assert {p.name for p in work.path.iterdir()} == {".lease", "chapters.zip"}
        with zipfile.ZipFile(result.archive) as archive:
            assert archive.namelist() == ["01_a_b.pdf", "02_a_b.pdf"]
            assert archive.testzip() is None
            archive.extractall(tmp_path / "verify")
        # Read with an independent library, including a chapter spanning three write batches.
        first = PdfReader(tmp_path / "verify" / "01_a_b.pdf")
        second = PdfReader(tmp_path / "verify" / "02_a_b.pdf")
        assert len(first.pages) == 17
        assert len(second.pages) == 3
        assert [p.extract_text().strip() for p in first.pages] == [f"Page {i}" for i in range(1, 18)]
        assert second.pages[-1].extract_text().strip() == "Page 20"
        assert summary_inputs == ["Page 1", "Page 18"]
        assert all(row["摘要"] == "測試摘要" for row in result.rows)
        directory = work.path
    assert not directory.exists()


def test_failure_removes_original_partial_chapter_and_zip(tmp_path, make_pdf, monkeypatch):
    source = make_pdf()
    with TemporaryWorkspace(tmp_path) as work:
        owned = work.path / "source.pdf"
        source.replace(owned)
        def disk_failure(*args):
            (work.path / "chapter.pdf").write_bytes(b"partial")
            raise OSError("disk full")
        monkeypatch.setattr(processor, "_write_chapter", disk_failure)
        with pytest.raises(PDFError):
            split_pdf(owned, work, fixed_chapters(20, 10))
        assert {p.name for p in work.path.iterdir()} == {".lease"}


def test_output_budget_failure_cleans_up(tmp_path, make_pdf, monkeypatch):
    source = make_pdf()
    monkeypatch.setattr(processor, "MAX_OUTPUT_BYTES", 1)
    with TemporaryWorkspace(tmp_path) as work:
        source.replace(work.path / "source.pdf")
        with pytest.raises(PDFError, match="上限"):
            split_pdf(work.path / "source.pdf", work, fixed_chapters(20, 10))
        assert not list(work.path.glob("*.pdf"))
        assert not list(work.path.glob("*.zip"))


def test_no_text_skips_summarizer(tmp_path):
    with TemporaryWorkspace(tmp_path) as work:
        path = work.path / "source.pdf"
        with fitz.open() as doc:
            doc.new_page()
            doc.save(path)
        def unexpected_call(text):
            pytest.fail("Blank pages must not call the API")
        result = split_pdf(path, work, [Chapter("空白", 1, 1)], summarize=unexpected_call)
        assert "無可擷取文字" in result.rows[0]["摘要"]


@pytest.mark.parametrize("chapters", [[], [Chapter("a", 2, 5)], [Chapter("a", 1, 2)],
                                     [Chapter("a", 1, 4), Chapter("b", 4, 5)]])
def test_invalid_ranges_are_rejected(chapters):
    with pytest.raises(PDFError):
        processor.validate_chapters(chapters, 5)
