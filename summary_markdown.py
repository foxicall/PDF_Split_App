"""Small UTF-8 exports generated from session results; no extra disk artifact."""

import re

from gemini_helper import GEMINI_MODEL, SUMMARY_STYLES
from pdf_processor import safe_filename


def summary_filename(source: str) -> str:
    stem = re.split(r"[/\\]", source)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    return safe_filename(1, stem)[3:-4] + "_摘要.md"


def summary_markdown(result: dict, filename: str) -> str:
    # The source name is untrusted metadata, not Markdown syntax.
    source = re.sub(r"[\r\n\t]", " ", filename)[:255]
    source = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", source)
    style, limit = SUMMARY_STYLES[result.get("summary_style", "detailed")]
    sections = ["# PDF 文件摘要" + ("（未完成／部分結果）" if result.get("partial") else ""), f"- 來源：{source}",
                f"- 選取／檢查範圍：第 {result['start_page']}–{result['requested_end_page']} 頁",
                f"- 可擷取文字：{result['text_chars']:,} 字元；{result['chunk_count']} 段",
                f"- 摘要長度：{style}（整合摘要上限 {limit:,} 字元）",
                f"- 模型：{GEMINI_MODEL}；請求嘗試（含重試）：{result['api_requests']} 次",
                "", "> 僅摘要所選範圍內可擷取的文字；圖片、圖表與掃描內容未執行 OCR，並非全文轉錄。"]
    if result.get("partial"):
        sections.extend(["", f"> 摘要暫停：僅完成 {len(result['parts'])} / {result['chunk_count']} 段，尚未產生完整整合摘要。下方只有已完成段落的摘要，請依各段頁碼判讀；其他內容未納入。"])
    if result.get("blank_pages"):
        sections.append("> 無可擷取文字的頁面：" + "、".join(map(str, result["blank_pages"])))
    if result.get("unreadable_page_numbers"):
        sections.append("> 部分內容缺漏：下列頁面擷取失敗，未納入摘要：" +
                        "、".join(map(str, result["unreadable_page_numbers"])))
    sections.extend(["", "## 整合摘要", "", result["text"]])
    if result.get("parts"):
        sections.extend(["", "## 分段重點", "", "以下保留整合前的各段摘要，方便查閱細節；同一頁可能跨段。"])
        for i, part in enumerate(result["parts"], 1):
            sections.extend(["", f"### 第 {i} 段 · 第 {part['start_page']}–{part['end_page']} 頁", "", part["text"]])
    return "\n".join(sections) + "\n"
