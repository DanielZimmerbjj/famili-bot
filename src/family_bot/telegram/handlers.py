from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from family_bot.config import Settings
from family_bot.models import (
    AuditLog,
    BudgetCycle,
    Category,
    CategoryAlias,
    ExchangeRate,
    Household,
    IncomeSource,
    LedgerEntry,
    Receipt,
    ReceiptItem,
    SavingsGoal,
)
from family_bot.services.cycles import get_current_cycle, get_household_by_chat
from family_bot.services.ledger import (
    cycle_rollover_kzt,
    get_category,
    goal_balance,
    post_expense,
    post_goal_contribution,
    post_goal_expense,
    post_income,
)
from family_bot.services.money import CurrencyError, normalize_currency, quantize
from family_bot.services.rates import RateService, RateUnavailableError
from family_bot.services.receipts import ReceiptService, format_money
from family_bot.services.reports import build_chart, build_report
from family_bot.services.text_parser import match_category, parse_text_intent
from family_bot.telegram.keyboards import cycle_close_keyboard

FIX_RE = re.compile(
    r"^/(?:fix|исправить)\s+(?P<receipt>[0-9a-f-]{36})\s+(?P<item>\d+)\s+(?P<category>.+)$",
    re.IGNORECASE,
)
RATE_RE = re.compile(
    r"^/(?:rate|курс)\s+(?P<currency>[A-Za-z]{3})\s+(?P<rate>\d+(?:[.,]\d+)?)"
    r"(?:\s+(?P<nominal>\d+(?:[.,]\d+)?))?$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TelegramDependencies:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    rate_service: RateService
    receipt_service: ReceiptService


def build_router(deps: TelegramDependencies) -> Router:
    router = Router(name="family-budget")

    @router.message(F.text.regexp(r"^/(?:ids|айди)(?:@\w+)?$"))
    async def setup_ids(message: Message) -> None:
        if not deps.settings.setup_mode:
            return
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{user_id}</code>\n"
            "После настройки выключите SETUP_MODE."
        )

    @router.message(F.photo | F.document)
    async def receipt_upload(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, user_id = authorization
        if message.photo:
            file = message.photo[-1]
            file_id = file.file_id
            file_unique_id = file.file_unique_id
            mime_type = "image/jpeg"
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            file_unique_id = message.document.file_unique_id
            mime_type = message.document.mime_type or "image/jpeg"
        else:
            await message.reply("Пришлите чек фотографией или изображением-файлом.")
            return

        local_now = datetime.now(deps.settings.timezone)
        async with deps.session_factory() as session, session.begin():
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            receipt, is_new_receipt, image_added = await deps.receipt_service.enqueue(
                session=session,
                household=household,
                cycle_id=cycle.id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                user_id=user_id,
                file_id=file_id,
                file_unique_id=file_unique_id,
                mime_type=mime_type,
                media_group_id=message.media_group_id,
            )
        if is_new_receipt:
            await message.reply(f"⏳ Чек принят, разбираю · <code>{receipt.id[:8]}</code>")
        elif not image_added:
            await message.reply("ℹ️ Это изображение уже есть в очереди или было обработано.")

    @router.message(
        F.text.regexp(r"^/(?:balance|report|остаток|отчет)(?:@\w+)?$|^(?:остаток|отчет)$")
    )
    async def balance(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, _ = authorization
        local_now = datetime.now(deps.settings.timezone)
        async with deps.session_factory() as session:
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            report = await build_report(
                session, deps.rate_service, household, cycle, local_now.date()
            )
            await session.commit()
        await message.answer(report)

    @router.message(F.text.regexp(r"^/(?:today|сегодня)(?:@\w+)?$|^сегодня$"))
    async def today(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, _ = authorization
        local_now = datetime.now(deps.settings.timezone)
        day_start = datetime.combine(local_now.date(), time.min, deps.settings.timezone)
        day_end = datetime.combine(local_now.date(), time.max, deps.settings.timezone)
        async with deps.session_factory() as session:
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            entries = (
                await session.scalars(
                    select(LedgerEntry)
                    .where(
                        LedgerEntry.cycle_id == cycle.id,
                        LedgerEntry.entry_type == "expense",
                        LedgerEntry.status == "posted",
                        LedgerEntry.occurred_at >= day_start.astimezone(UTC),
                        LedgerEntry.occurred_at <= day_end.astimezone(UTC),
                    )
                    .order_by(LedgerEntry.occurred_at.desc())
                    .limit(30)
                )
            ).all()
        lines = ["<b>Последние расходы</b>"]
        lines.extend(
            f"• {escape(entry.description)}: {format_money(entry.original_amount)} "
            f"{entry.original_currency} · {format_money(entry.amount_kzt)} ₸"
            for entry in entries
        )
        await message.answer("\n".join(lines) if entries else "Сегодня расходов пока нет.")

    @router.message(F.text.regexp(r"^/(?:income|доходы)(?:@\w+)?$|^доходы$"))
    async def incomes(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, _ = authorization
        local_now = datetime.now(deps.settings.timezone)
        async with deps.session_factory() as session:
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            sources = (
                await session.scalars(
                    select(IncomeSource)
                    .where(IncomeSource.household_id == household.id)
                    .order_by(IncomeSource.created_at)
                )
            ).all()
            lines = ["<b>Доходы текущего месяца</b>"]
            total_received = Decimal("0")
            for source in sources:
                received = Decimal(
                    await session.scalar(
                        select(func.coalesce(func.sum(LedgerEntry.amount_kzt), 0)).where(
                            LedgerEntry.cycle_id == cycle.id,
                            LedgerEntry.income_source_id == source.id,
                            LedgerEntry.entry_type == "income",
                            LedgerEntry.status == "posted",
                        )
                    )
                    or 0
                )
                total_received += received
                marker = "✅" if received >= Decimal(source.expected_amount) else "⏳"
                lines.append(
                    f"{marker} {source.name}: {format_money(received)} / "
                    f"{format_money(source.expected_amount)} ₸"
                )
            lines.append(
                f"\nВсего: <b>{format_money(total_received)} / "
                f"{format_money(cycle.expected_income_kzt)} ₸</b>"
            )
            await session.commit()
        await message.answer("\n".join(lines))

    @router.message(F.text.regexp(r"^/(?:chart|график)(?:@\w+)?$|^график$"))
    async def chart(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, _ = authorization
        local_now = datetime.now(deps.settings.timezone)
        async with deps.session_factory() as session:
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            payload = await build_chart(session, cycle)
            await session.commit()
        await message.answer_photo(
            BufferedInputFile(payload, filename="family-budget.png"),
            caption="Лимиты и расходы текущего финансового месяца",
        )

    @router.message(F.text.regexp(r"^/(?:goals?|цель)(?:@\w+)?$|^цель$"))
    async def goals(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None:
            return
        household, _ = authorization
        async with deps.session_factory() as session:
            goal_models = (
                await session.scalars(
                    select(SavingsGoal).where(
                        SavingsGoal.household_id == household.id,
                        SavingsGoal.active.is_(True),
                    )
                )
            ).all()
            lines = ["<b>Накопления</b>"]
            for goal in goal_models:
                value = await goal_balance(session, household.id, goal.id)
                lines.append(
                    f"{goal.icon} {goal.name}: {format_money(value)} / "
                    f"{format_money(goal.target_amount)} {goal.currency}"
                )
        await message.answer("\n".join(lines))

    @router.message(F.text.regexp(r"^/(?:help|помощь)(?:@\w+)?$|^помощь$|^/start"))
    async def help_message(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None and not deps.settings.setup_mode:
            return
        await message.answer(
            "<b>Как пользоваться</b>\n"
            "• Пришлите фотографию чека.\n"
            "• <code>такси 180 бат</code> — ручной расход.\n"
            "• <code>кафе 500000 донгов</code> — расход в другой валюте.\n"
            "• <code>получена зарплата 840000 тенге</code> — доход.\n"
            "• <code>отложил 600000 тенге на машину</code> — накопление.\n"
            "• <code>отложил 50000 тенге на бордерран</code> — фонд.\n\n"
            "• <code>купил билет на бордерран 300000 тенге</code> — списание фонда.\n\n"
            "Команды: /balance /today /income /chart /goal /close /help"
        )

    @router.message(F.text.regexp(r"^/(?:close|rollover|закрыть)(?:@\w+)?$"))
    async def close_cycle(message: Message) -> None:
        authorization = await authorize_message(message, deps, owner_only=True)
        if authorization is None:
            return
        household, _ = authorization
        local_now = datetime.now(deps.settings.timezone)
        try:
            async with deps.session_factory() as session:
                cycle = await get_current_cycle(
                    session,
                    household,
                    local_now.date(),
                    deps.settings.financial_cycle_start_day,
                )
                if cycle.status == "closed":
                    await message.reply("Этот финансовый месяц уже закрыт.")
                    return
                if local_now.date() < cycle.end_date:
                    await message.reply(
                        f"Закрыть месяц можно {cycle.end_date.strftime('%d.%m.%Y')}."
                    )
                    return
                pending = await pending_receipt_count(session, cycle.id)
                if pending:
                    await message.reply(
                        f"Сначала разберите {pending} чек(а/ов), которые ещё не проведены."
                    )
                    return
                amount = await cycle_rollover_kzt(
                    session, deps.rate_service, cycle.id, local_now.date()
                )
                await session.commit()
            await message.reply(
                f"В категориях осталось ≈ <b>{format_money(amount)} ₸</b>. Куда его направить?",
                reply_markup=cycle_close_keyboard(cycle.id),
            )
        except RateUnavailableError:
            await message.reply("Не удалось рассчитать остаток: нет курса к тенге.")

    @router.message(F.text.regexp(FIX_RE))
    async def fix_item(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None or not message.text:
            return
        household, user_id = authorization
        match = FIX_RE.match(message.text.strip())
        if match is None:
            return
        category_text = match.group("category").strip()
        category_key = match_category(category_text) or category_text
        item_number = int(match.group("item"))
        async with deps.session_factory() as session, session.begin():
            receipt = await session.scalar(
                select(Receipt)
                .where(
                    Receipt.id == match.group("receipt"),
                    Receipt.household_id == household.id,
                )
                .options(selectinload(Receipt.items))
            )
            if receipt is None or not (1 <= item_number <= len(receipt.items)):
                await message.reply("Чек или номер позиции не найден.")
                return
            category = await get_category(session, household.id, category_key)
            item = sorted(receipt.items, key=lambda value: value.created_at)[item_number - 1]
            before_category_id = item.category_id
            item.category_id = category.id
            item.subcategory_id = None
            await rebuild_receipt_ledger(session, household, receipt, user_id)
            normalized_name = " ".join(item.raw_name.casefold().split())
            alias = await session.scalar(
                select(CategoryAlias).where(
                    CategoryAlias.household_id == household.id,
                    CategoryAlias.normalized_name == normalized_name,
                )
            )
            if alias is None:
                session.add(
                    CategoryAlias(
                        household_id=household.id,
                        normalized_name=normalized_name,
                        category_id=category.id,
                    )
                )
            else:
                alias.category_id = category.id
                alias.subcategory_id = None
                alias.confirmations += 1
            session.add(
                AuditLog(
                    household_id=household.id,
                    actor_user_id=user_id,
                    action="receipt_item_category_changed",
                    entity_type="receipt_item",
                    entity_id=item.id,
                    before_data={"category_id": before_category_id},
                    after_data={"category_id": category.id},
                )
            )
        await message.reply(f"✅ Позиция {item_number} перенесена в «{category.name}».")

    @router.message(F.text.regexp(RATE_RE))
    async def manual_rate(message: Message) -> None:
        authorization = await authorize_message(message, deps, owner_only=True)
        if authorization is None or not message.text:
            return
        _, _ = authorization
        match = RATE_RE.match(message.text.strip())
        if match is None:
            return
        currency = normalize_currency(match.group("currency"))
        rate = Decimal(match.group("rate").replace(",", "."))
        nominal = Decimal((match.group("nominal") or "1").replace(",", "."))
        if rate <= 0 or nominal <= 0:
            await message.reply("Курс и номинал должны быть положительными.")
            return
        today_local = datetime.now(deps.settings.timezone).date()
        async with deps.session_factory() as session, session.begin():
            existing = await session.scalar(
                select(ExchangeRate).where(
                    ExchangeRate.rate_date == today_local,
                    ExchangeRate.currency == currency,
                    ExchangeRate.provider == "MANUAL",
                )
            )
            if existing is None:
                session.add(
                    ExchangeRate(
                        rate_date=today_local,
                        currency=currency,
                        nominal=nominal,
                        rate_kzt=rate,
                        provider="MANUAL",
                        is_manual=True,
                    )
                )
            else:
                existing.nominal = nominal
                existing.rate_kzt = rate
        await message.reply(
            f"✅ Ручной курс на сегодня: {format_money(nominal)} {currency} = "
            f"{format_money(rate)} ₸"
        )

    @router.message(F.text)
    async def free_text(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None or not message.text:
            return
        household, user_id = authorization
        intent = parse_text_intent(message.text)
        if intent is None:
            return
        local_now = datetime.now(deps.settings.timezone)
        try:
            async with deps.session_factory() as session, session.begin():
                cycle = await get_current_cycle(
                    session,
                    household,
                    local_now.date(),
                    deps.settings.financial_cycle_start_day,
                )
                if intent.kind == "expense" and intent.category_key:
                    category = await get_category(session, household.id, intent.category_key)
                    posted = await post_expense(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        category,
                        intent.amount,
                        intent.currency,
                        intent.description,
                        local_now,
                        user_id,
                    )
                    response = (
                        f"✅ {category.icon} {category.name}: {format_money(intent.amount)} "
                        f"{intent.currency} · {format_money(posted.entry.amount_kzt)} ₸"
                    )
                elif intent.kind == "income":
                    posted = await post_income(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        intent.amount,
                        intent.currency,
                        intent.description,
                        local_now,
                        user_id,
                    )
                    response = f"✅ Доход: {format_money(posted.entry.amount_kzt)} ₸"
                elif intent.kind == "goal" and intent.goal_key:
                    posted = await post_goal_contribution(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        intent.goal_key,
                        intent.amount,
                        intent.currency,
                        local_now,
                        user_id,
                    )
                    response = f"✅ В накопления: {format_money(posted.entry.amount_kzt)} ₸"
                elif intent.kind == "goal_expense" and intent.goal_key:
                    posted = await post_goal_expense(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        intent.goal_key,
                        intent.amount,
                        intent.currency,
                        intent.description,
                        local_now,
                        user_id,
                    )
                    response = f"✅ Из фонда списано: {format_money(posted.entry.amount_kzt)} ₸"
                else:
                    return
            await message.reply(response)
        except RateUnavailableError:
            await message.reply(
                "Не нашёл официальный курс. Операция не проведена. Владелец может задать "
                "курс: <code>/rate XXX курс_к_тенге номинал</code>."
            )
        except (CurrencyError, InvalidOperation, ValueError) as exc:
            await message.reply(f"Не удалось провести операцию: {escape(str(exc))}")

    @router.callback_query(F.data.startswith("cycle:"))
    async def cycle_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.data:
            return
        if not await authorize_callback(callback, deps):
            return
        if callback.from_user.id != deps.settings.telegram_owner_user_id:
            await callback.answer("Закрыть месяц может только владелец.", show_alert=True)
            return
        _, action, cycle_id = callback.data.split(":", 2)
        if action not in {"rollover", "keep"}:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
        local_now = datetime.now(deps.settings.timezone)
        amount = Decimal("0")
        try:
            async with deps.session_factory() as session, session.begin():
                household = await get_household_by_chat(session, callback.message.chat.id)
                if household is None:
                    await callback.answer("Семья не найдена.", show_alert=True)
                    return
                cycle = await session.scalar(
                    select(BudgetCycle)
                    .where(
                        BudgetCycle.id == cycle_id,
                        BudgetCycle.household_id == household.id,
                    )
                    .with_for_update()
                )
                if cycle is None:
                    await callback.answer("Месяц не найден.", show_alert=True)
                    return
                if cycle.status == "closed":
                    await callback.answer("Месяц уже закрыт.", show_alert=True)
                    return
                if local_now.date() < cycle.end_date:
                    await callback.answer("Ещё рано закрывать этот месяц.", show_alert=True)
                    return
                pending = await pending_receipt_count(session, cycle.id)
                if pending:
                    await callback.answer(f"Есть непроведённые чеки: {pending}.", show_alert=True)
                    return
                if action == "rollover":
                    amount = await cycle_rollover_kzt(
                        session, deps.rate_service, cycle.id, local_now.date()
                    )
                    if amount:
                        await post_goal_contribution(
                            session,
                            deps.rate_service,
                            household,
                            cycle,
                            "car",
                            amount,
                            "KZT",
                            local_now,
                            callback.from_user.id,
                        )
                cycle.status = "closed"
                cycle.closed_at = local_now.astimezone(UTC)
                session.add(
                    AuditLog(
                        household_id=household.id,
                        actor_user_id=callback.from_user.id,
                        action="budget_cycle_closed",
                        entity_type="budget_cycle",
                        entity_id=cycle.id,
                        before_data={"status": "open"},
                        after_data={
                            "status": "closed",
                            "rollover_kzt": str(amount),
                            "destination": "car" if action == "rollover" else "reserve",
                        },
                    )
                )
        except RateUnavailableError:
            await callback.answer("Нет курса для расчёта остатка.", show_alert=True)
            return
        await callback.answer("Месяц закрыт")
        await callback.message.edit_reply_markup(reply_markup=None)
        if action == "rollover":
            await callback.message.reply(
                f"✅ {format_money(amount)} ₸ добавлено к цели «Автомобиль»."
            )
        else:
            await callback.message.reply("✅ Месяц закрыт, остаток оставлен резервом.")

    @router.callback_query(F.data.startswith("receipt:"))
    async def receipt_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.data:
            return
        if not await authorize_callback(callback, deps):
            return
        _, action, receipt_id = callback.data.split(":", 2)
        if action == "confirm":
            await callback.answer("Чек подтверждён")
            await callback.message.edit_reply_markup(reply_markup=None)
            return
        async with deps.session_factory() as session, session.begin():
            receipt = await session.scalar(
                select(Receipt).where(Receipt.id == receipt_id).options(selectinload(Receipt.items))
            )
            if receipt is None:
                await callback.answer("Чек не найден", show_alert=True)
                return
            if action == "delete":
                await session.execute(
                    update(LedgerEntry)
                    .where(LedgerEntry.receipt_id == receipt.id, LedgerEntry.status == "posted")
                    .values(status="reversed")
                )
                receipt.status = "reversed"
                session.add(
                    AuditLog(
                        household_id=receipt.household_id,
                        actor_user_id=callback.from_user.id,
                        action="receipt_reversed",
                        entity_type="receipt",
                        entity_id=receipt.id,
                    )
                )
                await callback.answer("Расход удалён")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.reply("🗑 Чек отменён, остатки восстановлены.")
                return
            if action == "edit":
                lines = ["Позиции чека:"]
                sorted_items = sorted(receipt.items, key=lambda value: value.created_at)
                for index, item in enumerate(sorted_items, 1):
                    lines.append(
                        f"{index}. {escape(item.raw_name)} — {format_money(item.line_total)}"
                    )
                lines.append(
                    f"\nДля исправления отправьте:\n<code>/fix {receipt.id} номер мясо</code>"
                )
                await callback.answer()
                await callback.message.reply("\n".join(lines))

    return router


async def authorize_message(
    message: Message,
    deps: TelegramDependencies,
    owner_only: bool = False,
) -> tuple[Household, int] | None:
    if message.from_user is None:
        return None
    if deps.settings.telegram_allowed_chat_id != message.chat.id:
        return None
    user_id = message.from_user.id
    if user_id not in deps.settings.allowed_user_ids:
        return None
    if owner_only and user_id != deps.settings.telegram_owner_user_id:
        return None
    async with deps.session_factory() as session:
        household = await get_household_by_chat(session, message.chat.id)
        if household is None:
            return None
        return household, user_id


async def authorize_callback(callback: CallbackQuery, deps: TelegramDependencies) -> bool:
    if callback.message is None:
        return False
    return (
        callback.message.chat.id == deps.settings.telegram_allowed_chat_id
        and callback.from_user.id in deps.settings.allowed_user_ids
    )


async def pending_receipt_count(session: AsyncSession, cycle_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count(Receipt.id)).where(
                Receipt.cycle_id == cycle_id,
                Receipt.status.not_in(("posted", "reversed")),
            )
        )
        or 0
    )


async def rebuild_receipt_ledger(
    session: AsyncSession,
    household: Household,
    receipt: Receipt,
    user_id: int,
) -> None:
    await session.execute(
        update(LedgerEntry)
        .where(LedgerEntry.receipt_id == receipt.id, LedgerEntry.status == "posted")
        .values(status="reversed")
    )
    grouped: dict[str, list[ReceiptItem]] = defaultdict(list)
    for item in receipt.items:
        if item.category_id:
            grouped[item.category_id].append(item)
    categories = (
        await session.scalars(select(Category).where(Category.id.in_(grouped.keys())))
    ).all()
    category_by_id = {category.id: category for category in categories}
    for category_id, items in grouped.items():
        category = category_by_id[category_id]
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=receipt.cycle_id,
                category_id=category_id,
                receipt_id=receipt.id,
                entry_type="expense",
                description=f"{receipt.merchant or 'Чек'} · исправлено",
                original_amount=quantize(sum((Decimal(i.line_total) for i in items), Decimal(0))),
                original_currency=receipt.original_currency or "KZT",
                amount_kzt=quantize(sum((Decimal(i.amount_kzt or 0) for i in items), Decimal(0))),
                envelope_amount=quantize(
                    sum((Decimal(i.envelope_amount or 0) for i in items), Decimal(0))
                ),
                envelope_currency=category.envelope_currency,
                exchange_rate_id=receipt.exchange_rate_id,
                occurred_at=receipt.purchased_at or receipt.created_at,
                created_by_user_id=user_id,
            )
        )
