"""
Strict Schema Validator for output.json blueprint.

Validates that all serialized outputs conform strictly to the required schema:
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

from typing import Any, Dict, List, Tuple


class SchemaValidationError(ValueError):
    """Raised when an object violates the output.json blueprint."""
    pass


class OutputSchemaValidator:
    """Enforces the authoritative output.json blueprint contract."""

    REQUIRED_ROOT_KEYS = {"sections"}
    REQUIRED_SECTION_KEYS = {"section_code", "section_title", "items"}
    REQUIRED_ITEM_KEYS = {
        "item_no",
        "description",
        "unit",
        "quantity",
        "net_rate",
        "gross_rate",
        "amount",
        "materials",
    }

    @classmethod
    def validate_dict(cls, data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate a dictionary against the blueprint schema.
        Returns (is_valid, list_of_error_messages).
        """
        errors: List[str] = []

        if not isinstance(data, dict):
            return False, ["Root must be a JSON object (dict)."]

        if "sections" not in data:
            errors.append("Root missing required key: 'sections'.")
        elif not isinstance(data["sections"], list):
            errors.append("'sections' must be a list.")
        else:
            for s_idx, sec in enumerate(data["sections"]):
                if not isinstance(sec, dict):
                    errors.append(f"Section at index {s_idx} must be an object.")
                    continue

                # Check required section keys
                missing_sec_keys = cls.REQUIRED_SECTION_KEYS - set(sec.keys())
                if missing_sec_keys:
                    errors.append(f"Section {s_idx} missing required keys: {sorted(missing_sec_keys)}")

                if "items" in sec:
                    if not isinstance(sec["items"], list):
                        errors.append(f"Section {s_idx} 'items' must be a list.")
                    else:
                        for i_idx, item in enumerate(sec["items"]):
                            if not isinstance(item, dict):
                                errors.append(f"Section {s_idx}, item {i_idx} must be an object.")
                                continue

                            # Check required item keys
                            missing_item_keys = cls.REQUIRED_ITEM_KEYS - set(item.keys())
                            if missing_item_keys:
                                errors.append(
                                    f"Section {s_idx}, item {i_idx} (item_no='{item.get('item_no')}') "
                                    f"missing required keys: {sorted(missing_item_keys)}"
                                )

                            # Type checks
                            for num_field in ("quantity", "net_rate", "gross_rate", "amount"):
                                val = item.get(num_field)
                                if val is not None and not isinstance(val, (int, float)):
                                    errors.append(
                                        f"Section {s_idx}, item {i_idx} field '{num_field}' must be float, int, or null, got {type(val).__name__}"
                                    )

                            for str_field in ("item_no", "description", "unit"):
                                val = item.get(str_field)
                                if val is not None and not isinstance(val, str):
                                    errors.append(
                                        f"Section {s_idx}, item {i_idx} field '{str_field}' must be str or null, got {type(val).__name__}"
                                    )

                            if "materials" in item and not isinstance(item["materials"], list):
                                errors.append(f"Section {s_idx}, item {i_idx} 'materials' must be a list.")

        return len(errors) == 0, errors

    @classmethod
    def assert_valid(cls, data: Dict[str, Any]) -> None:
        """Raise SchemaValidationError if data fails validation."""
        is_valid, errors = cls.validate_dict(data)
        if not is_valid:
            raise SchemaValidationError(
                f"Data does not conform to output.json blueprint. Errors:\n" + "\n".join(f" - {e}" for e in errors[:20])
            )
