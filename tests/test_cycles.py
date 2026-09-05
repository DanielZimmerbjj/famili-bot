from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.models import Base, BudgetCycle, Household
from family_bot.services.cycles import cycle_dates, get_current_cycle


def test_cycle_after_salary_day() -> None:
    assert cycle_dates(date(2026, 8, 29), 5) == (
        date(2026, 8, 5),
        date(2026, 9, 4),
    )


def test_cycle_before_salary_day() -> None:
    assert cycle_dates(date(2026, 9, 2), 5) == (
        date(2026, 8, 5),
        date(2026, 9, 4),
    )


def test_first_day_cycle_supported() -> None:
    assert cycle_dates(date(2026, 9, 1), 1) == (
        date(2026, 9, 1),
        date(2026, 9, 30),
    )


async def test_open_cycle_does_not_roll_over_until_explicitly_closed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        household = Household(name="Family")
        session.add(household)
        await session.flush()
        august = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        september_request = await get_current_cycle(
            session, household, date(2026, 9, 5), 5
        )
        assert september_request.id == august.id
        assert await session.scalar(select(func.count(BudgetCycle.id))) == 1

        august.status = "closed"
        september = await get_current_cycle(session, household, date(2026, 9, 5), 5)
        assert september.id != august.id
        assert september.start_date == date(2026, 9, 5)

    await engine.dispose()


async def test_pending_calendar_cycle_activates_only_after_open_cycle_closes() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        household = Household(name="Family")
        session.add(household)
        await session.flush()
        august = BudgetCycle(
            household_id=household.id,
            start_date=date(2026, 8, 5),
            end_date=date(2026, 9, 4),
            status="open",
        )
        september = BudgetCycle(
            household_id=household.id,
            start_date=date(2026, 9, 5),
            end_date=date(2026, 10, 4),
            status="pending",
        )
        session.add_all([august, september])
        await session.flush()

        assert (
            await get_current_cycle(session, household, date(2026, 9, 5), 5)
        ).id == august.id
        august.status = "closed"
        activated = await get_current_cycle(session, household, date(2026, 9, 5), 5)
        assert activated.id == september.id
        assert activated.status == "open"

    await engine.dispose()
