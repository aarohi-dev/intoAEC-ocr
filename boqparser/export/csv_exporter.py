"""CSV Exporter — exports all BOQ rows as a flat CSV with section path column."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, List


_CSV_FIELDS = ["section_path", "item_no", "description", "quantity", "unit", "rate", "amount", "page_num"]


def _flatten_sections(sections, prefix: str = "") -> List[dict]:
    """Recursively flatten section tree into flat rows with section_path."""
    rows = []
    for sec in sections:
        path = f"{prefix}/{sec.number} {sec.title}".strip("/")
        for item in sec.items:
            if item.is_header_row:
                continue
            rows.append({
                "section_path": path,
                "item_no": item.item_no,
                "description": item.description,
                "quantity": item.quantity if item.quantity is not None else "",
                "unit": item.unit,
                "rate": item.rate if item.rate is not None else "",
                "amount": item.amount if item.amount is not None else "",
                "page_num": item.page_num,
            })
        # Recurse into children
        rows.extend(_flatten_sections(sec.children, prefix=path))
    return rows


class CSVExporter:
    """Export BOQ parse result to CSV."""

    def export(self, parse_result: Any, output_path: str) -> str:
        """Write to CSV file. Returns absolute path."""
        rows = _flatten_sections(getattr(parse_result, "sections", []))
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return str(out.resolve())

    def to_string(self, parse_result: Any) -> str:
        """Return CSV as a string."""
        rows = _flatten_sections(getattr(parse_result, "sections", []))
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()
