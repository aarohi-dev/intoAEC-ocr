"""
Adaptive Structure & Table Boundary Discovery.

Analyzes native PDF word/line coordinates to detect:
- Document & page headers
- Section titles & hierarchy markers
- Table header rows & column boundary intervals
- Table continuations across page breaks
- Footers & carried-forward/brought-forward lines
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from boqparser.pdf.pdf_processor import PageData, TextLine, WordBox

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary & Patterns for Structure Detection
# ─────────────────────────────────────────────────────────────────────────────

HEADER_KEYWORDS = {
    "item", "item no", "item no.", "item_no", "sl", "sl.no", "sl no", "sr", "sr.no",
    "description", "particulars", "work", "activity", "specification",
    "qty", "quantity", "total qty",
    "unit", "uom", "u/m",
    "rate", "price", "net rate", "gross rate", "unit rate",
    "amount", "total", "total amount", "cost", "value",
    "page", "page no", "page no.", "page_no",
}

SECTION_KEYWORDS_RE = re.compile(
    r"^(?:BILL\s+NO\.?|SECTION\s+NO\.?|TRADE|STAGE|PHASE|PART|DIVISION)\s*[\d\w.-]+|"
    r"^[A-Z0-9.\-]+\s+(?:WORKS|PRELIMINARIES|EARTHWORK|CONCRETE|MASONRY|FINISHING|PLASTERING|FLOORING|PAINTING|PLUMBING|ELECTRICAL)",
    re.IGNORECASE
)

PAGE_NUM_RE = re.compile(r"^(?:PAGE\s*)?-?\s*\d+\s*(?:OF\s*\d+)?\s*-?$", re.IGNORECASE)
CARRIED_FORWARD_RE = re.compile(
    r"^(?:Brought\s+Forward|Carried\s+Forward|Carried\s+to\s+(?:Final\s+)?Summary|Carried\s+to\s+Form\s+of\s+Tender|Carried\s+Forward\s+to\s+Summary)",
    re.IGNORECASE
)


@dataclass
class ColumnDef:
    """A detected table column with its horizontal boundaries and inferred role."""
    name: str              # e.g. "item_no", "description", "quantity", "unit", "rate", "amount", "page_ref"
    x_min: float
    x_max: float
    header_text: str = ""
    col_idx: int = 0


@dataclass
class TableStructure:
    """Discovered structure for a tabular section."""
    columns: List[ColumnDef]
    header_y0: float
    header_y1: float
    page_num: int
    has_headers: bool = True
    is_summary_table: bool = False
    summary_type: Optional[str] = None

    def find_column(self, x_center: float) -> Optional[ColumnDef]:
        """Find matching column for a given x coordinate."""
        for col in self.columns:
            if col.x_min <= x_center <= col.x_max:
                return col
        # If outside strict bounds, find the closest column
        if self.columns:
            return min(self.columns, key=lambda c: min(abs(x_center - c.x_min), abs(x_center - c.x_max)))
        return None


class StructureDiscovery:
    """Discovers document layout, section hierarchy, and table grids dynamically."""

    def __init__(self, tolerance_x: float = 8.0, ollama_adapter: Optional[Any] = None):
        self.tolerance_x = tolerance_x
        self.ollama_adapter = ollama_adapter
        self.active_table: Optional[TableStructure] = None

    @staticmethod
    def detect_summary_page(page: PageData) -> Optional[str]:
        """Detect whether this page is a section or final summary page."""
        for line in page.lines[:10]:
            t = line.text.strip().upper()
            if "FINAL SUMMARY" in t:
                return "final_summary"
            if "SECTION SUMMARY" in t:
                return "section_summary"
        return None

    def detect_table_header(self, page: PageData) -> Optional[TableStructure]:
        """
        Scan page lines to find a table header row containing recognized column keywords.
        """
        summary_type = self.detect_summary_page(page)

        for line in page.lines:
            words = line.words
            if len(words) < 2:
                continue

            # Check tokens against keywords
            matched_keywords: List[Tuple[WordBox, str]] = []
            for w in words:
                w_lower = w.text.lower().strip(",.:;/()")
                for kw in HEADER_KEYWORDS:
                    if w_lower == kw or w_lower in kw.split():
                        matched_keywords.append((w, kw))
                        break

            # If we matched 3 or more column keywords in the same horizontal band
            # Or on summary pages 2 keywords e.g. "Bill Page Amount" or "Section Page Amount"
            unique_kws = {kw for _, kw in matched_keywords}
            min_kws = 2 if summary_type else 3
            if len(unique_kws) >= min_kws:
                cols = self._build_columns_from_header_line(line, page.width)
                if len(cols) >= 2:
                    h_y1 = line.y1
                    # Check if the subsequent line is a continuation of the header (e.g. "No No")
                    try:
                        line_idx = page.lines.index(line)
                        if line_idx + 1 < len(page.lines):
                            next_l = page.lines[line_idx + 1]
                            if 0 <= next_l.y0 - line.y1 < 16.0:
                                next_words = [w.text.lower().strip(",.:;/()") for w in next_l.words]
                                if all(w in HEADER_KEYWORDS or w in ("no", "no.", "rate", "qty", "unit") for w in next_words):
                                    h_y1 = next_l.y1
                    except ValueError:
                        pass

                    struct = TableStructure(
                        columns=cols,
                        header_y0=line.y0,
                        header_y1=h_y1,
                        page_num=page.page_num,
                        has_headers=True,
                        is_summary_table=bool(summary_type),
                        summary_type=summary_type,
                    )
                    # Don't persist summary table structure across ordinary bill pages
                    if not summary_type:
                        self.active_table = struct
                    return struct

        return self.active_table

    def _build_columns_from_header_line(self, line: TextLine, page_width: float) -> List[ColumnDef]:
        """
        Derive column spans [x_min, x_max] and semantic names from words in the header row.
        """
        # Group header words into logical column headers
        words = sorted(line.words, key=lambda w: w.x0)
        clusters: List[List[WordBox]] = []
        curr: List[WordBox] = []

        for w in words:
            if not curr:
                curr = [w]
            else:
                w_role = self._classify_header_text(w.text)
                curr_role = self._classify_header_text(" ".join(cw.text for cw in curr))
                # If word has a distinct column role from current cluster, split!
                if w_role != curr_role and (w_role != "description" or curr_role != "description"):
                    clusters.append(curr)
                    curr = [w]
                elif w.x0 - curr[-1].x1 > 6.0 and w_role != "description":
                    clusters.append(curr)
                    curr = [w]
                else:
                    curr.append(w)
        if curr:
            clusters.append(curr)

        # Identify roles of each cluster with LLM disambiguation if available
        cluster_texts = [" ".join(w.text for w in cl) for cl in clusters]
        llm_roles: Dict[str, str] = {}
        if self.ollama_adapter and hasattr(self.ollama_adapter, "is_available") and self.ollama_adapter.is_available():
            try:
                llm_roles = self.ollama_adapter.classify_headers(cluster_texts)
            except Exception as e:
                logger.debug(f"LLM header classification error: {e}")

        headers: List[Tuple[float, float, str, str]] = []  # (x0, x1, text, role)
        for cl in clusters:
            x0 = min(w.x0 for w in cl)
            x1 = max(w.x1 for w in cl)
            text = " ".join(w.text for w in cl)
            role = llm_roles.get(text) or self._classify_header_text(text)
            headers.append((x0, x1, text, role))

        # Check if description column exists; if not, infer between item_no and unit/qty
        has_desc = any(h[3] == "description" for h in headers)
        if not has_desc:
            # Insert description column in the largest gap
            max_gap = 0.0
            gap_idx = 0
            for i in range(len(headers) - 1):
                gap = headers[i + 1][0] - headers[i][1]
                if gap > max_gap:
                    max_gap = gap
                    gap_idx = i
            if max_gap > 60.0:
                desc_x0 = headers[gap_idx][1] + 2.0
                desc_x1 = headers[gap_idx + 1][0] - 2.0
                headers.insert(gap_idx + 1, (desc_x0, desc_x1, "Description", "description"))

        # Expand column boundaries so they cover the intervals smoothly
        cols: List[ColumnDef] = []
        for i, (x0, x1, text, role) in enumerate(headers):
            left_bound = 0.0 if i == 0 else (headers[i - 1][1] + x0) / 2.0
            right_bound = page_width if i == len(headers) - 1 else (x1 + headers[i + 1][0]) / 2.0

            cols.append(
                ColumnDef(
                    name=role,
                    x_min=round(left_bound, 1),
                    x_max=round(right_bound, 1),
                    header_text=text,
                    col_idx=i,
                )
            )

        return cols

    @staticmethod
    def _classify_header_text(text: str) -> str:
        """Map text to a standardized column role."""
        t = text.lower().strip()
        if any(k in t for k in ("page no", "page no.", "page_no", "page")):
            return "page_ref"
        elif any(k in t for k in ("item", "bill", "section", "sl", "sr", "no.", "no")):
            return "item_no"
        elif any(k in t for k in ("desc", "particular", "work", "specification", "activity")):
            return "description"
        elif any(k in t for k in ("qty", "quantity")):
            return "quantity"
        elif any(k in t for k in ("unit", "uom", "u/m")):
            return "unit"
        elif any(k in t for k in ("rate", "price")):
            return "rate"
        elif any(k in t for k in ("amount", "total", "cost", "value")):
            return "amount"
        return "description"

    @staticmethod
    def is_page_header_or_footer(line: TextLine, page_height: float) -> bool:
        """Filter out running headers, page numbers, and breadcrumbs."""
        t = line.text.strip()
        if not t:
            return True

        # Running header top margin (top ~65pt)
        if line.y1 <= 65.0:
            return True

        # Page numbers
        if PAGE_NUM_RE.match(t):
            return True

        # Bottom breadcrumbs (not totals)
        if line.y0 >= (page_height - 160.0):
            if re.search(r"^(?:Section\s+No\.?|Bill\s+No\.?|HOUSE\s+MALULEKE|\-\s*\d+\s*\-)$", t, re.IGNORECASE):
                return True

        # Bottom absolute margin
        if line.y1 >= (page_height - 35.0):
            return True

        return False
