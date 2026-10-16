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

    def test_house_maluleke_full_document_reconciliation(self):
        """Full 62-page extraction and reconciliation test for HOUSE-MALULEKE_BoQ Print.pdf."""
        pdf_path = self.test_docs_dir / "HOUSE-MALULEKE_BoQ Print.pdf"
        self.assertTrue(pdf_path.exists(), f"File missing: {pdf_path}")

        res = self.parser.parse(str(pdf_path))
        blueprint = res.to_blueprint_dict()

        # 1. Blueprint schema validity
        OutputSchemaValidator.assert_valid(blueprint)

        # 2. Main sections hierarchy (3 sections)
        sec_codes = [s["section_code"] for s in blueprint["sections"]]
        self.assertEqual(sec_codes, ["1", "2", "3"])
        sec_titles = [s["section_title"] for s in blueprint["sections"]]
        self.assertIn("PRELIMINARIES", sec_titles[0].upper())
        self.assertIn("BUILDING WORKS", sec_titles[1].upper())
        self.assertIn("PROVISIONAL SUMS", sec_titles[2].upper())

        # 3. Building Works bill structure (Bills 1 to 13)
        building_sec = res.sections[1]
        bill_codes = [b.section_code for b in building_sec.children]
        expected_bills = [str(i) for i in range(1, 14)]
        self.assertEqual(bill_codes, expected_bills)

        # 4. Mathematical totals & reconciliation
        recon = res.reconciliation
        self.assertIsNotNone(recon)
        self.assertEqual(recon["declared_preliminaries"], 200000.0)
        self.assertEqual(recon["computed_preliminaries"], 200000.0)
        self.assertEqual(recon["declared_building_works"], 3967271.5)
        self.assertEqual(recon["computed_building_works"], 3967271.5)
        self.assertEqual(recon["declared_provisional_sums"], 262500.0)
        self.assertEqual(recon["computed_provisional_sums"], 262500.0)
        self.assertEqual(recon["declared_subtotal"], 4429771.5)
        self.assertEqual(recon["computed_items_subtotal"], 4429771.5)
        self.assertEqual(recon["contingency"], 80000.0)
        self.assertEqual(recon["vat"], 676465.73)
        self.assertEqual(recon["declared_final_total"], 5186237.23)
        self.assertTrue(res.validation_summary.reconciliation_valid)

        # 5. Problem C check: Verify no merged descriptions
        all_descs = [it["description"] for s in blueprint["sections"] for it in s["items"]]
        for desc in all_descs:
            self.assertNotIn("One brick wall BRICKWORK SUNDRIES", desc)

        # 6. Problem D check: Verify page numbers not assigned as quantities
        for s in blueprint["sections"]:
            for it in s["items"]:
                if "Earthwork" in it["description"]:
                    self.assertNotEqual(it["quantity"], 33.0)

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
