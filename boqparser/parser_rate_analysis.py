"""
Specialised parser for Rate Analysis reports.
Parses complex PDF structures containing nested sections, work items,
resource groups, and individual resources.
"""

from __future__ import annotations

import logging
import re
import html
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz

from boqparser.table.table_reconstructor import BOQRow
from boqparser.hierarchy.hierarchy_detector import Section

logger = logging.getLogger(__name__)

# Constants/Vocab for units
UNIT_PAT = r'(?:Cft|CFT|Sft|SFT|SqFt|Sqft|Kg|KG|BAG|Nos|NOS|Ltr|LTR|LS|Ls|RFT|Rft|hr|Day|N/day|NO|m2|m3|Rm|rm|nos|litr|Litr|Lit|Liter|Coils)'

# Regular Expressions matching generate_report_html.py
SEC_RE = re.compile(r'^(\d{4})\s+(.+)$')
GRP_RE = re.compile(r'^Group\s*:\s*(\d+)\s*(.*)')
RES_ID_RE = re.compile(r'^([A-Z][A-Z0-9]*-\d{2}-[A-Z0-9]+-?\d*(?:-\d+)?)')
UNIT_RE = re.compile(r'^(' + UNIT_PAT + r')\b\s*(.*)', re.I)
NUMS_RE = re.compile(r'[\d,]+\.?\d*')
NOTE_RE = re.compile(r'Forced gross price', re.I)

