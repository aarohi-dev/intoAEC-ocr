"""
Native PDF Processor — extracts text, words, and coordinates directly from PDF streams.

Primary extraction mode:
- Native character and word bounding box extraction via PyMuPDF (fitz) or pypdfium2.
- Low-memory streaming (processes page by page without loading all pages into RAM).
- Digital vs scanned detection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PageImage:
    """Rendered PDF page (optional compatibility model)."""
    page_num: int = 1
    image: Any = None
    width_pts: float = 0.0
    height_pts: float = 0.0
    width_px: int = 0
    height_px: int = 0
    dpi: int = 300
    is_scanned: bool = False


@dataclass
class WordBox:
    """A single word with bounding box coordinates and reading order."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    block_no: int = 0
    line_no: int = 0
    word_no: int = 0

    @property
    def bbox(self) -> List[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass
class TextLine:
    """A horizontal line of words grouped by y-proximity."""
    words: List[WordBox]
    text: str
    y0: float
    y1: float
    x0: float
    x1: float
    page_num: int = 0


@dataclass
class PageData:
    """Extracted text, geometry, and tokens for a single PDF page."""
    page_num: int                  # 1-indexed
    width: float                   # Width in points
    height: float                  # Height in points
    text: str                      # Plain text of the page
    words: List[WordBox] = field(default_factory=list)
    lines: List[TextLine] = field(default_factory=list)
    is_scanned: bool = False       # True if fewer than min_text_chars
    raw_blocks: List[Any] = field(default_factory=list)


class PDFProcessor:
    """
    Lightweight, native PDF extractor prioritizing bounded memory and vector text preservation.
    """

    def __init__(self, dpi: int = 300, min_text_chars: int = 20):
        self.dpi = max(dpi, 300)
        self.min_text_chars = min_text_chars

    def get_page_count(self, pdf_path: str) -> int:
        """Return total number of pages in the PDF."""
        import fitz
        doc = fitz.open(pdf_path)
        count = len(doc)
        doc.close()
        return count

    def get_page_dimensions(self, pdf_path: str) -> List[Dict[str, float]]:
        """Return dimensions for all pages."""
        import fitz
        doc = fitz.open(pdf_path)
        dims = []
        for i, p in enumerate(doc, start=1):
            r = p.rect
            dims.append({"page_num": i, "width_pts": r.width, "height_pts": r.height})
        doc.close()
        return dims

    def render_page(self, pdf_path: str, page_num: int) -> PageImage:
        """Render a single PDF page to a PageImage (enforcing min 300 DPI)."""
        import os
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        import fitz
        from PIL import Image
        import io
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_num - 1]
            zoom = self.dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            return PageImage(
                page_num=page_num,
                image=img,
                width_px=pix.width,
                height_px=pix.height,
                dpi=self.dpi,
            )
        finally:
            doc.close()

    def convert_to_images(self, pdf_path: str, pages: Optional[List[int]] = None) -> List[PageImage]:
        """Convert specified pages (or all pages) to PageImage objects."""
        import os
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        count = self.get_page_count(pdf_path)
        target = pages if pages is not None else list(range(1, count + 1))
        return [self.render_page(pdf_path, p) for p in target]

    def detect_pdf_type(self, pdf_path: str) -> str:
        """Return 'digital', 'scanned', or 'mixed'."""
        import fitz
        doc = fitz.open(pdf_path)
        total = len(doc)
        if total == 0:
            doc.close()
            return "scanned"

        scanned_count = 0
        sample_pages = min(total, 10)
        for i in range(sample_pages):
            text = doc[i].get_text()
            if len(text.strip()) < self.min_text_chars:
                scanned_count += 1
        doc.close()

        if scanned_count == 0:
            return "digital"
        elif scanned_count == sample_pages:
            return "scanned"
        return "mixed"

    def extract_page(self, pdf_path: str, page_num: int) -> PageData:
        """
        Extract native text and coordinates for a single page (1-indexed).
        """
        import fitz
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_num - 1]
            rect = page.rect
            width, height = rect.width, rect.height

            # Extract words: (x0, y0, x1, y1, "word", block_no, line_no, word_no)
            raw_words = page.get_text("words")
            text = page.get_text()
            is_scanned = len(text.strip()) < self.min_text_chars

            words: List[WordBox] = []
            for w in raw_words:
                words.append(
                    WordBox(
                        x0=round(w[0], 2),
                        y0=round(w[1], 2),
                        x1=round(w[2], 2),
                        y1=round(w[3], 2),
                        text=w[4],
                        block_no=w[5],
                        line_no=w[6],
                        word_no=w[7],
                    )
                )

            # Group words into lines based on line_no / block_no and y-proximity
            lines = self._build_lines(words, page_num)

            return PageData(
                page_num=page_num,
                width=width,
                height=height,
                text=text,
                words=words,
                lines=lines,
                is_scanned=is_scanned,
            )
        finally:
            doc.close()

    def stream_pages(self, pdf_path: str, pages: Optional[List[int]] = None) -> Generator[PageData, None, None]:
        """
        Stream pages one by one for bounded memory footprint.
        """
        total = self.get_page_count(pdf_path)
        target = [p for p in pages if 1 <= p <= total] if pages is not None else list(range(1, total + 1))
        for pnum in target:
            yield self.extract_page(pdf_path, pnum)

    def extract_all_pages(self, pdf_path: str, max_pages: Optional[int] = None) -> List[PageData]:
        """Extract all pages up to max_pages."""
        total = self.get_page_count(pdf_path)
        limit = min(total, max_pages) if max_pages else total
        return [self.extract_page(pdf_path, p) for p in range(1, limit + 1)]

    @staticmethod
    def _build_lines(words: List[WordBox], page_num: int) -> List[TextLine]:
        """Group words into horizontal text lines sorted top-to-bottom."""
        if not words:
            return []

        # Sort words primarily by block_no, line_no, then x0
        sorted_words = sorted(words, key=lambda w: (round(w.y0 / 3.0) * 3.0, w.x0))
        lines: List[TextLine] = []

        curr_words: List[WordBox] = []
        curr_y: Optional[float] = None

        for w in sorted_words:
            if curr_y is None:
                curr_words = [w]
                curr_y = w.y_center
            elif abs(w.y_center - curr_y) <= 4.0:
                curr_words.append(w)
            else:
                curr_words.sort(key=lambda x: x.x0)
                line_text = " ".join(cw.text for cw in curr_words)
                lines.append(
                    TextLine(
                        words=curr_words,
                        text=line_text,
                        x0=min(cw.x0 for cw in curr_words),
                        y0=min(cw.y0 for cw in curr_words),
                        x1=max(cw.x1 for cw in curr_words),
                        y1=max(cw.y1 for cw in curr_words),
                        page_num=page_num,
                    )
                )
                curr_words = [w]
                curr_y = w.y_center

        if curr_words:
            curr_words.sort(key=lambda x: x.x0)
            line_text = " ".join(cw.text for cw in curr_words)
            lines.append(
                TextLine(
                    words=curr_words,
                    text=line_text,
                    x0=min(cw.x0 for cw in curr_words),
                    y0=min(cw.y0 for cw in curr_words),
                    x1=max(cw.x1 for cw in curr_words),
                    y1=max(cw.y1 for cw in curr_words),
                    page_num=page_num,
                )
            )

        return lines
