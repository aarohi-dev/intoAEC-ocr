"""
Hierarchy Detector — reconstructs sections and items into a canonical hierarchy.

Detects:
- Section markers: 'BILL NO. 1', 'SECTION NO. 2', 'Bill No.1 Earthwork', 'FOUNDATION WORKS'
- 4-digit codes: '0066', '0068', '1500', '1510'
- Dot-notation hierarchies: 1 -> 1.1 -> 1.1.1
- Assigns line items to their enclosing section
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from boqparser.table.table_reconstructor import BOQRow


@dataclass
class Section:
    """A hierarchical section in a construction document."""
    section_code: str = ""
    section_title: str = ""
    number: str = ""                     # For backwards compatibility with older tests
    title: str = ""                      # For backwards compatibility with older tests
    depth: int = 0
    items: List[BOQRow] = field(default_factory=list)
    children: List["Section"] = field(default_factory=list)
    subtotal: Optional[float] = None
    page_num: int = 0

    def __post_init__(self):
        if not self.number and self.section_code:
            self.number = self.section_code
        if not self.title and self.section_title:
            self.title = self.section_title
        if not self.section_code and self.number:
            self.section_code = self.number
        if not self.section_title and self.title:
            self.section_title = self.title

    def all_items(self) -> List[BOQRow]:
        """Recursively collect all line items across this section and its children."""
        items = [it for it in self.items if not getattr(it, "is_header_row", False) and not getattr(it, "is_subtotal_row", False)]
        for child in self.children:
            items.extend(child.all_items())
        return items

    def to_dict(self) -> Dict[str, Any]:
        """Detailed legacy dictionary."""
        return {
            "section": f"{self.section_code} {self.section_title}".strip(),
            "section_code": self.section_code,
            "section_title": self.section_title,
            "number": self.section_code,
            "title": self.section_title,
            "depth": self.depth,
            "subtotal": self.subtotal,
            "items": [item.to_dict() for item in self.items],
            "children": [child.to_dict() for child in self.children],
        }

    def to_blueprint_dict(self) -> Dict[str, Any]:
        """Convert to the exact dictionary expected by output.json."""
        items_data = []
        for it in self.all_items():
            qty = getattr(it, "quantity", None)
            net_rate = getattr(it, "net_rate", None)
            if net_rate is None:
                net_rate = getattr(it, "rate", None)
            gross_rate = getattr(it, "gross_rate", None)
            if gross_rate is None:
                gross_rate = net_rate
            amount = getattr(it, "amount", None)

            items_data.append({
                "item_no": str(getattr(it, "item_no", "") or ""),
                "description": str(getattr(it, "description", "") or "").strip(),
                "unit": str(getattr(it, "unit", "") or ""),
                "quantity": float(qty) if qty is not None else None,
                "net_rate": float(net_rate) if net_rate is not None else None,
                "gross_rate": float(gross_rate) if gross_rate is not None else None,
                "amount": float(amount) if amount is not None else None,
                "materials": getattr(it, "materials", []) or [],
            })

        return {
            "section_code": self.section_code or "0001",
            "section_title": self.section_title or "General",
            "items": items_data,
        }


# Matches: "1", "1.", "1.1", "1.1.", "1.1.1", "1.1.1.1", "1A", "1.1A"
_ITEM_NO_RE = re.compile(r"^(\d+(?:\.\d+)*[A-Za-z]?)\.?$")


def _parse_depth(item_no: str) -> int:
    m = _ITEM_NO_RE.match(item_no.strip())
    if not m:
        return -1
    return m.group(1).count(".")


class HierarchyDetector:
    """
    Builds section hierarchy and associates items with sections.
    """

    def __init__(self, max_depth: int = 4):
        self.max_depth = max_depth

    def detect(self, rows: List[BOQRow]) -> List[Section]:
        """
        Build section tree from flat BOQRow list.
        """
        roots: List[Section] = []
        stack: List[Tuple[int, Section]] = []
        sec_counter = 1

        for row in rows:
            if getattr(row, "is_header_row", False):
                continue

            # Check if this row represents a section header
            is_sec = getattr(row, "is_section_header", False)
            depth = -1
            if is_sec:
                depth = _parse_depth(row.item_no) if row.item_no else 0
                if depth < 0:
                    depth = 0
            elif (
                row.quantity is None
                and row.rate is None
                and getattr(row, "net_rate", None) is None
                and row.amount is None
                and row.item_no
                and _parse_depth(row.item_no) >= 0
            ):
                is_sec = True
                depth = _parse_depth(row.item_no)

            if is_sec:
                code = row.section_code or row.item_no or f"{sec_counter:04d}"
                title = row.section_title or row.description or f"Section {code}"
                sec_counter += 1

                new_sec = Section(
                    section_code=code,
                    section_title=title,
                    number=code,
                    title=title,
                    depth=depth,
                    page_num=row.page_num,
                )

                while stack and stack[-1][0] >= depth:
                    stack.pop()

                if not stack:
                    roots.append(new_sec)
                else:
                    stack[-1][1].children.append(new_sec)

                stack.append((depth, new_sec))
                continue

            # Subtotal row
            if getattr(row, "is_subtotal_row", False):
                if stack and row.amount is not None:
                    stack[-1][1].subtotal = row.amount
                continue

            # Normal line item
            if not stack:
                default_sec = Section(
                    section_code=f"{sec_counter:04d}",
                    section_title="BILL NO. 1",
                    number=f"{sec_counter:04d}",
                    title="BILL NO. 1",
                    depth=0,
                    page_num=row.page_num,
                )
                sec_counter += 1
                roots.append(default_sec)
                stack.append((0, default_sec))

            stack[-1][1].items.append(row)

        def _clean_section(sec: Section) -> Optional[Section]:
            sec.children = [c for c in (_clean_section(ch) for ch in sec.children) if c is not None]
            if sec.items or sec.children:
                return sec
            return None

        clean_roots = [s for s in (_clean_section(r) for r in roots) if s is not None]
        return clean_roots if clean_roots else roots

    def flatten(self, sections: List[Section]) -> List[BOQRow]:
        """Return all items from section tree."""
        items: List[BOQRow] = []
        for sec in sections:
            items.extend(sec.all_items())
        return items

    def get_all_sections(self, roots: List[Section]) -> List[Section]:
        """Flatten section tree into list."""
        result: List[Section] = []

        def _dfs(sections: List[Section]):
            for s in sections:
                result.append(s)
                _dfs(s.children)

        _dfs(roots)
        return result
