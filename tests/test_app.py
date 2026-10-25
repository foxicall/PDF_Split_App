from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

import transfer_server
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
