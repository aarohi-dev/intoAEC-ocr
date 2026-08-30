"""BOQ OCR contract: Ollama primary, existing olmOCR fallback."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OCRToken:
    text: str
    confidence: float
    bbox: List[float]
    page_num: int = 0
    is_table: bool = False
    is_diagram: bool = False
    raw_html: Optional[str] = None


@dataclass
class PageOCRResult:
    page_num: int
    tokens: List[OCRToken]
    is_table: bool = False
    is_diagram: bool = False
    raw_text: str = ""
    raw_html: Optional[str] = None
    primary_language: Optional[str] = None
    ocr_mode: str = "ollama"


def page_ocr_result_to_dict(res: PageOCRResult) -> dict:
    return {
        "page_num": res.page_num,
        "is_table": res.is_table,
        "is_diagram": res.is_diagram,
        "raw_text": res.raw_text,
        "raw_html": res.raw_html,
        "primary_language": res.primary_language,
        "ocr_mode": res.ocr_mode,
        "tokens": [vars(token) for token in res.tokens],
    }


def dict_to_page_ocr_result(data: dict) -> PageOCRResult:
    return PageOCRResult(
        page_num=data["page_num"],
        tokens=[OCRToken(**token) for token in data.get("tokens", [])],
        is_table=data.get("is_table", False),
        is_diagram=data.get("is_diagram", False),
        raw_text=data.get("raw_text", ""),
        raw_html=data.get("raw_html"),
        primary_language=data.get("primary_language"),
        ocr_mode=data.get("ocr_mode", "ollama"),
    )


def _markdown_to_tokens(text: str, page_num: int, is_table: bool) -> List[OCRToken]:
    return [OCRToken(line.strip(), 0.9, [0.0, 0.0, 0.0, 0.0], page_num, is_table) for line in text.splitlines() if line.strip()]


def _html_to_text_tokens(html: str, page_num: int, is_table: bool) -> List[OCRToken]:
    from html.parser import HTMLParser

    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tokens: List[OCRToken] = []

        def handle_data(self, data: str):
            if data.strip():
                self.tokens.append(OCRToken(data.strip(), 0.95, [0.0, 0.0, 0.0, 0.0], page_num, is_table))

    parser = TextParser()
    parser.feed(html)
    return parser.tokens


class OCRAdapter:
    """Coordinates PyMuPDF + Ollama primary extraction and olmOCR fallback."""

    def __init__(self, server_url: str = "http://localhost:8000/v1", model_name: str = "olmocr", fallback_mode: bool = False, target_longest_image_dim: int = 1288, enable_olmocr_fallback: bool = False):
        self.server_url = server_url
        self.model_name = model_name
        self.fallback_mode = fallback_mode
        self.target_longest_image_dim = target_longest_image_dim
        self.enable_olmocr_fallback = enable_olmocr_fallback

    def check_server(self) -> bool:
        """Check the configured olmOCR fallback endpoint."""
        import httpx

        try:
            response = httpx.get(f"{self.server_url.rstrip('/')}/models", timeout=5.0)
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def extract(self, pdf_path: str, page_num: int, pdf_hash: Optional[str] = None, task_manager: Optional[Any] = None) -> PageOCRResult:
        return asyncio.run(self._extract_one(pdf_path, page_num))

    async def _extract_one(self, pdf_path: str, page_num: int) -> PageOCRResult:
        from boqparser.extraction_service import ExtractionError, OllamaBOQExtractor, OlmOCRFallbackExtractor

        if self.fallback_mode:
            fallback = OlmOCRFallbackExtractor(self.server_url, self.model_name)
            try:
                return (await fallback.extract_all(pdf_path, page_num))[page_num - 1]
            finally:
                await fallback.close()

        try:
            extractor = OllamaBOQExtractor()
            try:
                return await extractor.extract_page(pdf_path, page_num)
            finally:
                await extractor.close()
        except Exception as primary_error:
            if not self.enable_olmocr_fallback:
                raise ExtractionError(f"Primary BOQ extraction failed for page {page_num}: {primary_error}") from primary_error
            logger.error("Primary BOQ extraction failed for page %s: %s; falling back to olmOCR", page_num, primary_error)
            fallback = OlmOCRFallbackExtractor(self.server_url, self.model_name)
            try:
                return (await fallback.extract_all(pdf_path, page_num))[page_num - 1]
            finally:
                await fallback.close()

    def extract_all(self, pdf_path: str, total_pages: int, pdf_hash: Optional[str] = None, task_manager: Optional[Any] = None) -> List[PageOCRResult]:
        return asyncio.run(self.extract_all_async(pdf_path, total_pages, pdf_hash, task_manager))

    async def extract_all_async(self, pdf_path: str, total_pages: int, pdf_hash: Optional[str] = None, task_manager: Optional[Any] = None) -> List[PageOCRResult]:
        from boqparser.extraction_service import OllamaBOQExtractor, OlmOCRFallbackExtractor

        if self.fallback_mode:
            logger.info("Primary BOQ extraction disabled; using olmOCR fallback")
            fallback = OlmOCRFallbackExtractor(self.server_url, self.model_name)
            try:
                return await fallback.extract_all(pdf_path, total_pages)
            finally:
                await fallback.close()

        primary = OllamaBOQExtractor()
        try:
            logger.info("Starting BOQ extraction using PyMuPDF + Ollama Llama 3.2")
            results = await primary.extract_all(pdf_path, total_pages)
            logger.info("BOQ JSON validation successful for all pages")
            return results
        except Exception as primary_error:
            from boqparser.extraction_service import ExtractionError

            if not self.enable_olmocr_fallback:
                raise ExtractionError(f"Primary BOQ extraction failed: {primary_error}") from primary_error
            logger.error("Primary BOQ extraction failed: %s; falling back to olmOCR", primary_error)
            fallback = OlmOCRFallbackExtractor(self.server_url, self.model_name)
            try:
                return await fallback.extract_all(pdf_path, total_pages)
            finally:
                await fallback.close()
        finally:
            await primary.close()
