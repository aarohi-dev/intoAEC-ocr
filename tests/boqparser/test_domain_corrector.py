"""Tests for DomainCorrector."""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.domain.domain_corrector import DomainCorrector
from boqparser.table.table_reconstructor import BOQRow


class TestUnitNormalization:
    def setup_method(self):
        self.corrector = DomainCorrector()

    def test_cft_normalization(self):
        assert self.corrector.normalize_unit("cft") == "Cft"
        assert self.corrector.normalize_unit("CfT") == "Cft"

    def test_sqft_normalization(self):
        assert self.corrector.normalize_unit("sqft") == "Sqft"
        assert self.corrector.normalize_unit("sft") == "Sqft"

    def test_nos_normalization(self):
        assert self.corrector.normalize_unit("nos") == "Nos."
        assert self.corrector.normalize_unit("no") == "No."

    def test_kg_normalization(self):
        assert self.corrector.normalize_unit("kg") == "Kg"
        assert self.corrector.normalize_unit("kgs") == "Kg"

    def test_mt_normalization(self):
        assert self.corrector.normalize_unit("mt") == "MT"
        assert self.corrector.normalize_unit("ton") == "MT"

    def test_unknown_unit_passthrough(self):
        assert self.corrector.normalize_unit("XYZ") == "XYZ"

    def test_empty_unit(self):
        assert self.corrector.normalize_unit("") == ""


class TestOCRCorrections:
    def setup_method(self):
        self.corrector = DomainCorrector()

    def test_plastering_correction(self):
        result = self.corrector.correct("Plastreing work")
        assert "Plastering" in result

    def test_window_correction(self):
        result = self.corrector.correct("Winodw frame")
        assert "Window" in result

    def test_no_correction_needed(self):
        result = self.corrector.correct("Plastering")
        assert "Plastering" in result

    def test_empty_string(self):
        assert self.corrector.correct("") == ""

    def test_none_passthrough(self):
        assert self.corrector.correct(None) is None

    def test_numbers_not_corrected(self):
        result = self.corrector.correct("66393.25")
        assert result == "66393.25"


class TestRowCorrection:
    def setup_method(self):
        self.corrector = DomainCorrector()

    def test_correct_row(self):
        row = BOQRow(
            item_no="1.1",
            description="Plastreing work",
            quantity=100,
            unit="sqft",
            rate=15,
            amount=1500,
        )
        self.corrector.correct_row(row)
        assert "Plastering" in row.description
        assert row.unit == "Sqft"

    def test_correct_all(self):
        rows = [
            BOQRow(item_no="1", description="Winodw frame", unit="nos", quantity=5, rate=2000, amount=10000),
            BOQRow(item_no="2", description="RCC Work", unit="cft", quantity=100, rate=50, amount=5000),
        ]
        self.corrector.correct_all(rows)
        assert "Window" in rows[0].description
        assert rows[0].unit == "Nos."
        assert rows[1].unit == "Cft"
