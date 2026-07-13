"""Tests for Validator."""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.validation.validator import Validator, ValidationResult, ValidationSummary
from boqparser.table.table_reconstructor import BOQRow
from boqparser.hierarchy.hierarchy_detector import Section


def make_row(item_no="1", desc="Test Item", qty=100.0, unit="Cft", rate=10.0, amount=1000.0, **kwargs) -> BOQRow:
    return BOQRow(item_no=item_no, description=desc, quantity=qty, unit=unit, rate=rate, amount=amount, **kwargs)


class TestValidateRow:
    def setup_method(self):
        self.validator = Validator(tolerance=0.02)

    def test_valid_row(self):
        row = make_row(qty=100, rate=10, amount=1000)
        result = self.validator.validate_row(row)
        assert result.is_valid
        assert result.issue_type == "ok"

    def test_valid_with_rounding(self):
        row = make_row(qty=6035.75, rate=11.0, amount=66393.25)
        result = self.validator.validate_row(row)
        assert result.is_valid

    def test_amount_mismatch(self):
        row = make_row(qty=100, rate=10, amount=2000)  # Should be 1000
        result = self.validator.validate_row(row)
        assert not result.is_valid
        assert result.issue_type == "amount_mismatch"
        assert result.expected_amount == 1000.0
        assert result.actual_amount == 2000.0

    def test_within_tolerance(self):
        # 1% error within 2% tolerance
        row = make_row(qty=100, rate=10, amount=1010)
        result = self.validator.validate_row(row)
        assert result.is_valid

    def test_negative_quantity(self):
        row = make_row(qty=-100, rate=10, amount=-1000)
        result = self.validator.validate_row(row)
        assert not result.is_valid
        assert result.issue_type == "negative_value"

    def test_missing_quantity(self):
        row = BOQRow(item_no="1", description="Test", quantity=None, rate=10, amount=1000)
        result = self.validator.validate_row(row)
        assert result.is_valid  # Soft flag
        assert result.issue_type == "missing_field"

    def test_header_row_skipped(self):
        row = BOQRow(item_no="", description="Header", is_header_row=True)
        result = self.validator.validate_row(row)
        assert result.is_valid
        assert result.issue_type == "ok"

    def test_subtotal_row_skipped(self):
        row = BOQRow(item_no="", description="Total", amount=5000, is_subtotal_row=True)
        result = self.validator.validate_row(row)
        assert result.is_valid


class TestValidateAll:
    def setup_method(self):
        self.validator = Validator(tolerance=0.02)

    def test_all_valid(self):
        rows = [
            make_row("1.1", "Item A", 100, "Cft", 10, 1000),
            make_row("1.2", "Item B", 50, "Sqft", 20, 1000),
        ]
        summary = self.validator.validate_all(rows)
        assert summary.total_rows == 2
        assert summary.valid_rows == 2
        assert summary.invalid_rows == 0
        assert summary.grand_total_computed == 2000.0

    def test_with_mismatches(self):
        rows = [
            make_row("1.1", "Item A", 100, "Cft", 10, 1000),   # valid
            make_row("1.2", "Item B", 50, "Sqft", 20, 5000),   # invalid (should be 1000)
        ]
        summary = self.validator.validate_all(rows)
        assert summary.invalid_rows == 1
        assert summary.amount_mismatch_rows == 1

    def test_grand_total_excludes_subtotals(self):
        rows = [
            make_row("1.1", "Item A", 100, "Cft", 10, 1000),
            BOQRow(item_no="", description="Subtotal", amount=1000, is_subtotal_row=True),
        ]
        summary = self.validator.validate_all(rows)
        assert summary.grand_total_computed == 1000.0  # Subtotal excluded

    def test_to_dict(self):
        rows = [make_row()]
        summary = self.validator.validate_all(rows)
        d = summary.to_dict()
        assert "total_rows" in d
        assert "valid_rows" in d
        assert "grand_total_computed" in d
        assert "issues" in d


class TestSectionValidation:
    def setup_method(self):
        self.validator = Validator(subtotal_tolerance=0.01)

    def test_valid_subtotal(self):
        sec = Section(number="1", title="Foundation", depth=0)
        sec.items = [
            make_row("1.1", "A", 100, "Cft", 10, 1000),
            make_row("1.2", "B", 200, "Cft", 5, 1000),
        ]
        sec.subtotal = 2000.0
        result = self.validator.validate_section(sec)
        assert result.is_valid

    def test_invalid_subtotal(self):
        sec = Section(number="1", title="Foundation", depth=0)
        sec.items = [make_row("1.1", "A", 100, "Cft", 10, 1000)]
        sec.subtotal = 5000.0  # Wrong
        result = self.validator.validate_section(sec)
        assert not result.is_valid
        assert result.error_pct is not None

    def test_no_subtotal_declared(self):
        sec = Section(number="1", title="Foundation", depth=0)
        sec.items = [make_row()]
        result = self.validator.validate_section(sec)
        assert result.is_valid
        assert result.declared_subtotal is None
