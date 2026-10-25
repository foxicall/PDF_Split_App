import shutil
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest
from google import genai
from google.genai import errors

import pdf_processor
import transfer_server
from gemini_helper import GeminiSummarizer
from pdf_processor import PDFCancelled, TemporaryWorkspace, summarize_pdf
from tests.conftest import upload_to_store
from tests.test_transfer import http_client


def wait_done(session):
    deadline = time.monotonic() + 5
    while session.state == 'processing' and time.monotonic() < deadline:
        time.sleep(.01)
    assert session.state != 'processing'


def test_summary_reads_selected_pages_without_split_or_zip(tmp_path, make_pdf, monkeypatch):
    split = Mock(side_effect=AssertionError('Must not split'))
    monkeypatch.setattr(pdf_processor, '_write_chapter', split)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=5), source)
    def generate(text):
        assert sorted(p.name for p in workspace.path.iterdir()) == ['.lease', 'source.pdf']
        assert 'Page 2' in text and 'Page 4' in text
        assert 'Page 1' not in text and 'Page 5' not in text
        return '所選頁面摘要'
    result = summarize_pdf(source, workspace, 2, 4, generate)
    assert result['text'] == '所選頁面摘要'
    assert (result['start_page'], result['end_page'], result['requested_end_page']) == (2, 4, 4)
    assert result['truncated'] is False
    assert not workspace.path.exists()
    split.assert_not_called()


def test_summary_reports_actual_coverage_when_text_is_capped(tmp_path, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=5), source)
    generate = Mock(return_value='摘要')
    result = summarize_pdf(source, workspace, 1, 5, generate)
    assert generate.call_args.args[0] == 'Page 1\nPage 2\nPage 3'
    assert result['text_chars'] == 20 and result['truncated'] is True
    assert result['end_page'] == 3 and result['requested_end_page'] == 5
    assert not workspace.path.exists()


def test_summary_image_and_blank_pages_skip_google(tmp_path):
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    with fitz.open() as doc:
        image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
        image.clear_with(255)
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=image)
        doc.new_page(); doc.save(source)
    generate = Mock(side_effect=AssertionError('No text must not call Gemini'))
    result = summarize_pdf(source, workspace, 1, 2, generate)
    assert '所選頁面無可擷取文字' in result['text'] and result['text_chars'] == 0
    generate.assert_not_called()
    assert not workspace.path.exists()


def test_summary_extraction_cancellation_and_generation_failure_cleanup(tmp_path, make_pdf):
    for cancelled in [True, False]:
        workspace = TemporaryWorkspace(tmp_path)
        source = workspace.path / 'source.pdf'
        shutil.copyfile(make_pdf(pages=3), source)
        stop = threading.Event()
        generate = Mock(side_effect=RuntimeError('mock generation failure'))
        with pytest.raises(PDFCancelled if cancelled else RuntimeError):
            summarize_pdf(source, workspace, 1, 3, generate,
                          progress=lambda *_: stop.set() if cancelled else None,
                          cancelled=stop.is_set)
        assert not workspace.path.exists()
        if cancelled:
            generate.assert_not_called()


def test_document_summary_uses_larger_bounded_input_and_output(monkeypatch):
    client = Mock()
    client.models.generate_content.return_value = SimpleNamespace(text='摘' * 400)
    monkeypatch.setattr(genai, 'Client', Mock(return_value=client))
    with GeminiSummarizer('mock-key') as summarizer:
        result = summarizer.summarize_document('文' * 30_000)
    assert len(result) == 300
    assert len(client.models.generate_content.call_args.kwargs['contents']) == 24_000
    assert client.models.generate_content.call_args.kwargs['model'] == 'gemini-3.8-flash'
    client.close.assert_called_once()


