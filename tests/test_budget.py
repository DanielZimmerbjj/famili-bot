from decimal import Decimal

from family_bot.constants import BUDGET_TEMPLATES, EXPECTED_INCOMES
from family_bot.services.reports import progress_bar, status_icon


def test_budget_total() -> None:
    assert sum((item.limit_thb for item in BUDGET_TEMPLATES), Decimal(0)) == Decimal("57100")


def test_income_total() -> None:
    assert sum((amount for _, amount, _ in EXPECTED_INCOMES), Decimal(0)) == Decimal("1650000")


def test_progress_visuals() -> None:
    assert progress_bar(Decimal("50"), Decimal("100")) == "█████░░░░░"
    assert status_icon(Decimal("69"), Decimal("100")) == "🟢"
    assert status_icon(Decimal("70"), Decimal("100")) == "🟡"
    assert status_icon(Decimal("90"), Decimal("100")) == "🔴"
