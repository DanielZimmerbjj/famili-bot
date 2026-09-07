from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from family_bot.models import (
    BudgetAllocation,
    BudgetCycle,
    Category,
    Household,
    LedgerEntry,
    Receipt,
    SavingsGoal,
)
from family_bot.services.ledger import (
    allocation_and_spend,
    cycle_close_breakdown,
    goal_balance,
)
from family_bot.services.rates import RateService
from family_bot.services.receipts import format_money


def progress_bar(spent: Decimal, limit: Decimal, width: int = 10) -> str:
    if limit <= 0:
        return "░" * width
    fraction = max(Decimal("0"), min(spent / limit, Decimal("1")))
    filled = min(width, int(fraction * width))
    return "█" * filled + "░" * (width - filled)


def status_icon(spent: Decimal, limit: Decimal) -> str:
    if limit <= 0 or spent / limit < Decimal("0.7"):
        return "🟢"
    if spent / limit < Decimal("0.9"):
        return "🟡"
    return "🔴"


async def build_report(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    cycle: BudgetCycle,
    local_date: date,
) -> str:
    rows = await allocation_and_spend(session, cycle.id)
    day_start_local = datetime.combine(local_date, time.min).replace(
        tzinfo=ZoneInfo(household.timezone)
    )
    day_end_local = datetime.combine(local_date, time.max).replace(tzinfo=day_start_local.tzinfo)
    day_spent_kzt = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_kzt), 0)).where(
                LedgerEntry.household_id == household.id,
                LedgerEntry.entry_type == "expense",
                LedgerEntry.status == "posted",
                LedgerEntry.occurred_at >= day_start_local.astimezone(UTC),
                LedgerEntry.occurred_at <= day_end_local.astimezone(UTC),
            )
        )
        or 0
    )
    receipt_count = int(
        await session.scalar(
            select(func.count(Receipt.id)).where(
                Receipt.household_id == household.id,
                Receipt.posted_at >= day_start_local.astimezone(UTC),
                Receipt.posted_at <= day_end_local.astimezone(UTC),
            )
        )
        or 0
    )
    problem_count = int(
        await session.scalar(
            select(func.count(Receipt.id)).where(
                Receipt.household_id == household.id,
                Receipt.status.in_(("needs_review", "failed", "rate_pending")),
                Receipt.created_at >= day_start_local.astimezone(UTC),
                Receipt.created_at <= day_end_local.astimezone(UTC),
            )
        )
        or 0
    )
    income_received = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_kzt), 0)).where(
                LedgerEntry.cycle_id == cycle.id,
                LedgerEntry.entry_type == "income",
                LedgerEntry.status == "posted",
            )
        )
        or 0
    )
    cycle_spent_kzt = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_kzt), 0)).where(
                LedgerEntry.cycle_id == cycle.id,
                LedgerEntry.entry_type == "expense",
                LedgerEntry.status == "posted",
            )
        )
        or 0
    )

    total_limit = sum((limit for _, limit, _ in rows), Decimal("0"))
    total_spent = sum((spent for _, _, spent in rows), Decimal("0"))
    total_remaining = total_limit - total_spent
    thb_quote = await rate_service.get_quote(session, "THB", local_date)
    remaining_kzt = thb_quote.to_kzt(max(total_remaining, Decimal("0")))
    projected_car = (
        Decimal(cycle.expected_income_kzt)
        - Decimal(cycle.mandatory_kzt)
        - Decimal(cycle.border_run_target_kzt)
        - cycle_spent_kzt
        - remaining_kzt
    )
    close_breakdown = await cycle_close_breakdown(session, cycle)

    lines = [
        f"🌙 <b>Итоги за {local_date.strftime('%d.%m.%Y')}</b>",
        "",
        f"Сегодня потрачено: <b>{format_money(day_spent_kzt)} ₸</b>",
        f"Чеков сегодня: {receipt_count}",
        "",
        "<b>Бюджет месяца</b>",
    ]
    if problem_count:
        lines.insert(
            4,
            f"С ошибкой обработки сегодня: {problem_count} · подтверждение не требуется",
        )
    for category, limit, spent in rows:
        remaining = limit - spent
        remainder_text = (
            f"осталось {format_money(remaining)} ฿"
            if remaining >= 0
            else f"перерасход {format_money(abs(remaining))} ฿"
        )
        lines.append(
            f"{status_icon(spent, limit)} {category.icon} {category.name}\n"
            f"[{progress_bar(spent, limit)}] {format_money(spent)} / "
            f"{format_money(limit)} ฿ · <b>{remainder_text}</b>"
        )
    lines.extend(
        [
            "",
            f"Всего: {format_money(total_spent)} / {format_money(total_limit)} ฿",
            f"По лимитам осталось: <b>{format_money(total_remaining)} ฿</b> · "
            f"≈ {format_money(remaining_kzt)} ₸",
            "",
            f"Доход получен: {format_money(income_received)} / "
            f"{format_money(cycle.expected_income_kzt)} ₸",
            f"Обязательства: {format_money(cycle.mandatory_kzt)} ₸",
            f"Свободно сейчас: <b>{format_money(close_breakdown.transferable_kzt)} ₸</b>",
            f"Плановый остаток на накопления: <b>{format_money(projected_car)} ₸</b>",
            f"Ориентир на машину: {format_money(cycle.car_target_kzt)} ₸ (не обязательный)",
        ]
    )

    goals = (
        await session.scalars(
            select(SavingsGoal)
            .where(SavingsGoal.household_id == household.id, SavingsGoal.active.is_(True))
            .order_by(SavingsGoal.goal_type, SavingsGoal.name)
        )
    ).all()
    for goal in goals:
        balance = await goal_balance(session, household.id, goal.id)
        target = Decimal(goal.target_amount)
        if goal.goal_type == "reserve":
            progress = f"В резерве <b>{format_money(balance)} {goal.currency}</b>"
        elif target > 0:
            remaining = max(target - balance, Decimal("0"))
            progress = (
                f"[{progress_bar(balance, target)}] {format_money(balance)} / "
                f"{format_money(target)} {goal.currency} · "
                f"<b>осталось {format_money(remaining)} {goal.currency}</b>"
            )
        else:
            progress = (
                f"Накоплено <b>{format_money(balance)} {goal.currency}</b> · "
                "общая стоимость не задана"
            )
        lines.extend(["", f"{goal.icon} <b>{goal.name}</b>", progress])
    if local_date > cycle.end_date and cycle.status == "open":
        lines.extend(
            [
                "",
                "<b>Плановая дата окончания месяца прошла.</b>",
                "Месяц всё ещё открыт: новые расходы продолжают в нём учитываться. "
                "Закройте его вручную, когда будете готовы.",
            ]
        )
    return "\n".join(lines)


