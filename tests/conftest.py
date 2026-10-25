from pathlib import Path

import fitz
import pytest

from transfer_server import SessionStore, TransferService


@pytest.fixture
def make_pdf(tmp_path):
    def create(name="input.pdf", pages=20, toc=None, encrypted=False):
        path = tmp_path / name
        with fitz.open() as doc:
            for number in range(1, pages + 1):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {number}")
            if toc:
                doc.set_toc(toc)
            options = {"encryption": fitz.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "secret"} if encrypted else {}
            doc.save(path, **options)
        return path
    return create


@pytest.fixture
def store(tmp_path):
    instance = SessionStore(tmp_path / "spool", ttl_seconds=60)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def service(tmp_path):
    instance = TransferService(tmp_path / "http-spool", port=0, ttl_seconds=60)
    try:
        yield instance
    finally:
        instance.close()


def upload_to_store(store, path: Path, session=None):
    session = session or store.create()
    upload_id = store.begin(session, path.name, path.stat().st_size)
    with path.open("rb") as stream:
        while data := stream.read(4096):
            store.append(session, upload_id, session.received, data)
    store.finish(session, upload_id)
    return session
