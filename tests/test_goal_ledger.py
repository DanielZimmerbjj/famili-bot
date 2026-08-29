from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, SavingsGoal
from family_bot.services.cycles import get_current_cycle, seed_default_household
from family_bot.services.ledger import (
    LedgerError,
    goal_balance,
    post_goal_contribution,
    post_goal_expense,
)
from family_bot.services.rates import RateService


async def test_goal_contribution_and_expense_change_balance() -> None:
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
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        goal = await session.scalar(
            select(SavingsGoal).where(
                SavingsGoal.household_id == household.id,
                SavingsGoal.key == "border_run",
            )
        )
        assert goal is not None
        now = datetime(2026, 8, 29, 12, tzinfo=UTC)
        rates = RateService()

        await post_goal_contribution(
            session, rates, household, cycle, "border_run", Decimal("100000"), "KZT", now, 42
        )
        await post_goal_expense(
            session,
            rates,
            household,
            cycle,
            "border_run",
            Decimal("40000"),
            "KZT",
            "билет",
            now,
            42,
        )
        assert await goal_balance(session, household.id, goal.id) == Decimal("60000")

        with pytest.raises(LedgerError, match="не хватает"):
            await post_goal_expense(
                session,
                rates,
                household,
                cycle,
                "border_run",
                Decimal("70000"),
                "KZT",
                "билет",
                now,
                42,
            )

    await engine.dispose()
