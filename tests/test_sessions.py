import os
import time

import pytest

import transfer_server
from tests.conftest import upload_to_store
from pdf_processor import MAX_UPLOAD_BYTES, PDFError, fixed_chapters
from transfer_server import RequestError, SessionStore


def test_reupload_and_repeated_runs_clean_previous_results(store, make_pdf):
    path = make_pdf(pages=3)
    session = upload_to_store(store, path)
    old_directory = session.workspace.path
    store.process(session, fixed_chapters(3, 2))
    assert session.state == "ready"
    assert (old_directory / "chapters.zip").exists()
    assert not (old_directory / "source.pdf").exists()
    upload_to_store(store, path, session)
    assert not old_directory.exists()
    store.process(session, fixed_chapters(3, 1))
    assert len(session.rows) == 3
    new_directory = session.workspace.path
    store.clear(session)
    assert not new_directory.exists()
    assert session.state == "empty"


def test_expiration_cleans_idle_upload_and_preserves_active_work(store, make_pdf):
    expired = upload_to_store(store, make_pdf())
    active = upload_to_store(store, make_pdf(name="second.pdf"))
    removed_path, kept_path = expired.workspace.path, active.workspace.path
    expired.touched = active.touched = time.monotonic() - 100
    active.state = "processing"
    store.sweep()
    assert not removed_path.exists()
    assert kept_path.exists()
    with pytest.raises(RequestError):
        store.get(expired.token)
    active.state = "ready"
    store.sweep()
    assert not kept_path.exists()


def test_active_download_is_not_cleaned_or_replaced(store, make_pdf):
    session = upload_to_store(store, make_pdf())
    session.state = "downloading"
    session.touched = time.monotonic() - 100
    with pytest.raises(RequestError, match="正在"):
        store.clear(session)
    with pytest.raises(RequestError, match="正在"):
        store.begin(session, "another.pdf", 100)
    store.sweep()
    assert session.workspace.path.exists()


def test_orphans_are_reaped_but_recent_files_are_preserved(tmp_path):
    root = tmp_path / "orphans"
    old = root / "job-old"
    new = root / "job-new"
    old.mkdir(parents=True)
    new.mkdir()
    (old / "source.pdf").write_bytes(b"abandoned")
    os.utime(old, (time.time() - 100, time.time() - 100))
    store = SessionStore(root, ttl_seconds=60)
    try:
        assert not old.exists()
        assert new.exists()
    finally:
        store.close()


def test_declared_and_actual_upload_limits_and_offsets(store):
    session = store.create()
    with pytest.raises(RequestError) as error:
        store.begin(session, "big.pdf", MAX_UPLOAD_BYTES + 1)
    assert error.value.status == 413
    upload_id = store.begin(session, "max.pdf", MAX_UPLOAD_BYTES)
    assert session.expected == MAX_UPLOAD_BYTES  # Inclusive exact 1GiB boundary.
    with pytest.raises(RequestError, match="順序"):
        store.append(session, upload_id, 5, b"x")
    upload_id = store.begin(session, "small.pdf", 3)
    with pytest.raises(RequestError) as error:
        store.append(session, upload_id, 0, b"four")
    assert error.value.status == 413
    assert session.received == 0


def test_invalid_uploaded_pdf_is_deleted(store, tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"not a PDF")
    session = store.create()
    with pytest.raises(RequestError):
        upload_to_store(store, path, session)
    assert session.state == "error"
    assert session.workspace is None
    assert not list(store.root.glob("job-*"))


def test_low_disk_upload_is_cleaned(store, monkeypatch):
    session = store.create()
    upload_id = store.begin(session, "file.pdf", 10)
    old = session.workspace.path
    def no_space(*args):
        raise PDFError("full")
    monkeypatch.setattr(transfer_server, "ensure_space", no_space)
    with pytest.raises(RequestError) as error:
        store.append(session, upload_id, 0, b"123")
    assert error.value.status == 507
    assert not old.exists()


def test_processing_failure_cleans_session_and_releases_slot(store, make_pdf, monkeypatch):
    session = upload_to_store(store, make_pdf())
    old = session.workspace.path
    def broken_split(*args, **kwargs):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(transfer_server, "split_pdf", broken_split)
    with pytest.raises(RuntimeError):
        store.process(session, fixed_chapters(20, 10))
    assert not old.exists()
    assert session.state == "error"
    assert store._processing.acquire(blocking=False)
    store._processing.release()


def test_stale_preview_cannot_process_replacement_pdf(store, make_pdf):
    path = make_pdf()
    session = upload_to_store(store, path)
    old_id = session.upload_id
    upload_to_store(store, path, session)
    with pytest.raises(RequestError, match="已變更"):
        store.process(session, fixed_chapters(20, 10), expected_upload_id=old_id)
    assert session.state == "uploaded"
    assert (session.workspace.path / "source.pdf").exists()


def test_second_owner_cannot_reap_live_root(store):
    with pytest.raises(PDFError, match="另一個服務"):
        SessionStore(store.root)
