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
    page_ref: Optional[int] = None
    is_header_row: bool = False
    is_subtotal_row: bool = False
    is_summary_row: bool = False
    is_section_header: bool = False
    depth: int = -1
    is_trade_heading: bool = False
    trade_heading: str = ""
    is_percentage: bool = False
    base_amount: Optional[float] = None
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
        if self.page_ref is not None:
            d["page_ref"] = self.page_ref
        if self.is_summary_row:
            d["is_summary_row"] = True
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
    cleaned = re.sub(r"^[^\d\-+]+", "", cleaned)
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None

def _is_number(val: str) -> bool:
    if not val:
        return False
    cleaned = str(val).replace(",", "").replace(" ", "").strip()
    cleaned = re.sub(r"^[^\d\-+]+", "", cleaned)
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


def _cluster_line_words(words: List[WordBox]) -> List[WordBox]:
    """
    Cluster adjacent numeric fragments (e.g. South African Rand thousands separated by spaces)
    into a single WordBox so that numbers like '7 153 695.00' or '178 842.38' are never
    artificially split across column boundaries.
    """
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: w.x0)
    merged: List[WordBox] = []
    i = 0
    while i < len(sorted_words):
        w = sorted_words[i]
        w_clean = w.text.replace(",", "").replace(".", "").replace("R", "").strip()
        if w_clean.isdigit():
            chain = [w]
            j = i + 1
            while j < len(sorted_words):
                next_w = sorted_words[j]
                gap = next_w.x0 - chain[-1].x1
                next_clean = next_w.text.replace(",", "").replace(".", "").replace("R", "").strip()
                if gap <= 4.5 and (next_clean.isdigit() or re.match(r"^\d+\.\d{2}$", next_w.text)):
                    chain.append(next_w)
                    j += 1
                else:
                    break
            if len(chain) > 1:
                combined_text = " ".join(cw.text for cw in chain)
                combined_box = WordBox(
                    text=combined_text,
                    x0=chain[0].x0,
                    x1=chain[-1].x1,
                    y0=min(cw.y0 for cw in chain),
                    y1=max(cw.y1 for cw in chain),
                )
                merged.append(combined_box)
                i = j
                continue
        merged.append(w)
        i += 1
    return merged


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
        active_trade_heading = ""
        is_summary = getattr(table_struct, "is_summary_table", False)
        pending_summary_text: List[str] = []

        for line in page.lines:
            if line.y1 <= header_y1:
                continue

            # Check for running headers, page numbers, carried-forwards
            if self.structure_discovery.is_page_header_or_footer(line, page.height):
                continue

            # Map line words into columns with clustered numbers
            clustered_words = _cluster_line_words(line.words)
            line_cells: Dict[str, List[str]] = {}
            for w in clustered_words:
                col = table_struct.find_column(w.x_center)
                if col:
                    line_cells.setdefault(col.name, []).append(w.text)

            item_no_raw = " ".join(line_cells.get("item_no", [])).strip()
            desc_raw = " ".join(line_cells.get("description", [])).strip()
            qty_raw = " ".join(line_cells.get("quantity", [])).strip()
            unit_raw = " ".join(line_cells.get("unit", [])).strip()
            rate_raw = " ".join(line_cells.get("rate", [])).strip()
            amt_raw = " ".join(line_cells.get("amount", [])).strip()
            page_ref_raw = " ".join(line_cells.get("page_ref", [])).strip()

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

            # 1. Running subtotal check
            comb_text = (desc_raw + " " + line.text).strip()
            if re.search(r"\b(?:Brought\s+Forward|Carried\s+Forward|Carried\s+to\s+(?:Final\s+)?Summary|Carried\s+to\s+Form\s+of\s+Tender)\b", comb_text, re.I):
                if current_item:
                    rows.append(current_item)
                    current_item = None
                amt_val = self._to_float(amt_raw)
                if amt_val is not None:
                    clean_desc = desc_raw
                    m_tender = re.search(r"(Carried\s+to\s+Form\s+of\s+Tender)", line.text, re.I)
                    m_cf = re.search(r"(Carried\s+(?:to\s+(?:Final\s+)?Summary|Forward))", line.text, re.I)
                    m_bf = re.search(r"(Brought\s+Forward)", line.text, re.I)
                    if m_tender:
                        clean_desc = m_tender.group(1)
                    elif m_cf:
                        clean_desc = m_cf.group(1)
                    elif m_bf:
                        clean_desc = m_bf.group(1)
                    elif not clean_desc:
                        clean_desc = line.text.strip()
                    rows.append(BOQRow(
                        description=clean_desc,
                        amount=amt_val,
                        is_subtotal_row=True,
                        is_summary_row=True,
                        page_num=page.page_num,
                    ))
                continue

            # 2. Summary page handling (Page 59 Section Summary & Page 62 Final Summary)
            if is_summary:
                page_ref_val = int(page_ref_raw) if page_ref_raw.isdigit() else (int(qty_raw) if qty_raw.isdigit() else None)
                amt_val = self._to_float(amt_raw)

                if amt_val is None and not has_item_no:
                    # Line without amount or item number on summary page (e.g. multi-line contingency description)
                    line_desc = desc_raw or line.text.strip()
                    if line_desc:
                        pending_summary_text.append(line_desc)
                    continue

                if amt_val is not None or has_item_no:
                    if current_item:
                        rows.append(current_item)
                        current_item = None
                    full_desc = desc_raw or line.text.strip()
                    if pending_summary_text:
                        full_desc = " ".join(pending_summary_text + [full_desc]).strip()
                        pending_summary_text = []

                    rows.append(BOQRow(
                        item_no=item_no_raw,
                        description=full_desc,
                        page_ref=page_ref_val,
                        amount=amt_val,
                        is_summary_row=True,
                        is_subtotal_row=True,
                        page_num=page.page_num,
                    ))
                continue

            # 3. Section or Bill Header Check
            m_sec = re.search(r"^SECTION\s*(?:NO\.?)?\s*(\d+)", desc_raw or line.text, re.I)
            m_bill = re.search(r"^BILL\s*(?:NO\.?)?\s*(\d+)", desc_raw or line.text, re.I)
            if (m_sec or m_bill) and not has_qty and not has_rate and not has_amt and line.y0 < (page.height - 160.0):
                if current_item:
                    rows.append(current_item)
                    current_item = None
                if m_sec:
                    current_section_code = m_sec.group(1)
                    sec_titles = {"1": "PRELIMINARIES", "2": "BUILDING WORKS", "3": "PROVISIONAL SUMS"}
                    current_section_title = sec_titles.get(current_section_code, f"Section {current_section_code}")
                    rows.append(BOQRow(
                        description=current_section_title,
                        section_code=current_section_code,
                        section_title=current_section_title,
                        is_section_header=True,
                        page_num=page.page_num,
                        depth=0,
                    ))
                if m_bill:
                    b_num = m_bill.group(1)
                    bill_title = f"BILL NO. {b_num}"
                    line_idx = -1
                    for idx, pl in enumerate(page.lines):
                        if pl.y0 == line.y0 and pl.y1 == line.y1:
                            line_idx = idx
                            break
                    if line_idx >= 0 and line_idx + 1 < len(page.lines):
                        next_line = page.lines[line_idx + 1]
                        if next_line.text.strip().isupper() and "PREAMBLE" not in next_line.text.upper() and "NOTE" not in next_line.text.upper():
                            bill_title = next_line.text.strip()
                            if line_idx + 2 < len(page.lines) and page.lines[line_idx + 2].text.strip().isupper() and "PREAMBLE" not in page.lines[line_idx + 2].text.upper():
                                bill_title += " " + page.lines[line_idx + 2].text.strip()
                    rows.append(BOQRow(
                        description=bill_title,
                        section_code=b_num,
                        section_title=bill_title,
                        item_no=b_num,
                        is_section_header=True,
                        page_num=page.page_num,
                        depth=1,
                    ))
                continue

            # 4. Completed item & Trade Heading check (Problem C)
            is_item_complete = current_item is not None and (
                current_item.amount is not None or (current_item.rate is not None and current_item.quantity is not None)
            )
            if is_item_complete and not has_item_no and not has_amt and not has_rate and not has_qty:
                rows.append(current_item)
                current_item = None

                desc_clean = desc_raw.strip()
                if desc_clean:
                    is_trade_heading = bool(
                        re.match(r"^[A-Z0-9\s,\(\)\/\-\.\:]{3,}$", desc_clean)
                        and not desc_clean.lower().startswith("item")
                        and not desc_clean.isdigit()
                    )
                    if is_trade_heading:
                        active_trade_heading = desc_clean
                    rows.append(BOQRow(
                        description=desc_clean,
                        is_header_row=True,
                        is_trade_heading=is_trade_heading,
                        trade_heading=desc_clean if is_trade_heading else active_trade_heading,
                        page_num=page.page_num,
                        section_code=current_section_code,
                        section_title=current_section_title,
                    ))
                continue

            # 5. Percentage and Provisional Sums item check (Problem G)
            has_regular_pricing = has_qty and has_rate and has_amt and unit_raw.lower() not in ("%", "item", "")
            is_pct = (
                ("%" in unit_raw or "%" in rate_raw or bool(re.search(r"\b(?:profit|attendance)\b", desc_raw, re.I)))
                and not has_regular_pricing
            )
            if is_pct and has_amt:
                if current_item:
                    rows.append(current_item)
                amt_val = self._to_float(amt_raw)
                m_rate_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", rate_raw + " " + unit_raw + " " + qty_raw + " " + desc_raw + " " + line.text)
                pct_rate = float(m_rate_pct.group(1)) if m_rate_pct else (self._to_float(rate_raw) or 2.5)
                cand_nums = [
                    self._to_float(qty_raw.replace("%", "").strip()),
                    self._to_float(rate_raw.replace("%", "").strip()),
                ]
                base_amt = next((n for n in cand_nums if n is not None and abs(n - pct_rate) > 0.01), None)

                current_item = BOQRow(
                    item_no=self._clean_item_no(item_no_raw),
                    description=desc_raw,
                    quantity=None,
                    rate=pct_rate,
                    net_rate=pct_rate,
                    gross_rate=pct_rate,
                    unit="%",
                    amount=amt_val,
                    base_amount=base_amt,
                    is_percentage=True,
                    page_num=page.page_num,
                    section_code=current_section_code,
                    section_title=current_section_title,
                    trade_heading=active_trade_heading,
                )
                continue

            # 6. Unpriced item check (Problem E)
            is_unpriced = has_item_no and ("not priced" in line.text.lower() or (not has_amt and not has_rate and not has_qty))
            if is_unpriced:
                if current_item:
                    rows.append(current_item)
                current_item = BOQRow(
                    item_no=self._clean_item_no(item_no_raw),
                    description=desc_raw,
                    quantity=None,
                    rate=None,
                    net_rate=None,
                    gross_rate=None,
                    amount=None,
                    unit=unit_raw or "Item",
                    page_num=page.page_num,
                    section_code=current_section_code,
                    section_title=current_section_title,
                    trade_heading=active_trade_heading,
                )
                continue

            # 7. Standard Priced line item or new item number start
            if has_item_no:
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
                    trade_heading=active_trade_heading,
                )
            elif (has_amt or has_rate or has_qty) and current_item and current_item.amount is None:
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
                    trade_heading=active_trade_heading,
                )
            else:
                # Text continuation only if current_item is not yet complete
                if current_item and current_item.amount is None and desc_raw:
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
