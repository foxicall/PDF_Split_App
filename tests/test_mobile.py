import time
import io
import zipfile
from unittest.mock import Mock

from google import genai
from google.genai import errors

from tests.conftest import upload_to_store
from tests.test_transfer import http_client


def test_pwa_static_routes_and_manifest(service):
    base = f"http://127.0.0.1:{service.port}"
    with http_client() as client:
        assert client.get(base + "/health", timeout=5).json() == {"ok": True}
        assert client.get(base, timeout=5).status_code == 200
        manifest = client.get(base + "/manifest.webmanifest", timeout=5).json()
        assert manifest["display"] == "standalone"
        for icon in manifest["icons"]:
            response = client.get(base + icon["src"], timeout=5)
            assert response.headers["Content-Type"] == "image/png"
            assert response.content.startswith(b"\x89PNG")
        assert client.get(base + "/sw.js", timeout=5).status_code == 200
        assert client.get(base + "/../mobile_server.py", timeout=5).status_code == 404
        assert client.post(base + "/sessions", data="{}", timeout=5).status_code == 415
        response = client.post(base + "/sessions", json={}, timeout=5)
        assert response.status_code == 201
        assert len(response.json()["token"]) == 43


def test_mobile_process_requires_current_upload_and_explicit_summary_opt_in(service, make_pdf, monkeypatch):
    factory = Mock(side_effect=AssertionError("Gemini must not be used without opt-in"))
    monkeypatch.setattr(genai, "Client", factory)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-no-network")
    session = upload_to_store(service.store, make_pdf(pages=3))
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        details = client.get(base + "details", timeout=5).json()
        assert details["page_count"] == 3 and details["levels"] == []
        options = {"mode": "fixed", "pages": 2, "upload_id": "old"}
        assert client.post(base + "process", json=options, timeout=5).status_code == 409
        options["upload_id"] = details["upload_id"]
        assert client.post(base + "process", json=options, timeout=5).status_code == 202
        deadline = time.monotonic() + 5
        while session.state == "processing" and time.monotonic() < deadline:
            time.sleep(.01)
        assert session.state == "ready"
        assert len(session.rows) == 2
        assert all(row["摘要"] == "未啟用" for row in session.rows)
        assert client.post(base + "process", json=options, timeout=5).status_code == 409
    factory.assert_not_called()


def test_mobile_bad_settings_preserve_upload(service, make_pdf):
    session = upload_to_store(service.store, make_pdf(pages=1))
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        response = client.post(base + "process", json={"upload_id": session.upload_id, "mode": "fixed", "pages": 0}, timeout=5)
        assert response.status_code >= 400
        assert session.state == "uploaded"
        assert (session.workspace.path / "source.pdf").exists()


def test_summary_quota_failure_preserves_zip_and_cleans_download(service, make_pdf, monkeypatch):
    google_client = Mock()
    google_client.models.generate_content.side_effect = errors.APIError(
        429, {'error': {'message': 'private-document test-key', 'status': 'RESOURCE_EXHAUSTED'}})
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google_client))
    session = upload_to_store(service.store, make_pdf(pages=2))
    base = f"http://127.0.0.1:{service.port}/{session.token}/"
    with http_client() as client:
        response = client.post(base + 'process', json={
            'upload_id': session.upload_id, 'mode': 'fixed', 'pages': 1,
            'gemini': True, 'api_key': 'test-key'}, timeout=5)
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while session.state == 'processing' and time.monotonic() < deadline:
            time.sleep(.01)
        assert session.state == 'ready'
        details = client.get(base + 'details', timeout=5).json()
        assert all('GEMINI_QUOTA' in row['摘要'] for row in details['rows'])
        assert 'test-key' not in str(details) and 'private-document' not in str(details)
        directory = session.workspace.path
        response = client.get(base + 'download', timeout=5)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len(archive.namelist()) == 2 and archive.testzip() is None
        deadline = time.monotonic() + 2
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not directory.exists()
    google_client.models.generate_content.assert_called_once()
