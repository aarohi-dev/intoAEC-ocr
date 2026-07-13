"""
PDF Processor — converts PDF pages to images using pypdfium2.

Features:
- Scanned vs digital detection (checks if pdfium text layer has content)
- 300 DPI minimum rendering
- PIL Image output per page
- Mixed PDF support (some pages scanned, some digital)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PageImage:
    """Rendered PDF page."""
    page_num: int                  # 1-indexed
    image: Image.Image             # PIL Image (RGB)
    width_pts: float               # Page width in PDF points
    height_pts: float              # Page height in PDF points
    width_px: int                  # Rendered width in pixels
    height_px: int                 # Rendered height in pixels
    dpi: int                       # Effective DPI used for rendering
    is_scanned: bool               # True if no selectable text layer


# ─────────────────────────────────────────────────────────────────────────────
# PDFProcessor
# ─────────────────────────────────────────────────────────────────────────────

class PDFProcessor:
    """
    Converts PDF pages to PIL Images using pypdfium2.

    Args:
        dpi: Rendering resolution. Minimum 300.
        min_text_chars: Number of characters below which a page is considered scanned.
    """

    # PDF has 72 points per inch
    POINTS_PER_INCH = 72.0

    def __init__(self, dpi: int = 300, min_text_chars: int = 20):
        self.dpi = max(dpi, 300)
        self.min_text_chars = min_text_chars

    # ── helpers ──────────────────────────────────────────────────────────────

    def _scale_for_dpi(self, dpi: int) -> float:
        """Convert DPI to pypdfium2 scale factor (scale 1.0 = 72 DPI)."""
        return dpi / self.POINTS_PER_INCH

    def _is_page_scanned(self, page: pdfium.PdfPage) -> bool:
        """Return True if the page has fewer than min_text_chars of selectable text."""
        try:
            textpage = page.get_textpage()
            text = textpage.get_text_bounded()
            return len(text.strip()) < self.min_text_chars
        except Exception:
            return True

    # ── public API ────────────────────────────────────────────────────────────

    def get_page_count(self, pdf_path: str) -> int:
        """Return total number of pages in the PDF."""
        doc = pdfium.PdfDocument(pdf_path)
        count = len(doc)
        doc.close()
        return count

    def render_page(self, pdf_path: str, page_num: int) -> PageImage:
        """
        Render a single PDF page to a PIL Image.

        Args:
            pdf_path: Path to the PDF file.
            page_num: 1-indexed page number.

        Returns:
            PageImage dataclass.
        """
        doc = pdfium.PdfDocument(pdf_path)
        try:
            page = doc[page_num - 1]
            width_pts = page.get_width()
            height_pts = page.get_height()
            is_scanned = self._is_page_scanned(page)

            scale = self._scale_for_dpi(self.dpi)
            bitmap = page.render(scale=scale, rotation=0)
            pil_img = bitmap.to_pil().convert("RGB")

            return PageImage(
                page_num=page_num,
                image=pil_img,
                width_pts=width_pts,
                height_pts=height_pts,
                width_px=pil_img.width,
                height_px=pil_img.height,
                dpi=self.dpi,
                is_scanned=is_scanned,
            )
        finally:
            doc.close()

    def convert_to_images(self, pdf_path: str, pages: Optional[List[int]] = None) -> List[PageImage]:
        """
        Render all (or specified) pages of a PDF.

        Args:
            pdf_path: Path to the PDF file.
            pages:    Optional list of 1-indexed page numbers. Defaults to all pages.

        Returns:
            List of PageImage, one per page.
        """
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        doc = pdfium.PdfDocument(str(path))
        total = len(doc)
        doc.close()

        page_numbers = pages if pages is not None else list(range(1, total + 1))
        results: List[PageImage] = []

        for pnum in page_numbers:
            if pnum < 1 or pnum > total:
                logger.warning(f"Skipping out-of-range page {pnum} (total {total})")
                continue
            logger.info(f"Rendering page {pnum}/{total} at {self.dpi} DPI ...")
            try:
                page_img = self.render_page(str(path), pnum)
                results.append(page_img)
            except Exception as exc:
                logger.error(f"Failed to render page {pnum}: {exc}")

        logger.info(f"Rendered {len(results)} pages from {path.name}")
        return results

    def get_page_dimensions(self, pdf_path: str) -> List[dict]:
        """Return width/height in points for every page (no image rendering)."""
        doc = pdfium.PdfDocument(pdf_path)
        dims = []
        for i, page in enumerate(doc):
            dims.append({
                "page_num": i + 1,
                "width_pts": page.get_width(),
                "height_pts": page.get_height(),
            })
        doc.close()
        return dims

    def detect_pdf_type(self, pdf_path: str) -> str:
        """
        Classify the PDF as 'digital', 'scanned', or 'mixed'.

        Returns one of: 'digital', 'scanned', 'mixed'
        """
        doc = pdfium.PdfDocument(pdf_path)
        scanned_count = 0
        total = len(doc)

        for i, page in enumerate(doc):
            if self._is_page_scanned(page):
                scanned_count += 1

        doc.close()

        if scanned_count == 0:
            return "digital"
        elif scanned_count == total:
            return "scanned"
        else:
            return "mixed"
