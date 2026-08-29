from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, SavingsGoal
from family_bot.services.cycles import get_current_cycle, seed_household
from family_bot.services.ledger import get_category, post_expense, post_goal_contribution
from family_bot.services.money import RateQuote
from family_bot.services.reports import build_report


class FixedRates:
    async def get_quote(self, session: object, currency: str, on_date: date) -> RateQuote:
        rate = Decimal("1") if currency == "KZT" else Decimal("14")
        return RateQuote(currency, on_date, Decimal("1"), rate)


async def test_daily_report_shows_category_and_goal_remaining_amounts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    rates = FixedRates()
    report_date = date(2026, 8, 29)
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)

    async with factory() as session, session.begin():
        household = await seed_household(session, settings, -100777, 42)
        cycle = await get_current_cycle(session, household, report_date, 5)
        groceries = await get_category(session, household.id, "groceries_household")
        await post_expense(
            session,
            rates,
            household,
            cycle,
            groceries,
            Decimal("100"),
            "THB",
            "молоко",
            now,
            42,
        )
        laptop = SavingsGoal(
            household_id=household.id,
            key="custom_laptop",
            name="Ноутбук",
            icon="💻",
            target_amount=Decimal("1000000"),
            monthly_target=Decimal("0"),
            currency="KZT",
        )
        session.add(laptop)
        await session.flush()
        await post_goal_contribution(
            session,
            rates,
            household,
            cycle,
            laptop.key,
            Decimal("300000"),
            "KZT",
            now,
            42,
        )

        report = await build_report(session, rates, household, cycle, report_date)

    assert "Продукты и бытовая химия" in report
    assert "осталось 9 900 ฿" in report
    assert "Ноутбук" in report
    assert "осталось 700 000 KZT" in report
    await engine.dispose()
