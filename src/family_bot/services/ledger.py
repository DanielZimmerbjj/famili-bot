from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from family_bot.models import (
    BudgetAllocation,
    BudgetCycle,
    Category,
    Household,
    IncomeSource,
    LedgerEntry,
    SavingsGoal,
)
from family_bot.services.money import RateQuote, quantize
from family_bot.services.rates import RateService


class LedgerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PostedEntry:
    entry: LedgerEntry
    source_rate: RateQuote
    envelope_rate: RateQuote | None


async def get_category(session: AsyncSession, household_id: str, key: str) -> Category:
    category = await session.scalar(
        select(Category).where(
            Category.household_id == household_id,
            Category.key == key,
            Category.active.is_(True),
        )
    )
    if category is None:
        raise LedgerError(f"Unknown category: {key}")
    return category


async def post_expense(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    cycle: BudgetCycle,
    category: Category,
    amount: Decimal,
    currency: str,
    description: str,
    occurred_at: datetime,
    user_id: int | None,
    receipt_id: str | None = None,
) -> PostedEntry:
    if amount <= 0:
        raise LedgerError("Expense amount must be positive")
    source_rate = await rate_service.get_quote(session, currency, occurred_at.date())
    amount_kzt = source_rate.to_kzt(amount)
    envelope_rate = await rate_service.get_quote(
        session, category.envelope_currency, occurred_at.date()
    )
    envelope_amount = envelope_rate.from_kzt(amount_kzt)
    entry = LedgerEntry(
        household_id=household.id,
        cycle_id=cycle.id,
        category_id=category.id,
        receipt_id=receipt_id,
        entry_type="expense",
        description=description,
        original_amount=quantize(amount),
        original_currency=currency,
        amount_kzt=amount_kzt,
        envelope_amount=envelope_amount,
        envelope_currency=category.envelope_currency,
        exchange_rate_id=source_rate.rate_id,
        occurred_at=occurred_at,
        created_by_user_id=user_id,
    )
    session.add(entry)
    await session.flush()
    return PostedEntry(entry=entry, source_rate=source_rate, envelope_rate=envelope_rate)


async def post_income(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    cycle: BudgetCycle,
    amount: Decimal,
    currency: str,
    description: str,
    occurred_at: datetime,
    user_id: int | None,
) -> PostedEntry:
    if amount <= 0:
        raise LedgerError("Income amount must be positive")
    source_rate = await rate_service.get_quote(session, currency, occurred_at.date())
    amount_kzt = source_rate.to_kzt(amount)
    income_source = await match_income_source(session, household.id, amount_kzt)
    entry = LedgerEntry(
        household_id=household.id,
        cycle_id=cycle.id,
        income_source_id=income_source.id if income_source else None,
        entry_type="income",
        description=description,
        original_amount=quantize(amount),
        original_currency=currency,
        amount_kzt=amount_kzt,
        exchange_rate_id=source_rate.rate_id,
        occurred_at=occurred_at,
        created_by_user_id=user_id,
    )
    session.add(entry)
    await session.flush()
    return PostedEntry(entry=entry, source_rate=source_rate, envelope_rate=None)


async def match_income_source(
    session: AsyncSession, household_id: str, amount_kzt: Decimal
) -> IncomeSource | None:
    sources = (
        await session.scalars(
            select(IncomeSource).where(
                IncomeSource.household_id == household_id,
                IncomeSource.active.is_(True),
            )
        )
    ).all()
    if not sources:
        return None
    return min(sources, key=lambda source: abs(Decimal(source.expected_amount) - amount_kzt))


async def post_goal_contribution(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    cycle: BudgetCycle,
    goal_key: str,
    amount: Decimal,
    currency: str,
    occurred_at: datetime,
    user_id: int | None,
) -> PostedEntry:
    goal = await session.scalar(
        select(SavingsGoal).where(
            SavingsGoal.household_id == household.id,
            SavingsGoal.key == goal_key,
            SavingsGoal.active.is_(True),
        )
    )
    if goal is None:
        raise LedgerError(f"Unknown savings goal: {goal_key}")
    source_rate = await rate_service.get_quote(session, currency, occurred_at.date())
    amount_kzt = source_rate.to_kzt(amount)
    entry = LedgerEntry(
        household_id=household.id,
        cycle_id=cycle.id,
        goal_id=goal.id,
        entry_type="goal_contribution",
        description=f"Пополнение цели «{goal.name}»",
        original_amount=quantize(amount),
        original_currency=currency,
        amount_kzt=amount_kzt,
        exchange_rate_id=source_rate.rate_id,
        occurred_at=occurred_at,
        created_by_user_id=user_id,
    )
    session.add(entry)
    await session.flush()
    return PostedEntry(entry=entry, source_rate=source_rate, envelope_rate=None)


