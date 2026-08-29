from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.constants import BUDGET_TEMPLATES
from family_bot.models import Base, Category
from family_bot.services.cycles import get_current_cycle, seed_default_household, seed_household


async def test_seed_and_cycle() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_allowed_chat_id=-100123,
        telegram_owner_user_id=42,
        telegram_member_user_id=43,
    )
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        assert cycle.expected_income_kzt == 1_650_000
        category_count = await session.scalar(
            select(func.count(Category.id)).where(Category.household_id == household.id)
        )
        assert category_count == len(BUDGET_TEMPLATES)
    await engine.dispose()


async def test_seed_household_from_group_setup() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    async with factory() as session, session.begin():
        household = await seed_household(session, settings, -100777, 42)
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        assert household.telegram_chat_id == -100777
        assert cycle.household_id == household.id
        category_count = await session.scalar(
            select(func.count(Category.id)).where(Category.household_id == household.id)
        )
        assert category_count == len(BUDGET_TEMPLATES)
    await engine.dispose()
