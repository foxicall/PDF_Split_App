from types import SimpleNamespace
from unittest.mock import Mock

from google import genai
from google.genai import errors
import httpx
import pytest

from gemini_helper import GeminiSummarizer


def test_gemini_is_lazy_and_uses_environment_fallback(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-environment-key")
    client = Mock()
    monkeypatch.setattr(genai, "Client", client)
    with GeminiSummarizer() as summarizer:
        assert summarizer.api_key == "test-environment-key"
    client.assert_not_called()


def test_summary_is_limited_and_client_closed(monkeypatch):
    client = Mock()
    client.models.generate_content.return_value = SimpleNamespace(text="摘" * 90)
    factory = Mock(return_value=client)
    monkeypatch.setattr(genai, "Client", factory)
    with GeminiSummarizer("test-explicit-key") as summarizer:
        result = summarizer.summarize("文" * 9000)
    assert len(result) == 50
    call = client.models.generate_content.call_args.kwargs
    assert call["model"] == "gemini-3.8-flash"
    assert len(call["contents"]) == 8000
    assert factory.call_args.kwargs["api_key"] == "test-explicit-key"
    client.close.assert_called_once()
    assert summarizer.api_key == ""


def test_failure_is_redacted_and_no_further_requests(monkeypatch):
    client = Mock()
    client.models.generate_content.side_effect = RuntimeError("private-document test-secret-key")
    monkeypatch.setattr(genai, "Client", Mock(return_value=client))
    with GeminiSummarizer("test-secret-key") as summarizer:
        first = summarizer.summarize("private-document")
        second = summarizer.summarize("other content")
    assert first == second
    assert "private-document" not in first and "test-secret-key" not in first
    assert "摘要暫停" in first
    client.models.generate_content.assert_called_once()


def test_blank_text_never_calls_api(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(genai, "Client", factory)
    with GeminiSummarizer("test-key") as summarizer:
        assert "無可擷取文字" in summarizer.summarize(" \n")
    factory.assert_not_called()


@pytest.mark.parametrize(('status', 'reason', 'expected'), [
    (400, 'API_KEY_INVALID', 'GEMINI_KEY_INVALID'),
    (400, 'API_KEY_EXPIRED', 'GEMINI_KEY_INVALID'),
    (401, None, 'GEMINI_KEY_INVALID'),
    (403, 'API_KEY_HTTP_REFERRER_BLOCKED', 'GEMINI_KEY_RESTRICTED'),
    (403, 'SERVICE_DISABLED', 'GEMINI_API_DISABLED'),
    (403, 'BILLING_DISABLED', 'GEMINI_BILLING'),
    (403, None, 'GEMINI_PERMISSION'),
    (404, None, 'GEMINI_MODEL'),
    (429, None, 'GEMINI_QUOTA'),
    (400, None, 'GEMINI_REQUEST'),
    (503, None, 'GEMINI_UNAVAILABLE'),
])
def test_google_errors_are_actionable_without_leaking_details(monkeypatch, caplog, status, reason, expected):
    payload = {'error': {'code': status, 'message': 'private-document test-secret-key',
                        'details': [{'reason': reason, 'metadata': {'key': 'test-secret-key'}}]}}
    client = Mock()
    client.models.generate_content.side_effect = errors.APIError(status, payload)
    monkeypatch.setattr(genai, 'Client', Mock(return_value=client))
    with GeminiSummarizer('test-secret-key') as summarizer:
        first = summarizer.summarize('private-document')
        assert expected in first
        assert summarizer.summarize('another chapter') == first
    client.models.generate_content.assert_called_once()
    client.close.assert_called_once()
    assert expected in caplog.text
    for secret in ['private-document', 'test-secret-key', 'another chapter']:
        assert secret not in first + caplog.text


@pytest.mark.parametrize(('error', 'expected'), [
    (httpx.ReadTimeout('private-document test-secret-key'), 'GEMINI_TIMEOUT'),
    (httpx.ConnectError('private-document test-secret-key'), 'GEMINI_NETWORK'),
    (RuntimeError('private-document test-secret-key'), 'GEMINI_UNKNOWN'),
    (ImportError('private-document test-secret-key'), 'GEMINI_SDK'),
])
def test_transport_and_sdk_errors_are_redacted(monkeypatch, caplog, error, expected):
    monkeypatch.setattr(genai, 'Client', Mock(side_effect=error))
    with GeminiSummarizer('test-secret-key') as summarizer:
        result = summarizer.summarize('private-document')
    assert expected in result
    assert 'private-document' not in result + caplog.text
    assert 'test-secret-key' not in result + caplog.text
