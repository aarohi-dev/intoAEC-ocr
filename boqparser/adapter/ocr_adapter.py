"""
OCR Adapter — normalizes olmocr vLLM output into a standard token list.

Two modes:
  1. VLM mode  — POSTs to a running olmocr vLLM server (via OpenAI-compatible API)
                  Uses build_no_anchoring_v4_yaml_prompt() + FrontMatterParser exactly
                  as olmocr/pipeline.py does.
  2. Fallback  — uses pypdfium2 character-level text extraction when no server is
                  available (no VLM inference; position data is preserved).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import List, Optional

import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OCRToken:
    """A single recognised text span with position and confidence."""
    text: str
    confidence: float
    bbox: List[float]          # [x1, y1, x2, y2] in PDF points (origin top-left)
    page_num: int = 0
    is_table: bool = False     # True when the olmocr model flagged the page as a table
    is_diagram: bool = False
    raw_html: Optional[str] = None  # raw HTML table content when is_table=True


@dataclass
class PageOCRResult:
    """All OCR tokens extracted from a single page."""
    page_num: int
    tokens: List[OCRToken]
    is_table: bool = False
    is_diagram: bool = False
    raw_text: str = ""
    raw_html: Optional[str] = None
    primary_language: Optional[str] = None
    ocr_mode: str = "fallback"  # "vlm" or "fallback"


# ─────────────────────────────────────────────────────────────────────────────
# HTML table text extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _html_to_text_tokens(html: str, page_num: int, is_table: bool) -> List[OCRToken]:
    """
    Convert an HTML string (from olmocr v4 output) into OCRToken list.
    Bboxes are set to [0,0,0,0] because olmocr's VLM output does not
    include per-character coordinates in the HTML; coordinate-level
    analysis uses the pypdfium2 fallback path.
    """
    try:
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tokens: List[OCRToken] = []
                self._current = ""

            def handle_data(self, data):
                stripped = data.strip()
                if stripped:
                    self.tokens.append(
                        OCRToken(
                            text=stripped,
                            confidence=0.95,
                            bbox=[0.0, 0.0, 0.0, 0.0],
                            page_num=page_num,
                            is_table=is_table,
                            raw_html=None,
                        )
                    )

        extractor = _TextExtractor()
        extractor.feed(html)
        return extractor.tokens
    except Exception as exc:
        logger.warning(f"_html_to_text_tokens failed: {exc}")
        return []


def _markdown_to_tokens(text: str, page_num: int, is_table: bool) -> List[OCRToken]:
    """Split plain markdown text into word-level OCRTokens (no coordinate data)."""
    tokens = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        tokens.append(
            OCRToken(
                text=line,
                confidence=0.90,
                bbox=[0.0, 0.0, 0.0, 0.0],
                page_num=page_num,
                is_table=is_table,
                raw_html=None,
            )
        )
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# pypdfium2 fallback extraction (with character positions)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdfium_tokens(pdf_path: str, page_num: int) -> List[OCRToken]:
    """
    Extract text tokens with character-level bounding boxes using pypdfium2.
    Page coordinates: origin at top-left, in PDF points.
    page_num is 1-indexed.
    """
    tokens: List[OCRToken] = []
    try:
        doc = pdfium.PdfDocument(pdf_path)
        page = doc[page_num - 1]
        page_height = page.get_height()
        textpage = page.get_textpage()

        full_text = textpage.get_text_bounded()
        if not full_text.strip():
            return tokens

        # Extract word-level spans using character search
        n_chars = textpage.count_chars()
        if n_chars == 0:
            return tokens

        # Build word groups by scanning characters
        words: List[dict] = []
        current_word_chars = []
        current_rects = []

        for i in range(n_chars):
            char = textpage.get_charbox(i, loose=True)
            ch = full_text[i] if i < len(full_text) else ""
            if ch in (" ", "\n", "\r", "\t") or ch == "":
                if current_word_chars:
                    word_text = "".join(current_word_chars).strip()
                    if word_text:
                        xs = [r[0] for r in current_rects] + [r[2] for r in current_rects]
                        ys = [r[1] for r in current_rects] + [r[3] for r in current_rects]
                        words.append({
                            "text": word_text,
                            "x1": min(xs),
                            "y1": page_height - max(ys),   # flip y so origin is top-left
                            "x2": max(xs),
                            "y2": page_height - min(ys),
                        })
                    current_word_chars = []
                    current_rects = []
            else:
                current_word_chars.append(ch)
                if char is not None:
                    current_rects.append(char)

        # Flush last word
        if current_word_chars:
            word_text = "".join(current_word_chars).strip()
            if word_text and current_rects:
                xs = [r[0] for r in current_rects] + [r[2] for r in current_rects]
                ys = [r[1] for r in current_rects] + [r[3] for r in current_rects]
                words.append({
                    "text": word_text,
                    "x1": min(xs),
                    "y1": page_height - max(ys),
                    "x2": max(xs),
                    "y2": page_height - min(ys),
                })

        for w in words:
            tokens.append(
                OCRToken(
                    text=w["text"],
                    confidence=0.85,
                    bbox=[w["x1"], w["y1"], w["x2"], w["y2"]],
                    page_num=page_num,
                    is_table=False,
                    raw_html=None,
                )
            )
        doc.close()
    except Exception as exc:
        logger.warning(f"pypdfium2 extraction failed for page {page_num}: {exc}")
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# OCRAdapter
# ─────────────────────────────────────────────────────────────────────────────

class OCRAdapter:
    """
    Adapter that wraps the olmocr vLLM inference server.

    When the server is unreachable, falls back to pypdfium2 text extraction
    (no VLM inference).  Both paths return the same normalised List[OCRToken].

    Args:
        server_url:    Base URL of the olmocr vLLM server, e.g. "http://localhost:8000/v1"
        model_name:    Model name as registered in the vLLM server (default "olmocr")
        fallback_mode: If True, always use pypdfium2 (skip VLM even if server is reachable)
        target_longest_image_dim: Longest image dimension for rendering (matches pipeline default)
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8000/v1",
        model_name: str = "olmocr",
        fallback_mode: bool = False,
        target_longest_image_dim: int = 1288,
    ):
        self.server_url = server_url.rstrip("/")
        self.model_name = model_name
        self.fallback_mode = fallback_mode
        self.target_longest_image_dim = target_longest_image_dim
        self._server_available: Optional[bool] = None  # cached after first check

    # ── server health ────────────────────────────────────────────────────────

    def check_server(self) -> bool:
        """Return True if the vLLM server is reachable."""
        if self.fallback_mode:
            return False
        try:
            import httpx
            resp = httpx.get(f"{self.server_url}/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _ensure_server_checked(self) -> bool:
        if self._server_available is None:
            self._server_available = self.check_server()
            if self._server_available:
                logger.info(f"OCRAdapter: vLLM server reachable at {self.server_url}")
            else:
                logger.warning(
                    f"OCRAdapter: vLLM server not reachable at {self.server_url}. "
                    "Using pypdfium2 fallback mode."
                )
        return self._server_available

    # ── image rendering helper ───────────────────────────────────────────────

    @staticmethod
    def _render_page_to_base64png(pdf_path: str, page_num: int, target_longest_image_dim: int) -> str:
        """
        Render a PDF page to base64-encoded PNG using pypdfium2.
        Falls back to the olmocr render_pdf_to_base64png (pdftoppm) if available.
        page_num is 1-indexed.
        """
        try:
            # Prefer pypdfium2 — works cross-platform without Poppler
            doc = pdfium.PdfDocument(pdf_path)
            page = doc[page_num - 1]
            w, h = page.get_width(), page.get_height()
            longest = max(w, h)
            scale = target_longest_image_dim / longest if longest > 0 else 1.0
            bitmap = page.render(scale=scale, rotation=0)
            pil_img = bitmap.to_pil()
            buf = BytesIO()
            pil_img.save(buf, format="PNG")
            doc.close()
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as exc:
            logger.warning(f"pypdfium2 render failed: {exc}, trying olmocr renderer")
            try:
                from olmocr.data.renderpdf import render_pdf_to_base64png
                return render_pdf_to_base64png(pdf_path, page_num, target_longest_image_dim)
            except Exception as exc2:
                raise RuntimeError(f"All render methods failed: {exc2}") from exc2

    # ── VLM extraction ───────────────────────────────────────────────────────

    def _call_vlm(self, pdf_path: str, page_num: int) -> PageOCRResult:
        """
        Call the olmocr vLLM server for a single page.
        Returns a PageOCRResult.
        """
        import httpx
        from olmocr.prompts import build_no_anchoring_v4_yaml_prompt, PageResponse
        from olmocr.train.front_matter import FrontMatterParser

        image_b64 = self._render_page_to_base64png(pdf_path, page_num, self.target_longest_image_dim)
        prompt_text = build_no_anchoring_v4_yaml_prompt()

        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
            "max_tokens": 8000,
            "temperature": 0.1,
        }

        try:
            resp = httpx.post(
                f"{self.server_url}/chat/completions",
                json=payload,
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f"VLM call failed for page {page_num}: {exc}. Using fallback.")
            return self._fallback_extract(pdf_path, page_num)

        model_output = data["choices"][0]["message"]["content"]

        parser = FrontMatterParser(front_matter_class=PageResponse)
        try:
            fm, text = parser._extract_front_matter_and_text(model_output)
            page_response: PageResponse = parser._parse_front_matter(fm, text)
        except Exception as exc:
            logger.warning(f"FrontMatterParser failed for page {page_num}: {exc}")
            # Treat entire output as natural_text
            page_response = PageResponse(
                primary_language="en",
                is_rotation_valid=True,
                rotation_correction=0,
                is_table=False,
                is_diagram=False,
                natural_text=model_output,
            )

        natural_text = page_response.natural_text or ""

        # Convert the model's output to OCRTokens
        if page_response.is_table and "<table" in natural_text.lower():
            tokens = _html_to_text_tokens(natural_text, page_num, is_table=True)
            raw_html = natural_text
        else:
            tokens = _markdown_to_tokens(natural_text, page_num, is_table=page_response.is_table)
            raw_html = None

        # Mark first token with raw_html for table reconstructor
        if tokens and raw_html:
            tokens[0] = OCRToken(
                text=tokens[0].text,
                confidence=tokens[0].confidence,
                bbox=tokens[0].bbox,
                page_num=page_num,
                is_table=True,
                raw_html=raw_html,
            )

        return PageOCRResult(
            page_num=page_num,
            tokens=tokens,
            is_table=page_response.is_table,
            is_diagram=page_response.is_diagram,
            raw_text=natural_text,
            raw_html=raw_html,
            primary_language=page_response.primary_language,
            ocr_mode="vlm",
        )

    # ── fallback extraction ──────────────────────────────────────────────────

    def _fallback_extract(self, pdf_path: str, page_num: int) -> PageOCRResult:
        """Extract text using pypdfium2 (no VLM)."""
        tokens = _extract_pdfium_tokens(pdf_path, page_num)
        raw_text = " ".join(t.text for t in tokens)
        return PageOCRResult(
            page_num=page_num,
            tokens=tokens,
            is_table=False,
            is_diagram=False,
            raw_text=raw_text,
            raw_html=None,
            primary_language=None,
            ocr_mode="fallback",
        )

    # ── public API ───────────────────────────────────────────────────────────

    def extract(self, pdf_path: str, page_num: int) -> PageOCRResult:
        """
        Extract OCR tokens for a single page.

        Args:
            pdf_path: Path to the PDF file.
            page_num: 1-indexed page number.

        Returns:
            PageOCRResult with normalised OCRToken list.
        """
        use_vlm = self._ensure_server_checked()
        if use_vlm:
            try:
                return self._call_vlm(pdf_path, page_num)
            except Exception as exc:
                logger.warning(f"VLM extraction failed for page {page_num}: {exc}. Falling back.")
        return self._fallback_extract(pdf_path, page_num)

    def extract_all(self, pdf_path: str, total_pages: int) -> List[PageOCRResult]:
        """Extract OCR results for all pages sequentially."""
        results = []
        for page_num in range(1, total_pages + 1):
            logger.info(f"OCR: page {page_num}/{total_pages}")
            results.append(self.extract(pdf_path, page_num))
        return results

    # ── async API ────────────────────────────────────────────────────────────

    async def extract_async(self, pdf_path: str, page_num: int) -> PageOCRResult:
        """Async wrapper around extract()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract, pdf_path, page_num)

    async def extract_all_async(self, pdf_path: str, total_pages: int) -> List[PageOCRResult]:
        """Extract all pages concurrently (limited to 4 at a time)."""
        sem = asyncio.Semaphore(4)

        async def _extract_limited(page_num):
            async with sem:
                return await self.extract_async(pdf_path, page_num)

        tasks = [_extract_limited(p) for p in range(1, total_pages + 1)]
        return await asyncio.gather(*tasks)
