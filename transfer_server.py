"""Bounded HTTP upload/download transport beside Streamlit (no in-memory media store)."""

from __future__ import annotations

import atexit
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from filelock import FileLock, Timeout
from gemini_helper import GeminiSummaryError, SUMMARY_STYLES
from summary_markdown import summary_filename, summary_markdown

from pdf_processor import (
    CHUNK_BYTES, MAX_UPLOAD_BYTES, MAX_SUMMARY_PAGES, PDFError, PDFCancelled, PDFInfo, TemporaryWorkspace,
    bookmark_chapters, fixed_chapters, ensure_space, inspect_pdf, split_pdf,
    summarize_pdf, validate_summary_range,
)

LOGGER = logging.getLogger(__name__)


class RequestError(PDFError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    token: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    state: str = "empty"
    workspace: TemporaryWorkspace | None = None
    upload_id: str = ""
    filename: str = ""
    expected: int = 0
    received: int = 0
    info: PDFInfo | None = None
    rows: list[dict] = field(default_factory=list)
    operation: str = "split"
    document_summary: dict | None = None
    progress: dict = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    error: str = ""
    touched: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched = time.monotonic()
        if self.workspace:
            self.workspace.touch()

    def cleanup(self) -> None:
        if self.workspace:
            self.workspace.cleanup()
            self.workspace = None

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "filename": self.filename,
                    "received": self.received, "expected": self.expected,
                    "error": self.error, "info": self.info, "rows": list(self.rows),
                    "upload_id": self.upload_id, "operation": self.operation,
                    "document_summary": dict(self.document_summary) if self.document_summary else None,
                    "progress": dict(self.progress), "cancel_requested": self.cancel_event.is_set()}


