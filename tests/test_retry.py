import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import httpx
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google import genai
from google.genai import errors

import gemini_helper
import pdf_processor
from gemini_helper import GeminiSummarizer, GeminiSummaryError
from tests.conftest import upload_to_store
from tests.test_summary import wait_done
from tests.test_transfer import http_client


@pytest.fixture(autouse=True)
def no_real_wait_jitter(monkeypatch):
    monkeypatch.setattr(gemini_helper.random, 'uniform', lambda *_: 0)


def unavailable():
    return errors.APIError(503, {'error': {'message': 'private-document private-key'}})


def test_transient_retry_budget_is_shared_across_chunks(monkeypatch, caplog):
    monkeypatch.setattr(gemini_helper, 'RETRY_DELAYS', (0, 0))
    google = Mock()
    google.models.generate_content.side_effect = [unavailable(), SimpleNamespace(text='第一段'),
        unavailable(), SimpleNamespace(text='第二段'), unavailable()]
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    requests, waiting = Mock(), Mock()
    with GeminiSummarizer('private-key', retry_transient=True, on_request=requests, on_retry=waiting) as helper:
        assert helper.summarize_chunk('private-document') == '第一段'
        assert helper.summarize_chunk('next') == '第二段'
        with pytest.raises(GeminiSummaryError, match='GEMINI_UNAVAILABLE') as caught:
            helper.summarize_chunk('last')
    assert caught.value.retryable and requests.call_count == 5 and waiting.call_count == 2
    assert google.models.generate_content.call_count == 5
    assert 'private-key' not in caplog.text and 'private-document' not in caplog.text
    google.close.assert_called_once()


@pytest.mark.parametrize('code', [400, 401, 403, 404, 429])
def test_permanent_and_quota_failures_are_not_automatically_retried(monkeypatch, code):
    google = Mock()
    google.models.generate_content.side_effect = errors.APIError(code, {'error': {'message':'secret'}})
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    waiting = Mock()
    with GeminiSummarizer('mock-key', retry_transient=True, on_retry=waiting) as helper:
        with pytest.raises(GeminiSummaryError):
            helper.summarize_document('text')
    google.models.generate_content.assert_called_once()
    waiting.assert_not_called()


def options(session, **extra):
    return {'operation':'summary', 'upload_id':session.upload_id, 'gemini':True,
            'api_key':'private-key', 'allow_multiple_requests':True, 'retry_transient':True, 'retain_for_retry':True, **extra}


def paused_session(service, make_pdf, monkeypatch):
    monkeypatch.setattr(gemini_helper, 'RETRY_DELAYS', (0, 0))
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    google = Mock()
    google.models.generate_content.side_effect = [SimpleNamespace(text='已完成第一段'), unavailable(), unavailable(), unavailable()]
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=9))
    service.store.start_mobile_process(session, options(session))
    wait_done(session)
    assert session.state == 'summary_paused'
    assert session.summary_requests == 4 and len(session.summary_checkpoint['parts']) == 1
    return session, google


def test_resume_skips_completed_chunk_and_counts_attempts(service, make_pdf, monkeypatch):
    session, google = paused_session(service, make_pdf, monkeypatch)
    directory = session.workspace.path
    assert not (directory / 'source.pdf').exists()
    assert not (directory / 'summary-00.txt').exists()
    assert len(list(directory.glob('summary-*.txt'))) == 3
    assert 'private-key' not in str(session.snapshot()) + session.error
    assert session.document_summary is None  # Partial work must not be exported as a complete summary.
    first = google.models.generate_content.call_args_list[0].kwargs['contents']
    failed_chunk = google.models.generate_content.call_args.kwargs['contents']
    google.models.generate_content.reset_mock()
    google.models.generate_content.side_effect = None
    google.models.generate_content.return_value = SimpleNamespace(text='接續的摘要')
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    with http_client() as client:
        partial = client.get(base + 'summary.md', timeout=5)
        assert partial.status_code == 200
        assert '未完成／部分結果' in partial.text and '僅完成 1 / 4 段' in partial.text
        assert '已完成第一段' in partial.text and 'private-key' not in partial.text
        assert client.post(base + 'retry-summary', json=options(session, gemini=False), timeout=5).status_code == 400
        assert client.post(base + 'retry-summary', json=options(session, upload_id='old'), timeout=5).status_code == 409
        assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 202
        wait_done(session)
        assert session.state == 'summarized'
        assert google.models.generate_content.call_count == 4  # Three remaining chunks + synthesis.
        inputs = [c.kwargs['contents'] for c in google.models.generate_content.call_args_list]
        assert inputs[0] == failed_chunk and first not in inputs
        assert session.document_summary['api_requests'] == 8
        assert session.document_summary['parts'][0]['text'] == '已完成第一段'
        assert not directory.exists() and not session.summary_checkpoint
        assert client.get(base + 'summary.md', timeout=5).status_code == 200
        assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 409


