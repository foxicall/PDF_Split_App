import time
import zipfile

import requests

from tests.conftest import upload_to_store
from pdf_processor import MAX_UPLOAD_BYTES, fixed_chapters


def http_client():
    client = requests.Session()
    client.trust_env = False
    return client


def test_http_upload_download_and_completion_cleanup(service, make_pdf, tmp_path):
    source = make_pdf(pages=4)
    session = service.store.create()
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        assert client.get(base, timeout=5).status_code == 200
        response = client.post(base + "start", json={"name": source.name, "size": source.stat().st_size}, timeout=5)
        assert response.status_code == 200
        upload_id = response.json()["id"]
        offset = 0
        with source.open("rb") as stream:
            while block := stream.read(512):
                response = client.post(base + "chunk", params={"id": upload_id, "offset": offset}, data=block, timeout=5)
                assert response.status_code == 200
                offset += len(block)
        assert client.post(base + "finish", json={"id": upload_id}, timeout=5).status_code == 200
        directory = session.workspace.path
        service.store.process(session, fixed_chapters(4, 2))
        # Polling and HEAD/Range requests must not consume the one-shot download.
        original_touched = session.touched
        assert client.get(base + "status", timeout=5).json()["state"] == "ready"
        assert session.touched == original_touched
        assert client.head(base + "download", timeout=5).status_code == 501
        assert client.get(base + "download", headers={"Range": "bytes=0-10"}, timeout=5).status_code == 416
        assert directory.exists()
        downloaded = tmp_path / "download.zip"
        with client.get(base + "download", stream=True, timeout=5) as response:
            assert response.status_code == 200
            assert response.headers["Content-Type"] == "application/zip"
            assert response.headers["Cache-Control"] == "no-store"
            with downloaded.open("wb") as stream:
                for block in response.iter_content(1024):
                    stream.write(block)
        with zipfile.ZipFile(downloaded) as archive:
            assert len(archive.namelist()) == 2
            assert archive.testzip() is None
        deadline = time.monotonic() + 3
        # Read the completed state under the session lock: removing the directory
        # happens before the download worker publishes its final state.
        while session.snapshot()["state"] == "downloading" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not directory.exists()
        assert session.snapshot()["state"] == "downloaded"
        assert client.get(base + "download", timeout=5).status_code == 409


def test_disconnect_retains_artifact_for_retry(service, make_pdf):
    session = upload_to_store(service.store, make_pdf(pages=1))
    service.store.process(session, fixed_chapters(1, 1))
    directory = session.workspace.path
    # A sparse large response exceeds socket buffers, forcing an actual interrupted write.
    with (directory / "chapters.zip").open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024)
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        response = client.get(base + "download", stream=True, timeout=5)
        assert response.raw.read(1024)
        assert session.state == "downloading"
        # A second GET, clear, and reupload cannot invalidate an in-flight response.
        assert client.get(base + "download", timeout=5).status_code == 409
        assert client.post(base + "clear", json={}, timeout=5).status_code == 409
        assert client.post(base + "start", json={"name": "new.pdf", "size": 100}, timeout=5).status_code == 409
        session.touched = time.monotonic() - 100
        service.store.sweep()
        assert directory.exists()
        session.touch()
        response.close()
    deadline = time.monotonic() + 3
    while session.state == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.state == "ready"
    assert directory.exists()
    session.touched = time.monotonic() - 100
    service.store.sweep()
    assert not directory.exists()


def test_unknown_token_and_oversized_http_upload(service):
    session = service.store.create()
    root = f"http://127.0.0.1:{service.port}/"
    base = root + session.token + "/"
    with http_client() as client:
        assert client.get(root + "unknown/status", timeout=5).status_code == 404
        response = client.post(base + "start", json={"name": "huge.pdf", "size": MAX_UPLOAD_BYTES + 1}, timeout=5)
        assert response.status_code == 413
        upload_id = client.post(base + "start", json={"name": "file.pdf", "size": 1}, timeout=5).json()["id"]
        response = client.post(base + "chunk", params={"id": upload_id, "offset": 0}, data=b"xx", timeout=5)
        assert response.status_code == 413
        response = client.post(base + "finish", json={"id": upload_id}, timeout=5)
        assert response.status_code == 409


def test_session_capabilities_isolate_artifacts_and_expire(service, make_pdf, tmp_path):
    first = upload_to_store(service.store, make_pdf(name="first.pdf", pages=2))
    second = upload_to_store(service.store, make_pdf(name="second.pdf", pages=1))
    service.store.process(first, fixed_chapters(2, 1))
    service.store.process(second, fixed_chapters(1, 1))
    first_directory = first.workspace.path
    assert first.token != second.token
    assert len(first.token) == len(second.token) == 43  # 32 random bytes, URL-safe base64.
    root = f"http://127.0.0.1:{service.port}/"
    with http_client() as client:
        # Caller-controlled query parameters cannot select a different session's file.
        response = client.get(root + second.token + "/download", params={
            "token": first.token, "file": str(first_directory / "chapters.zip"),
        }, timeout=5)
        assert response.status_code == 200
        downloaded = tmp_path / "second-session.zip"
        downloaded.write_bytes(response.content)
        with zipfile.ZipFile(downloaded) as archive:
            assert len(archive.namelist()) == 1
        assert first.state == "ready"
        assert (first_directory / "chapters.zip").exists()
        assert client.get(root + "invalid-capability/download", timeout=5).status_code == 404
        first.touched = time.monotonic() - 100
        service.store.sweep()
        assert client.get(root + first.token + "/download", timeout=5).status_code == 404
        assert not first_directory.exists()


def test_upload_name_and_routes_cannot_traverse_filesystem(service, make_pdf, tmp_path):
    session = service.store.create()
    source = make_pdf(pages=1)
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        response = client.post(base + "start", json={
            "name": "../../escaped.pdf", "size": source.stat().st_size,
        }, timeout=5)
        assert response.status_code == 200
        upload_id = response.json()["id"]
        with source.open("rb") as stream:
            response = client.post(base + "chunk", params={"id": upload_id, "offset": 0}, data=stream.read(4096), timeout=5)
        assert response.status_code == 200
        assert client.post(base + "finish", json={"id": upload_id}, timeout=5).status_code == 200
        assert (session.workspace.path / "source.pdf").exists()
        assert not (tmp_path / "escaped.pdf").exists()
        for suffix in ["../.owner.lock", "%2e%2e%2f.owner.lock", "download/../../source.pdf",
                       "source.pdf", "chapters.zip"]:
            assert client.get(base + suffix, timeout=5).status_code == 404
        assert session.state == "uploaded"
