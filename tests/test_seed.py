from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.constants import BUDGET_TEMPLATES
from family_bot.models import Base, BudgetAllocation, BudgetCycle, Category, Household
from family_bot.services.cycles import (
    get_current_cycle,
    seed_all_households,
    seed_default_household,
    seed_household,
)


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
        allocation_count = await session.scalar(
            select(func.count(BudgetAllocation.id)).where(BudgetAllocation.cycle_id == cycle.id)
        )
        assert allocation_count == len(BUDGET_TEMPLATES)
    await engine.dispose()


async def test_reseed_backfills_missing_allocation_in_open_cycle() -> None:
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
        seven_eleven = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        assert seven_eleven is not None
        allocation = await session.scalar(
            select(BudgetAllocation).where(
                BudgetAllocation.cycle_id == cycle.id,
                BudgetAllocation.category_id == seven_eleven.id,
            )
        )
        assert allocation is not None
        await session.delete(allocation)
        await session.flush()

        await seed_household(session, settings, -100777, 42)

        restored = await session.scalar(
            select(BudgetAllocation).where(
                BudgetAllocation.cycle_id == cycle.id,
                BudgetAllocation.category_id == seven_eleven.id,
            )
        )
        assert restored is not None
        assert restored.amount == 3000
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


async def test_seed_all_households_completes_persisted_group_without_env_chat_id() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_allowed_chat_id=None,
    )
    async with factory() as session, session.begin():
        household = Household(name="Existing family", telegram_chat_id=-100888)
        session.add(household)
        await session.flush()
        cycle = BudgetCycle(
            household_id=household.id,
            start_date=date(2026, 8, 5),
            end_date=date(2026, 9, 4),
        )
        session.add(cycle)
        await session.flush()

        seeded = await seed_all_households(session, settings)

        assert [item.id for item in seeded] == [household.id]
        seven_eleven = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        assert seven_eleven is not None
        allocation = await session.scalar(
            select(BudgetAllocation).where(
                BudgetAllocation.cycle_id == cycle.id,
                BudgetAllocation.category_id == seven_eleven.id,
            )
        )
        assert allocation is not None
        assert allocation.amount == 3000
    await engine.dispose()
