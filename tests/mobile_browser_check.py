"""Chromium mobile viewport + installability metadata + offline-shell smoke test."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import fitz
import requests
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory(prefix="pdf-mobile-browser-") as directory:
        root = Path(directory)
        source = root / "phone-test.pdf"
        with fitz.open() as doc:
            for number in range(1, 6):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {number}")
            doc.set_toc([[1, "First chapter", 1], [1, "Second chapter", 4]])
            doc.save(source)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "PORT": str(port), "PDF_TRANSFER_HOST": "127.0.0.1",
               "PDF_TEMP_ROOT": str(root / "spool"), "GEMINI_API_KEY": ""}
        server = subprocess.Popen([sys.executable, "mobile_server.py"], env=env, stdout=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}"
            with requests.Session() as client:
                client.trust_env = False
                for _ in range(100):
                    try:
                        if client.get(url + "/health", timeout=1).ok:
                            break
                    except requests.RequestException:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Mobile server did not start")
            with sync_playwright() as browser_tool:
                browser = browser_tool.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=True, has_touch=True, accept_downloads=True)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                expect(page.get_by_role("heading", level=1)).to_be_visible()
                page.evaluate("navigator.serviceWorker.ready")
                cdp = context.new_cdp_session(page)
                cdp.send("Page.enable")
                manifest = cdp.send("Page.getAppManifest")
                assert not manifest["errors"], manifest["errors"]
                assert not cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
                registration = page.evaluate("async () => {const r=await navigator.serviceWorker.ready; return new URL(r.scope).pathname;}")
                assert registration == "/"
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator("#file").set_input_files(source)
                page.locator("#upload").click()
                expect(page.locator("#settings")).to_be_visible(timeout=20000)
                assert not page.locator("#gemini").is_checked()
                page.locator("#split").click()
                expect(page.locator("#download")).to_be_visible(timeout=20000)
                with page.expect_download() as event:
                    page.locator("#download").click()
                download = event.value
                target = root / "download.zip"
                download.save_as(target)
                assert download.failure() is None
                with zipfile.ZipFile(target) as archive:
                    assert archive.testzip() is None and len(archive.namelist()) == 2
                expect(page.locator("#status")).to_contain_text("伺服器檔案已清除", timeout=10000)
                assert not list((root / "spool").glob("job-*"))
                cached = page.evaluate("async () => {const keys=await caches.keys(); return (await Promise.all(keys.map(async key => (await (await caches.open(key)).keys()).map(r => new URL(r.url).pathname)))).flat();}")
                assert sorted(cached) == sorted(["/", "/app.js", "/app.css", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"])
                context.set_offline(True)
                page.reload()
                expect(page.get_by_role("heading", level=1)).to_be_visible()
                expect(page.locator("#status")).to_contain_text("目前離線", timeout=10000)
                assert not errors, errors
                browser.close()
                print("Mobile Chromium: installability, touch layout, upload, split, ZIP, cleanup, and offline shell passed.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
