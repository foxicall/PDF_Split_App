"""Run with: streamlit run app.py"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components

from gemini_helper import GEMINI_MODEL, GeminiSummarizer
from pdf_processor import PDFError, bookmark_chapters, fixed_chapters
from transfer_server import RequestError, default_service


@st.cache_resource
def get_service():
    return default_service()


def transfer_widget(token: str, port: int) -> None:
    base = os.environ.get("PDF_TRANSFER_PUBLIC_URL", "").rstrip("/")
    if base and not (urlsplit(base).scheme in {"http", "https"} or (base.startswith("/") and not base.startswith("//"))):
        raise PDFError("PDF_TRANSFER_PUBLIC_URL 必須為 HTTP(S) 網址或以 / 開頭的同站台路徑。")
    config = json.dumps({"base": base, "token": token, "port": port}).replace("<", "\\u003c")
    markup = (
        '<iframe id="transfer" title="PDF 分塊上傳與串流下載" '
        'style="border:0;width:100%;height:345px" referrerpolicy="no-referrer"></iframe>'
        '<script>const c = ' + config + ';'
        'const here = new URL(document.referrer || window.parent.location.href);'
        'const base = c.base || (here.protocol + "//" + here.hostname + ":" + c.port);'
        'document.getElementById("transfer").src = new URL(base + "/" + c.token + "/", here).href;</script>'
    )
    if hasattr(st, "iframe"):
        st.iframe(markup, height=355)
    else:
        components.html(markup, height=355)


def main() -> None:
    st.set_page_config(page_title="PDF 書籤拆分工具", page_icon="📚", layout="wide")
    st.title("📚 PDF 書籤拆分工具")
    st.write("依書籤或固定頁數拆分 PDF，打包為 ZIP 一次下載。支援最大 1GB（1024MB）。")
    try:
        service = get_service()
        token = st.session_state.get("transfer_token")
        try:
            session = service.store.get(token) if token else service.store.create()
        except RequestError:
            session = service.store.create()
            st.info("上一個工作已到期，暫存檔已清除；請重新上傳。")
        st.session_state.transfer_token = session.token
    except (OSError, ValueError, PDFError):
        st.error("無法啟動傳輸服務。請檢查 PDF_TRANSFER_PORT、暫存目錄權限與磁碟空間，並確認只有一個服務程序。")
        st.stop()

    with st.sidebar:
        st.subheader("智慧摘要（選用）")
        use_gemini = st.checkbox("啟用 Gemini 章節摘要", value=False)
        api_key = st.text_input("Gemini API Key", type="password", key="gemini_key",
                                help="留空時使用伺服器環境變數 GEMINI_API_KEY；不會寫入檔案。")
        has_key = bool(api_key.strip() or os.environ.get("GEMINI_API_KEY", "").strip())
        if os.environ.get("GEMINI_API_KEY"):
            st.caption("已設定伺服器環境變數 API Key。")
        st.caption(f"使用 {GEMINI_MODEL}。啟用後會將每章首頁最多 8,000 個字元傳送至 Google Gemini，可能產生 API 費用；掃描頁面不執行 OCR。")
        if use_gemini and not has_key:
            st.warning("尚未提供 API Key；仍可拆分，但不會產生摘要。")
        st.divider()
        st.caption(f"暫存保留期限：{service.store.ttl / 60:g} 分鐘。背景每 30 秒檢查一次。")
        st.caption("處理完成後只保留 ZIP；重新上傳、清除或下載串流完成後刪除實體檔案。")

    try:
        transfer_widget(session.token, service.port)
    except PDFError as exc:
        st.error(str(exc))
        st.stop()
    st.button("讀取上傳狀態／更新結果", type="secondary")
    snapshot = session.snapshot()
    state = snapshot["state"]
    if state == "empty":
        st.info("請先在上方選擇並上傳 PDF。上傳完成後，按「讀取上傳狀態／更新結果」。")
        st.stop()
    if state == "uploading":
        st.info(f"上傳中：{snapshot['received'] / 1024**2:.1f} / {snapshot['expected'] / 1024**2:.1f} MB。完成後請更新狀態。")
        st.stop()
    if state == "error":
        st.error(snapshot["error"] or "工作失敗，請重新上傳。")
        st.stop()
    if state == "processing":
        st.info("拆分作業進行中，完成後請更新結果。")
        st.stop()

    if snapshot["info"]:
        info = snapshot["info"]
        st.caption(f"檔案：{snapshot['filename']} ｜ {info.page_count:,} 頁 ｜ {len(info.bookmarks):,} 個有效書籤")
    if state in {"ready", "downloading", "downloaded"}:
        if state == "ready":
            st.success("拆分完成！原始 PDF 與章節中間檔已清除，請使用上方「下載章節 ZIP」。")
        elif state == "downloaded":
            st.success("ZIP 串流已送出，伺服器暫存檔已清除。需要再次拆分時請重新上傳。")
        else:
            st.info("ZIP 下載中，串流送出後將自動清除暫存檔。")
        st.dataframe(snapshot["rows"], use_container_width=True, hide_index=True)
        st.stop()

    info = snapshot["info"]
    if info is None:
        st.info("此工作已到期，請重新上傳。")
        st.stop()
    for warning in info.warnings:
        st.warning(warning)
    if not info.bookmarks:
        st.warning("這份 PDF 沒有有效的內建書籤，可改用固定頁數拆分。")
        mode = "固定頁數"
    else:
        mode = st.radio("拆分方式", ["依書籤", "固定頁數"], horizontal=True)
    try:
        if mode == "依書籤":
            levels = sorted({b.level for b in info.bookmarks})
            depth = st.selectbox("使用第 1 層至指定層級的所有書籤", levels,
                                 format_func=lambda value: f"第 {value} 層")
            st.caption("以選定書籤的起始頁作為切點；同頁書籤合併，前置頁面另存「前言與封面」。")
            chapters = bookmark_chapters(info, depth)
        else:
            count = st.number_input("每個檔案的頁數", min_value=1, max_value=info.page_count,
                                    value=min(10, info.page_count), step=1)
            chapters = fixed_chapters(info.page_count, int(count))
        st.subheader(f"章節預覽 · {len(chapters):,} 個檔案")
        st.dataframe([{"章節": c.title, "起始頁": c.start_page, "結束頁": c.end_page,
                       "頁數": c.end_page - c.start_page + 1} for c in chapters],
                     use_container_width=True, hide_index=True)
    except PDFError as exc:
        st.error(str(exc))
        st.stop()

    if st.button("開始拆分並打包 ZIP", type="primary"):
        bar = st.progress(0.0, text="準備拆分…")
        try:
            with GeminiSummarizer(api_key) as summarizer:
                service.store.process(
                    session, chapters,
                    summarize=summarizer.summarize if use_gemini and has_key else None,
                    progress=lambda done, total: bar.progress(done / total, text=f"已完成 {done} / {total} 個章節"),
                    expected_upload_id=snapshot["upload_id"],
                )
        except PDFError as exc:
            bar.empty()
            st.error(str(exc))
        except Exception:
            bar.empty()
            st.error("拆分遇到非預期錯誤，暫存檔已清理；請重新上傳後重試。")
        else:
            st.rerun()


if __name__ == "__main__":
    main()