class RateAnalysisParser:
    """
    Parser for Aathiv Rate Analysis format PDFs.
    Extracts Sections -> Work Items -> Resource Groups -> Resources.
    """

    def __init__(self, tolerance: float = 0.02):
        self.tolerance = tolerance

    @staticmethod
    def detect(pdf_path: str) -> bool:
        """
        Check if the PDF is in Rate Analysis format.
        """
        try:
            doc = fitz.open(pdf_path)
            num_pages = len(doc)
            test_pages = min(5, num_pages)
            
            for pg_idx in range(test_pages):
                text = doc[pg_idx].get_text()
                if "Analysis of work items" in text or "Resource ID" in text or "Gross Rate / Unit" in text:
                    doc.close()
                    return True
            doc.close()
        except Exception as exc:
            logger.warning(f"Error checking PDF for Rate Analysis format: {exc}")
        return False

    def _parse_float(self, val: str) -> Optional[float]:
        if not val:
            return None
        cleaned = val.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _parse_resource_nums(self, nums: List[str]) -> Dict[str, Any]:
        # Order from PDF: qty_wi, price, net_amt, net_unit, 1.000(fixed), gross_unit, gross_amt, pct
        r = dict(qty_wi="", price="", net_unit="", gross_unit="", net_amt="", gross_amt="", pct="")
        if len(nums) > 0: r["qty_wi"] = nums[0]
        if len(nums) > 1: r["price"] = nums[1]
        if len(nums) > 2: r["net_amt"] = nums[2]
        if len(nums) > 3: r["net_unit"] = nums[3]
        # index 4 is the fixed multiplier "1.000" usually, skip it
        if len(nums) > 5: r["gross_unit"] = nums[5]
        if len(nums) > 6: r["gross_amt"] = nums[6]
        if len(nums) > 7: r["pct"] = nums[7]
        return r

    def parse(self, pdf_path: str, pages: Optional[List[int]] = None) -> Tuple[List[Section], List[BOQRow]]:
        """
        Parse the PDF and return sections tree and a flat list of work items.
        """
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        
        # 1. Extract words and form lines, keeping track of page numbers
        all_lines: List[Tuple[str, int]] = []
        target_indices = [p - 1 for p in pages if 1 <= p <= total_pages] if pages is not None else list(range(total_pages))
        for pg_idx in target_indices:
            page = doc[pg_idx]
            words = page.get_text("words")
            if not words:
                continue
            rows = {}
            for w in words:
                y = round(w[1], 0)
                rows.setdefault(y, []).append((w[0], w[4]))
            for y in sorted(rows):
                line = " ".join(word for _, word in sorted(rows[y]))
                all_lines.append((line.strip(), pg_idx + 1))
        doc.close()

        # 2. Filter header and page numbering lines
        SKIP_RE = re.compile(r'^(PAGE \d+ OF \d+|Aathiv builds and designs)$', re.I)
        HDR_WORDS = {"Resource ID", "Resource name", "Qty/W.I", "Net rate", "Gross rate", "Net Amount", "Gross Amount"}

        def is_hdr(ln: str) -> bool:
            return sum(1 for k in HDR_WORDS if k in ln) >= 3

        clean_lines: List[Tuple[str, int]] = []
        for ln, pg in all_lines:
            if ln and not SKIP_RE.match(ln) and not is_hdr(ln):
                clean_lines.append((ln, pg))

        # 3. Process lines into tokens
        tokens = []
        i = 0
        n = len(clean_lines)

        while i < n:
            ln, pg_num = clean_lines[i]

            if ln.strip() == "Analysis of work items":
                tokens.append({"type": "doc_title", "page_num": pg_num})
                i += 1
                continue

            # Work item: unit after qty pattern
            m = re.match(
                r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+(' + UNIT_PAT + r')\s+'
                r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s*$',
                ln, re.I
            )
            if m:
                tokens.append({
                    "type": "work_item", "code": m.group(1), "number": m.group(2),
                    "description": m.group(3).strip(), "qty": m.group(4),
                    "unit": m.group(5), "net_rate": m.group(6), "gross_rate": m.group(7),
                    "amount": m.group(8), "pct": m.group(9).strip(), "page_num": pg_num
                })
                i += 1
                continue

            # Work item: unit at the end pattern
            m2 = re.match(
                r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+'
                r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s+(\S+)\s*$',
                ln
            )
            if m2:
                tokens.append({
                    "type": "work_item", "code": m2.group(1), "number": m2.group(2),
                    "description": m2.group(3).strip(), "qty": m2.group(4),
                    "unit": m2.group(9), "net_rate": m2.group(5), "gross_rate": m2.group(6),
                    "amount": m2.group(7), "pct": m2.group(8).strip(), "page_num": pg_num
                })
                i += 1
                continue

            # Work item: wrapped description multiline support
            m_start = re.match(r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+)', ln)
            if m_start:
                full = ln
                j = i + 1
                while j < n and j < i + 6:
                    if re.search(r'\(Amt\.\)', full):
                        break
                    nl, _ = clean_lines[j]
                    if re.match(r'^(\d{4})\s+', nl) or re.match(r'^Group\s*:', nl):
                        break
                    full += " " + nl
                    j += 1

                if re.search(r'\(Amt\.\)', full):
                    ma = re.match(
                        r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+(' + UNIT_PAT + r')\s+'
                        r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                        r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s*$',
                        full, re.I
                    )
                    if ma:
                        tokens.append({
                            "type": "work_item", "code": ma.group(1), "number": ma.group(2),
                            "description": ma.group(3).strip(), "qty": ma.group(4),
                            "unit": ma.group(5), "net_rate": ma.group(6), "gross_rate": ma.group(7),
                            "amount": ma.group(8), "pct": ma.group(9).strip(), "page_num": pg_num
                        })
                        i = j
                        continue

                    mb = re.match(
                        r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+'
                        r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                        r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s+(\S+)\s*$',
                        full
                    )
                    if mb:
                        tokens.append({
                            "type": "work_item", "code": mb.group(1), "number": mb.group(2),
                            "description": mb.group(3).strip(), "qty": mb.group(4),
                            "unit": mb.group(9), "net_rate": mb.group(5), "gross_rate": mb.group(6),
                            "amount": mb.group(7), "pct": mb.group(8).strip(), "page_num": pg_num
                        })
                        i = j
                        continue

            # Section heading
            m_sec = SEC_RE.match(ln)
            if m_sec and not re.search(r'\(Net\)|\(Amt\)', ln) and not re.match(r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+', ln):
                tokens.append({"type": "section", "code": m_sec.group(1), "title": m_sec.group(2).strip(), "page_num": pg_num})
                i += 1
                continue

            # Group header
            m_grp = GRP_RE.match(ln)
            if m_grp:
                tokens.append({"type": "group", "num": m_grp.group(1), "name": m_grp.group(2).strip(), "page_num": pg_num})
                i += 1
                continue

            # Resource rows
            m_rid = RES_ID_RE.match(ln)
            if m_rid:
                res_id = m_rid.group(1)
                rest = ln[len(res_id):].strip()

                full_id = res_id
                j = i + 1
                if res_id.endswith('-') and j < n:
                    nl, _ = clean_lines[j]
                    m_tail = re.match(r'^(\d{3})\s*(.*)', nl)
                    if m_tail:
                        full_id = res_id + m_tail.group(1)
                        tail_rest = m_tail.group(2).strip()
                        rest = (rest + " " + tail_rest).strip() if tail_rest else rest
                        j += 1

                unit_m = re.search(r'\b(' + UNIT_PAT + r')\b', rest, re.I)
                name = rest
                unit = ""
                nums = []

                if unit_m:
                    unit = unit_m.group(1)
                    name = rest[:unit_m.start()].strip()
                    after = rest[unit_m.end():]
                    nums = NUMS_RE.findall(after)
                else:
                    name_parts = [rest] if rest else []
                    while j < n and j < i + 6:
                        nl, _ = clean_lines[j]
                        if GRP_RE.match(nl) or re.match(r'^\d{4}\s', nl) or RES_ID_RE.match(nl):
                            break
                        um = UNIT_RE.match(nl)
                        if um:
                            unit = um.group(1)
                            nums = NUMS_RE.findall(um.group(2))
                            j += 1
                            break
                        if re.match(r'^[\d,]+\.?\d*(?:\s+[\d,]+\.?\d*)*$', nl):
                            nums = NUMS_RE.findall(nl)
                            j += 1
                            break
                        name_parts.append(nl)
                        j += 1
                    name = " ".join(name_parts).strip()

                row = {"id": full_id, "name": name, "unit": unit}
                row.update(self._parse_resource_nums(nums))
                tokens.append({"type": "resource", "row": row, "page_num": pg_num})
                i = j
                continue

            # Forced-price notes
            if NOTE_RE.search(ln):
                note = ln
                j = i + 1
                while j < n and j < i + 3:
                    nl, _ = clean_lines[j]
                    if re.match(r'^\d{4}|^Group', nl):
                        break
                    note += " " + nl
                    j += 1
                tokens.append({"type": "note", "text": note.strip(), "page_num": pg_num})
                i = j
                continue

            i += 1

        # 4. Reconstruct Sections and BOQRows from tokens
        roots: List[Section] = []
        section_stack: List[Section] = []
        all_rows: List[BOQRow] = []

        current_wi: Optional[BOQRow] = None
        current_grp: Optional[Dict[str, Any]] = None

        def _find_parent_section(stack: List[Section], code: str) -> Optional[Section]:
            # Depth 0 if code ends with "00", e.g., "0100", otherwise depth 1
            depth = 0 if code.endswith("00") else 1
            for sec in reversed(stack):
                if sec.depth < depth:
                    return sec
            return None

        for tok in tokens:
            t_type = tok["type"]

            if t_type == "section":
                code = tok["code"]
                title = tok["title"]
                pg = tok["page_num"]
                depth = 0 if code.endswith("00") else 1
                
                new_sec = Section(
                    number=code,
                    title=title,
                    section_code=code,
                    section_title=title,
                    depth=depth,
                    items=[],
                    children=[],
                    page_num=pg
                )

                if depth == 0:
                    roots.append(new_sec)
                    section_stack = [new_sec]
                else:
                    parent = _find_parent_section(section_stack, code)
                    if parent:
                        parent.children.append(new_sec)
                        section_stack = section_stack[:parent.depth + 1] + [new_sec]
                    else:
                        roots.append(new_sec)
                        section_stack = [new_sec]

            elif t_type == "work_item":
                n_rate = self._parse_float(tok["net_rate"])
                g_rate = self._parse_float(tok["gross_rate"])
                current_wi = BOQRow(
                    item_no=tok["number"],
                    description=tok["description"],
                    quantity=self._parse_float(tok["qty"]),
                    unit=tok["unit"],
                    rate=n_rate,
                    net_rate=n_rate,
                    gross_rate=g_rate if g_rate is not None else n_rate,
                    amount=self._parse_float(tok["amount"]),
                    page_num=tok["page_num"],
                    groups=[],
                    materials=[],
                )
                
                current_wi.raw_cells = [tok["code"], tok["number"], tok["description"], tok["qty"], tok["unit"], tok["net_rate"], tok["gross_rate"], tok["amount"], tok["pct"]]
                all_rows.append(current_wi)
                
                if section_stack:
                    section_stack[-1].items.append(current_wi)

            elif t_type == "group":
                if current_wi is not None:
                    current_grp = {
                        "group_num": tok["num"],
                        "group_name": tok["name"],
                        "resources": []
                    }
                    current_wi.groups.append(current_grp)

            elif t_type == "resource":
                if current_grp is not None:
                    res_row = tok["row"]
                    current_grp["resources"].append(res_row)
                    if current_wi is not None:
                        current_wi.materials.append({
                            "material_id": res_row.get("id", ""),
                            "description": res_row.get("name", ""),
                            "unit": res_row.get("unit", ""),
                            "quantity": self._parse_float(res_row.get("qty_wi")),
                            "rate": self._parse_float(res_row.get("price")),
                            "amount": self._parse_float(res_row.get("net_amt")),
                            "group_name": current_grp.get("group_name", ""),
                        })

            elif t_type == "note":
                if current_wi is not None:
                    current_wi.description += f"\nNote: {tok['text']}"

        if not roots and all_rows:
            default_sec = Section(number="0", title="General", depth=0, items=all_rows, children=[], page_num=1)
            roots.append(default_sec)

        return roots, all_rows
