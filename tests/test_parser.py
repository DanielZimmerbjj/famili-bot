from family_bot.services.text_parser import match_category


def test_manual_receipt_category_aliases() -> None:
    assert match_category("перенеси в бытовую химию") == "groceries_household"
    assert match_category("это были памперсы") == "baby"
