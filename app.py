"""Run with: streamlit run app.py"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components

from gemini_helper import GEMINI_MODEL, GeminiSummarizer, SUMMARY_STYLES, REQUEST_TIMEOUT_SECONDS
from summary_markdown import summary_filename, summary_markdown
from pdf_processor import PDFError, MAX_SUMMARY_PAGES, bookmark_chapters, fixed_chapters, validate_summary_range
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
    st.set_page_config(page_title="PDF 拆分與摘要工具", page_icon="📚", layout="wide")
    st.title("📚 PDF 拆分與摘要工具")
    st.write("依書籤或固定頁數拆分 PDF，也能不拆分、直接摘要所選頁面。支援最大 1GB（1024MB）。")
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
        use_gemini = st.checkbox("啟用 Gemini 摘要並同意傳送文字", value=False)
        api_key = st.text_input("Gemini API Key", type="password", key="gemini_key",
                                help="留空時使用伺服器環境變數 GEMINI_API_KEY；不會寫入檔案。")
        has_key = bool(api_key.strip() or os.environ.get("GEMINI_API_KEY", "").strip())
        if os.environ.get("GEMINI_API_KEY"):
            st.caption("已設定伺服器環境變數 API Key。")
        st.caption(f"使用 {GEMINI_MODEL}。勾選即同意將文字傳送 Google。拆分模式每章首頁最多 8,000 字；只摘要模式每段最多 24,000 字、整次最多 480,000 字。1 段為 1 次請求，多段為段數加 1 次整合（最多 21 次），另有最多 2 次暫時性服務錯誤重試；每次按產生或接續最多 23 次嘗試，可能再次計費。每次請求最長等待 {REQUEST_TIMEOUT_SECONDS} 秒；HTTP 504／逾時不自動重送。不執行 OCR。")
        if use_gemini and not has_key:
            st.warning("尚未提供 API Key；仍可拆分，但不會產生摘要。")
        st.divider()
        st.caption(f"暫存保留期限：{service.store.ttl / 60:g} 分鐘。背景每 30 秒檢查一次。")
        st.caption("拆分完成後只保留 ZIP。只摘要會先刪除原始 PDF；預設失敗即清除段落原文，選擇保留供接續時最長 10 分鐘，可隨時清除。")

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
    if state == "summary_paused":
        st.warning(snapshot["error"])
        st.caption(f"已完成 {snapshot['resume_completed']} / {snapshot['resume_chunks']} 段；"
                   f"目前共 {snapshot['summary_requests']} 次請求嘗試，暫存剩餘 {snapshot['retry_retention_seconds']} 秒。完成段落不會重送。")
        if snapshot["resume_completed"]:
            try:
                partial = session.export_summary()
                st.download_button("下載已完成段落（部分 Markdown）", summary_markdown(partial, snapshot["filename"]).encode("utf-8"),
                                   file_name=summary_filename(snapshot["filename"])[:-3] + "_部分.md", mime="text/markdown")
            except RequestError:
                st.info("工作狀態已更新，請更新結果。")
        if snapshot["retry_after_seconds"]:
            st.info(f"Google 要求等待，至少 {snapshot['retry_after_seconds']} 秒後才能接續。")
        retry_consent = st.checkbox("同意接續未完成步驟及最多 2 次暫時性服務錯誤重試，可能再次計費")
        if st.button("接續重試", disabled=not retry_consent or not has_key or snapshot["retry_after_seconds"] > 0):
            try:
                service.store.start_mobile_process(session, {"upload_id":snapshot["upload_id"],
                    "gemini": True, "api_key": api_key, "retry_transient": True}, resume=True)
            except PDFError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        if st.button("取消摘要並清除暫存"):
            service.store.cancel_summary(session)
            st.rerun()
        st.caption("重新輸入金鑰或使用伺服器金鑰即可接續。逾時不代表 Google 未處理請求，重試可能再次計費。工作到期或伺服器重新部署後需重新上傳。")
        st.stop()
    if state == "processing":
        if snapshot["operation"] == "summary":
            progress = snapshot["progress"]
            st.info(f"正在取消，完成目前頁面擷取或 Gemini 請求後停止（單次請求最長 {REQUEST_TIMEOUT_SECONDS} 秒）。已送出請求仍可能計費。"
                    if snapshot["cancel_requested"] else f"Google 暫時無法回應，等待 {progress.get('wait_seconds')} 秒後重試；整次最多額外 2 次。"
                    if progress.get("stage") == "retry_wait" else f"正在產生分段摘要／整合：已完成 {progress.get('done', 0)} / {progress.get('total', '…')} 步驟，共 {snapshot['summary_requests']} 次請求嘗試。"
                    if progress.get("stage") in {"summarizing", "integrating", "requesting"} else
                    f"正在擷取文字：{progress.get('done', 0)} / {progress.get('total', '…')} 頁。請更新結果查看進度。")
            if st.button("取消摘要", disabled=snapshot["cancel_requested"]):
                try:
                    service.store.cancel_summary(session)
                except RequestError as exc:
                    st.info(str(exc))
                st.rerun()
        else:
            st.info("拆分作業進行中，完成後請更新結果。")
        st.stop()
    if state == "cancelled":
        st.info("摘要已取消，暫存檔已清除；請重新上傳以開始新工作。")
        st.stop()

    if snapshot["info"]:
        info = snapshot["info"]
        st.caption(f"檔案：{snapshot['filename']} ｜ {info.page_count:,} 頁 ｜ {len(info.bookmarks):,} 個有效書籤")
    if state in {"summarized", "summary_partial"}:
        result = snapshot["document_summary"]
        st.subheader("部分摘要（未完成）" if result.get("partial") else "文件摘要結果")
        if result.get("partial"):
            st.warning(snapshot["error"])
        st.caption(f"已檢查所選第 {result['start_page']}–{result['requested_end_page']} 頁；"
                   f"可擷取文字共 {result['text_chars']:,} 字元，分 {result['chunk_count']} 段，{result['api_requests']} 次生成請求。")
        if result["unreadable_pages"]:
            st.warning(f"有 {result['unreadable_pages']} 頁無法擷取文字。")
        if result["blank_pages"]:
            st.warning("無可擷取文字的頁面（未執行 OCR）：" + "、".join(map(str, result["blank_pages"])))
        st.text_area("摘要內容", result["text"], height=400)
        st.download_button("下載 Markdown 摘要（.md）", summary_markdown(result, snapshot["filename"]).encode("utf-8"),
                           file_name=summary_filename(snapshot["filename"]), mime="text/markdown")
        st.info("Markdown 包含整合摘要、分段重點與來源範圍。原始 PDF 暫存已清除，沒有建立章節 PDF 或 ZIP。工作到期前可重複下載；需要再次摘要時請重新上傳。")
        st.stop()
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
    operation = st.radio("處理方式", ["拆分 PDF 並打包 ZIP", "只產生摘要（不拆分）"], horizontal=True)
    if operation == "只產生摘要（不拆分）":
        retain_for_retry = st.checkbox("暫時故障時保留未完成文字供接續（選用，最長 10 分鐘）", value=False)
        st.caption("預設失敗即清除段落原文，已完成摘要仍可下載。勾選後只暫存最多 480,000 字元與已完成摘要，原始 PDF 先刪除；暫停後保留 10 分鐘，不因重試延長，到期後背景於 30 秒內清理。取消、清除或重新上傳也會刪除。")
        start = st.number_input("摘要起始頁", min_value=1, max_value=info.page_count, value=1, step=1)
        end = st.number_input("摘要結束頁", min_value=1, max_value=info.page_count,
                              value=min(info.page_count, MAX_SUMMARY_PAGES), step=1)
        style = st.selectbox("摘要詳細程度", list(SUMMARY_STYLES), index=2,
                             format_func=lambda value: f"{SUMMARY_STYLES[value][0]}（最多 {SUMMARY_STYLES[value][1]:,} 字）")
        st.caption("最多 50 頁／480,000 字元；每段最多 24,000 字，逐段摘要後整合，不略過後面的頁面。單段 1 次、多段為段數 + 1 次（最多 21 次 Gemini 請求）。超過上限會停止並提示縮小範圍，不先呼叫 API。只處理可擷取文字，不含 OCR。")
        if not use_gemini or not has_key:
            st.info("請在側欄提供 API Key 並勾選啟用 Gemini，才會傳送文字以產生摘要。")
        if st.button("產生文件摘要", type="primary", disabled=not use_gemini or not has_key):
            try:
                validate_summary_range(info.page_count, int(start), int(end))
                service.store.start_mobile_process(session, {
                    "operation": "summary", "upload_id": snapshot["upload_id"],
                    "start_page": int(start), "end_page": int(end), "summary_style": style,
                    "allow_multiple_requests": True, "retry_transient": True, "gemini": True, "api_key": api_key,
                    "retain_for_retry": retain_for_retry,
                })
            except PDFError as exc:
                st.error(str(exc))
            else:
                st.rerun()
        st.stop()
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