async def build_spending_answer(
    session: AsyncSession,
    household: Household,
    cycle: BudgetCycle,
    local_date: date,
    period: str,
    focus: str,
    category_key: str | None = None,
) -> str:
    """Build a short, exact answer to a conversational spending question."""

    filters = [
        LedgerEntry.household_id == household.id,
        LedgerEntry.entry_type == "expense",
        LedgerEntry.status == "posted",
    ]
    period_label = "Сегодня"
    if period in {"today", "yesterday"}:
        report_date = local_date
        if period == "yesterday":
            report_date -= timedelta(days=1)
            period_label = "Вчера"
        day_start_local = datetime.combine(report_date, time.min).replace(
            tzinfo=ZoneInfo(household.timezone)
        )
        day_end_local = datetime.combine(report_date, time.max).replace(
            tzinfo=day_start_local.tzinfo
        )
        filters.extend(
            [
                LedgerEntry.occurred_at >= day_start_local.astimezone(UTC),
                LedgerEntry.occurred_at <= day_end_local.astimezone(UTC),
            ]
        )
    else:
        period_label = "В этом финансовом месяце"
        filters.append(LedgerEntry.cycle_id == cycle.id)
    if category_key:
        filters.append(Category.key == category_key)

    category_rows = (
        await session.execute(
            select(
                Category.name,
                Category.icon,
                Category.envelope_currency,
                func.coalesce(func.sum(LedgerEntry.envelope_amount), 0),
                func.coalesce(func.sum(LedgerEntry.amount_kzt), 0),
            )
            .join(Category, Category.id == LedgerEntry.category_id)
            .where(*filters)
            .group_by(
                Category.id,
                Category.name,
                Category.icon,
                Category.envelope_currency,
            )
            .order_by(desc(func.sum(LedgerEntry.amount_kzt)))
        )
    ).all()
    total_kzt = sum((Decimal(row[4]) for row in category_rows), Decimal("0"))
    if not category_rows:
        if category_key:
            category = await session.scalar(
                select(Category).where(
                    Category.household_id == household.id,
                    Category.key == category_key,
                )
            )
            if category is not None:
                return (
                    f"{period_label} по статье {category.icon} <b>{category.name}</b> расходов нет."
                )
        return f"{period_label} расходов пока нет."

    def category_amount(row: tuple[object, ...]) -> str:
        envelope_amount = Decimal(row[3])
        envelope_currency = str(row[2])
        amount_kzt = Decimal(row[4])
        return (
            f"{format_money(envelope_amount)} {envelope_currency} (≈ {format_money(amount_kzt)} ₸)"
        )

    if category_key:
        row = category_rows[0]
        allocation = await session.scalar(
            select(BudgetAllocation.amount)
            .join(Category, Category.id == BudgetAllocation.category_id)
            .where(
                BudgetAllocation.cycle_id == cycle.id,
                Category.household_id == household.id,
                Category.key == category_key,
            )
        )
        line = f"{period_label} на {row[1]} <b>{row[0]}</b> потрачено {category_amount(row)}."
        if period == "current_cycle" and allocation is not None:
            spent = Decimal(row[3])
            limit = Decimal(allocation)
            remaining = limit - spent
            if remaining >= 0:
                line += (
                    f" Осталось <b>{format_money(remaining)} {row[2]}</b> "
                    f"из {format_money(limit)} {row[2]}."
                )
            else:
                line += f" Перерасход <b>{format_money(abs(remaining))} {row[2]}</b>."
        return line

    if focus == "largest_category":
        largest = category_rows[0]
        return (
            f"{period_label} больше всего ушло на {largest[1]} "
            f"<b>{largest[0]}</b> — {category_amount(largest)}.\n"
            f"Всего расходов: <b>{format_money(total_kzt)} ₸</b>."
        )

    if focus == "recent_expenses":
        recent_rows = (
            await session.execute(
                select(LedgerEntry, Category)
                .join(Category, Category.id == LedgerEntry.category_id)
                .where(*filters)
                .order_by(LedgerEntry.occurred_at.desc(), LedgerEntry.created_at.desc())
                .limit(5)
            )
        ).all()
        lines = [f"{period_label} последние расходы:"]
        lines.extend(
            f"• {category.icon} {entry.description}: "
            f"{format_money(entry.original_amount)} {entry.original_currency}"
            for entry, category in recent_rows
        )
        lines.append(f"Всего: <b>{format_money(total_kzt)} ₸</b>.")
        return "\n".join(lines)

    if focus == "category_breakdown":
        lines = [f"{period_label} расходы по категориям:"]
        lines.extend(f"• {row[1]} {row[0]} — {category_amount(row)}" for row in category_rows[:7])
        lines.append(f"Всего: <b>{format_money(total_kzt)} ₸</b>.")
        return "\n".join(lines)

    largest = category_rows[0]
    return (
        f"{period_label} потрачено <b>{format_money(total_kzt)} ₸</b>.\n"
        f"Больше всего — {largest[1]} {largest[0]}: {category_amount(largest)}."
    )


async def build_chart(session: AsyncSession, cycle: BudgetCycle) -> bytes:
    rows = await allocation_and_spend(session, cycle.id)
    labels = [category.name for category, _, _ in rows]
    limits = [float(limit) for _, limit, _ in rows]
    spent = [float(value) for _, _, value in rows]

    height = max(6, len(rows) * 0.45)
    fig, axis = plt.subplots(figsize=(11, height))
    y_positions = list(range(len(labels)))
    axis.barh(y_positions, limits, color="#e5e7eb", label="Лимит")
    axis.barh(y_positions, spent, color="#2563eb", label="Потрачено")
    axis.set_yticks(y_positions, labels=labels)
    axis.invert_yaxis()
    axis.set_xlabel("THB")
    axis.set_title("Семейный бюджет: лимит и расходы")
    axis.legend()
    axis.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    stream = BytesIO()
    fig.savefig(stream, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return stream.getvalue()
