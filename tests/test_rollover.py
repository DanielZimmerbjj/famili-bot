from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base
from family_bot.services.cycles import get_current_cycle, seed_default_household
from family_bot.services.ledger import cycle_rollover_kzt
from family_bot.services.money import RateQuote
from family_bot.services.rates import RateService


class StubRateClient:
    async def fetch(self, currency: str, requested_date: date) -> RateQuote:
        assert currency == "THB"
        return RateQuote(
            currency="THB",
            rate_date=requested_date,
            nominal=Decimal("1"),
            rate_kzt=Decimal("14"),
            provider="NBK",
        )


async def test_full_unused_budget_can_roll_into_savings() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_allowed_chat_id=-100123,
        telegram_owner_user_id=42,
    )
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 9, 4), 5)
        amount = await cycle_rollover_kzt(
            session,
            RateService(client=StubRateClient()),  # type: ignore[arg-type]
            cycle.id,
            date(2026, 9, 4),
        )
        assert amount == Decimal("841400.00000000")
    await engine.dispose()
