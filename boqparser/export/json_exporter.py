"""JSON Exporter — exports ParseResult to structured JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


class JSONExporter:
    """Export BOQ parse result to JSON file."""

    def __init__(self, indent: int = 2):
        self.indent = indent

    def export(self, parse_result: Any, output_path: str) -> str:
        """
        Write parse result to a JSON file.

        Args:
            parse_result: ParseResult object from BOQParser.
            output_path:  Destination file path.

        Returns:
            Absolute path to the written file.
        """
        data = self._build_dict(parse_result)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=self.indent, ensure_ascii=False)
        return str(out.resolve())

    def to_string(self, parse_result: Any) -> str:
        """Return JSON as a string."""
        return json.dumps(self._build_dict(parse_result), indent=self.indent, ensure_ascii=False)

    @staticmethod
    def _build_dict(parse_result: Any) -> Dict[str, Any]:
        """Convert ParseResult to a serialisable dict."""
        sections_data = []
        for sec in getattr(parse_result, "sections", []):
            sections_data.append(sec.to_dict())

        return {
            "project_name": getattr(parse_result, "project_name", ""),
            "report_type": getattr(parse_result, "report_type", ""),
            "source_file": getattr(parse_result, "source_file", ""),
            "total_pages": getattr(parse_result, "total_pages", 0),
            "pdf_type": getattr(parse_result, "pdf_type", ""),
            "ocr_mode": getattr(parse_result, "ocr_mode", ""),
            "sections": sections_data,
            "all_items": [row.to_dict() for row in getattr(parse_result, "all_rows", [])],
            "validation": getattr(parse_result, "validation_summary", {}).to_dict()
                          if hasattr(getattr(parse_result, "validation_summary", None), "to_dict")
                          else {},
            "metadata": getattr(parse_result, "metadata", {}),
        }
