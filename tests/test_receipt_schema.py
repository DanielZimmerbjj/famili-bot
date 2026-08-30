import json
from decimal import Decimal

from family_bot.services.receipt_ai import ExtractedItem, ReceiptExtraction


def test_receipt_schema_has_all_fields_required_for_structured_output() -> None:
    schema = ReceiptExtraction.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    item_schema = schema["$defs"]["ExtractedItem"]
    assert set(item_schema["required"]) == set(item_schema["properties"])


def test_item_model_is_strict() -> None:
    assert ExtractedItem.model_config["extra"] == "forbid"


def test_receipt_schema_avoids_unsupported_decimal_patterns() -> None:
    schema = ReceiptExtraction.model_json_schema()
    serialized = json.dumps(schema)
    assert "(?=" not in serialized
    assert "(?!" not in serialized
    assert "pattern" not in serialized
    assert schema["properties"]["total"]["type"] == "string"
    item_schema = schema["$defs"]["ExtractedItem"]
    assert item_schema["properties"]["quantity"]["type"] == "string"


def test_receipt_schema_preserves_decimal_strings_exactly() -> None:
    extraction = ReceiptExtraction.model_validate(
        {
            "document_type": "receipt",
            "merchant": "7-Eleven",
            "purchased_at": None,
            "currency": "thb",
            "subtotal": "15.25",
            "discount": "0",
            "tax": "0",
            "total": "15.25",
            "items": [
                {
                    "raw_name": "Milk",
                    "quantity": "1",
                    "unit_price": "15.25",
                    "line_total": "15.25",
                    "category_key": "groceries_household",
                    "subcategory_key": None,
                    "confidence": 0.99,
                }
            ],
            "warnings": [],
            "overall_confidence": 0.99,
        }
    )
    assert extraction.total == Decimal("15.25")
    assert extraction.items[0].unit_price == Decimal("15.25")
