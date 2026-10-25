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
from gemini_helper import GeminiSummaryError
from summary_markdown import summary_filename
from pdf_processor import PDFCancelled, TemporaryWorkspace, summarize_pdf
from tests.conftest import upload_to_store
from tests.test_transfer import http_client


def wait_done(session):
    deadline = time.monotonic() + 5
    # Wait on the same locked snapshot used by HTTP/UI readers, so a state
    # transition cannot be observed before its timestamp/cleanup is committed.
    while session.snapshot()['state'] == 'processing' and time.monotonic() < deadline:
        time.sleep(.01)
    assert session.snapshot()['state'] != 'processing'


def test_summary_reads_selected_pages_without_split_or_zip(tmp_path, make_pdf, monkeypatch):
    split = Mock(side_effect=AssertionError('Must not split'))
    monkeypatch.setattr(pdf_processor, '_write_chapter', split)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=5), source)
    def generate(text):
        assert sorted(p.name for p in workspace.path.iterdir()) == ['.lease', '.text-expiry', 'summary-00.txt']
        assert 'Page 2' in text and 'Page 4' in text
        assert 'Page 1' not in text and 'Page 5' not in text
        return '所選頁面摘要'
    result = summarize_pdf(source, workspace, 2, 4, generate)
    assert result['text'] == '所選頁面摘要'
    assert (result['start_page'], result['end_page'], result['requested_end_page']) == (2, 4, 4)
    assert result['truncated'] is False
    assert not workspace.path.exists()
    split.assert_not_called()


def test_summary_covers_tail_pages_across_bounded_chunks(tmp_path, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=9), source)
    generate = Mock(side_effect=lambda text: '重點：' + text)
    combine = Mock(return_value='含後半段的整合摘要')
    result = summarize_pdf(source, workspace, 1, 9, generate, combine=combine)
    chunks = [call.args[0] for call in generate.call_args_list]
    assert ''.join(chunks) == '\n'.join(f'Page {i}' for i in range(1, 10))
    assert all(len(text) <= 20 for text in chunks)
    assert '第 9–9 頁' in combine.call_args.args[0]
    assert result['parts'][-1]['text'] in combine.call_args.args[0]
    assert result['end_page'] == 9 and result['truncated'] is False
    assert result['chunk_count'] == 4 and result['api_requests'] == 5
    assert len(result['parts']) == 4 and not workspace.path.exists()


def test_summary_rejects_oversize_before_any_api_call(tmp_path, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 10)
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHUNKS', 2)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=9), source)
    generate = Mock()
    with pytest.raises(pdf_processor.PDFError, match='尚未呼叫 Gemini'):
        summarize_pdf(source, workspace, 1, 9, generate)
    generate.assert_not_called()
    assert not workspace.path.exists()


def test_summary_splits_within_a_dense_page_without_losing_characters(tmp_path, make_pdf, monkeypatch):
    monkeypatch.setattr(fitz.Page, 'get_text', lambda self, _: '密' * 24_001 + f'頁{self.number + 1}')
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=2), source)
    generate = Mock(return_value='分段筆記')
    result = summarize_pdf(source, workspace, 1, 2, generate, combine=Mock(return_value='整合'))
    inputs = [call.args[0] for call in generate.call_args_list]
    assert ''.join(inputs) == '密' * 24_001 + '頁1\n' + '密' * 24_001 + '頁2'
    assert [len(text) for text in inputs] == [24_000, 24_000, 7]
    assert result['parts'][0]['start_page'] == result['parts'][0]['end_page'] == 1
    assert result['parts'][1]['end_page'] == 2
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
    client.models.generate_content.return_value = SimpleNamespace(text='摘' * 4000)
    monkeypatch.setattr(genai, 'Client', Mock(return_value=client))
    with GeminiSummarizer('mock-key') as summarizer:
        result = summarizer.summarize_document('文' * 24_000)
    assert len(result) == 3000
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
                    {'end_page': 61}, {'end_page': 51}, {'summary_style': 'unknown'}, {'summary_style': []}, {'start_page': 3}, {'upload_id': 'old'}]:
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
    assert session.state == 'error' and code in session.error
    assert session.document_summary is None
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
            # PDF parsing for another upload must not wait on the Gemini network request.
            other = upload_to_store(service.store, make_pdf(name='other.pdf', pages=1))
            assert other.state == 'uploaded'
            service.store.clear(other)
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


