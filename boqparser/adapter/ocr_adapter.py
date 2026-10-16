"""
Extraction Adapter — normalizes native PDF text and tokens into a standard token list.

Primary mode:
  Native vector text and word bounding box extraction using PyMuPDF (fitz).
  Fast, lightweight, works locally with minimal memory footprint without GPU/vLLM.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OCRToken:
    """A single recognized text token with position and metadata."""
    text: str
    confidence: float = 1.0
    bbox: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])  # [x0, y0, x1, y1]
    page_num: int = 0
    is_table: bool = False
    is_diagram: bool = False
    raw_html: Optional[str] = None


@dataclass
class PageOCRResult:
    """All tokens extracted from a single page."""
    page_num: int
    tokens: List[OCRToken]
    is_table: bool = False
    is_diagram: bool = False
    raw_text: str = ""
    raw_html: Optional[str] = None
    primary_language: Optional[str] = "en"
    ocr_mode: str = "native"


def page_ocr_result_to_dict(res: PageOCRResult) -> dict:
    return {
        "page_num": res.page_num,
        "is_table": res.is_table,
        "is_diagram": res.is_diagram,
        "raw_text": res.raw_text,
        "raw_html": res.raw_html,
        "primary_language": res.primary_language,
        "ocr_mode": res.ocr_mode,
        "tokens": [
            {
                "text": t.text,
                "confidence": t.confidence,
                "bbox": t.bbox,
                "page_num": t.page_num,
                "is_table": t.is_table,
                "is_diagram": t.is_diagram,
                "raw_html": t.raw_html,
            }
            for t in res.tokens
        ],
    }


def dict_to_page_ocr_result(d: dict) -> PageOCRResult:
    return PageOCRResult(
        page_num=d["page_num"],
        is_table=d.get("is_table", False),
        is_diagram=d.get("is_diagram", False),
        raw_text=d.get("raw_text", ""),
        raw_html=d.get("raw_html"),
        primary_language=d.get("primary_language", "en"),
        ocr_mode=d.get("ocr_mode", "native"),
        tokens=[
            OCRToken(
                text=t["text"],
                confidence=t.get("confidence", 1.0),
                bbox=t.get("bbox", [0.0, 0.0, 0.0, 0.0]),
                page_num=t.get("page_num", d["page_num"]),
                is_table=t.get("is_table", False),
                is_diagram=t.get("is_diagram", False),
                raw_html=t.get("raw_html"),
            )
            for t in d.get("tokens", [])
        ],
    )


def _html_to_text_tokens(html_str: str, page_num: int = 1, is_table: bool = True) -> List[OCRToken]:
    """Helper to convert HTML table cells to OCR tokens."""
    if not html_str or not html_str.strip():
        return []
    import re
    tokens = []
    cell_pattern = re.compile(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>', re.IGNORECASE | re.DOTALL)
    matches = cell_pattern.findall(html_str)
    if matches:
        for m in matches:
            clean = re.sub(r'<[^>]+>', ' ', m).strip()
            if clean:
                tokens.append(OCRToken(text=clean, confidence=0.95, page_num=page_num, is_table=is_table))
    else:
        text = re.sub(r'<[^>]+>', ' ', html_str).strip()
        for word in text.split():
            tokens.append(OCRToken(text=word, confidence=0.95, page_num=page_num, is_table=is_table))
    return tokens


def _markdown_to_tokens(md_str: str, page_num: int = 1, is_table: bool = False) -> List[OCRToken]:
    """Helper to convert Markdown text lines to OCR tokens."""
    if not md_str or not md_str.strip():
        return []
    tokens = []
    lines = md_str.strip().splitlines()
    for line in lines:
        line_s = line.strip()
        if line_s:
            tokens.append(OCRToken(text=line_s, confidence=0.90, page_num=page_num, is_table=is_table))
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# OCRAdapter
# ─────────────────────────────────────────────────────────────────────────────

class OCRAdapter:
    """
    Lightweight Extraction Adapter. Uses native vector PDF extraction.
    Does not require vLLM or GPU.
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8000/v1",
        model_name: str = "olmocr",
        fallback_mode: bool = True,
        target_longest_image_dim: int = 1024,
    ):
        self.server_url = server_url.rstrip("/")
        self.model_name = model_name
        self.fallback_mode = fallback_mode
        self.target_longest_image_dim = target_longest_image_dim

    def check_server(self) -> bool:
        """Check if vLLM server is available. Returns False immediately without hanging."""
        return False

    @staticmethod
    def _render_page_to_base64png(pdf_path: str, page_num: int, target_longest_image_dim: int = 1024) -> str:
        """Render a page to base64 PNG string."""
        import base64
        import fitz
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_num - 1]
            rect = page.rect
            longest = max(rect.width, rect.height)
            scale = target_longest_image_dim / longest if longest > 0 else 1.0
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)
            return base64.b64encode(pix.tobytes("png")).decode("utf-8")
        finally:
            doc.close()

    def extract_native(self, pdf_path: str, page_num: int) -> PageOCRResult:
        """Extract words and text with bounding boxes natively via PyMuPDF."""
        import fitz
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_num - 1]
            words = page.get_text("words")
            raw_text = page.get_text()

            tokens: List[OCRToken] = []
            for w in words:
                tokens.append(
                    OCRToken(
                        text=w[4],
                        confidence=1.0,
                        bbox=[round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2)],
                        page_num=page_num,
                    )
                )

            return PageOCRResult(
                page_num=page_num,
                tokens=tokens,
                is_table=False,
                is_diagram=False,
                raw_text=raw_text,
                raw_html=None,
                primary_language="en",
                ocr_mode="fallback" if self.fallback_mode else "native",
            )
        finally:
            doc.close()

    def extract(
        self,
        pdf_path: str,
        page_num: int,
        pdf_hash: Optional[str] = None,
        task_manager: Optional[Any] = None,
    ) -> PageOCRResult:
        """Extract OCR tokens for a single page with caching support."""
        if task_manager and pdf_hash:
            cached = task_manager.get_cached_page(pdf_hash, page_num)
            if cached:
                return dict_to_page_ocr_result(cached)

        result = self.extract_native(pdf_path, page_num)

        if task_manager and pdf_hash:
            task_manager.cache_page(pdf_hash, page_num, page_ocr_result_to_dict(result))

        return result

    def extract_all(
        self,
        pdf_path: str,
        total_pages: int,
        pdf_hash: Optional[str] = None,
        task_manager: Optional[Any] = None,
    ) -> List[PageOCRResult]:
        """Extract all pages sequentially."""
        return [self.extract(pdf_path, p, pdf_hash, task_manager) for p in range(1, total_pages + 1)]

    async def extract_async(
        self,
        pdf_path: str,
        page_num: int,
        pdf_hash: Optional[str] = None,
        task_manager: Optional[Any] = None,
    ) -> PageOCRResult:
        """Async extraction."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract, pdf_path, page_num, pdf_hash, task_manager)

    async def extract_all_async(
        self,
        pdf_path: str,
        total_pages: int,
        pdf_hash: Optional[str] = None,
        task_manager: Optional[Any] = None,
    ) -> List[PageOCRResult]:
        """Async all pages extraction with bounded concurrency."""
        sem = asyncio.Semaphore(4)

        async def _extract_bounded(p: int):
            async with sem:
                return await self.extract_async(pdf_path, p, pdf_hash, task_manager)

        tasks = [_extract_bounded(p) for p in range(1, total_pages + 1)]
        return await asyncio.gather(*tasks)
