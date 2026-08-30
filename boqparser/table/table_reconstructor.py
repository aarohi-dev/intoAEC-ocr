"""
Table Reconstructor — converts OCR tokens or VLM HTML table output into
structured BOQRow records.

Two reconstruction paths:
    1. HTML path  — when the model returns <table> HTML (is_table=True)
                  Parse with html.parser / lxml.
  2. Text path  — when tokens are plain text with or without coordinates.
                  Uses x-coordinate k-means clustering to find columns,
                  then groups tokens into rows by y-proximity.

Column labeling heuristics detect: item_no, description, quantity, unit, rate, amount.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# BOQ Row data type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BOQRow:
    """A single line item in a Bill of Quantities."""
    item_no: str = ""
    description: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    rate: Optional[float] = None
    amount: Optional[float] = None
    raw_cells: List[str] = field(default_factory=list)   # original cell texts
    page_num: int = 0
    is_header_row: bool = False
    is_subtotal_row: bool = False
    confidence: float = 1.0
    groups: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "item_no": self.item_no,
            "description": self.description,
            "quantity": self.quantity,
            "unit": self.unit,
            "rate": self.rate,
            "amount": self.amount,
        }
        if self.groups:
            d["groups"] = self.groups
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_ITEM_NO_RE   = re.compile(r"^\d+(?:[.\-]\d+)*\.?[A-Za-z]?$")
_NUMBER_RE    = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_UNIT_VOCAB   = {
    "cft", "sqft", "sqmt", "sft", "rmt", "rm", "no", "nos", "nos.",
    "no.", "kg", "mt", "ltr", "litre", "bag", "bags", "cum", "cmt",
    "sqm", "m2", "m3", "rft", "lm", "unit", "units", "set", "sets",
    "pair", "pairs", "roll", "rolls", "lump", "ls", "l/s",
}
_SUBTOTAL_KEYWORDS = {"total", "subtotal", "sub total", "grand total", "sum"}
_HEADER_KEYWORDS   = {
    "item", "description", "qty", "quantity", "unit", "rate", "amount",
    "sl", "sl.no", "s.no", "sr", "sr.no", "particulars", "work",
}


def _to_float(text: str) -> Optional[float]:
    """Parse a number string, tolerating commas."""
    if text is None:
        return None
    cleaned = str(text).replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _is_number(text: str) -> bool:
    cleaned = text.replace(",", "").strip()
    return bool(_NUMBER_RE.match(cleaned))


def _is_unit(text: str) -> bool:
    return text.strip().lower() in _UNIT_VOCAB


def _is_item_no(text: str) -> bool:
    return bool(_ITEM_NO_RE.match(text.strip()))


def _is_header_row(cells: List[str]) -> bool:
    lc = {c.strip().lower() for c in cells}
    return len(lc & _HEADER_KEYWORDS) >= 2


def _is_subtotal_row(cells: List[str]) -> bool:
    combined = " ".join(cells).lower()
    for kw in _SUBTOTAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", combined):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Column label detection
# ─────────────────────────────────────────────────────────────────────────────

_COLUMN_LABELS = ["item_no", "description", "quantity", "unit", "rate", "amount"]

def _detect_column_map(header_cells: List[str]) -> Dict[int, str]:
    """
    Map column index → field name using header row text.
    Falls back to positional heuristics if header is missing.
    """
    col_map: Dict[int, str] = {}
    assigned_labels = set()
    for i, cell in enumerate(header_cells):
        lc = cell.strip().lower()
        if any(x in lc for x in ("item", "sl", "sr", "s.no", "no.", "id", "resource id")) and "item_no" not in assigned_labels:
            col_map[i] = "item_no"
            assigned_labels.add("item_no")
        elif any(x in lc for x in ("desc", "particular", "work", "material", "activity", "name", "resource name")) and "description" not in assigned_labels:
            col_map[i] = "description"
            assigned_labels.add("description")
        elif any(x in lc for x in ("qty", "quant")) and "quantity" not in assigned_labels:
            col_map[i] = "quantity"
            assigned_labels.add("quantity")
        elif any(x in lc for x in ("unit", "uom", "u/m")) and "unit" not in assigned_labels:
            col_map[i] = "unit"
            assigned_labels.add("unit")
        elif any(x in lc for x in ("rate", "price")) and "rate" not in assigned_labels:
            col_map[i] = "rate"
            assigned_labels.add("rate")
        elif any(x in lc for x in ("amount", "total", "value", "cost", "rs.", "inr")) and "amount" not in assigned_labels:
            col_map[i] = "amount"
            assigned_labels.add("amount")
    return col_map


def _positional_column_map(n_cols: int) -> Dict[int, str]:
    """
    Fallback column map based on typical BOQ column order:
    item_no | description | quantity | unit | rate | amount
    """
    if n_cols == 6:
        return {0: "item_no", 1: "description", 2: "quantity", 3: "unit", 4: "rate", 5: "amount"}
    elif n_cols == 5:
        return {0: "item_no", 1: "description", 2: "quantity", 3: "unit", 4: "amount"}
    elif n_cols == 4:
        return {0: "item_no", 1: "description", 2: "quantity", 3: "amount"}
    elif n_cols == 3:
        return {0: "description", 1: "quantity", 2: "amount"}
    elif n_cols == 2:
        return {0: "description", 1: "amount"}
    else:
        return {i: "description" for i in range(n_cols)}


def _cells_to_row(cells: List[str], col_map: Dict[int, str], page_num: int) -> BOQRow:
    """Convert a list of cell strings to a BOQRow using a column map."""
    row = BOQRow(raw_cells=list(cells), page_num=page_num)

    for i, cell in enumerate(cells):
        label = col_map.get(i)
        if label is None:
            # Try to infer: if it looks like a number and nothing is set yet
            if _is_item_no(cell) and not row.item_no:
                row.item_no = cell.strip()
            elif _is_unit(cell) and not row.unit:
                row.unit = cell.strip()
            elif _is_number(cell):
                v = _to_float(cell)
                if row.quantity is None:
                    row.quantity = v
                elif row.rate is None:
                    row.rate = v
                elif row.amount is None:
                    row.amount = v
            elif not row.description:
                row.description = cell.strip()
            continue

        val = cell.strip()
        if label == "item_no":
            row.item_no = val
        elif label == "description":
            row.description = val
        elif label == "quantity":
            row.quantity = _to_float(val)
        elif label == "unit":
            row.unit = val
        elif label == "rate":
            row.rate = _to_float(val)
        elif label == "amount":
            row.amount = _to_float(val)

    row.is_header_row = _is_header_row(cells)
    row.is_subtotal_row = _is_subtotal_row(cells)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# HTML table parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_html_table(html: str, page_num: int) -> List[BOQRow]:
    """Parse an HTML <table> string into BOQRows."""
    rows: List[BOQRow] = []
    col_map: Dict[int, str] = {}

    try:
        from html.parser import HTMLParser

        class _TableParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.all_rows: List[List[str]] = []
                self._current_row: List[str] = []
                self._current_cell: str = ""
                self._in_cell: bool = False
                self._in_header: bool = False

            def handle_starttag(self, tag, attrs):
                if tag in ("td", "th"):
                    self._in_cell = True
                    self._in_header = (tag == "th")
                    self._current_cell = ""
                elif tag == "tr":
                    self._current_row = []

            def handle_endtag(self, tag):
                if tag in ("td", "th"):
                    self._current_row.append(self._current_cell.strip())
                    self._in_cell = False
                    self._current_cell = ""
                elif tag == "tr":
                    if self._current_row:
                        self.all_rows.append(self._current_row)
                        self._current_row = []

            def handle_data(self, data):
                if self._in_cell:
                    self._current_cell += data

        parser = _TableParser()
        parser.feed(html)
        raw_rows = parser.all_rows

    except Exception as exc:
        logger.warning(f"HTML table parse failed: {exc}")
        return []

    if not raw_rows:
        return []

    # Detect header row
    header_idx = -1
    for i, r in enumerate(raw_rows[:3]):
        if _is_header_row(r):
            col_map = _detect_column_map(r)
            header_idx = i
            break

    if not col_map and raw_rows:
        col_map = _positional_column_map(len(raw_rows[0]))

    for i, r in enumerate(raw_rows):
        if i == header_idx:
            continue
        if not any(c.strip() for c in r):
            continue
        row = _cells_to_row(r, col_map, page_num)
        if i == header_idx:
            row.is_header_row = True
        rows.append(row)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# X-coordinate clustering (for plain text tokens)
# ─────────────────────────────────────────────────────────────────────────────

def _cluster_x_centers(x_centers: List[float], n_cols: int) -> List[float]:
    """
    Simple 1-D k-means on x-center values to find column centers.
    Returns sorted column center positions.
    """
    import numpy as np
    if not x_centers or n_cols <= 0:
        return []

    arr = np.array(x_centers).reshape(-1, 1)
    n_cols = min(n_cols, len(x_centers))

    # Initialize centers spread across x range
    mn, mx = arr.min(), arr.max()
    centers = np.linspace(mn, mx, n_cols).reshape(-1, 1)

    for _ in range(20):
        # Assign
        dists = np.abs(arr - centers.T)
        labels = dists.argmin(axis=1)
        # Update
        new_centers = np.array([
            arr[labels == k].mean() if (labels == k).any() else centers[k]
            for k in range(n_cols)
        ])
        if np.allclose(centers.flatten(), new_centers.flatten(), atol=1.0):
            break
        centers = new_centers.reshape(-1, 1)

    return sorted(centers.flatten().tolist())


from boqparser.adapter.ocr_adapter import OCRToken


def _tokens_to_column_rows(
    tokens: List[OCRToken],
    n_cols: int = 6,
    y_gap: float = 6.0,
    page_num: int = 0,
) -> List[BOQRow]:
    """
    Reconstruct table rows from tokens with x/y coordinate data.

    1. Dynamically detect column count.
    2. Cluster token x-centers into n_cols columns.
    3. Group tokens with similar y_min into rows.
    4. Map each token to its nearest column.
    5. Detect column labels and convert to BOQRow.
    """
    if not tokens:
        return []

    has_coords = any(
        t.bbox[0] != 0 or t.bbox[2] != 0 for t in tokens
    )

    if not has_coords:
        # No coordinates — treat each token as a separate "line"
        return _tokens_to_lines(tokens, page_num)

    import numpy as np

    # ── Step 1: Dynamically detect column count ──────────────────────────────
    # Group tokens into lines to find the maximum horizontal components
    sorted_tokens = sorted(tokens, key=lambda t: t.bbox[1])
    y_groups_detect: List[List[OCRToken]] = []
    current_group_detect: List[OCRToken] = [sorted_tokens[0]]
    current_y_detect = sorted_tokens[0].bbox[1]

    for tok in sorted_tokens[1:]:
        if abs(tok.bbox[1] - current_y_detect) <= y_gap:
            current_group_detect.append(tok)
        else:
            y_groups_detect.append(current_group_detect)
            current_group_detect = [tok]
            current_y_detect = tok.bbox[1]
    if current_group_detect:
        y_groups_detect.append(current_group_detect)

    max_cells_found = 0
    for g in y_groups_detect:
        line_sorted = sorted(g, key=lambda t: t.bbox[0])
        if not line_sorted:
            continue
        cells_count = 1
        last_x2 = line_sorted[0].bbox[2]
        for t in line_sorted[1:]:
            # If gap between current token start and previous token end > 8.0 points
            if t.bbox[0] - last_x2 > 8.0:
                cells_count += 1
            last_x2 = max(last_x2, t.bbox[2])
        if cells_count > max_cells_found:
            max_cells_found = cells_count

    # Enforce minimum 3 and maximum 15 columns for stability
    n_cols = min(15, max(3, max_cells_found))

    # ── Step 2: cluster x centers ────────────────────────────────────────────
    x_centers = [(t.bbox[0] + t.bbox[2]) / 2.0 for t in tokens]
    col_centers = _cluster_x_centers(x_centers, n_cols)

    if not col_centers:
        return _tokens_to_lines(tokens, page_num)

    def nearest_col(xc: float) -> int:
        return int(np.argmin([abs(xc - cc) for cc in col_centers]))

    # ── Step 2: group by y band ──────────────────────────────────────────────
    sorted_tokens = sorted(tokens, key=lambda t: t.bbox[1])
    y_groups: List[List[OCRToken]] = []
    current_group: List[OCRToken] = [sorted_tokens[0]]
    current_y = sorted_tokens[0].bbox[1]

    for tok in sorted_tokens[1:]:
        if abs(tok.bbox[1] - current_y) <= y_gap:
            current_group.append(tok)
        else:
            y_groups.append(current_group)
            current_group = [tok]
            current_y = tok.bbox[1]
    if current_group:
        y_groups.append(current_group)

    # ── Step 3: build cell matrix ────────────────────────────────────────────
    raw_rows: List[List[str]] = []
    for group in y_groups:
        cells = [""] * len(col_centers)
        for tok in group:
            xc = (tok.bbox[0] + tok.bbox[2]) / 2.0
            col_idx = nearest_col(xc)
            if cells[col_idx]:
                cells[col_idx] += " " + tok.text
            else:
                cells[col_idx] = tok.text
        raw_rows.append(cells)

    if not raw_rows:
        return []

    # ── Step 4: detect column labels ─────────────────────────────────────────
    col_map: Dict[int, str] = {}
    for i, r in enumerate(raw_rows[:3]):
        if _is_header_row(r):
            col_map = _detect_column_map(r)
            break

    if not col_map:
        col_map = _positional_column_map(len(col_centers))

    return [_cells_to_row(r, col_map, page_num) for r in raw_rows if any(c.strip() for c in r)]


def _tokens_to_lines(tokens: List[OCRToken], page_num: int) -> List[BOQRow]:
    """
    Convert a flat list of text tokens into BOQRows using positional heuristics.
    Used when no coordinate data is present.
    """
    rows: List[BOQRow] = []
    # Group consecutive tokens into logical lines (splitting on obvious separators)
    lines: List[str] = []
    current = []
    for tok in tokens:
        if tok.text.strip() in ("", "|"):
            if current:
                lines.append(" ".join(current))
                current = []
        else:
            current.append(tok.text.strip())
    if current:
        lines.append(" ".join(current))

    for line in lines:
        if not line.strip():
            continue
        # Try to split a line into cells by multiple spaces or tab characters
        cells = re.split(r"\s{2,}|\t", line.strip())
        cells = [c.strip() for c in cells if c.strip()]
        if not cells:
            continue
        col_map = _positional_column_map(len(cells))
        rows.append(_cells_to_row(cells, col_map, page_num))

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# TableReconstructor
# ─────────────────────────────────────────────────────────────────────────────

class TableReconstructor:
    """
    Reconstructs structured BOQ rows from OCR output.

    Strategy:
    - If olmocr VLM returned is_table=True and raw_html contains <table>:
        → Parse HTML directly (most accurate)
    - Elif raw_text contains pipe-separated markdown table:
        → Parse markdown table
    - Else:
        → Use x-coordinate clustering on tokens with bbox data
        → Or line-splitting heuristic for tokenized plain text

    Args:
        n_cols:       Expected number of columns (default 6 for standard BOQ).
        y_gap:        Maximum y-distance (pts) between tokens on the same row.
    """

    def __init__(self, n_cols: int = 6, y_gap: float = 8.0):
        self.n_cols = n_cols
        self.y_gap = y_gap

    # ── markdown table parser ────────────────────────────────────────────────

    @staticmethod
    def _parse_markdown_table(text: str, page_num: int) -> List[BOQRow]:
        rows: List[BOQRow] = []
        col_map: Dict[int, str] = {}
        raw_rows: List[List[str]] = []

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            if re.match(r"^\|[-| ]+\|$", line):
                continue  # separator row
            cells = [c.strip() for c in line.strip("|").split("|")]
            raw_rows.append(cells)

        if not raw_rows:
            return []

        for i, r in enumerate(raw_rows[:2]):
            if _is_header_row(r):
                col_map = _detect_column_map(r)
                break

        if not col_map and raw_rows:
            col_map = _positional_column_map(len(raw_rows[0]))

        for r in raw_rows:
            if any(c.strip() for c in r):
                rows.append(_cells_to_row(r, col_map, page_num))

        return rows

    # ── public API ────────────────────────────────────────────────────────────

    def reconstruct(
        self,
        tokens: List[OCRToken],
        raw_html: Optional[str] = None,
        raw_text: str = "",
        is_table: bool = False,
        page_num: int = 0,
    ) -> List[BOQRow]:
        """
        Reconstruct BOQ rows from OCR output for a single page.

        Args:
            tokens:   OCRToken list for this page.
            raw_html: HTML table string from VLM output (if any).
            raw_text: Natural text from VLM output (if any).
            is_table: Whether olmocr flagged this page as a table page.
            page_num: Page number for metadata.

        Returns:
            List of BOQRow.
        """
        # ── Path 1: HTML table (VLM, most accurate) ──────────────────────────
        if raw_html and "<table" in raw_html.lower():
            logger.debug(f"Page {page_num}: using HTML table reconstruction")
            rows = _parse_html_table(raw_html, page_num)
            if rows:
                return rows

        # ── Path 2: Markdown table ────────────────────────────────────────────
        if raw_text and "|" in raw_text:
            rows = self._parse_markdown_table(raw_text, page_num)
            if rows:
                logger.debug(f"Page {page_num}: using markdown table reconstruction")
                return rows

        # ── Path 3: coordinate-based clustering ───────────────────────────────
        if tokens:
            logger.debug(f"Page {page_num}: using coordinate-based reconstruction")
            return _tokens_to_column_rows(tokens, self.n_cols, self.y_gap, page_num)

        return []

    def reconstruct_all(
        self,
        ocr_results,  # List[PageOCRResult]
    ) -> List[BOQRow]:
        """
        Reconstruct BOQ rows from all pages.
        Filters out header rows and empty rows.
        """
        all_rows: List[BOQRow] = []
        for result in ocr_results:
            raw_html = result.raw_html
            # Check first token for raw_html (VLM path attaches it there)
            if not raw_html and result.tokens and result.tokens[0].raw_html:
                raw_html = result.tokens[0].raw_html

            page_rows = self.reconstruct(
                tokens=result.tokens,
                raw_html=raw_html,
                raw_text=result.raw_text,
                is_table=result.is_table,
                page_num=result.page_num,
            )
            all_rows.extend(page_rows)

        return all_rows
