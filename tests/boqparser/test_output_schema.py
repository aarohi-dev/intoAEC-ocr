import json
import unittest
from pathlib import Path

from boqparser.models.intermediate import (
    CanonicalDocument,
    CanonicalSection,
    CanonicalItem,
    CanonicalMaterial,
)
from boqparser.schemas.output_schema import OutputSchemaValidator, SchemaValidationError
from boqparser.export.json_exporter import JSONExporter


class TestOutputSchema(unittest.TestCase):

    def test_validate_real_blueprint_file(self):
        """Verify that the user's actual output.json passes OutputSchemaValidator."""
        blueprint_path = Path(__file__).parents[2] / "output.json"
        self.assertTrue(blueprint_path.exists(), f"Blueprint not found at {blueprint_path}")

        with open(blueprint_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        is_valid, errors = OutputSchemaValidator.validate_dict(data)
        self.assertTrue(is_valid, f"Validation failed on real output.json: {errors}")
        OutputSchemaValidator.assert_valid(data)

    def test_canonical_document_serialization(self):
        """Verify that CanonicalDocument exports to valid blueprint format."""
        doc = CanonicalDocument(
            source_file="sample.pdf",
            sections=[
                CanonicalSection(
                    section_code="0066",
                    section_title="BILL NO. 1",
                    items=[
                        CanonicalItem(
                            item_no="1",
                            description="Excavation in earth",
                            unit="m3",
                            quantity=140.0,
                            net_rate=95.0,
                            gross_rate=95.0,
                            amount=13300.0,
                            materials=[
                                CanonicalMaterial(
                                    material_id="MAT-01",
                                    description="Diesel",
                                    unit="Ltr",
                                    quantity=50.0,
                                    rate=90.0,
                                    amount=4500.0,
                                    group_name="Machinery",
                                )
                            ],
                        )
                    ],
                )
            ],
        )

        exporter = JSONExporter(validate=True)
        json_str = exporter.to_string(doc)
        parsed = json.loads(json_str)

        self.assertIn("sections", parsed)
        self.assertEqual(len(parsed["sections"]), 1)
        self.assertEqual(parsed["sections"][0]["section_code"], "0066")
        self.assertEqual(parsed["sections"][0]["items"][0]["item_no"], "1")
        self.assertEqual(parsed["sections"][0]["items"][0]["materials"][0]["material_id"], "MAT-01")

    def test_schema_validator_catches_invalid_structure(self):
        """Verify that invalid field types or missing keys raise SchemaValidationError."""
        bad_data = {
            "sections": [
                {
                    "section_code": "001",
                    # missing section_title and items
                }
            ]
        }
        with self.assertRaises(SchemaValidationError):
            OutputSchemaValidator.assert_valid(bad_data)


if __name__ == "__main__":
    unittest.main()
