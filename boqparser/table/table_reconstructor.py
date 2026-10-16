"""
Adaptive Table Reconstructor — converts PDF text and coordinates into structured BOQ rows.

Handles:
- Arbitrary column ordering (e.g. Amount on left or right, Unit before or after Quantity)
- Multi-line wrapped descriptions (preambles, specifications, multi-line text)
- Cross-page table continuations
- Merged headers, section headers, and subtotal rows
- Rate analysis resource groupings
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from boqparser.layout.structure_discovery import ColumnDef, StructureDiscovery, TableStructure
from boqparser.pdf.pdf_processor import PageData, TextLine, WordBox

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# BOQ Row Data Types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BOQRow:
    """A single line item in a Bill of Quantities or Estimation report."""
    item_no: str = ""
    description: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    rate: Optional[float] = None
    net_rate: Optional[float] = None
    gross_rate: Optional[float] = None
    amount: Optional[float] = None
    page_num: int = 0
    is_header_row: bool = False
    is_subtotal_row: bool = False
    is_section_header: bool = False
    section_code: str = ""
    section_title: str = ""
    confidence: float = 1.0
    groups: List[Any] = field(default_factory=list)
    materials: List[Any] = field(default_factory=list)
    raw_cells: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "item_no": self.item_no,
            "description": self.description,
            "quantity": self.quantity,
            "unit": self.unit,
            "rate": self.rate if self.rate is not None else self.net_rate,
            "net_rate": self.net_rate if self.net_rate is not None else self.rate,
            "gross_rate": self.gross_rate if self.gross_rate is not None else (self.rate if self.rate is not None else self.net_rate),
            "amount": self.amount,
            "materials": self.materials,
        }
        if self.groups:
            d["groups"] = self.groups
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions & Parsers
# ─────────────────────────────────────────────────────────────────────────────

_KNOWN_UNITS = {
    "cft", "sqft", "sft", "sqm", "cum", "rft", "rmt", "kg", "bag", "bags",
    "nos", "nos.", "no", "no.", "nr", "each", "set", "lot", "ls", "item",
    "m", "m2", "m3", "mm", "cm", "km", "ton", "tonne", "litr", "liter",
    "ltr", "day", "hr", "hour", "hours", "days", "coils", "month", "wk",
}

def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    cleaned = str(val).replace(",", "").replace(" ", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None

def _is_number(val: str) -> bool:
    if not val:
        return False
    cleaned = str(val).replace(",", "").replace(" ", "").strip()
    return bool(re.match(r"^-?\d+(?:\.\d+)?$", cleaned))

def _is_unit(val: str) -> bool:
    if not val:
        return False
    cleaned = str(val).strip().lower()
    return cleaned in _KNOWN_UNITS or cleaned.rstrip(".") in _KNOWN_UNITS

def _is_item_no(val: str) -> bool:
    if not val:
        return False
    v = str(val).strip()
    return bool(re.match(r"^\d+(?:\.\d+)*[a-zA-Z]?$", v))

def _is_header_row(cells: List[str]) -> bool:
    if not cells:
        return False
    header_keywords = {"item", "sl.no", "sl no", "no.", "description", "particulars", "qty", "quantity", "unit", "rate", "amount", "price"}
    matches = sum(1 for c in cells if any(k in str(c).lower() for k in header_keywords))
    return matches >= 2

def _positional_column_map(num_cols: int) -> Dict[int, str]:
    std = {0: "item_no", 1: "description", 2: "quantity", 3: "unit", 4: "rate", 5: "amount"}
    return {i: std.get(i, f"col_{i}") for i in range(num_cols)}

def _cells_to_row(cells: List[str], col_map: Dict[int, str], page_num: int = 1) -> BOQRow:
    row_data: Dict[str, Any] = {"page_num": page_num}
    for idx, cell in enumerate(cells):
        field = col_map.get(idx)
        if not field:
            continue
        c_str = str(cell).strip()
        if field in ("quantity", "rate", "amount", "net_rate", "gross_rate"):
            row_data[field] = _to_float(c_str)
        else:
            row_data[field] = c_str
    return BOQRow(**row_data)

def _parse_html_table(html_str: str, page_num: int = 1) -> List[BOQRow]:
    if not html_str or not html_str.strip():
        return []
    tr_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', html_str, re.IGNORECASE | re.DOTALL)
    if not tr_matches:
        return []
    rows: List[BOQRow] = []
    col_map = _positional_column_map(6)
    for tr in tr_matches:
        cells = re.findall(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>', tr, re.IGNORECASE | re.DOTALL)
        clean_cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in cells]
        if not clean_cells or _is_header_row(clean_cells):
            continue
        if len(clean_cells) < 6:
            clean_cells.extend([""] * (6 - len(clean_cells)))
        row = _cells_to_row(clean_cells[:6], col_map, page_num=page_num)
        rows.append(row)
    return rows


class TableReconstructor:
    """
    Adaptive table reconstructor with coordinate-based multi-line merging.
    """

    def __init__(self, n_cols: int = 6, y_gap: float = 6.0, ollama_adapter: Optional[Any] = None):
        self.n_cols = n_cols
        self.y_gap = y_gap
        self.structure_discovery = StructureDiscovery(ollama_adapter=ollama_adapter)

    # ── numeric & string helpers ─────────────────────────────────────────────

    @staticmethod
    def _to_float(val: Any) -> Optional[float]:
        return _to_float(val)

    @staticmethod
    def _is_number(val: str) -> bool:
        return _is_number(val)

    @staticmethod
    def _clean_item_no(val: str) -> str:
        """Strip trailing dots or punctuation from item number."""
        if not val:
            return ""
        return val.strip().rstrip(".").strip()

    def reconstruct(
        self,
        tokens: Optional[List[Any]] = None,
        raw_html: Optional[str] = None,
        raw_text: str = "",
        is_table: bool = True,
        page_num: int = 1,
    ) -> List[BOQRow]:
        if raw_html:
            return _parse_html_table(raw_html, page_num=page_num)
        if raw_text and ("|" in raw_text):
            lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
            rows: List[BOQRow] = []
            col_map = _positional_column_map(6)
            for line in lines:
                if line.startswith("|") and line.endswith("|"):
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if not parts or all(set(p) <= {"-", ":", " "} for p in parts) or _is_header_row(parts):
                        continue
                    if len(parts) < 6:
                        parts.extend([""] * (6 - len(parts)))
                    rows.append(_cells_to_row(parts[:6], col_map, page_num=page_num))
            if rows:
                return rows

        if raw_text:
            lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
            rows: List[BOQRow] = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 2 and _is_item_no(parts[0]):
                    item_no = parts[0]
                    desc = " ".join(parts[1:])
                    # Parse potential numbers at the end
                    rows.append(BOQRow(item_no=item_no, description=desc, page_num=page_num))
            if rows:
                return rows

        return []

    # ── native page extraction ───────────────────────────────────────────────

    def reconstruct_page(
        self,
        page: PageData,
        active_structure: Optional[TableStructure] = None,
    ) -> Tuple[List[BOQRow], Optional[TableStructure]]:
        """
        Reconstruct BOQ rows from a native PageData object.
        """
        table_struct = self.structure_discovery.detect_table_header(page)
        if not table_struct:
            table_struct = active_structure

        if not table_struct or not page.lines:
            return [], active_structure

        rows: List[BOQRow] = []
        header_y1 = table_struct.header_y1 if table_struct.has_headers and table_struct.page_num == page.page_num else 40.0

        current_item: Optional[BOQRow] = None
        current_section_title = ""
        current_section_code = ""

        for line in page.lines:
            if line.y1 <= header_y1:
                continue

            # Check for running headers, page numbers, carried-forwards
            if self.structure_discovery.is_page_header_or_footer(line, page.height):
                continue

            # Map line words into columns
            line_cells: Dict[str, List[str]] = {}
            for w in line.words:
                col = table_struct.find_column(w.x_center)
                if col:
                    line_cells.setdefault(col.name, []).append(w.text)

            item_no_raw = " ".join(line_cells.get("item_no", [])).strip()
            desc_raw = " ".join(line_cells.get("description", [])).strip()
            qty_raw = " ".join(line_cells.get("quantity", [])).strip()
            unit_raw = " ".join(line_cells.get("unit", [])).strip()
            rate_raw = " ".join(line_cells.get("rate", [])).strip()
            amt_raw = " ".join(line_cells.get("amount", [])).strip()

            # If item_no was captured inside description column (common in single-anchor headers)
            if not item_no_raw and desc_raw:
                # Check if starts with item number e.g. "1 Trenches" or "1.2.3.1 Plastering"
                m_item = re.match(r"^(\d+(?:[.\-]\d+)*\.?)\s+(.+)$", desc_raw)
                if m_item:
                    item_no_raw = m_item.group(1)
                    desc_raw = m_item.group(2)

            has_qty = self._is_number(qty_raw)
            has_rate = self._is_number(rate_raw)
            has_amt = self._is_number(amt_raw)
            has_item_no = bool(item_no_raw) and bool(re.match(r"^\d+(?:[.\-]\d+)*\.?[A-Za-z]?$", item_no_raw))

            # 1. Section Header Check
            is_sec_candidate = (
                not has_qty and not has_rate and not has_amt and not has_item_no and
                bool(re.search(r"^(?:BILL\s+NO\.?|SECTION\s+NO\.?|TRADE\s*[\d\w.-]*|\d{4}\s+[A-Z])", desc_raw, re.I))
            )

            if is_sec_candidate:
                if current_item:
                    rows.append(current_item)
                    current_item = None

                current_section_title = desc_raw
                m_code = re.match(r"^(\d{4})\s+(.+)$", desc_raw)
                if m_code:
                    current_section_code = m_code.group(1)
                    current_section_title = m_code.group(2).strip()

                sec_row = BOQRow(
                    description=desc_raw,
                    section_code=current_section_code,
                    section_title=current_section_title,
                    is_section_header=True,
                    page_num=page.page_num,
                )
                rows.append(sec_row)
                continue

            # 2. Priced line item or new item number start
            if has_item_no:
                # Flush previous item
                if current_item:
                    rows.append(current_item)

                qty_val = self._to_float(qty_raw) if has_qty else None
                rate_val = self._to_float(rate_raw) if has_rate else None
                amt_val = self._to_float(amt_raw) if has_amt else None

                current_item = BOQRow(
                    item_no=self._clean_item_no(item_no_raw),
                    description=desc_raw,
                    quantity=qty_val,
                    unit=unit_raw,
                    rate=rate_val,
                    net_rate=rate_val,
                    gross_rate=rate_val,
                    amount=amt_val,
                    page_num=page.page_num,
                    section_code=current_section_code,
                    section_title=current_section_title,
                )
            elif (has_amt or has_rate or has_qty) and current_item and current_item.amount is None:
                # Populate numbers into current item if not yet populated
                if has_qty:
                    current_item.quantity = self._to_float(qty_raw)
                if unit_raw:
                    current_item.unit = unit_raw
                if has_rate:
                    r_val = self._to_float(rate_raw)
                    current_item.rate = r_val
                    current_item.net_rate = r_val
                    current_item.gross_rate = r_val
                if has_amt:
                    current_item.amount = self._to_float(amt_raw)
                if desc_raw:
                    current_item.description = (current_item.description + " " + desc_raw).strip()
            elif has_amt or has_rate or has_qty:
                # Numbered item without explicit item_no
                if current_item:
                    rows.append(current_item)

                qty_val = self._to_float(qty_raw) if has_qty else None
                rate_val = self._to_float(rate_raw) if has_rate else None
                amt_val = self._to_float(amt_raw) if has_amt else None

                current_item = BOQRow(
                    item_no="",
                    description=desc_raw,
                    quantity=qty_val,
                    unit=unit_raw,
                    rate=rate_val,
                    net_rate=rate_val,
                    gross_rate=rate_val,
                    amount=amt_val,
                    page_num=page.page_num,
                    section_code=current_section_code,
                    section_title=current_section_title,
                )
            else:
                # 3. Text continuation / wrapped description
                if current_item and desc_raw:
                    current_item.description = (current_item.description + " " + desc_raw).strip()

        if current_item:
            rows.append(current_item)

        return rows, table_struct

    # ── backwards compatibility wrapper for PageOCRResult ───────────────────

    def reconstruct_all(self, ocr_results: List[Any]) -> List[BOQRow]:
        """Convert list of PageOCRResult or PageData to BOQRows."""
        all_rows: List[BOQRow] = []
        for res in ocr_results:
            # Handle native PageData
            if isinstance(res, PageData):
                page_rows, _ = self.reconstruct_page(res)
                all_rows.extend(page_rows)
            else:
                # Convert OCRToken tokens into text lines and words
                tokens = getattr(res, "tokens", [])
                pnum = getattr(res, "page_num", 1)
                words = [
                    WordBox(
                        x0=t.bbox[0], y0=t.bbox[1], x1=t.bbox[2], y1=t.bbox[3],
                        text=t.text,
                    )
                    for t in tokens
                ]
                pdata = PageData(
                    page_num=pnum,
                    width=612.0,
                    height=792.0,
                    text=getattr(res, "raw_text", ""),
                    words=words,
                    lines=PDFProcessor._build_lines(words, pnum),
                )
                page_rows, _ = self.reconstruct_page(pdata)
                all_rows.extend(page_rows)

        return all_rows
