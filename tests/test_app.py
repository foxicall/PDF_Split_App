from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import streamlit as st
from streamlit.testing.v1 import AppTest

import transfer_server
from google import genai
from tests.conftest import upload_to_store


def test_streamlit_fixed_fallback_and_split(service, make_pdf, monkeypatch):
    st.cache_resource.clear()
    monkeypatch.setattr(transfer_server, "default_service", lambda: service)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"))
    try:
        app.run(timeout=20)
        assert not app.exception
        assert app.checkbox[0].value is False
        session = service.store.get(app.session_state.transfer_token)
        upload_to_store(service.store, make_pdf(pages=5), session)
        app.button[0].click().run(timeout=20)
        assert not app.exception
        assert any("沒有有效的內建書籤" in item.value for item in app.warning)
        app.number_input[0].set_value(2).run()
        app.button[1].click().run(timeout=20)
        assert not app.exception
        assert session.state == "ready"
        assert len(session.rows) == 3
        assert all(row["摘要"] == "未啟用" for row in session.rows)
        assert any("拆分完成" in item.value for item in app.success)
    finally:
        st.cache_resource.clear()


def test_streamlit_summary_only_without_bookmarks(service, make_pdf, monkeypatch):
    st.cache_resource.clear()
    monkeypatch.setattr(transfer_server, 'default_service', lambda: service)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    google = Mock()
    google.models.generate_content.return_value = SimpleNamespace(text='不拆分的文件摘要')
    monkeypatch.setattr(genai, 'Client', Mock(return_value=google))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'))
    try:
        app.run(timeout=20)
        session = service.store.get(app.session_state.transfer_token)
        upload_to_store(service.store, make_pdf(pages=3), session)
        app.button[0].click().run(timeout=20)
        app.radio[0].set_value('只產生摘要（不拆分）').run()
        action = next(b for b in app.button if b.label == '產生文件摘要')
        assert action.disabled
        next(c for c in app.checkbox if c.label == '啟用 Gemini 摘要並同意傳送文字').check().run()
        app.text_input[0].set_value('mock-key').run()
        next(b for b in app.button if b.label == '產生文件摘要').click().run(timeout=20)
        deadline = time.monotonic() + 5
        while session.state == 'processing' and time.monotonic() < deadline:
            time.sleep(.01)
        app.run(timeout=20)
        assert not app.exception
        assert session.state == 'summarized'
        assert app.text_area[0].value == '不拆分的文件摘要'
        assert session.workspace is None and not list(service.store.root.glob('job-*'))
        google.models.generate_content.assert_called_once()
    finally:
        st.cache_resource.clear()


def test_streamlit_can_resume_with_fresh_consent(service, make_pdf, monkeypatch):
    from tests.test_retry import paused_session
    from tests.test_summary import wait_done
    st.cache_resource.clear()
    monkeypatch.setattr(transfer_server, 'default_service', lambda: service)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    session, google = paused_session(service, make_pdf, monkeypatch)
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py'))
    try:
        app.session_state.transfer_token = session.token
        app.run(timeout=20)
        assert not app.exception
        assert next(b for b in app.button if b.label == '接續重試').disabled
        next(c for c in app.checkbox if c.label.startswith('同意接續未完成')).check().run()
        app.text_input[0].set_value('mock-key').run()
        google.models.generate_content.side_effect = None
        google.models.generate_content.return_value = SimpleNamespace(text='接續完成')
        next(b for b in app.button if b.label == '接續重試').click().run(timeout=20)
        wait_done(session)
        app.run(timeout=20)
        assert not app.exception and session.state == 'summarized'
        assert app.text_area[0].value == '接續完成'
        assert session.workspace is None
    finally:
        st.cache_resource.clear()