class SessionStore:
    def __init__(self, root: Path, ttl_seconds: float = 1800, max_sessions: int = 32):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        if ttl_seconds <= 0 or max_sessions < 1:
            raise ValueError("暫存期限與工作數上限必須大於零。")
        # One owner per spool root; prevents another worker's janitor deleting live files.
        self._owner = FileLock(str(self.root / ".owner.lock"), thread_local=False)
        try:
            self._owner.acquire(timeout=0)
        except Timeout as exc:
            raise PDFError("暫存目錄已由另一個服務使用；請使用單一程序或指定不同 PDF_TEMP_ROOT。") from exc
        self._lock = threading.RLock()
        self._processing = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self.sweep()

    def create(self) -> Session:
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                raise PDFError("目前工作數已達上限，請稍後再試。")
            session = Session(secrets.token_urlsafe(32))
            self.sessions[session.token] = session
            return session

    def get(self, token: str) -> Session:
        with self._lock:
            session = self.sessions.get(token)
        if session is None:
            raise RequestError("工作已到期，請回到主介面重新整理並上傳。", 404)
        return session

    @staticmethod
    def _editable(session: Session) -> None:
        if session.state in {"processing", "downloading"}:
            raise RequestError("目前正在處理或下載，請待作業結束再上傳或清除。", 409)
        if session.state == "expired":
            raise RequestError("工作已到期，請重新整理主介面。", 410)

    def begin(self, session: Session, filename: str, size: int) -> str:
        if type(size) is not int or not 0 < size <= MAX_UPLOAD_BYTES:
            raise RequestError("檔案必須大於 0 且不超過 1GB（1024MB）。", 413)
        if not isinstance(filename, str) or not filename.lower().endswith(".pdf"):
            raise RequestError("請選擇副檔名為 .pdf 的檔案。")
        with session.lock:
            self._editable(session)
            session.cleanup()
            session.state = "empty"
            session.info = None
            session.rows = []
            session.document_summary = None
            session.operation = "split"
            session.progress = {}
            session.cancel_event.clear()
            session.error = ""
            session.workspace = TemporaryWorkspace(self.root)
            session.upload_id = secrets.token_urlsafe(16)
            session.filename = filename[:255]
            session.expected, session.received = size, 0
            session.state = "uploading"
            session.touch()
            return session.upload_id

    @staticmethod
    def _check_upload(session: Session, upload_id: str) -> None:
        if session.state != "uploading" or upload_id != session.upload_id:
            raise RequestError("上傳工作已失效，請重新上傳。", 409)

    def append(self, session: Session, upload_id: str, offset: int, data: bytes) -> None:
        with session.lock:
            self._check_upload(session, upload_id)
            if offset != session.received:
                raise RequestError("分塊順序不符，請重新上傳。", 409)
            if not data or len(data) > CHUNK_BYTES or session.received + len(data) > session.expected:
                raise RequestError("上傳資料超過宣告大小或分塊限制。", 413)
            try:
                ensure_space(session.workspace.path, len(data))
                with (session.workspace.path / "source.pdf").open("ab") as stream:
                    stream.write(data)
                session.received += len(data)
                session.touch()
            except (OSError, PDFError):
                session.cleanup()
                session.state = "error"
                session.error = "上傳寫入失敗，請確認暫存磁碟空間後重試。"
                raise RequestError(session.error, 507) from None

    def finish(self, session: Session, upload_id: str) -> None:
        with session.lock:
            self._check_upload(session, upload_id)
            if session.received != session.expected:
                raise RequestError("上傳尚未完成，請重新上傳。", 409)
            try:
                session.info = inspect_pdf(session.workspace.path / "source.pdf")
                session.state = "uploaded"
                session.touch()
            except Exception:
                session.cleanup()
                session.state = "error"
                session.error = "PDF 無法讀取、已加密或已損毀，請確認檔案後重新上傳。"
                raise RequestError(session.error) from None

    def clear(self, session: Session) -> None:
        with session.lock:
            self._editable(session)
            session.cleanup()
            session.info = None
            session.rows = []
            session.document_summary = None
            session.progress = {}
            session.cancel_event.clear()
            session.expected = session.received = 0
            session.state = "empty"
            session.error = ""
            session.touch()

    def cancel_summary(self, session: Session) -> None:
        with session.lock:
            if session.state != "processing" or session.operation != "summary":
                raise RequestError("目前沒有可取消的文件摘要作業。", 409)
            session.cancel_event.set()
            session.touched = time.monotonic()  # The worker may already have removed its files.

    def process_summary(self, session: Session, start_page: int, end_page: int, summarize,
                        expected_upload_id: str, _reserved: bool = False, combine=None,
                        summary_style: str = "detailed", allow_multiple_requests: bool = False) -> None:
        if not _reserved and not self._processing.acquire(blocking=False):
            raise RequestError("其他文件正在處理，請稍後再試。", 409)
        started = False
        try:
            with session.lock:
                if session.state != ("processing" if _reserved else "uploaded") or expected_upload_id != session.upload_id:
                    raise RequestError("PDF 已變更或未完成上傳，請重新確認。", 409)
                validate_summary_range(session.info.page_count, start_page, end_page)
                session.state = "processing"
                session.operation = "summary"
                if not _reserved:
                    session.cancel_event.clear()
                session.touch()
                workspace = session.workspace
                started = True

            def progress(done, total, stage):
                with session.lock:
                    session.progress = {"done": done, "total": total, "stage": stage}
                    session.touch()

            result = summarize_pdf(workspace.path / "source.pdf", workspace, start_page, end_page,
                                   summarize, progress=progress, cancelled=session.cancel_event.is_set,
                                   combine=combine, summary_style=summary_style,
                                   allow_multiple_requests=allow_multiple_requests)
            with session.lock:
                if session.cancel_event.is_set():
                    raise PDFCancelled("摘要已取消。")
                session.cleanup()
                session.document_summary = result
                session.rows = []
                session.state = "summarized"
                session.touch()
        except PDFCancelled:
            with session.lock:
                session.cleanup()
                session.document_summary = None
                session.progress = {}
                session.state = "cancelled"
                session.touch()
        except (PDFError, GeminiSummaryError) as exc:
            if started:
                with session.lock:
                    session.cleanup()
                    session.document_summary = None
                    session.state = "error"
                    session.error = str(exc) + " 暫存已清除；請重新上傳後重試。"
            raise
        except BaseException:
            if started:
                with session.lock:
                    session.cleanup()
                    session.document_summary = None
                    session.state = "error"
                    session.error = "摘要未完成；暫存檔已清理，請重新上傳後重試。"
            raise
        finally:
            self._processing.release()

    def process(self, session: Session, chapters: list, summarize=None, progress=None,
                expected_upload_id: str | None = None, _reserved: bool = False) -> None:
        if not _reserved and not self._processing.acquire(blocking=False):
            raise RequestError("其他文件正在處理，請稍後再試。", 409)
        started = False
        try:
            with session.lock:
                if session.state != ("processing" if _reserved else "uploaded"):
                    raise RequestError("請先完成 PDF 上傳。", 409)
                if expected_upload_id is not None and expected_upload_id != session.upload_id:
                    raise RequestError("PDF 已變更，請更新上傳狀態並重新確認章節。", 409)
                session.state = "processing"
                session.operation = "split"
                session.touch()
                workspace = session.workspace
                started = True
            result = split_pdf(workspace.path / "source.pdf", workspace, chapters,
                               summarize=summarize, progress=progress)
            with session.lock:
                session.rows = result.rows
                session.state = "ready"
                session.touch()
        except BaseException:
            if started:
                with session.lock:
                    session.cleanup()
                    session.state = "error"
                    session.error = "拆分已中止；暫存檔已清理，請重新上傳後重試。"
            raise
        finally:
            self._processing.release()

    def start_mobile_process(self, session: Session, options: dict) -> None:
        """Reserve the single processing slot before acknowledging a background task."""
        from gemini_helper import GeminiSummarizer

        with session.lock:
            if session.state != "uploaded" or options.get("upload_id") != session.upload_id:
                raise RequestError("上傳內容已變更，請更新狀態後重試。", 409)
            info = session.info
            operation = options.get("operation", "split")
            mode = options.get("mode", "bookmarks")
            if operation == "summary":
                style = options.get("summary_style", "detailed")
                if not isinstance(style, str) or style not in SUMMARY_STYLES:
                    raise RequestError("請選擇精簡、標準或詳細摘要。")
                start = options.get("start_page", 1)
                end = options.get("end_page", min(info.page_count, MAX_SUMMARY_PAGES))
                try:
                    validate_summary_range(info.page_count, start, end)
                except PDFError as exc:
                    raise RequestError(str(exc)) from None
                chapters = []
            elif operation != "split":
                raise RequestError("不支援的處理方式。")
            elif mode == "bookmarks":
                chapters = bookmark_chapters(info, int(options.get("depth", 1)))
            elif mode == "fixed":
                chapters = fixed_chapters(info.page_count, int(options.get("pages", 10)))
            else:
                raise RequestError("不支援的拆分方式。")
            enabled = options.get("gemini") is True
            if operation == "summary" and not enabled:
                raise RequestError("請先勾選啟用 Gemini，才會將所選頁面文字送出以產生摘要。")
            key = options.get("api_key", "")
            if not isinstance(key, str) or len(key) > 512:
                raise RequestError("API Key 格式錯誤。")
            if enabled and not (key.strip() or os.environ.get("GEMINI_API_KEY", "").strip()):
                raise RequestError("請輸入 Gemini API Key 或關閉摘要。")
            if not self._processing.acquire(blocking=False):
                raise RequestError("另一份文件正在處理，請稍後重試。", 409)
            upload_id = session.upload_id
            session.state = "processing"
            session.operation = operation
            session.document_summary = None
            session.cancel_event.clear()
            session.progress = {}
            session.touch()
            def run():
                try:
                    with GeminiSummarizer(key) as helper:
                        if operation == "summary":
                            self.process_summary(session, start, end, helper.summarize_chunk,
                                                 expected_upload_id=upload_id, _reserved=True,
                                                 combine=lambda text: helper.summarize_document(text, style),
                                                 summary_style=style,
                                                 allow_multiple_requests=options.get("allow_multiple_requests") is True)
                        else:
                            self.process(session, chapters, summarize=helper.summarize if enabled else None,
                                         expected_upload_id=upload_id, _reserved=True)
                except Exception:
                    # process() owns failure cleanup; no request bodies or secrets in logs.
                    pass
            worker = threading.Thread(target=run, daemon=True)
            try:
                worker.start()
            except BaseException:
                session.state = "uploaded"
                self._processing.release()
                raise

    def sweep(self) -> None:
        with self._lock:
            for token, session in list(self.sessions.items()):
                if not session.lock.acquire(blocking=False):
                    continue
                try:
                    if session.state not in {"processing", "downloading"} and time.monotonic() - session.touched > self.ttl:
                        session.cleanup()
                        session.state = "expired"
                        del self.sessions[token]
                except OSError:
                    LOGGER.warning("暫存清理失敗，將於下一輪重試。")
                finally:
                    session.lock.release()
            active = {workspace.path for s in self.sessions.values() if (workspace := s.workspace) is not None}
            # Reap leftovers from abrupt process termination. The root owner lock is held.
            for path in self.root.glob("job-*"):
                if path in active or path.is_symlink() or not path.is_dir():
                    continue
                try:
                    lease = path / ".lease"
                    modified = lease.stat().st_mtime if lease.exists() else path.stat().st_mtime
                    if time.time() - modified > self.ttl:
                        shutil.rmtree(path)
                except OSError:
                    LOGGER.warning("過期暫存目錄清理失敗，將於下一輪重試。")

    def close(self) -> None:
        try:
            with self._lock:
                for session in self.sessions.values():
                    with session.lock:
                        session.cancel_event.set()
                        session.cleanup()
                self.sessions.clear()
        finally:
            self._owner.release()


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class TransferService:
    def __init__(self, root: Path, host: str = "127.0.0.1", port: int = 8765,
                 ttl_seconds: float = 1800):
        self.store = SessionStore(root, ttl_seconds)
        self._stop = threading.Event()
        self._closed = False
        try:
            self.server = _BoundedServer((host, port), self._handler())
        except BaseException:
            self.store.close()
            raise
        self.port = self.server.server_port
        self._http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._janitor_thread = threading.Thread(target=self._janitor, daemon=True)
        self._http_thread.start()
        self._janitor_thread.start()
        atexit.register(self.close)

    def _janitor(self) -> None:
        while not self._stop.wait(30):
            self.store.sweep()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        atexit.unregister(self.close)

    def _handler(self):
        store = self.store
        widget = (Path(__file__).parent / "upload_widget.html").read_bytes()
        mobile_root = Path(__file__).parent / "mobile"
        public_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"),
            "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
            "/icon-192.png": ("icon-192.png", "image/png"),
            "/icon-512.png": ("icon-512.png", "image/png"),
        }

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 closes each response, including unread rejected request bodies.
            def setup(self):
                super().setup()
                self.connection.settimeout(30)

            def log_message(self, *_):
                pass  # URLs carry capability tokens; never write them to logs.

            def _headers(self, status: int, mime: str, size: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")

            def _json(self, data: dict, status: int = 200) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                url = urlsplit(self.path)
                parts = url.path.strip("/").split("/")
                if len(parts) not in (1, 2):
                    raise RequestError("找不到此資源。", 404)
                return store.get(parts[0]), parts[1] if len(parts) == 2 else "", parse_qs(url.query)

            def _body(self, limit: int) -> bytes:
                if self.headers.get("Transfer-Encoding"):
                    raise RequestError("請使用含 Content-Length 的分塊請求。", 411)
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    raise RequestError("無效的 Content-Length。", 400) from None
                if not 0 <= length <= limit:
                    raise RequestError("請求內容超過大小限制。", 413)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise RequestError("上傳連線已中斷，請重新上傳。")
                return data

            def do_POST(self):
                try:
                    if urlsplit(self.path).path == "/sessions":
                        # Disallow cross-site form submissions; no CORS is granted.
                        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                            raise RequestError("需要 JSON 請求。", 415)
                        self._body(1024)
                        session = store.create()
                        self._json({"token": session.token, "retention": store.ttl}, 201)
                        return
                    session, action, query = self._route()
                    if action == "start":
                        data = json.loads(self._body(4096))
                        if not isinstance(data, dict):
                            raise RequestError("上傳資訊格式錯誤。")
                        upload_id = store.begin(session, data.get("name", ""), data.get("size", 0))
                        self._json({"id": upload_id, "chunkSize": CHUNK_BYTES})
                    elif action == "chunk":
                        store.append(session, query.get("id", [""])[0],
                                     int(query.get("offset", ["-1"])[0]), self._body(CHUNK_BYTES))
                        self._json({"received": session.received})
                    elif action == "finish":
                        data = json.loads(self._body(4096))
                        if not isinstance(data, dict):
                            raise RequestError("上傳資訊格式錯誤。")
                        store.finish(session, data.get("id", ""))
                        self._json({"ok": True})
                    elif action == "clear":
                        store.clear(session)
                        self._json({"ok": True})
                    elif action == "cancel":
                        self._body(1024)
                        store.cancel_summary(session)
                        self._json({"ok": True}, 202)
                    elif action == "process":
                        data = json.loads(self._body(4096))
                        if not isinstance(data, dict):
                            raise RequestError("拆分設定格式錯誤。")
                        store.start_mobile_process(session, data)
                        self._json({"ok": True}, 202)
                    else:
                        raise RequestError("找不到此操作。", 404)
                except RequestError as exc:
                    self._json({"error": str(exc)}, exc.status)
                except (ValueError, TypeError):
                    self._json({"error": "請求格式錯誤。"}, 400)
                except PDFError as exc:
                    self._json({"error": str(exc)}, 507)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass  # Incomplete upload is reclaimed by TTL or the next upload.
                except Exception:
                    self._json({"error": "傳輸失敗，請重新上傳。"}, 500)

            def do_GET(self):
                try:
                    route = urlsplit(self.path).path
                    if route == "/health":
                        self._json({"ok": True})
                        return
                    if route in public_files:
                        name, mime = public_files[route]
                        content = (mobile_root / name).read_bytes()
                        self._headers(200, mime, len(content))
                        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                        self.end_headers()
                        self.wfile.write(content)
                        return
                    session, action, _ = self._route()
                    if action == "":
                        if not urlsplit(self.path).path.endswith("/"):
                            raise RequestError("請使用完整上傳網址。", 404)
                        self._headers(200, "text/html; charset=utf-8", len(widget))
                        self.send_header("Content-Security-Policy",
                                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'")
                        self.end_headers()
                        self.wfile.write(widget)
                    elif action == "status":
                        snapshot = session.snapshot()
                        self._json({k: snapshot[k] for k in ("state", "received", "expected", "error")})
                    elif action == "details":
                        snapshot = session.snapshot()
                        info = snapshot.pop("info")
                        snapshot["page_count"] = info.page_count if info else 0
                        snapshot["levels"] = sorted({b.level for b in info.bookmarks}) if info else []
                        snapshot["warnings"] = list(info.warnings) if info else []
                        snapshot["retention"] = store.ttl
                        self._json(snapshot)
                    elif action == "summary.md":
                        with session.lock:
                            if session.state != "summarized" or session.document_summary is None:
                                raise RequestError("摘要尚未完成、已清除或已到期。", 409)
                            body = summary_markdown(session.document_summary, session.filename).encode("utf-8")
                            filename = summary_filename(session.filename)
                        self._headers(200, "text/markdown; charset=utf-8", len(body))
                        self.send_header("Content-Disposition", "attachment; filename=summary.md; filename*=UTF-8''" + quote(filename))
                        self.end_headers()
                        self.wfile.write(body)
                    elif action == "download":
                        self._download(session)
                    else:
                        raise RequestError("找不到此資源。", 404)
                except RequestError as exc:
                    self._json({"error": str(exc)}, exc.status)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

            def _download(self, session: Session) -> None:
                if self.headers.get("Range"):
                    raise RequestError("此下載為一次性串流，不支援分段續傳；請重新下載完整 ZIP。", 416)
                with session.lock:
                    if session.state != "ready" or session.workspace is None:
                        raise RequestError("ZIP 尚未完成、已下載或已到期。", 409)
                    archive = session.workspace.path / "chapters.zip"
                    session.state = "downloading"
                complete = False
                try:
                    with archive.open("rb") as stream:
                        self._headers(200, "application/zip", os.fstat(stream.fileno()).st_size)
                        self.send_header("Content-Disposition", 'attachment; filename="chapters.zip"')
                        self.send_header("Accept-Ranges", "none")
                        self.end_headers()
                        while block := stream.read(CHUNK_BYTES):
                            self.wfile.write(block)
                        self.wfile.flush()
                    complete = True
                except OSError:
                    # A disconnected client can retry until the original retention period expires.
                    pass
                finally:
                    with session.lock:
                        if complete:
                            try:
                                session.cleanup()
                                session.state = "downloaded"
                            except OSError:
                                session.state = "error"
                                session.error = "下載已送出，暫存清理將由背景清理器重試。"
                        else:
                            session.state = "ready"

        return Handler


def default_service() -> TransferService:
    root = Path(os.environ.get("PDF_TEMP_ROOT", str(Path(tempfile.gettempdir()) / "pdf-bookmark-splitter")))
    return TransferService(root, host=os.environ.get("PDF_TRANSFER_HOST", "127.0.0.1"),
                           port=int(os.environ.get("PDF_TRANSFER_PORT", "8765")),
                           ttl_seconds=int(os.environ.get("PDF_RETENTION_SECONDS", "1800")))
