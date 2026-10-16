"""
Canonical Intermediate Data Model for Construction Document Intelligence.

Decouples document extraction from input format and layout, providing
a normalized representation for sections, work items, rates, and materials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Provenance:
    """Tracks origin coordinates and source text for validation and auditability."""
    page_num: int
    bbox: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])  # [x0, y0, x1, y1]
    raw_text: str = ""
    confidence: float = 1.0


@dataclass
class CanonicalMaterial:
    """A resource/material breakdown item (used in Rate Analysis and material breakdowns)."""
    material_id: str = ""
    description: str = ""
    unit: str = ""
    quantity: Optional[float] = None
    rate: Optional[float] = None
    amount: Optional[float] = None
    group_name: str = ""
    provenance: Optional[Provenance] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "material_id": self.material_id,
            "description": self.description,
            "unit": self.unit,
            "quantity": self.quantity,
            "rate": self.rate,
            "amount": self.amount,
            "group_name": self.group_name,
        }


@dataclass
class CanonicalItem:
    """A single bill/line item adhering to the output.json blueprint."""
    item_no: str
    description: str
    unit: str
    quantity: Optional[float]
    net_rate: Optional[float]
    gross_rate: Optional[float]
    amount: Optional[float]
    materials: List[CanonicalMaterial] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    review_flags: List[str] = field(default_factory=list)
    is_subtotal: bool = False
    is_header: bool = False

    def to_blueprint_dict(self) -> Dict[str, Any]:
        """Convert to the exact dictionary expected by output.json."""
        return {
            "item_no": self.item_no or "",
            "description": self.description or "",
            "unit": self.unit or "",
            "quantity": self.quantity,
            "net_rate": self.net_rate,
            "gross_rate": self.gross_rate if self.gross_rate is not None else self.net_rate,
            "amount": self.amount,
            "materials": [m.to_dict() for m in self.materials] if self.materials else [],
        }


@dataclass
class CanonicalSection:
    """A section of work items corresponding to a Bill or Trade section."""
    section_code: str
    section_title: str
    items: List[CanonicalItem] = field(default_factory=list)
    subtotal: Optional[float] = None
    depth: int = 0
    provenance: Optional[Provenance] = None

    def all_items(self) -> List[CanonicalItem]:
        """Return all non-header, non-subtotal items."""
        return [it for it in self.items if not it.is_header and not it.is_subtotal]

    def to_blueprint_dict(self) -> Dict[str, Any]:
        """Convert to the exact dictionary expected by output.json."""
        return {
            "section_code": self.section_code or "",
            "section_title": self.section_title or "",
            "items": [it.to_blueprint_dict() for it in self.items if not it.is_header and not it.is_subtotal],
        }


@dataclass
class CanonicalDocument:
    """Full document representation conforming to the blueprint."""
    source_file: str
    sections: List[CanonicalSection] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    validation_issues: List[Dict[str, Any]] = field(default_factory=list)
    total_pages: int = 0

    def all_items(self) -> List[CanonicalItem]:
        """Collect all line items across all sections."""
        items = []
        for sec in self.sections:
            items.extend(sec.all_items())
        return items

    def to_blueprint_dict(self) -> Dict[str, Any]:
        """Return the exact root schema matching output.json."""
        return {
            "sections": [sec.to_blueprint_dict() for sec in self.sections]
        }
