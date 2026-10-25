"""Exercise summary-only and cancellation in mobile Chromium with mocked Gemini."""
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz
from playwright.sync_api import expect, sync_playwright

from transfer_server import TransferService


def main():
    with tempfile.TemporaryDirectory(prefix='pdf-summary-browser-') as temp:
        root = Path(temp)
        source = root / 'no-bookmarks.pdf'
        with fitz.open() as doc:
            for n in range(3):
                doc.new_page().insert_text((72, 72), f'Generated page {n + 1}')
            doc.save(source)
        google = Mock()
        google.models.generate_content.return_value = SimpleNamespace(text='所選頁面摘要，無須拆分。')
        release = threading.Event()
        service = TransferService(root / 'spool', port=0)
        try:
            with patch('google.genai.Client', return_value=google), patch('pdf_processor.MAX_SUMMARY_CHARS', 20), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(viewport={'width':390, 'height':844}, is_mobile=True,
                                              has_touch=True, permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{service.port}')

                def upload():
                    page.locator('#file').set_input_files(source)
                    page.locator('#upload').click()
                    expect(page.locator('#settings')).to_be_visible(timeout=15000)
                    page.locator('#operation').select_option('summary')
                    expect(page.locator('#split-options')).to_be_hidden()
                    expect(page.locator('#summary-options')).to_be_visible()
                    page.locator('#api-key').fill('mock-key-not-sent-to-google')

                upload()
                assert not page.locator('#gemini').is_checked()
                page.locator('#split').click()
                expect(page.locator('#status')).to_contain_text('請先勾選', timeout=5000)
                google.models.generate_content.assert_not_called()
                page.locator('#gemini').check()
                page.locator('#summary-start').fill('2')
                page.locator('#split').click()
                expect(page.locator('#document-result')).to_be_visible(timeout=15000)
                expect(page.locator('#document-summary')).to_have_value('所選頁面摘要，無須拆分。')
                expect(page.locator('#summary-coverage')).to_contain_text('第 2–3 頁')
                expect(page.locator('#result')).to_be_hidden()
                expect(page.locator('#download')).to_be_hidden()
                page.locator('#copy-summary').click()
                expect(page.locator('#copy-status')).to_have_text('摘要已複製。')
                assert page.evaluate('navigator.clipboard.readText()') == '所選頁面摘要，無須拆分。'
                assert not list((root / 'spool').glob('job-*'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert google.models.generate_content.call_count == 3
                assert 'Generated page 1' not in ''.join(call.kwargs['contents'] for call in google.models.generate_content.call_args_list)
                for _ in range(2):
                    with page.expect_download() as event:
                        page.locator('#download-summary').click()
                    download = event.value
                    assert download.suggested_filename.endswith('_摘要.md')
                    markdown = Path(download.path()).read_text(encoding='utf-8')
                    assert '## 分段重點' in markdown and '第 2–3 頁' in markdown
                    assert '所選頁面摘要，無須拆分。' in markdown
                    assert 'mock-key' not in markdown
                expect(page.locator('#document-result')).to_be_visible()

                def pending(**_):
                    assert release.wait(15)
                    return SimpleNamespace(text='This cancelled response must never be shown')
                google.models.generate_content.side_effect = pending
                upload()
                page.locator('#split').click()
                expect(page.locator('#cancel-summary')).to_be_visible(timeout=10000)
                page.locator('#cancel-summary').click()
                expect(page.locator('#status')).to_contain_text('正在取消', timeout=5000)
                release.set()
                expect(page.locator('#status')).to_contain_text('摘要已取消', timeout=10000)
                expect(page.locator('#document-result')).to_be_hidden()
                assert not list((root / 'spool').glob('job-*'))
                assert not errors, errors
                browser.close()
                print('Summary-only mobile Chromium: consent, page range, copy, no ZIP, cancellation and cleanup passed; Gemini mocked.')
        finally:
            release.set()
            service.close()


if __name__ == '__main__':
    main()
