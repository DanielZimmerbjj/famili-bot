from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, ExchangeRate
from family_bot.services.money import RateQuote, convert, normalize_currency
from family_bot.services.rates import RateService


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


async def test_rate_service_fetches_exact_date_instead_of_reusing_recent_cache() -> None:
    class FreshRateClient:
        calls = 0

        async def fetch(self, currency: str, requested_date: date) -> RateQuote:
            self.calls += 1
            return RateQuote(
                currency=currency,
                rate_date=requested_date,
                nominal=Decimal("1"),
                rate_kzt=Decimal("14.11"),
                provider="NBK",
            )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = FreshRateClient()
    async with factory() as session, session.begin():
        session.add(
            ExchangeRate(
                rate_date=date(2026, 8, 29),
                currency="THB",
                nominal=Decimal("1"),
                rate_kzt=Decimal("14"),
                provider="NBK",
            )
        )
    async with factory() as session, session.begin():
        quote = await RateService(client=client).get_quote(  # type: ignore[arg-type]
            session,
            "THB",
            date(2026, 8, 30),
        )

    assert client.calls == 1
    assert quote.rate_date == date(2026, 8, 30)
    assert quote.rate_kzt == Decimal("14.11")
    await engine.dispose()
