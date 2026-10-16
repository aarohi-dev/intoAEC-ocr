"""
JSON Exporter — exports CanonicalDocument or ParseResult to authoritative output.json blueprint.

Mandatory Schema:
{
  "sections": [
    {
      "section_code": str,
      "section_title": str,
      "items": [
        {
          "item_no": str,
          "description": str,
          "unit": str,
          "quantity": float | null,
          "net_rate": float | null,
          "gross_rate": float | null,
          "amount": float | null,
          "materials": list
        }
      ]
    }
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from boqparser.schemas.output_schema import OutputSchemaValidator


class JSONExporter:
    """Exports construction document data to the authoritative output.json blueprint."""

    def __init__(self, indent: int = 2, validate: bool = True):
        self.indent = indent
        self.validate = validate

    def export(self, document_or_result: Any, output_path: str) -> str:
        """
        Write parse result to a JSON file matching output.json blueprint.

        Args:
            document_or_result: CanonicalDocument or ParseResult object.
            output_path: Destination file path.

        Returns:
            Absolute path to the written file.
        """
        data = self._build_blueprint_dict(document_or_result)
        if self.validate:
            OutputSchemaValidator.assert_valid(data)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=self.indent, ensure_ascii=False)
        return str(out.resolve())

    def to_string(self, document_or_result: Any) -> str:
        """Return blueprint JSON as a string."""
        data = self._build_blueprint_dict(document_or_result)
        if self.validate:
            OutputSchemaValidator.assert_valid(data)
        return json.dumps(data, indent=self.indent, ensure_ascii=False)

    @classmethod
    def _build_blueprint_dict(cls, obj: Any) -> Dict[str, Any]:
        """Convert CanonicalDocument or ParseResult to the exact blueprint dictionary."""
        # 1. CanonicalDocument direct path
        if hasattr(obj, "to_blueprint_dict") and type(obj).__name__ != "ParseResult":
            return obj.to_blueprint_dict()

        # 2. Existing ParseResult / Section fallback adaptation
        sections_data = []
        raw_sections = getattr(obj, "sections", [])

        if raw_sections:
            for sec in raw_sections:
                sec_code = getattr(sec, "section_code", None) or getattr(sec, "number", "")
                sec_title = getattr(sec, "section_title", None) or getattr(sec, "title", "")

                items_data = []
                # Collect direct items and recursive children items
                all_items = getattr(sec, "all_items", None)
                item_list = all_items() if callable(all_items) else getattr(sec, "items", [])

                for item in item_list:
                    if (
                        getattr(item, "is_header_row", False)
                        or getattr(item, "is_header", False)
                        or getattr(item, "is_subtotal_row", False)
                        or getattr(item, "is_subtotal", False)
                        or getattr(item, "is_summary_row", False)
                    ):
                        continue

                    # Safe numeric extraction
                    qty = getattr(item, "quantity", None)
                    net_rate = getattr(item, "net_rate", None)
                    if net_rate is None:
                        net_rate = getattr(item, "rate", None)
                    gross_rate = getattr(item, "gross_rate", None)
                    if gross_rate is None:
                        gross_rate = net_rate
                    amount = getattr(item, "amount", None)

                    materials = getattr(item, "materials", [])
                    if hasattr(item, "groups") and getattr(item, "groups", None):
                        # Flatten RateAnalysis groups into materials list
                        materials = []
                        for grp in getattr(item, "groups", []):
                            grp_name = grp.get("group_name", "")
                            for res in grp.get("resources", []):
                                materials.append({
                                    "material_id": res.get("id", ""),
                                    "description": res.get("name", ""),
                                    "unit": res.get("unit", ""),
                                    "quantity": cls._to_float(res.get("qty_wi")),
                                    "rate": cls._to_float(res.get("price")),
                                    "amount": cls._to_float(res.get("net_amt")),
                                    "group_name": grp_name,
                                })

                    items_data.append({
                        "item_no": str(getattr(item, "item_no", "") or ""),
                        "description": str(getattr(item, "description", "") or "").strip(),
                        "unit": str(getattr(item, "unit", "") or ""),
                        "quantity": float(qty) if qty is not None else None,
                        "net_rate": float(net_rate) if net_rate is not None else None,
                        "gross_rate": float(gross_rate) if gross_rate is not None else None,
                        "amount": float(amount) if amount is not None else None,
                        "materials": materials if isinstance(materials, list) else [],
                    })

                sections_data.append({
                    "section_code": str(sec_code or ""),
                    "section_title": str(sec_title or ""),
                    "items": items_data,
                })
        else:
            # Flat rows fallback
            all_rows = getattr(obj, "all_rows", [])
            items_data = []
            for item in all_rows:
                if getattr(item, "is_header_row", False) or getattr(item, "is_subtotal_row", False):
                    continue
                qty = getattr(item, "quantity", None)
                net_rate = getattr(item, "net_rate", None) or getattr(item, "rate", None)
                gross_rate = getattr(item, "gross_rate", None) or net_rate
                amount = getattr(item, "amount", None)

                items_data.append({
                    "item_no": str(getattr(item, "item_no", "") or ""),
                    "description": str(getattr(item, "description", "") or "").strip(),
                    "unit": str(getattr(item, "unit", "") or ""),
                    "quantity": float(qty) if qty is not None else None,
                    "net_rate": float(net_rate) if net_rate is not None else None,
                    "gross_rate": float(gross_rate) if gross_rate is not None else None,
                    "amount": float(amount) if amount is not None else None,
                    "materials": [],
                })
            sections_data.append({
                "section_code": "0001",
                "section_title": "General Items",
                "items": items_data,
            })

        out_dict: Dict[str, Any] = {"sections": sections_data}
        if hasattr(obj, "project_name") and obj.project_name:
            out_dict["project_name"] = obj.project_name
            out_dict["report_type"] = getattr(obj, "report_type", "BOQ")
            out_dict["source_file"] = getattr(obj, "source_file", "")
            out_dict["total_pages"] = getattr(obj, "total_pages", 0)
            out_dict["ocr_mode"] = getattr(obj, "ocr_mode", "native")
            if hasattr(obj, "all_rows"):
                out_dict["all_items"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in obj.all_rows]
            if hasattr(obj, "validation_summary") and obj.validation_summary:
                vs = obj.validation_summary
                out_dict["validation"] = vs.to_dict() if hasattr(vs, "to_dict") else vs

        return out_dict

    @staticmethod
    def _to_float(val: Any) -> Optional[float]:
        if val is None or val == "":
            return None
        try:
            return float(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return None
