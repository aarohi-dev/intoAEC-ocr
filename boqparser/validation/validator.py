"""
Validator — checks mathematical consistency of BOQ rows.

Validation rule:  amount ≈ quantity × rate

Also validates:
- Section subtotals (sum of item amounts ≈ declared subtotal)
- Missing required fields
- Negative values
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from boqparser.table.table_reconstructor import BOQRow
from boqparser.hierarchy.hierarchy_detector import Section

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ValidationResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Validation result for a single BOQRow."""
    is_valid: bool
    row: BOQRow
    expected_amount: Optional[float] = None
    actual_amount: Optional[float] = None
    error_pct: Optional[float] = None
    flag_message: str = ""
    issue_type: str = ""   # "amount_mismatch", "missing_field", "negative_value", "ok"


@dataclass
class SectionValidation:
    """Validation result for a section's subtotal."""
    section_number: str
    section_title: str
    declared_subtotal: Optional[float]
    computed_subtotal: float
    is_valid: bool
    error_pct: Optional[float] = None
    flag_message: str = ""


@dataclass
class ValidationSummary:
    """Overall validation summary for the entire document."""
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    missing_field_rows: int = 0
    amount_mismatch_rows: int = 0
    negative_value_rows: int = 0
    row_validations: List[ValidationResult] = field(default_factory=list)
    section_validations: List[SectionValidation] = field(default_factory=list)
    grand_total_declared: Optional[float] = None
    grand_total_computed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "missing_field_rows": self.missing_field_rows,
            "amount_mismatch_rows": self.amount_mismatch_rows,
            "negative_value_rows": self.negative_value_rows,
            "grand_total_declared": self.grand_total_declared,
            "grand_total_computed": self.grand_total_computed,
            "issues": [
                {
                    "item_no": v.row.item_no,
                    "description": v.row.description,
                    "issue": v.flag_message,
                    "issue_type": v.issue_type,
                    "expected_amount": v.expected_amount,
                    "actual_amount": v.actual_amount,
                    "error_pct": v.error_pct,
                }
                for v in self.row_validations
                if not v.is_valid
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

class Validator:
    """
    Validates BOQ data for mathematical consistency.

    Args:
        tolerance: Maximum relative error allowed for amount = qty × rate.
                   Default 0.02 (2%).
        subtotal_tolerance: Tolerance for section subtotal checks (default 0.01).
    """

    def __init__(self, tolerance: float = 0.02, subtotal_tolerance: float = 0.01):
        self.tolerance = tolerance
        self.subtotal_tolerance = subtotal_tolerance

    # ── single row validation ────────────────────────────────────────────────

    def validate_row(self, row: BOQRow) -> ValidationResult:
        """
        Validate a single BOQRow.

        Checks:
        1. Missing quantity, rate, or amount → flag (not fail — partial data is common)
        2. Negative values
        3. amount ≈ quantity × rate
        """
        # Skip header and subtotal rows
        if row.is_header_row or row.is_subtotal_row:
            return ValidationResult(is_valid=True, row=row, issue_type="ok")

        # Check for missing fields
        has_qty = row.quantity is not None
        has_rate = row.rate is not None
        has_amt = row.amount is not None

        if not (has_qty or has_rate or has_amt):
            # Completely empty numeric row — likely a section heading or partial
            return ValidationResult(
                is_valid=True,  # not an error per se
                row=row,
                issue_type="ok",
                flag_message="",
            )

        # Negative value check
        for attr_name, val in [("quantity", row.quantity), ("rate", row.rate), ("amount", row.amount)]:
            if val is not None and val < 0:
                return ValidationResult(
                    is_valid=False,
                    row=row,
                    issue_type="negative_value",
                    flag_message=f"Negative {attr_name}: {val}",
                )

        # Missing required fields for computation
        if not (has_qty and has_rate and has_amt):
            missing = []
            if not has_qty:
                missing.append("quantity")
            if not has_rate:
                missing.append("rate")
            if not has_amt:
                missing.append("amount")
            return ValidationResult(
                is_valid=True,  # Soft flag — missing fields are common in partial OCR
                row=row,
                issue_type="missing_field",
                flag_message=f"Missing fields: {', '.join(missing)}",
            )

        # amount ≈ quantity × rate
        expected = row.quantity * row.rate  # type: ignore[operator]
        actual = row.amount
        denom = max(abs(actual), abs(expected), 1.0)
        error_pct = abs(expected - actual) / denom

        if error_pct > self.tolerance:
            return ValidationResult(
                is_valid=False,
                row=row,
                expected_amount=round(expected, 2),
                actual_amount=actual,
                error_pct=round(error_pct * 100, 2),
                issue_type="amount_mismatch",
                flag_message=(
                    f"Amount mismatch: {row.quantity} × {row.rate} = {expected:.2f} "
                    f"but recorded {actual} (error {error_pct*100:.1f}%)"
                ),
            )

        return ValidationResult(
            is_valid=True,
            row=row,
            expected_amount=round(expected, 2),
            actual_amount=actual,
            error_pct=round(error_pct * 100, 4),
            issue_type="ok",
        )

    # ── section subtotal validation ──────────────────────────────────────────

    def validate_section(self, section: Section) -> SectionValidation:
        """Validate the subtotal of a section against the sum of its item amounts."""
        all_items = section.all_items() if hasattr(section, "all_items") else section.items
        computed = sum(
            item.amount for item in all_items
            if getattr(item, "amount", None) is not None and not getattr(item, "is_subtotal_row", False)
        )

        if section.subtotal is None:
            return SectionValidation(
                section_number=section.number,
                section_title=section.title,
                declared_subtotal=None,
                computed_subtotal=round(computed, 2),
                is_valid=True,
                flag_message="No subtotal declared",
            )

        denom = max(abs(section.subtotal), abs(computed), 1.0)
        error_pct = abs(section.subtotal - computed) / denom

        is_valid = error_pct <= self.subtotal_tolerance

        return SectionValidation(
            section_number=section.number,
            section_title=section.title,
            declared_subtotal=section.subtotal,
            computed_subtotal=round(computed, 2),
            is_valid=is_valid,
            error_pct=round(error_pct * 100, 2) if not is_valid else None,
            flag_message=(
                f"Subtotal mismatch: declared {section.subtotal:.2f}, computed {computed:.2f}"
                if not is_valid else ""
            ),
        )

    # ── batch validation ─────────────────────────────────────────────────────

    def validate_all(self, rows: List[BOQRow], sections: Optional[List[Section]] = None) -> ValidationSummary:
        """
        Validate all BOQ rows and (optionally) section subtotals.

        Args:
            rows:     All BOQRow objects (flat list).
            sections: Section tree roots for subtotal validation.

        Returns:
            ValidationSummary.
        """
        summary = ValidationSummary(total_rows=len(rows))
        row_validations: List[ValidationResult] = []

        for row in rows:
            result = self.validate_row(row)
            row_validations.append(result)

            if result.is_valid:
                summary.valid_rows += 1
            else:
                summary.invalid_rows += 1

            if result.issue_type == "missing_field":
                summary.missing_field_rows += 1
            elif result.issue_type == "amount_mismatch":
                summary.amount_mismatch_rows += 1
            elif result.issue_type == "negative_value":
                summary.negative_value_rows += 1

        summary.row_validations = row_validations

        # Compute grand total
        summary.grand_total_computed = round(
            sum(r.amount for r in rows if r.amount is not None and not r.is_subtotal_row and not r.is_header_row),
            2,
        )

        # Section subtotal validation
        if sections:
            sec_validations = []
            from boqparser.hierarchy.hierarchy_detector import HierarchyDetector
            hd = HierarchyDetector()
            all_sections = hd.get_all_sections(sections)
            for sec in all_sections:
                sec_validations.append(self.validate_section(sec))
            summary.section_validations = sec_validations

        logger.info(
            f"Validation: {summary.valid_rows}/{summary.total_rows} rows valid. "
            f"Mismatches: {summary.amount_mismatch_rows}. "
            f"Grand total: {summary.grand_total_computed}"
        )

        return summary
