from datetime import date
from decimal import Decimal

import pytest

from family_bot.config import Settings
from family_bot.services.money import RateQuote, convert, normalize_currency


def test_vnd_nominal_is_applied() -> None:
    quote = RateQuote(
        currency="VND",
        rate_date=date(2026, 8, 29),
        nominal=Decimal("1000"),
        rate_kzt=Decimal("17.83"),
    )
    assert quote.to_kzt(Decimal("500000")) == Decimal("8915.00000000")


def test_vnd_to_thb_through_kzt() -> None:
    vnd = RateQuote("VND", date(2026, 8, 29), Decimal("1000"), Decimal("17.83"))
    thb = RateQuote("THB", date(2026, 8, 29), Decimal("1"), Decimal("14.11"))
    assert convert(Decimal("500000"), vnd, thb) == Decimal("631.82140326")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("бат", "THB"), ("₫", "VND"), ("тенге", "KZT"), ("RUB", "RUB")],
)
def test_currency_aliases(raw: str, expected: str) -> None:
    assert normalize_currency(raw) == expected


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://"])
def test_coolify_postgres_url_uses_async_driver(scheme: str) -> None:
    settings = Settings(database_url=f"{scheme}user:password@database:5432/family")
    assert settings.database_url.startswith("postgresql+asyncpg://")
