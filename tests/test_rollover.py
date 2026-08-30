from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, LedgerEntry
from family_bot.services.cycles import get_current_cycle, seed_default_household
from family_bot.services.ledger import cycle_cash_remainder_kzt, cycle_rollover_kzt
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


async def test_cash_remainder_uses_real_income_expenses_and_existing_savings() -> None:
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

        def entry(kind: str, amount: str, *, status: str = "posted") -> LedgerEntry:
            return LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                entry_type=kind,
                description=kind,
                original_amount=Decimal(amount),
                original_currency="KZT",
                amount_kzt=Decimal(amount),
                status=status,
            )

        session.add_all(
            [
                entry("income", "1000000"),
                entry("expense", "250000"),
                entry("goal_contribution", "300000"),
                entry("goal_expense", "50000"),
                entry("expense", "999999", status="reversed"),
            ]
        )
        await session.flush()

        assert await cycle_cash_remainder_kzt(session, cycle.id) == Decimal("450000")
    await engine.dispose()


async def test_cash_remainder_never_becomes_negative() -> None:
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
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                entry_type="expense",
                description="перерасход",
                original_amount=Decimal("1000"),
                original_currency="KZT",
                amount_kzt=Decimal("1000"),
            )
        )
        await session.flush()

        assert await cycle_cash_remainder_kzt(session, cycle.id) == Decimal("0")
    await engine.dispose()
