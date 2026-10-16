"""
Semantic Mapper — maps arbitrary column headers and fields to canonical BOQ field names.

Deterministic synonym mappings:
- item_no: Item, Item No, Item No., Sl No, Sl.No, S.No, Sr, Sr.No, No, Pos, Code
- description: Description, Particulars, Scope of Work, Item Description, Work Description, Specification
- quantity: Qty, Quantity, Total Qty, Est Qty, Billed Qty, Volume
- unit: Unit, UOM, U/M, Units, Measurement Unit
- rate: Rate, Unit Rate, Price, Net Rate, Gross Rate, Rate (Rs), Rate (₹), Unit Price
- amount: Amount, Total Amount, Cost, Total Cost, Total (Rs), Total (₹), Value, Ext. Price
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class SemanticMapper:
    """Maps varying terminology to canonical field names deterministically."""

    SYNONYMS: Dict[str, List[str]] = {
        "item_no": [
            "item", "item no", "item no.", "item_no", "item number", "item code",
            "sl", "sl no", "sl.no", "sl.no.", "s.no", "sr", "sr no", "sr.no",
            "pos", "pos.", "no", "no."
        ],
        "description": [
            "description", "particulars", "item description", "work description",
            "scope of work", "specification", "activity", "item / description",
            "details", "material description"
        ],
        "quantity": [
            "qty", "quantity", "total qty", "est qty", "estimated quantity",
            "billed qty", "quant", "volume", "takeoff"
        ],
        "unit": [
            "unit", "uom", "u/m", "units", "meas unit", "measurement unit", "metric"
        ],
        "rate": [
            "rate", "unit rate", "price", "unit price", "net rate", "gross rate",
            "rate (rs)", "rate (inr)", "rate (r)", "rate / unit"
        ],
        "amount": [
            "amount", "total amount", "cost", "total cost", "total", "value",
            "amount (rs)", "amount (inr)", "amount (r)", "ext price", "extended amount"
        ],
    }

    @classmethod
    def normalize_header(cls, header_text: str) -> Optional[str]:
        """Map raw header text to canonical field name."""
        if not header_text:
            return None

        cleaned = re.sub(r"[^\w\s./-]", "", header_text.lower()).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)

        for canonical_name, variations in cls.SYNONYMS.items():
            for v in variations:
                if cleaned == v or cleaned.startswith(v + " ") or cleaned.endswith(" " + v):
                    return canonical_name

        # Fuzzy / keyword check
        if any(w in cleaned for w in ("qty", "quant")):
            return "quantity"
        if any(w in cleaned for w in ("rate", "price")):
            return "rate"
        if any(w in cleaned for w in ("amount", "total", "cost", "value")):
            return "amount"
        if any(w in cleaned for w in ("unit", "uom")):
            return "unit"
        if any(w in cleaned for w in ("item", "sl no", "sr no")):
            return "item_no"
        if any(w in cleaned for w in ("desc", "particular", "work", "spec")):
            return "description"

        return None