def test_resume_synthesis_only_and_reject_concurrent_retry(service, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    google = Mock()
    google.models.generate_content.side_effect = [SimpleNamespace(text='分段') for _ in range(4)] + [unavailable()]
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=9))
    service.store.start_mobile_process(session, options(session, retry_transient=False))
    wait_done(session)
    assert session.state == 'summary_paused' and len(session.summary_checkpoint['parts']) == 4
    assert not list(session.workspace.path.glob('*.txt'))
    synthesis_input = google.models.generate_content.call_args.kwargs['contents']
    entered, release = threading.Event(), threading.Event()
    def pending(**_):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(text='最後整合')
    google.models.generate_content.reset_mock()
    google.models.generate_content.side_effect = pending
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    with http_client() as client:
        try:
            assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 202
            assert entered.wait(5)
            session.retry_expires -= 601
            service.store.sweep()
            assert session.token in service.store.sessions and session.workspace.path.exists()
            assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 409
        finally:
            release.set()
    wait_done(session)
    assert session.state == 'summarized'
    google.models.generate_content.assert_called_once()
    assert google.models.generate_content.call_args.kwargs['contents'] == synthesis_input
    assert not list(service.store.root.glob('job-*'))


@pytest.mark.parametrize('action', ['clear', 'cancel', 'replacement', 'expiry', 'expired_retry'])
def test_paused_checkpoint_cleanup(service, make_pdf, monkeypatch, action):
    session, google = paused_session(service, make_pdf, monkeypatch)
    directory = session.workspace.path
    calls = google.models.generate_content.call_count
    if action == 'clear':
        service.store.clear(session)
    elif action == 'cancel':
        service.store.cancel_summary(session)
        assert session.state == 'cancelled'
    elif action == 'replacement':
        upload_to_store(service.store, make_pdf(name='replacement.pdf', pages=1), session)
    else:
        session.touched -= service.store.ttl + 1
        if action == 'expiry':
            service.store.sweep()
            assert session.token not in service.store.sessions
        else:
            base = f'http://127.0.0.1:{service.port}/{session.token}/'
            with http_client() as client:
                assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 410
    assert not directory.exists() and not session.summary_checkpoint
    assert google.models.generate_content.call_count == calls


def test_cancel_during_backoff_makes_no_more_requests(service, make_pdf, monkeypatch):
    google = Mock()
    session = upload_to_store(service.store, make_pdf(pages=2))
    def fail(**_):
        session.cancel_event.set()
        raise unavailable()
    google.models.generate_content.side_effect = fail
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    service.store.start_mobile_process(session, options(session))
    wait_done(session)
    assert session.state == 'cancelled'
    google.models.generate_content.assert_called_once()
    assert not list(service.store.root.glob('job-*')) and not session.summary_checkpoint


