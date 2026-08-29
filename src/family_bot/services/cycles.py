from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from family_bot.config import Settings
from family_bot.constants import (
    BORDER_RUN_MONTHLY_KZT,
    BORDER_RUN_TARGET_KZT,
    BUDGET_TEMPLATES,
    CAR_MONTHLY_TARGET_KZT,
    CAR_TARGET_KZT,
    EXPECTED_INCOMES,
    MANDATORY_MONTHLY_KZT,
)
from family_bot.models import (
    BudgetAllocation,
    BudgetCycle,
    Category,
    Household,
    IncomeSource,
    Member,
    SavingsGoal,
    Subcategory,
)

SUBCATEGORY_NAMES = {
    "groceries": "Продукты",
    "household": "Бытовая химия",
    "workout": "Тренировка",
    "training_taxi": "Такси до тренировки",
    "baby_formula": "Детское питание",
    "diapers": "Памперсы",
    "baby_misc": "Прочее для младенца",
}


def shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def cycle_dates(current: date, start_day: int) -> tuple[date, date]:
    if current.day >= start_day:
        start_year, start_month = current.year, current.month
    else:
        start_year, start_month = shift_month(current.year, current.month, -1)
    start = date(start_year, start_month, start_day)
    end_year, end_month = shift_month(start_year, start_month, 1)
    end = date(end_year, end_month, start_day) - timedelta(days=1)
    return start, end


async def get_household_by_chat(session: AsyncSession, chat_id: int) -> Household | None:
    return await session.scalar(
        select(Household).where(Household.telegram_chat_id == chat_id, Household.active.is_(True))
    )


async def get_current_cycle(
    session: AsyncSession,
    household: Household,
    on_date: date,
    start_day: int,
) -> BudgetCycle:
    start, end = cycle_dates(on_date, start_day)
    cycle = await session.scalar(
        select(BudgetCycle).where(
            BudgetCycle.household_id == household.id,
            BudgetCycle.start_date == start,
        )
    )
    if cycle is not None:
        return cycle

    cycle = BudgetCycle(
        household_id=household.id,
        start_date=start,
        end_date=end,
        expected_income_kzt=sum((item[1] for item in EXPECTED_INCOMES), Decimal("0")),
        mandatory_kzt=MANDATORY_MONTHLY_KZT,
        car_target_kzt=CAR_MONTHLY_TARGET_KZT,
        border_run_target_kzt=BORDER_RUN_MONTHLY_KZT,
    )
    session.add(cycle)
    await session.flush()

    categories = (
        await session.scalars(
            select(Category).where(Category.household_id == household.id, Category.active.is_(True))
        )
    ).all()
    for category in categories:
        session.add(
            BudgetAllocation(
                cycle_id=cycle.id,
                category_id=category.id,
                amount=category.default_limit,
                currency=category.envelope_currency,
            )
        )
    await session.flush()
    return cycle


async def seed_default_household(session: AsyncSession, settings: Settings) -> Household | None:
    if settings.telegram_allowed_chat_id is None:
        return None

    return await seed_household(
        session,
        settings,
        settings.telegram_allowed_chat_id,
        settings.telegram_owner_user_id,
        settings.telegram_member_user_id,
    )


async def seed_household(
    session: AsyncSession,
    settings: Settings,
    chat_id: int,
    owner_user_id: int | None,
    member_user_id: int | None = None,
) -> Household:
    """Create or complete a family household for a Telegram group."""

    household = await get_household_by_chat(session, chat_id)
    if household is None:
        household = Household(
            name="Семья",
            telegram_chat_id=chat_id,
            timezone=settings.app_timezone,
            base_currency=settings.base_currency,
        )
        session.add(household)
        await session.flush()

    member_specs = (
        (owner_user_id, "owner"),
        (member_user_id, "member"),
    )
    for user_id, role in member_specs:
        if user_id is None:
            continue
        member = await session.scalar(
            select(Member).where(
                Member.household_id == household.id,
                Member.telegram_user_id == user_id,
            )
        )
        if member is None:
            session.add(Member(household_id=household.id, telegram_user_id=user_id, role=role))

    for order, template in enumerate(BUDGET_TEMPLATES):
        category = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == template.key,
            )
        )
        if category is None:
            category = Category(
                household_id=household.id,
                key=template.key,
                name=template.name,
                icon=template.icon,
                envelope_currency="THB",
                default_limit=template.limit_thb,
                sort_order=order,
            )
            session.add(category)
            await session.flush()
        for subcategory_key in template.subcategories:
            existing_subcategory = await session.scalar(
                select(Subcategory).where(
                    Subcategory.category_id == category.id,
                    Subcategory.key == subcategory_key,
                )
            )
            if existing_subcategory is None:
                session.add(
                    Subcategory(
                        category_id=category.id,
                        key=subcategory_key,
                        name=SUBCATEGORY_NAMES[subcategory_key],
                    )
                )

    for name, amount, currency in EXPECTED_INCOMES:
        source = await session.scalar(
            select(IncomeSource).where(
                IncomeSource.household_id == household.id,
                IncomeSource.name == name,
            )
        )
        if source is None:
            session.add(
                IncomeSource(
                    household_id=household.id,
                    name=name,
                    expected_amount=amount,
                    currency=currency,
                )
            )

    goal_specs = (
        ("car", "Автомобиль", "🚙", "goal", CAR_TARGET_KZT, CAR_MONTHLY_TARGET_KZT, None),
        (
            "border_run",
            "Бордерран",
            "🛂",
            "sinking_fund",
            BORDER_RUN_TARGET_KZT,
            BORDER_RUN_MONTHLY_KZT,
            6,
        ),
    )
    for key, name, icon, goal_type, target, monthly, recurrence in goal_specs:
        goal = await session.scalar(
            select(SavingsGoal).where(
                SavingsGoal.household_id == household.id,
                SavingsGoal.key == key,
            )
        )
        if goal is None:
            session.add(
                SavingsGoal(
                    household_id=household.id,
                    key=key,
                    name=name,
                    icon=icon,
                    goal_type=goal_type,
                    target_amount=target,
                    monthly_target=monthly,
                    currency="KZT",
                    recurrence_months=recurrence,
                )
            )

    await session.flush()
    return household