@pytest.mark.parametrize('cancel', [True, False])
def test_chunk_cancellation_or_failure_stops_synthesis_and_cleans(tmp_path, make_pdf, monkeypatch, cancel):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 10)
    workspace = TemporaryWorkspace(tmp_path)
    source = workspace.path / 'source.pdf'
    shutil.copyfile(make_pdf(pages=9), source)
    stop = threading.Event()
    def generate(text):
        if cancel:
            stop.set()
            return 'late response'
        raise GeminiSummaryError('摘要暫停 [GEMINI_QUOTA]')
    combine = Mock()
    with pytest.raises(PDFCancelled if cancel else GeminiSummaryError):
        summarize_pdf(source, workspace, 1, 9, generate, combine=combine, cancelled=stop.is_set)
    combine.assert_not_called()
    assert not workspace.path.exists()


def test_markdown_export_unicode_repeat_expiry_and_session_isolation(service, make_pdf, monkeypatch):
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text='### 主旨\n\n- 繁體中文摘要 📝\n- 保留換行')
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(name='測試：文件.pdf', pages=2))
    other = service.store.create()
    root = f'http://127.0.0.1:{service.port}/'
    url = root + session.token + '/summary.md'
    with http_client() as client:
        assert client.get(url, timeout=5).status_code == 409
        service.store.start_mobile_process(session, {'operation':'summary', 'upload_id':session.upload_id,
                            'gemini':True, 'api_key':'do-not-export-this-key', 'summary_style':'standard'})
        wait_done(session)
        response = client.get(url, timeout=5)
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Content-Type'] == 'text/markdown; charset=utf-8'
        assert "filename*=UTF-8''" in response.headers['Content-Disposition']
        text = response.content.decode('utf-8')
        assert '### 主旨\n\n- 繁體中文摘要 📝\n- 保留換行' in text
        assert '第 1–2 頁' in text and '標準' in text and 'OCR' in text
        assert session.token not in text and 'do-not-export-this-key' not in text
        assert client.get(url, timeout=5).content == response.content
        assert client.get(root + other.token + '/summary.md', params={'token':session.token}, timeout=5).status_code == 409
        assert session.state == 'summarized' and session.workspace is None
        assert not list(service.store.root.glob('job-*'))
        session.touched -= service.store.ttl + 1
        service.store.sweep()
        assert client.get(url, timeout=5).status_code == 404
    assert summary_filename('../../測試：文件.pdf') == '測試_文件_摘要.md'
    assert '/' not in summary_filename('a/b\\c.pdf')


@pytest.mark.parametrize(('style', 'limit'), [('brief',500), ('standard',1500), ('detailed',3000)])
def test_document_detail_levels_preserve_markdown_and_bound_output(monkeypatch, style, limit):
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text='## 重點\n\n- ' + '文' * 4000)
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    with GeminiSummarizer('mock-key') as helper:
        result = helper.summarize_document('文字', style)
    assert result.startswith('## 重點\n\n- ') and len(result) == limit
    assert '內容已截短' in result
    assert google.models.generate_content.call_args.kwargs['config'].max_output_tokens == 8192


def test_model_token_limit_is_disclosed_even_for_short_output(monkeypatch):
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text='## 部分內容', candidates=[SimpleNamespace(finish_reason='MAX_TOKENS')])
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    with GeminiSummarizer('mock-key') as helper:
        assert '內容已截短' in helper.summarize_document('文')


def test_legacy_client_cannot_silently_opt_into_multiple_paid_calls(service, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    factory = Mock(side_effect=AssertionError('No multi-call consent'))
    monkeypatch.setattr(genai, 'Client', factory)
    session = upload_to_store(service.store, make_pdf(pages=9))
    service.store.start_mobile_process(session, {'operation':'summary', 'upload_id':session.upload_id,
                                               'gemini':True, 'api_key':'mock-key'})
    wait_done(session)
    assert session.state == 'error' and '需要 5 次' in session.error
    factory.assert_not_called()
    assert session.workspace is None
