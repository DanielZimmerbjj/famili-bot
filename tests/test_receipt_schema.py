import json
from decimal import Decimal

from family_bot.models import ReceiptItem
from family_bot.services.receipt_ai import ExtractedItem, ReceiptExtraction, ReceiptExtractor


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
                    "name_ru": "Молоко",
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
    assert extraction.items[0].raw_name == "Milk"
    assert extraction.items[0].name_ru == "Молоко"


def test_receipt_schema_accepts_free_or_fully_discounted_item() -> None:
    extraction = ReceiptExtraction.model_validate(
        {
            "document_type": "receipt",
            "merchant": "7-Eleven",
            "purchased_at": None,
            "currency": "THB",
            "subtotal": "20",
            "discount": "0",
            "tax": "0",
            "total": "20",
            "items": [
                {
                    "raw_name": "Paid drink",
                    "name_ru": "Напиток",
                    "quantity": "1",
                    "unit_price": "20",
                    "line_total": "20",
                    "category_key": "groceries_household",
                    "subcategory_key": None,
                    "confidence": 0.99,
                },
                {
                    "raw_name": "Promotion gift",
                    "name_ru": "Подарок по акции",
                    "quantity": "1",
                    "unit_price": "0.00",
                    "line_total": "0.00",
                    "category_key": "unknown",
                    "subcategory_key": None,
                    "confidence": 0.6,
                },
            ],
            "warnings": [],
            "overall_confidence": 0.99,
        }
    )

    assert extraction.items[1].unit_price == Decimal("0.00")
    assert extraction.items[1].line_total == Decimal("0.00")


def test_receipt_prompt_requires_russian_item_names_without_thai_script() -> None:
    prompt = ReceiptExtractor._prompt({"seven_eleven": ()}, {})

    assert "name_ru" in prompt
    assert "natural Russian translation" in prompt
    assert "Do not copy Thai or Vietnamese script into name_ru" in prompt


def test_receipt_item_uses_russian_name_and_keeps_legacy_fallback() -> None:
    translated = ReceiptItem(raw_name="เนสท์เล่ ลาเต้", display_name_ru="Кофе Nestle Latte")
    legacy = ReceiptItem(raw_name="Milk", display_name_ru=None)

    assert translated.display_name == "Кофе Nestle Latte"
    assert translated.raw_name == "เนสท์เล่ ลาเต้"
    assert legacy.display_name == "Milk"