def test_retry_after_and_jitter_are_respected_without_real_wait(monkeypatch):
    google = Mock()
    response = httpx.Response(503, headers={'Retry-After':'5'})
    google.models.generate_content.side_effect = [errors.APIError(503, {}, response=response), SimpleNamespace(text='完成')]
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    monkeypatch.setattr(gemini_helper.random, 'uniform', lambda *_: .25)
    stop = Mock()
    stop.is_set.return_value = stop.wait.return_value = False
    with GeminiSummarizer('mock-key', retry_transient=True, cancel_event=stop) as helper:
        assert helper.summarize_document('文字') == '完成'
    stop.wait.assert_called_once_with(5)
    date_error = errors.APIError(503, {}, response=httpx.Response(503, headers={
        'Retry-After':format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)}))
    assert 58 <= gemini_helper.retry_after_seconds(date_error) <= 60


def test_long_retry_after_pauses_and_blocks_early_manual_retry(service, make_pdf, monkeypatch):
    google = Mock()
    google.models.generate_content.side_effect = errors.APIError(503, {}, response=httpx.Response(503, headers={'Retry-After':'120'}))
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=2))
    service.store.start_mobile_process(session, options(session))
    wait_done(session)
    assert session.state == 'summary_paused' and session.snapshot()['retry_after_seconds'] > 115
    google.models.generate_content.assert_called_once()
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    with http_client() as client:
        assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 429
    google.models.generate_content.assert_called_once()
    service.store.cancel_summary(session)
    assert not list(service.store.root.glob('job-*'))


def test_ambiguous_timeout_pauses_without_automatic_resend(service, make_pdf, monkeypatch):
    google = Mock()
    google.models.generate_content.side_effect = httpx.ReadTimeout('private-key private-document')
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=2))
    service.store.start_mobile_process(session, options(session))
    wait_done(session)
    assert session.state == 'summary_paused' and 'GEMINI_TIMEOUT' in session.error
    google.models.generate_content.assert_called_once()
    assert 'private-key' not in str(session.snapshot())
    assert not (session.workspace.path / 'source.pdf').exists()
    service.store.clear(session)


def test_opt_out_cleans_original_text_but_keeps_partial_markdown(service, make_pdf, monkeypatch):
    monkeypatch.setattr(pdf_processor, 'MAX_SUMMARY_CHARS', 20)
    google = Mock()
    google.models.generate_content.side_effect = [SimpleNamespace(text='第一段筆記'), unavailable()]
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    session = upload_to_store(service.store, make_pdf(pages=9))
    service.store.start_mobile_process(session, options(session, retain_for_retry=False, retry_transient=False))
    wait_done(session)
    assert session.state == 'summary_partial'
    assert not session.summary_checkpoint and session.workspace is None
    assert not list(service.store.root.glob('job-*'))
    base = f'http://127.0.0.1:{service.port}/{session.token}/'
    with http_client() as client:
        result = client.get(base + 'summary.md', timeout=5)
        assert result.status_code == 200 and '未完成／部分結果' in result.text
        assert '第一段筆記' in result.text and 'private-key' not in result.text
        assert client.post(base + 'retry-summary', json=options(session), timeout=5).status_code == 409


def test_retention_deadline_is_not_extended_by_repeated_failure(service, make_pdf, monkeypatch):
    session, google = paused_session(service, make_pdf, monkeypatch)
    expiry = session.retry_expires
    google.models.generate_content.side_effect = unavailable()
    service.store.start_mobile_process(session, options(session), resume=True)
    wait_done(session)
    assert session.state == 'summary_paused' and session.retry_expires == expiry
    session.retry_expires -= 601
    session.touch()  # Recent activity must not extend the fixed deadline.
    service.store.sweep()
    assert session.token not in service.store.sessions
    assert not list(service.store.root.glob('job-*'))


def test_orphaned_summary_text_uses_short_expiry_even_with_fresh_lease(service):
    from pdf_processor import TemporaryWorkspace
    import time
    workspace = TemporaryWorkspace(service.store.root)
    (workspace.path / '.text-expiry').write_text(str(time.time() - 1))
    (workspace.path / 'summary-00.txt').write_text('generated fixture text')
    service.store.sweep()
    assert not workspace.path.exists()
