from types import SimpleNamespace
from unittest.mock import Mock

from google import genai

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
    assert call["model"] == "gemini-2.5-flash"
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
