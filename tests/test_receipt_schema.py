from family_bot.services.receipt_ai import ExtractedItem, ReceiptExtraction


def test_receipt_schema_has_all_fields_required_for_structured_output() -> None:
    schema = ReceiptExtraction.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    item_schema = schema["$defs"]["ExtractedItem"]
    assert set(item_schema["required"]) == set(item_schema["properties"])


def test_item_model_is_strict() -> None:
    assert ExtractedItem.model_config["extra"] == "forbid"
