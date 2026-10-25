"""Exercise transient failures, optional retention, partial MD and resume without Google."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz
from google.genai import errors
from playwright.sync_api import expect, sync_playwright
from transfer_server import TransferService


def main():
    with tempfile.TemporaryDirectory(prefix='pdf-retry-browser-') as temp:
        root = Path(temp)
        source = root / 'retry.pdf'
        with fitz.open() as doc:
            for i in range(3):
                doc.new_page().insert_text((72, 72), f'Generated page {i + 1}')
            doc.save(source)
        google = Mock()
        failure = errors.APIError(503, {'error':{'message':'private-key private-document'}})
        service = TransferService(root / 'spool', port=0)
        try:
            with patch('google.genai.Client', return_value=google), patch('pdf_processor.MAX_SUMMARY_CHARS', 20), \
                    patch('gemini_helper.RETRY_DELAYS', (0, 0)), patch('gemini_helper.random.uniform', return_value=0), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
                js_errors = []
                page.on('pageerror', lambda error: js_errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{service.port}/')
                def upload(retain):
                    page.locator('#file').set_input_files(source)
                    page.locator('#upload').click()
                    expect(page.locator('#settings')).to_be_visible(timeout=15000)
                    page.locator('#operation').select_option('summary')
                    page.locator('#gemini').check()
                    page.locator('#api-key').fill('mock-key')
                    page.locator('#retain-for-retry').set_checked(retain)
                    google.models.generate_content.reset_mock()
                    google.models.generate_content.side_effect = [SimpleNamespace(text='第一段筆記'), failure, failure, failure]
                    page.locator('#split').click()
                assert not page.locator('#retain-for-retry').is_checked()
                upload(True)
                expect(page.locator('#retry-panel')).to_be_visible(timeout=15000)
                expect(page.locator('#retry-info')).to_contain_text('已完成 1 / 3 段')
                assert google.models.generate_content.call_count == 4
                assert not list((root / 'spool').glob('job-*/source.pdf'))
                with page.expect_download() as event:
                    page.locator('#download-summary').click()
                assert '_部分.md' in event.value.suggested_filename
                assert '未完成／部分結果' in Path(event.value.path()).read_text(encoding='utf-8')
                page.locator('#retry-summary').click()
                expect(page.locator('#status')).to_contain_text('請先勾選同意接續重試')
                assert google.models.generate_content.call_count == 4
                google.models.generate_content.side_effect = None
                google.models.generate_content.return_value = SimpleNamespace(text='接續完成的摘要')
                page.locator('#retry-consent').check()
                page.locator('#retry-key').fill('mock-key')
                page.locator('#retry-summary').click()
                expect(page.locator('#document-result')).to_be_visible(timeout=15000)
                expect(page.locator('#document-summary')).to_have_value('接續完成的摘要')
                expect(page.locator('#retry-panel')).to_be_hidden()
                assert google.models.generate_content.call_count == 7
                assert not list((root / 'spool').glob('job-*'))
                upload(False)
                expect(page.locator('#document-result')).to_be_visible(timeout=15000)
                expect(page.locator('#document-result h2')).to_have_text('部分摘要（未完成）')
                expect(page.locator('#retry-panel')).to_be_hidden()
                assert not list((root / 'spool').glob('job-*'))
                with page.expect_download() as event:
                    page.locator('#download-summary').click()
                assert '第一段筆記' in Path(event.value.path()).read_text(encoding='utf-8')
                assert not js_errors, js_errors
                browser.close()
                print('Mobile Chromium retry: bounded 503 retry, retention opt-in, partial Markdown, resume, opt-out cleanup passed; Gemini mocked.')
        finally:
            service.close()


if __name__ == '__main__':
    main()
