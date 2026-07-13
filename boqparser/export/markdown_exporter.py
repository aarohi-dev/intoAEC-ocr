"""Markdown Exporter — generates Markdown report with tables per section."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any


def _fmt(val, decimals: int = 2) -> str:
    """Format a number or return empty string."""
    if val is None:
        return ""
    try:
        return f"{float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(val)


class MarkdownExporter:
    """Export BOQ parse result to a Markdown report."""

    def export(self, parse_result: Any, output_path: str) -> str:
        """Write to Markdown file. Returns absolute path."""
        content = self.to_string(parse_result)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        return str(out.resolve())

    def to_string(self, parse_result: Any) -> str:
        """Render parse result as Markdown string."""
        buf = io.StringIO()
        project = getattr(parse_result, "project_name", "BOQ Report")
        report_type = getattr(parse_result, "report_type", "")

        buf.write(f"# {project}\n\n")
        if report_type:
            buf.write(f"**Report Type:** {report_type}  \n")
        buf.write(f"**Source:** {getattr(parse_result, 'source_file', '')}  \n")
        buf.write(f"**OCR Mode:** {getattr(parse_result, 'ocr_mode', '')}  \n\n")

        vs = getattr(parse_result, "validation_summary", None)
        if vs:
            buf.write(f"> **Grand Total: ₹{vs.grand_total_computed:,.2f}**  \n")
            if vs.invalid_rows:
                buf.write(f"> ⚠️ {vs.invalid_rows} validation issue(s) found.\n")
            buf.write("\n")

        buf.write("---\n\n")

        for sec in getattr(parse_result, "sections", []):
            self._write_section(buf, sec, level=2)

        # Validation Issues appendix
        if vs and vs.invalid_rows:
            buf.write("---\n\n## Validation Issues\n\n")
            buf.write("| Item No. | Description | Issue | Expected (₹) | Actual (₹) | Error % |\n")
            buf.write("|----------|-------------|-------|-------------|------------|--------|\n")
            for v in vs.row_validations:
                if not v.is_valid:
                    buf.write(
                        f"| {v.row.item_no} | {v.row.description[:40]} "
                        f"| {v.flag_message[:50]} "
                        f"| {_fmt(v.expected_amount)} "
                        f"| {_fmt(v.actual_amount)} "
                        f"| {v.error_pct or ''}% |\n"
                    )

        return buf.getvalue()

    def _write_section(self, buf: io.StringIO, section: Any, level: int = 2) -> None:
        """Recursively write a section with its items as a Markdown table."""
        heading = "#" * min(level, 6)
        buf.write(f"{heading} {section.number} {section.title}\n\n")

        # Write items table
        items = [i for i in section.items if not i.is_header_row]
        if items:
            buf.write("| Item No | Description | Qty | Unit | Rate (₹) | Amount (₹) |\n")
            buf.write("|---------|-------------|-----|------|---------|----------|\n")
            for item in items:
                desc = item.description.replace("|", "\\|")[:60]
                qty  = _fmt(item.quantity, 3) if item.quantity is not None else ""
                rate = _fmt(item.rate) if item.rate is not None else ""
                amt  = _fmt(item.amount) if item.amount is not None else ""
                buf.write(
                    f"| {item.item_no} | {desc} | {qty} | {item.unit} | {rate} | {amt} |\n"
                )

            if section.subtotal is not None:
                buf.write(
                    f"| | **Total — {section.title}** | | | | **{_fmt(section.subtotal)}** |\n"
                )
            buf.write("\n")

        # Recurse children
        for child in section.children:
            self._write_section(buf, child, level=level + 1)
