from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from family_bot.models import BudgetCycle, Household, LedgerEntry, Receipt, SavingsGoal
from family_bot.services.ledger import allocation_and_spend, goal_balance
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
    pending_count = int(
        await session.scalar(
            select(func.count(Receipt.id)).where(
                Receipt.household_id == household.id,
                Receipt.status.in_(("needs_review", "failed", "rate_pending")),
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
    car_gap = Decimal(cycle.car_target_kzt) - projected_car

    lines = [
        f"🌙 <b>Итоги за {local_date.strftime('%d.%m.%Y')}</b>",
        "",
        f"Сегодня потрачено: <b>{format_money(day_spent_kzt)} ₸</b>",
        f"Чеков: {receipt_count} · На проверке: {pending_count}",
        "",
        "<b>Бюджет месяца</b>",
    ]
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
            f"Осталось: <b>{format_money(total_remaining)} ฿</b> · "
            f"≈ {format_money(remaining_kzt)} ₸",
            "",
            f"Доход получен: {format_money(income_received)} / "
            f"{format_money(cycle.expected_income_kzt)} ₸",
            f"Обязательства: {format_money(cycle.mandatory_kzt)} ₸",
            f"Прогноз на автомобиль: <b>{format_money(projected_car)} ₸</b>",
        ]
    )
    if car_gap > 0:
        lines.append(
            f"⚠️ До плана {format_money(cycle.car_target_kzt)} ₸ "
            f"не хватает ≈ {format_money(car_gap)} ₸"
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
        if target > 0:
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
    if local_date >= cycle.end_date and cycle.status == "open":
        lines.extend(
            [
                "",
                "<b>Финансовый месяц завершён.</b>",
                "Выберите под отчётом: добавить остаток к машине или оставить резервом.",
            ]
        )
    return "\n".join(lines)


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
