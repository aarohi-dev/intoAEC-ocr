"""
Layout Analyzer — detects page regions (title, header, footer, section headings,
table regions, page numbers) from OCR tokens using y-coordinate band clustering
and content heuristics.

Works with model tokens and PyMuPDF/Ollama tokens (bbox=[0,0,0,0]).
When coordinates are absent, falls back to line-order heuristics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple

from boqparser.adapter.ocr_adapter import OCRToken, PageOCRResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Region types
# ─────────────────────────────────────────────────────────────────────────────

class RegionType(Enum):
    TITLE = auto()
    HEADER = auto()
    FOOTER = auto()
    PAGE_NUMBER = auto()
    SECTION_HEADING = auto()
    TABLE = auto()
    TEXT = auto()
    UNKNOWN = auto()


@dataclass
class TextRegion:
    """A group of tokens identified as a distinct layout region."""
    region_type: RegionType
    tokens: List[OCRToken]
    text: str          # concatenated text
    y_min: float       # top coordinate (PDF points, or line index if no coords)
    y_max: float
    x_min: float
    x_max: float
    page_num: int


@dataclass
class LayoutResult:
    """Layout analysis result for a single page."""
    page_num: int
    regions: List[TextRegion]
    reading_order: List[int]          # indices into `regions` in reading order
    page_width: float = 0.0
    page_height: float = 0.0
    has_coordinates: bool = False     # True if bbox data is real


# ─────────────────────────────────────────────────────────────────────────────
# Heuristics / regex
# ─────────────────────────────────────────────────────────────────────────────

_PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?\d+\s*(?:of\s*\d+)?$", re.IGNORECASE)
_ITEM_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*\.?$")
_ALL_CAPS_RE    = re.compile(r"^[A-Z][A-Z\s\-/&(),.:]+$")

# Patterns that suggest a section heading
_SECTION_KEYWORDS = {
    "FOUNDATION", "FLOOR", "TERRACE", "SLAB", "BEAM", "COLUMN",
    "FOOTING", "PLINTH", "STILT", "GROUND", "FIRST", "SECOND", "THIRD",
    "ROOF", "BASEMENT", "ELECTRICAL", "PLUMBING", "FINISHING", "PAINTING",
    "FLOORING", "WATERPROOFING", "MISCELLANEOUS", "EARTHWORK", "MASONRY",
    "PLASTERING", "WOODWORK", "STEEL", "STRUCTURAL", "CIVIL", "WORKS",
    "TOTAL", "SUBTOTAL", "GRAND",
}


def _has_coordinates(tokens: List[OCRToken]) -> bool:
    """True if at least some tokens have non-zero bounding boxes."""
    return any(
        t.bbox[0] != 0 or t.bbox[1] != 0 or t.bbox[2] != 0 or t.bbox[3] != 0
        for t in tokens
    )


def _tokens_to_text(tokens: List[OCRToken]) -> str:
    return " ".join(t.text for t in tokens).strip()


def _classify_line(text: str) -> RegionType:
    """Rule-based classification of a single line of text."""
    stripped = text.strip()
    if not stripped:
        return RegionType.UNKNOWN

    # Page number
    if _PAGE_NUMBER_RE.match(stripped):
        return RegionType.PAGE_NUMBER

    # Numeric item number alone on a line
    if _ITEM_NUMBER_RE.match(stripped):
        return RegionType.SECTION_HEADING

    # ALL-CAPS short line → likely title or section heading
    upper = stripped.upper()
    words = upper.split()
    if len(words) <= 8 and _ALL_CAPS_RE.match(stripped) and len(stripped) > 3:
        # Check for known section keywords
        if any(kw in upper for kw in _SECTION_KEYWORDS):
            return RegionType.SECTION_HEADING
        return RegionType.TITLE

    # Markdown table line → table region
    if stripped.startswith("|") or stripped.startswith("<table") or stripped.startswith("<tr"):
        return RegionType.TABLE

    # HTML table continuation
    if stripped.startswith("<td") or stripped.startswith("<th") or stripped.startswith("</"):
        return RegionType.TABLE

    return RegionType.TEXT


# ─────────────────────────────────────────────────────────────────────────────
# LayoutAnalyzer
# ─────────────────────────────────────────────────────────────────────────────

class LayoutAnalyzer:
    """
    Analyzes page layout from OCR tokens.

    When coordinate data is available, uses y-band
    clustering and x-range analysis.  When coordinates are absent (model mode
    with no per-char positions), falls back to line-order heuristics (top N%
    lines = header, bottom N% = footer, etc.).
    """

    def __init__(
        self,
        header_zone_pct: float = 0.10,
        footer_zone_pct: float = 0.08,
        y_cluster_gap: float = 6.0,    # pts gap to split into separate lines
    ):
        self.header_zone_pct = header_zone_pct
        self.footer_zone_pct = footer_zone_pct
        self.y_cluster_gap = y_cluster_gap

    # ── coordinate-based path ────────────────────────────────────────────────

    def _group_by_y_band(self, tokens: List[OCRToken], page_height: float) -> List[List[OCRToken]]:
        """Group tokens into horizontal lines by y-coordinate proximity."""
        if not tokens:
            return []

        # Sort by y_min (top of bbox)
        sorted_tokens = sorted(tokens, key=lambda t: (t.bbox[1], t.bbox[0]))

        lines: List[List[OCRToken]] = []
        current_line: List[OCRToken] = [sorted_tokens[0]]
        current_y = sorted_tokens[0].bbox[1]

        for tok in sorted_tokens[1:]:
            tok_y = tok.bbox[1]
            if abs(tok_y - current_y) <= self.y_cluster_gap:
                current_line.append(tok)
            else:
                # Sort current line left-to-right before saving
                lines.append(sorted(current_line, key=lambda t: t.bbox[0]))
                current_line = [tok]
                current_y = tok_y

        if current_line:
            lines.append(sorted(current_line, key=lambda t: t.bbox[0]))

        return lines

    def _analyze_with_coordinates(self, result: PageOCRResult, page_height: float, page_width: float) -> LayoutResult:
        tokens = result.tokens
        lines = self._group_by_y_band(tokens, page_height)

        header_threshold = page_height * self.header_zone_pct
        footer_threshold = page_height * (1.0 - self.footer_zone_pct)

        regions: List[TextRegion] = []

        for line_tokens in lines:
            if not line_tokens:
                continue

            y_min = min(t.bbox[1] for t in line_tokens)
            y_max = max(t.bbox[3] for t in line_tokens)
            x_min = min(t.bbox[0] for t in line_tokens)
            x_max = max(t.bbox[2] for t in line_tokens)
            text  = _tokens_to_text(line_tokens)

            # Zone classification
            if y_min <= header_threshold:
                rtype = RegionType.HEADER
            elif y_min >= footer_threshold:
                rtype = RegionType.FOOTER
            else:
                rtype = _classify_line(text)

            regions.append(TextRegion(
                region_type=rtype,
                tokens=line_tokens,
                text=text,
                y_min=y_min,
                y_max=y_max,
                x_min=x_min,
                x_max=x_max,
                page_num=result.page_num,
            ))

        reading_order = list(range(len(regions)))  # already sorted by y
        return LayoutResult(
            page_num=result.page_num,
            regions=regions,
            reading_order=reading_order,
            page_width=page_width,
            page_height=page_height,
            has_coordinates=True,
        )

    # ── line-order (VLM) path ────────────────────────────────────────────────

    def _analyze_without_coordinates(self, result: PageOCRResult) -> LayoutResult:
        """
        Heuristic layout analysis when no coordinate data is available.
        Treats each OCRToken (which represents a line) in order.
        """
        tokens = result.tokens
        n = len(tokens)
        regions: List[TextRegion] = []

        for i, tok in enumerate(tokens):
            text = tok.text.strip()
            if not text:
                continue

            # Zone heuristics by position in token list
            if i < max(2, int(n * 0.05)):
                rtype = RegionType.HEADER
            elif i >= n - max(2, int(n * 0.05)):
                rtype = RegionType.FOOTER
            elif result.is_table:
                rtype = RegionType.TABLE
            else:
                rtype = _classify_line(text)

            regions.append(TextRegion(
                region_type=rtype,
                tokens=[tok],
                text=text,
                y_min=float(i),
                y_max=float(i),
                x_min=0.0,
                x_max=0.0,
                page_num=result.page_num,
            ))

        reading_order = list(range(len(regions)))
        return LayoutResult(
            page_num=result.page_num,
            regions=regions,
            reading_order=reading_order,
            has_coordinates=False,
        )

    # ── public API ────────────────────────────────────────────────────────────

    def analyze(
        self,
        result: PageOCRResult,
        page_width: float = 0.0,
        page_height: float = 0.0,
    ) -> LayoutResult:
        """
        Analyze the layout of a single page.

        Args:
            result:      PageOCRResult from the OCRAdapter.
            page_width:  Page width in PDF points (from PDFProcessor).
            page_height: Page height in PDF points.

        Returns:
            LayoutResult with classified TextRegion list.
        """
        if _has_coordinates(result.tokens):
            return self._analyze_with_coordinates(result, page_height or 842.0, page_width or 595.0)
        else:
            return self._analyze_without_coordinates(result)

    def analyze_all(
        self,
        ocr_results: List[PageOCRResult],
        page_dims: Optional[List[Tuple[float, float]]] = None,
    ) -> List[LayoutResult]:
        """
        Analyze layout for all pages.

        Args:
            ocr_results: List of PageOCRResult (one per page).
            page_dims:   Optional list of (width_pts, height_pts) tuples per page.

        Returns:
            List of LayoutResult.
        """
        layout_results = []
        for i, ocr_result in enumerate(ocr_results):
            w, h = 0.0, 0.0
            if page_dims and i < len(page_dims):
                w, h = page_dims[i]
            layout_results.append(self.analyze(ocr_result, page_width=w, page_height=h))
        return layout_results

    def get_table_regions(self, layout: LayoutResult) -> List[TextRegion]:
        """Filter regions classified as TABLE."""
        return [r for r in layout.regions if r.region_type == RegionType.TABLE]

    def get_section_headings(self, layout: LayoutResult) -> List[TextRegion]:
        """Filter regions classified as SECTION_HEADING or TITLE."""
        return [
            r for r in layout.regions
            if r.region_type in (RegionType.SECTION_HEADING, RegionType.TITLE)
        ]

    def get_body_regions(self, layout: LayoutResult) -> List[TextRegion]:
        """All regions except HEADER and FOOTER."""
        return [
            r for r in layout.regions
            if r.region_type not in (RegionType.HEADER, RegionType.FOOTER, RegionType.PAGE_NUMBER)
        ]
