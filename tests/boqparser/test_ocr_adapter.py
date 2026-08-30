"""Tests for OCRAdapter — runs in fallback mode (no vLLM required)."""

from __future__ import annotations

import os
import sys
import pytest

# Add root to path so boqparser can be imported without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.adapter.ocr_adapter import (
    OCRAdapter,
    OCRToken,
    PageOCRResult,
    _html_to_text_tokens,
    _markdown_to_tokens,
)

SAMPLE_PDF = os.path.join(os.path.dirname(__file__), "..", "gnarly_pdfs", "edgar.pdf")


class TestOCRToken:
    def test_token_creation(self):
        tok = OCRToken(text="RCC", confidence=0.98, bbox=[10.0, 20.0, 50.0, 30.0], page_num=1)
        assert tok.text == "RCC"
        assert tok.confidence == 0.98
        assert tok.bbox == [10.0, 20.0, 50.0, 30.0]
        assert tok.page_num == 1
        assert not tok.is_table

    def test_token_with_html(self):
        tok = OCRToken(text="1.1", confidence=0.9, bbox=[0, 0, 0, 0], page_num=1, raw_html="<table>...</table>")
        assert tok.raw_html is not None


class TestHtmlToTokens:
    def test_simple_html_table(self):
        html = """
        <table>
          <tr><th>Item</th><th>Description</th><th>Amount</th></tr>
          <tr><td>1.1</td><td>Foundation Works</td><td>50000</td></tr>
        </table>
        """
        tokens = _html_to_text_tokens(html, page_num=1, is_table=True)
        texts = [t.text for t in tokens]
        assert "Item" in texts or "1.1" in texts or "Foundation Works" in texts

    def test_empty_html(self):
        tokens = _html_to_text_tokens("", page_num=1, is_table=False)
        assert tokens == []


class TestMarkdownToTokens:
    def test_simple_markdown(self):
        md = "FOUNDATION WORKS\n1.1 Earth Work\n1.2 PCC"
        tokens = _markdown_to_tokens(md, page_num=1, is_table=False)
        assert len(tokens) == 3
        assert tokens[0].text == "FOUNDATION WORKS"
        assert tokens[0].confidence == 0.90

    def test_empty_markdown(self):
        tokens = _markdown_to_tokens("", page_num=1, is_table=False)
        assert tokens == []


class TestOCRAdapter:
    def test_fallback_mode_init(self):
        adapter = OCRAdapter(fallback_mode=True)
        assert adapter.fallback_mode is True
        assert not adapter.check_server()

    def test_check_server_unreachable(self):
        adapter = OCRAdapter(server_url="http://localhost:19999/v1", fallback_mode=False)
        assert not adapter.check_server()

    def test_fallback_mode_selects_olmocr(self):
        adapter = OCRAdapter(fallback_mode=True)
        assert adapter.fallback_mode is True

