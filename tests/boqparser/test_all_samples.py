"""
End-to-End Regression Test across test_documents.

Verifies that BOQParser and DocumentProcessor parse sample documents natively
and that the extracted output strictly complies with the output.json blueprint contract.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from boqparser.parser import BOQParser
from boqparser.schemas.output_schema import OutputSchemaValidator


class TestAllSampleDocuments(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.test_docs_dir = cls.repo_root / "test_documents"
        cls.parser = BOQParser(fallback_mode=True)

    def test_house_maluleke_sample(self):
        """Test HOUSE-MALULEKE_BoQ Print.pdf (source of output.json blueprint)."""
        pdf_path = self.test_docs_dir / "HOUSE-MALULEKE_BoQ Print.pdf"
        self.assertTrue(pdf_path.exists(), f"File missing: {pdf_path}")

        # Parse pages 32-33 (the pages corresponding to blueprint items 1-12)
        res = self.parser.parse(str(pdf_path), pages=[32, 33])
        blueprint = res.to_blueprint_dict()

        is_valid, errors = OutputSchemaValidator.validate_dict(blueprint)
        self.assertTrue(is_valid, f"Validation errors: {errors}")
        OutputSchemaValidator.assert_valid(blueprint)

        # Check sections exist
        self.assertGreaterEqual(len(blueprint["sections"]), 1)
        total_items = sum(len(s["items"]) for s in blueprint["sections"])
        self.assertGreaterEqual(total_items, 1)

    def test_rev_report_2_boq(self):
        """Test Rev.Report 2 - BOQ.pdf."""
        pdf_path = self.test_docs_dir / "Rev.Report 2 - BOQ.pdf"
        if not pdf_path.exists():
            self.skipTest(f"File missing: {pdf_path}")

        res = self.parser.parse(str(pdf_path), pages=[1, 2])
        blueprint = res.to_blueprint_dict()

        is_valid, errors = OutputSchemaValidator.validate_dict(blueprint)
        self.assertTrue(is_valid, f"Validation errors: {errors}")
        OutputSchemaValidator.assert_valid(blueprint)

    def test_rev_report_5_rate_analysis(self):
        """Test Rev.Report 5 - Rate analyis.pdf."""
        pdf_path = self.test_docs_dir / "Rev.Report 5 - Rate analyis.pdf"
        if not pdf_path.exists():
            self.skipTest(f"File missing: {pdf_path}")

        # Parse sample pages 1-2
        res = self.parser.parse(str(pdf_path), pages=[1, 2])
        blueprint = res.to_blueprint_dict()

        is_valid, errors = OutputSchemaValidator.validate_dict(blueprint)
        self.assertTrue(is_valid, f"Validation errors: {errors}")
        OutputSchemaValidator.assert_valid(blueprint)

    def test_rev_report_8_stagewise(self):
        """Test Rev.Report 8 - Stagewise report.pdf."""
        pdf_path = self.test_docs_dir / "Rev.Report 8 - Stagewise report.pdf"
        if not pdf_path.exists():
            self.skipTest(f"File missing: {pdf_path}")

        res = self.parser.parse(str(pdf_path), pages=[1, 2])
        blueprint = res.to_blueprint_dict()

        is_valid, errors = OutputSchemaValidator.validate_dict(blueprint)
        self.assertTrue(is_valid, f"Validation errors: {errors}")
        OutputSchemaValidator.assert_valid(blueprint)

    def test_jda_hillbrow_sample(self):
        """Test JDA-HILLBROW_BoQ Print.pdf."""
        pdf_path = self.test_docs_dir / "JDA-HILLBROW_BoQ Print.pdf"
        if not pdf_path.exists():
            self.skipTest(f"File missing: {pdf_path}")

        res = self.parser.parse(str(pdf_path), pages=[10, 11])
        blueprint = res.to_blueprint_dict()

        is_valid, errors = OutputSchemaValidator.validate_dict(blueprint)
        self.assertTrue(is_valid, f"Validation errors: {errors}")
        OutputSchemaValidator.assert_valid(blueprint)


if __name__ == "__main__":
    unittest.main()