async def post_goal_expense(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    cycle: BudgetCycle,
    goal_key: str,
    amount: Decimal,
    currency: str,
    description: str,
    occurred_at: datetime,
    user_id: int | None,
) -> PostedEntry:
    goal = await session.scalar(
        select(SavingsGoal).where(
            SavingsGoal.household_id == household.id,
            SavingsGoal.key == goal_key,
            SavingsGoal.active.is_(True),
        )
    )
    if goal is None:
        raise LedgerError(f"Unknown savings goal: {goal_key}")
    source_rate = await rate_service.get_quote(session, currency, occurred_at.date())
    amount_kzt = source_rate.to_kzt(amount)
    available = await goal_balance(session, household.id, goal.id)
    if amount_kzt > available:
        shortage = amount_kzt - available
        raise LedgerError(
            f"В фонде «{goal.name}» не хватает {shortage:,.0f} ₸. "
            "Укажите, из какой обычной категории покрыть разницу."
        )
    entry = LedgerEntry(
        household_id=household.id,
        cycle_id=cycle.id,
        goal_id=goal.id,
        entry_type="goal_expense",
        description=description,
        original_amount=quantize(amount),
        original_currency=currency,
        amount_kzt=amount_kzt,
        exchange_rate_id=source_rate.rate_id,
        occurred_at=occurred_at,
        created_by_user_id=user_id,
    )
    session.add(entry)
    await session.flush()
    return PostedEntry(entry=entry, source_rate=source_rate, envelope_rate=None)


async def goal_balance(session: AsyncSession, household_id: str, goal_id: str) -> Decimal:
    value = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (LedgerEntry.entry_type == "goal_contribution", LedgerEntry.amount_kzt),
                        (LedgerEntry.entry_type == "goal_expense", -LedgerEntry.amount_kzt),
                        else_=0,
                    )
                ),
                0,
            )
        ).where(
            LedgerEntry.household_id == household_id,
            LedgerEntry.goal_id == goal_id,
            LedgerEntry.entry_type.in_(("goal_contribution", "goal_expense")),
            LedgerEntry.status == "posted",
        )
    )
    return Decimal(value or 0)


async def allocation_and_spend(
    session: AsyncSession, cycle_id: str
) -> list[tuple[Category, Decimal, Decimal]]:
    statement = (
        select(
            Category,
            BudgetAllocation.amount,
            func.coalesce(func.sum(LedgerEntry.envelope_amount), 0),
        )
        .join(BudgetAllocation, BudgetAllocation.category_id == Category.id)
        .outerjoin(
            LedgerEntry,
            (LedgerEntry.category_id == Category.id)
            & (LedgerEntry.cycle_id == cycle_id)
            & (LedgerEntry.entry_type == "expense")
            & (LedgerEntry.status == "posted"),
        )
        .where(BudgetAllocation.cycle_id == cycle_id)
        .group_by(Category.id, BudgetAllocation.amount)
        .order_by(Category.sort_order)
    )
    rows = (await session.execute(statement)).all()
    return [(category, Decimal(limit), Decimal(spent or 0)) for category, limit, spent in rows]


async def cycle_rollover_kzt(
    session: AsyncSession,
    rate_service: RateService,
    cycle_id: str,
    on_date: date,
) -> Decimal:
    total = Decimal("0")
    for category, limit, spent in await allocation_and_spend(session, cycle_id):
        remaining = max(limit - spent, Decimal("0"))
        if not remaining:
            continue
        quote = await rate_service.get_quote(session, category.envelope_currency, on_date)
        total += quote.to_kzt(remaining)
    return quantize(total)


async def cycle_cash_remainder_kzt(
    session: AsyncSession,
    cycle_id: str,
) -> Decimal:
    """Return money that is still free at the end of a financial cycle.

    Income increases the free balance. Posted expenses and transfers that were
    already made into savings decrease it. Goal expenses are intentionally not
    included: they are paid from a separate savings balance, not from the
    current month's free cash.
    """

    amount = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (LedgerEntry.entry_type == "income", LedgerEntry.amount_kzt),
                        (
                            LedgerEntry.entry_type.in_(("expense", "goal_contribution")),
                            -LedgerEntry.amount_kzt,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        ).where(
            LedgerEntry.cycle_id == cycle_id,
            LedgerEntry.status == "posted",
        )
    )
    return quantize(max(Decimal(amount or 0), Decimal("0")))