def test_summary_requires_consent_key_valid_range_and_current_upload(service, make_pdf, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    factory = Mock(side_effect=AssertionError('Invalid options must not call Gemini'))
    monkeypatch.setattr(genai, 'Client', factory)
    session = upload_to_store(service.store, make_pdf(pages=60))
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    good = {'operation': 'summary', 'upload_id': session.upload_id, 'start_page': 1,
            'end_page': 2, 'gemini': True, 'api_key': 'mock-key'}
    with http_client() as client:
        for bad in [{'gemini': False}, {'api_key': ''}, {'start_page': 0}, {'start_page': True},
                    {'end_page': 61}, {'end_page': 51}, {'start_page': 3}, {'upload_id': 'old'}]:
            response = client.post(base + 'process', json={**good, **bad}, timeout=5)
            assert response.status_code == (409 if 'upload_id' in bad else 400)
            assert session.state == 'uploaded'
            assert (session.workspace.path / 'source.pdf').exists()
    factory.assert_not_called()


def test_summary_without_bookmarks_preserves_no_artifacts_and_resets_on_upload(service, make_pdf, monkeypatch):
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text='文件重點摘要')
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=3))
    directory = session.workspace.path
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    options = {'operation': 'summary', 'upload_id': session.upload_id, 'start_page': 2,
               'end_page': 3, 'gemini': True, 'api_key': 'mock-key'}
    with http_client() as client:
        assert client.post(base + 'process', json=options, timeout=5).status_code == 202
        wait_done(session)
        details = client.get(base + 'details', timeout=5).json()
        assert details['state'] == 'summarized' and details['rows'] == []
        assert details['document_summary']['text'] == '文件重點摘要'
        assert 'Page 1' not in google.models.generate_content.call_args.kwargs['contents']
        assert client.get(base + 'download', timeout=5).status_code == 409
        assert client.post(base + 'process', json=options, timeout=5).status_code == 409
        assert not directory.exists() and session.workspace is None
        upload_to_store(service.store, make_pdf(name='new.pdf', pages=1), session)
        assert session.document_summary is None
        assert client.post(base + 'process', json=options, timeout=5).status_code == 409
        client.post(base + 'clear', json={}, timeout=5)
    assert not list(service.store.root.glob('job-*'))
    session.touched -= service.store.ttl + 1
    service.store.sweep()
    assert session.token not in service.store.sessions


@pytest.mark.parametrize(('status', 'code'), [(401, 'GEMINI_KEY_INVALID'), (404, 'GEMINI_MODEL'), (429, 'GEMINI_QUOTA')])
def test_summary_api_errors_remain_distinguishable_and_clean(service, make_pdf, monkeypatch, status, code):
    google = Mock()
    google.models.generate_content.side_effect = errors.APIError(status, {'error': {'message': 'private-key'}})
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=2))
    service.store.start_mobile_process(session, {'operation': 'summary', 'upload_id': session.upload_id,
                                               'gemini': True, 'api_key': 'private-key'})
    wait_done(session)
    assert session.state == 'summarized' and code in session.document_summary['text']
    assert 'private-key' not in str(session.snapshot())
    assert not list(service.store.root.glob('job-*'))


def test_cancelling_pending_google_request_discards_result_and_cleans(service, make_pdf, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    google = Mock()
    def generate(**_):
        started.set()
        assert finish.wait(5)
        return SimpleNamespace(text='must be discarded')
    google.models.generate_content.side_effect = generate
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=2))
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    with http_client() as client:
        try:
            response = client.post(base + 'process', json={'operation': 'summary', 'upload_id': session.upload_id,
                                   'gemini': True, 'api_key': 'mock-key'}, timeout=5)
            assert response.status_code == 202 and started.wait(5)
            assert client.post(base + 'cancel', json={}, timeout=5).status_code == 202
            assert client.get(base + 'details', timeout=5).json()['cancel_requested'] is True
            assert client.post(base + 'start', json={'name':'new.pdf','size':10}, timeout=5).status_code == 409
        finally:
            finish.set()
        wait_done(session)
        assert session.state == 'cancelled' and session.document_summary is None
        assert not list(service.store.root.glob('job-*'))
        upload_to_store(service.store, make_pdf(name='next.pdf', pages=1), session)
        assert session.state == 'uploaded' and not session.cancel_event.is_set()
        assert service.store._processing.acquire(blocking=False)
        service.store._processing.release()


def test_unexpected_summary_failure_releases_slot_and_files(service, make_pdf, monkeypatch):
    monkeypatch.setattr(transfer_server, 'summarize_pdf', Mock(side_effect=OSError('private-key')))
    session = upload_to_store(service.store, make_pdf(pages=2))
    service.store.start_mobile_process(session, {'operation': 'summary', 'upload_id': session.upload_id,
                                               'gemini': True, 'api_key': 'private-key'})
    wait_done(session)
    assert session.state == 'error' and session.document_summary is None
    assert 'private-key' not in session.error
    assert not list(service.store.root.glob('job-*'))
    assert service.store._processing.acquire(blocking=False)
    service.store._processing.release()
