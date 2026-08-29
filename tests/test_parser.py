from decimal import Decimal

from family_bot.services.text_parser import parse_text_intent


def test_parse_thb_expense() -> None:
    intent = parse_text_intent("такси 180 бат")
    assert intent is not None
    assert intent.kind == "expense"
    assert intent.category_key == "transport"
    assert intent.amount == Decimal("180")
    assert intent.currency == "THB"


def test_parse_vnd_expense() -> None:
    intent = parse_text_intent("кафе 500 000 донгов")
    assert intent is not None
    assert intent.category_key == "cafe"
    assert intent.amount == Decimal("500000")
    assert intent.currency == "VND"


def test_parse_income() -> None:
    intent = parse_text_intent("получена зарплата 840000 тенге")
    assert intent is not None
    assert intent.kind == "income"
    assert intent.amount == Decimal("840000")


def test_parse_border_run_contribution() -> None:
    intent = parse_text_intent("отложил 50000 тенге на бордерран")
    assert intent is not None
    assert intent.kind == "goal"
    assert intent.goal_key == "border_run"


def test_parse_border_run_expense() -> None:
    intent = parse_text_intent("купил билет на бордерран 300000 тенге")
    assert intent is not None
    assert intent.kind == "goal_expense"
    assert intent.goal_key == "border_run"
