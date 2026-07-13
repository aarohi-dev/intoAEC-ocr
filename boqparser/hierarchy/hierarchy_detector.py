"""
Hierarchy Detector — builds a nested section tree from BOQ rows.

Detects item numbering patterns like:
  1
  1.1
  1.1.1
  1.1.1.1
  1A, 1.1A  (alphanumeric suffixes)

Constructs a tree of Section nodes and assigns BOQ line items to the
deepest matching section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from boqparser.table.table_reconstructor import BOQRow


# ─────────────────────────────────────────────────────────────────────────────
# Section data type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Section:
    """A hierarchical section in a BOQ document."""
    number: str                          # e.g. "1.1"
    title: str                           # e.g. "FOUNDATION WORKS"
    depth: int                           # 0 = top-level, 1 = sub, ...
    items: List[BOQRow] = field(default_factory=list)
    children: List["Section"] = field(default_factory=list)
    subtotal: Optional[float] = None
    page_num: int = 0

    def to_dict(self) -> dict:
        return {
            "section": f"{self.number} {self.title}".strip(),
            "number": self.number,
            "title": self.title,
            "depth": self.depth,
            "subtotal": self.subtotal,
            "items": [item.to_dict() for item in self.items],
            "children": [child.to_dict() for child in self.children],
        }

    def all_items(self) -> List[BOQRow]:
        """Recursively collect all items in this section and its children."""
        items = list(self.items)
        for child in self.children:
            items.extend(child.all_items())
        return items


# ─────────────────────────────────────────────────────────────────────────────
# Item number parsing
# ─────────────────────────────────────────────────────────────────────────────

# Matches: "1", "1.", "1.1", "1.1.", "1.1.1", "1.1.1.1", "1A", "1.1A"
_ITEM_NO_RE = re.compile(
    r"^(\d+(?:\.\d+)*[A-Za-z]?)\.?$"
)


def _parse_depth(item_no: str) -> int:
    """
    Return the nesting depth of an item number.
    "1"     → 0
    "1.1"   → 1
    "1.1.1" → 2
    """
    m = _ITEM_NO_RE.match(item_no.strip())
    if not m:
        return -1
    # Count dots
    core = m.group(1)
    return core.count(".")


def _is_section_row(row: BOQRow) -> bool:
    """
    Heuristic: a row is a section header if:
    - It has no numeric quantity/rate/amount, AND
    - Its item_no matches a section-level pattern (0 or 1 dots), AND
    - It has a non-empty description
    """
    no_numbers = (row.quantity is None and row.rate is None and row.amount is None)
    has_item_no = bool(row.item_no.strip()) and _ITEM_NO_RE.match(row.item_no.strip())
    has_description = bool(row.description.strip())
    return bool(no_numbers and has_item_no and has_description)


def _is_subtotal_row(row: BOQRow) -> bool:
    return row.is_subtotal_row


# ─────────────────────────────────────────────────────────────────────────────
# HierarchyDetector
# ─────────────────────────────────────────────────────────────────────────────

class HierarchyDetector:
    """
    Builds a hierarchical section tree from a flat list of BOQRows.

    Algorithm:
    1. Scan rows in order.
    2. When a section-header row is detected (item_no matches, no numeric values),
       start a new Section node.
    3. Assign subsequent item rows to the current deepest matching section.
    4. Handle subtotal rows: store the amount in the parent section.
    5. Return a list of top-level Section objects.
    """

    def __init__(self, max_depth: int = 4):
        self.max_depth = max_depth

    # ── stack management ─────────────────────────────────────────────────────

    def _find_parent(self, stack: List[Section], depth: int) -> Optional[Section]:
        """Find the deepest section on the stack with depth < given depth."""
        for sec in reversed(stack):
            if sec.depth < depth:
                return sec
        return None

    # ── public API ────────────────────────────────────────────────────────────

    def detect(self, rows: List[BOQRow]) -> List[Section]:
        """
        Build section tree from flat BOQRow list.

        Returns:
            List of top-level Section objects (depth=0).
        """
        roots: List[Section] = []
        stack: List[Section] = []   # sections open at each depth level
        current_section: Optional[Section] = None

        for row in rows:
            if row.is_header_row:
                continue

            item_no = row.item_no.strip()

            # ── Detect section heading row ────────────────────────────────────
            if item_no and _ITEM_NO_RE.match(item_no) and _is_section_row(row):
                depth = _parse_depth(item_no)
                if depth < 0:
                    depth = 0

                section = Section(
                    number=item_no,
                    title=row.description.strip(),
                    depth=depth,
                    page_num=row.page_num,
                )

                # Pop stack to depth level
                while stack and stack[-1].depth >= depth:
                    stack.pop()

                parent = self._find_parent(stack, depth)
                if parent is not None:
                    parent.children.append(section)
                else:
                    roots.append(section)

                stack.append(section)
                current_section = section

            # ── Subtotal row ──────────────────────────────────────────────────
            elif _is_subtotal_row(row):
                if current_section is not None and row.amount is not None:
                    current_section.subtotal = row.amount
                # Also add as item for visibility
                if current_section is not None:
                    row.is_subtotal_row = True
                    current_section.items.append(row)

            # ── Normal line item ──────────────────────────────────────────────
            else:
                if current_section is not None:
                    current_section.items.append(row)
                else:
                    # Orphan items — create a default root section
                    if not roots or roots[-1].number != "0":
                        default_sec = Section(number="0", title="General", depth=0)
                        roots.append(default_sec)
                        stack = [default_sec]
                        current_section = default_sec
                    current_section.items.append(row)  # type: ignore[union-attr]

        return roots

    def flatten(self, sections: List[Section]) -> List[BOQRow]:
        """Return all items in reading order from the section tree."""
        items: List[BOQRow] = []
        for sec in sections:
            items.extend(sec.all_items())
        return items

    def get_all_sections(self, roots: List[Section]) -> List[Section]:
        """Flatten section tree into depth-first list."""
        result: List[Section] = []

        def _dfs(sections: List[Section]):
            for s in sections:
                result.append(s)
                _dfs(s.children)

        _dfs(roots)
        return result
