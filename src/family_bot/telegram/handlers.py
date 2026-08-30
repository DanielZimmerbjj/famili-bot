from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape
from io import BytesIO

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
    Member,
    Receipt,
    ReceiptItem,
    SavingsGoal,
)
from family_bot.services.cycles import (
    get_current_cycle,
    get_household_by_chat,
    seed_household,
)
from family_bot.services.expense_ai import (
    ExpenseInterpretation,
    ExpenseInterpreter,
    InterpretedExpenseItem,
)
from family_bot.services.ledger import (
    PostedEntry,
    allocation_and_spend,
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
from family_bot.services.text_parser import match_category
from family_bot.telegram.keyboards import cycle_close_keyboard, main_menu_keyboard

FIX_RE = re.compile(
    r"^/(?:fix|исправить)\s+(?P<receipt>[0-9a-f-]{36})\s+(?P<item>\d+)\s+(?P<category>.+)$",
    re.IGNORECASE,
)
RATE_RE = re.compile(
    r"^/(?:rate|курс)\s+(?P<currency>[A-Za-z]{3})\s+(?P<rate>\d+(?:[.,]\d+)?)"
    r"(?:\s+(?P<nominal>\d+(?:[.,]\d+)?))?$",
    re.IGNORECASE,
)


def simple_command_pattern(
    *commands: str,
    plain: tuple[str, ...] = (),
) -> re.Pattern[str]:
    """Match a Telegram command even when a user adds harmless punctuation."""

    slash_names = "|".join(re.escape(command) for command in commands)
    alternatives = [rf"/(?:{slash_names})(?:@\w+)?"]
    if plain:
        plain_names = "|".join(re.escape(command) for command in plain)
        alternatives.append(rf"(?:{plain_names})")
    return re.compile(
        rf"^(?:{'|'.join(alternatives)})\s*[.!?,;:…]*\s*$",
        re.IGNORECASE,
    )


IDS_COMMAND_RE = simple_command_pattern("ids", "айди")
SETUP_COMMAND_RE = simple_command_pattern("setup", "настроить")
JOIN_COMMAND_RE = simple_command_pattern("join", "войти")
BALANCE_COMMAND_RE = simple_command_pattern(
    "balance",
    "report",
    "остаток",
    "отчет",
    plain=("остаток", "отчет", "💰 Баланс"),
)
TODAY_COMMAND_RE = simple_command_pattern(
    "today", "сегодня", plain=("сегодня", "🧾 Сегодня")
)
INCOME_COMMAND_RE = simple_command_pattern(
    "income", "доходы", plain=("доходы", "💵 Доходы")
)
CHART_COMMAND_RE = simple_command_pattern(
    "chart", "график", plain=("график", "📊 График")
)
GOAL_COMMAND_RE = simple_command_pattern(
    "goal",
    "goals",
    "цель",
    plain=("цель", "накопления", "🎯 Накопления"),
)
HELP_COMMAND_RE = simple_command_pattern(
    "start", "help", "помощь", plain=("помощь", "❓ Помощь")
)
CLOSE_COMMAND_RE = simple_command_pattern("close", "rollover", "закрыть")


@dataclass(slots=True)
class TelegramDependencies:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    rate_service: RateService
    receipt_service: ReceiptService
    expense_interpreter: ExpenseInterpreter


def build_router(deps: TelegramDependencies) -> Router:
    router = Router(name="family-budget")

    @router.message(F.text.regexp(IDS_COMMAND_RE))
    async def setup_ids(message: Message) -> None:
        if not deps.settings.setup_mode:
            return
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{user_id}</code>\n"
            "После настройки выключите SETUP_MODE."
        )

    @router.message(F.text.regexp(SETUP_COMMAND_RE))
    async def setup_group(message: Message) -> None:
        if not deps.settings.setup_mode or message.from_user is None:
            return
        if message.chat.type not in {"group", "supergroup"}:
            await message.reply(
                "Добавьте бота в семейную группу и отправьте <code>/setup</code> там."
            )
            return
        owner_id = deps.settings.telegram_owner_user_id
        if owner_id is None or message.from_user.id != owner_id:
            await message.reply("Первичную настройку может запустить только владелец.")
            return
        async with deps.session_factory() as session, session.begin():
            household = await seed_household(
                session,
                deps.settings,
                message.chat.id,
                owner_id,
            )
            await get_current_cycle(
                session,
                household,
                datetime.now(deps.settings.timezone).date(),
                deps.settings.financial_cycle_start_day,
            )
        await message.reply(
            "✅ <b>Семейная группа подключена.</b>\n"
            "Теперь любой участник группы может присылать чеки, голосовые или "
            "писать финансовые операции обычным текстом.",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(F.text.regexp(JOIN_COMMAND_RE))
    async def join_household(message: Message) -> None:
        if not deps.settings.setup_mode or message.from_user is None:
            return
        household = None
        async with deps.session_factory() as session, session.begin():
            household = await get_household_by_chat(session, message.chat.id)
            if household is None:
                await message.reply(
                    "Сначала владелец должен отправить в группе <code>/setup</code>."
                )
                return
            existing = await session.scalar(
                select(Member).where(
                    Member.household_id == household.id,
                    Member.telegram_user_id == message.from_user.id,
                )
            )
            if existing is not None:
                existing.active = True
                response = "✅ Вы уже подключены к семейному бюджету."
            else:
                session.add(
                    Member(
                        household_id=household.id,
                        telegram_user_id=message.from_user.id,
                        role="member",
                        display_name=message.from_user.full_name[:120],
                    )
                )
                response = "✅ Вы подключены к семейному бюджету."
        await message.reply(response, reply_markup=main_menu_keyboard())

    @router.message(F.voice)
    async def voice_expense(message: Message) -> None:
        authorization = await authorize_message(message, deps)
        if authorization is None or message.voice is None:
            return
        household, user_id = authorization
        if message.voice.duration > deps.settings.telegram_voice_max_seconds:
            await message.reply(
                f"Голосовое слишком длинное. Максимум — "
                f"{deps.settings.telegram_voice_max_seconds} секунд."
            )
            return
        progress = await message.reply("🎙 Голосовое принял, расшифровываю…")
        try:
            stream = BytesIO()
            await message.bot.download(message.voice.file_id, destination=stream)
            transcript = await deps.expense_interpreter.transcribe(
                stream.getvalue(),
                filename="voice.ogg",
                mime_type=message.voice.mime_type or "audio/ogg",
            )
            await progress.edit_text(f"📝 Распознал: <i>{escape(transcript)}</i>")
            await handle_natural_operation(message, deps, household, user_id, transcript)
        except Exception as exc:
            await message.reply(f"❌ Не удалось обработать голосовое: {escape(str(exc))}")

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

    @router.message(F.text.regexp(BALANCE_COMMAND_RE))
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
        await message.answer(report, reply_markup=main_menu_keyboard())

    @router.message(F.text.regexp(TODAY_COMMAND_RE))
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
        await message.answer(
            "\n".join(lines) if entries else "Сегодня расходов пока нет.",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(F.text.regexp(INCOME_COMMAND_RE))
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
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())

    @router.message(F.text.regexp(CHART_COMMAND_RE))
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
            reply_markup=main_menu_keyboard(),
        )

    @router.message(F.text.regexp(GOAL_COMMAND_RE))
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
                if Decimal(goal.target_amount) > 0:
                    remaining = max(Decimal(goal.target_amount) - value, Decimal("0"))
                    lines.append(
                        f"{goal.icon} {goal.name}: {format_money(value)} / "
                        f"{format_money(goal.target_amount)} {goal.currency} · "
                        f"осталось {format_money(remaining)} {goal.currency}"
                    )
                else:
                    lines.append(
                        f"{goal.icon} {goal.name}: накоплено {format_money(value)} "
                        f"{goal.currency} · общая стоимость не задана"
                    )
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())

    @router.message(F.text.regexp(HELP_COMMAND_RE))
    async def help_message(message: Message) -> None:
        authorization = await authorize_message(message, deps, notify=False)
        if authorization is None and not deps.settings.setup_mode:
            return
        if authorization is None:
            await message.answer(
                "👋 Бот работает.\n\n"
                "1. Добавьте его в семейную Telegram-группу.\n"
                "2. Владелец отправляет в группе <code>/setup</code>.\n"
                "3. После этого все участники подключаются автоматически."
            )
            return
        await message.answer(
            "<b>Как пользоваться</b>\n"
            "• Пришлите фотографию чека.\n"
            "• Или наговорите расход голосом.\n"
            "• <code>такси 180 бат</code> — ручной расход.\n"
            "• <code>в 7-Eleven купил колу за 35 бат</code> — расход свободной фразой.\n"
            "• <code>нет, это было молоко</code> — исправление последнего расхода.\n"
            "• <code>кафе 500000 донгов</code> — расход в другой валюте.\n"
            "• <code>получена зарплата 840000 тенге</code> — доход.\n"
            "• <code>отложил 600000 тенге на машину</code> — накопление.\n"
            "• <code>ноутбук стоит миллион, отложил 300000 тенге</code> — новая цель.\n"
            "• <code>отложил 50000 тенге на бордерран</code> — фонд.\n\n"
            "• <code>купил билет на бордерран 300000 тенге</code> — списание фонда.\n\n"
            "Используйте кнопки внизу чата — слеш-команды запоминать не нужно.",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(F.text.regexp(CLOSE_COMMAND_RE))
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
        await handle_natural_operation(message, deps, household, user_id, message.text)

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
            if action == "retry":
                if receipt.status == "posted":
                    await callback.answer("Чек уже проведён", show_alert=True)
                    return
                if receipt.status == "reversed":
                    await callback.answer("Этот чек был удалён", show_alert=True)
                    return
                receipt.status = "retrying"
                receipt.retry_count = 0
                receipt.next_attempt_at = datetime.now(UTC)
                receipt.error_message = None
                await callback.answer("Чек снова в очереди")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.reply("🔄 Повторно обрабатываю чек…")
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
                    "\nОтветьте текстом или голосом, например:\n"
                    "<i>«нет, позиция 2 — молоко»</i>\n\n"
                    f"Или точно смените категорию:\n"
                    f"<code>/fix {receipt.id} номер мясо</code>"
                )
                await callback.answer()
                await callback.message.reply("\n".join(lines))

    return router


async def handle_natural_operation(
    message: Message,
    deps: TelegramDependencies,
    household: Household,
    user_id: int,
    text: str,
) -> None:
    local_now = datetime.now(deps.settings.timezone)
    try:
        async with deps.session_factory() as session:
            category_models = (
                await session.scalars(
                    select(Category).where(
                        Category.household_id == household.id,
                        Category.active.is_(True),
                    )
                )
            ).all()
            goal_models = (
                await session.scalars(
                    select(SavingsGoal).where(
                        SavingsGoal.household_id == household.id,
                        SavingsGoal.active.is_(True),
                    )
                )
            ).all()
            previous_context = await latest_expense_context(session, household.id, user_id)
        categories = {category.key: category.name for category in category_models}
        category_by_key = {category.key: category for category in category_models}
        goals = {
            goal.key: (
                f"{goal.name}; target={format_money(goal.target_amount)} {goal.currency}"
            )
            for goal in goal_models
        }
        goal_by_key = {goal.key: goal for goal in goal_models}
        interpretation = await deps.expense_interpreter.interpret(
            text,
            categories,
            previous_context=previous_context,
            goals=goals,
        )
        if interpretation.overall_confidence < 0.55:
            await message.reply(
                "🤔 Не уверен, что правильно понял сообщение. Ничего не записал — "
                "уточните операцию, сумму и валюту."
            )
            return
        if interpretation.kind == "correction":
            await correct_last_expense(
                message,
                deps,
                household,
                user_id,
                text,
                interpretation=interpretation,
            )
            return
        if interpretation.kind == "other":
            await message.reply(
                "ℹ️ Нейросеть не нашла в сообщении финансовой операции. Ничего не записал."
            )
            return
        if interpretation.kind == "report":
            async with deps.session_factory() as session:
                cycle = await get_current_cycle(
                    session,
                    household,
                    local_now.date(),
                    deps.settings.financial_cycle_start_day,
                )
                report = await build_report(
                    session,
                    deps.rate_service,
                    household,
                    cycle,
                    local_now.date(),
                )
                await session.commit()
            await message.reply(report, reply_markup=main_menu_keyboard())
            return
        if not interpretation.items:
            raise ValueError("нейросеть определила тип операции, но не вернула сумму")

        async with deps.session_factory() as session, session.begin():
            cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            if interpretation.kind == "expense":
                posted_expenses: list[tuple[PostedEntry, Category]] = []
                for item in interpretation.items:
                    if (
                        item.amount is None
                        or item.currency is None
                        or item.category_key not in category_by_key
                        or not item.description
                    ):
                        raise ValueError(
                            "не хватает товара, суммы, валюты или категории"
                        )
                    category = category_by_key[item.category_key]
                    posted = await post_expense(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        category,
                        Decimal(str(item.amount)),
                        normalize_currency(item.currency),
                        item.description,
                        local_now,
                        user_id,
                    )
                    posted_expenses.append((posted, category))
                response = await build_expense_response(
                    session, cycle.id, posted_expenses
                )
            elif interpretation.kind == "income":
                lines = ["✅ <b>Доход записан</b>"]
                for item in interpretation.items:
                    if item.amount is None or item.currency is None or not item.description:
                        raise ValueError("не хватает описания дохода, суммы или валюты")
                    posted = await post_income(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        Decimal(str(item.amount)),
                        normalize_currency(item.currency),
                        item.description,
                        local_now,
                        user_id,
                    )
                    source = (
                        await session.get(IncomeSource, posted.entry.income_source_id)
                        if posted.entry.income_source_id
                        else None
                    )
                    lines.append(
                        f"• {escape(item.description)}: "
                        f"{format_money(posted.entry.original_amount)} "
                        f"{posted.entry.original_currency} → "
                        f"<b>{format_money(posted.entry.amount_kzt)} ₸</b>"
                        f"{f' · {escape(source.name)}' if source else ''}"
                    )
                response = "\n".join(lines)
            elif interpretation.kind in {
                "goal_contribution",
                "goal_expense",
                "goal_target",
                "goal_update",
            }:
                heading = (
                    "✅ <b>Добавлено в накопления</b>"
                    if interpretation.kind == "goal_contribution"
                    else (
                        "✅ <b>Списано из накоплений</b>"
                        if interpretation.kind == "goal_expense"
                        else "✅ <b>Цель обновлена</b>"
                    )
                )
                lines = [heading]
                for item in interpretation.items:
                    if interpretation.kind == "goal_update":
                        goal = await load_existing_savings_goal(
                            session,
                            household,
                            item,
                            goal_by_key,
                        )
                        old_name = goal.name
                        new_name = (item.new_goal_name or "").strip()
                        changed = False
                        if new_name and new_name.casefold() != old_name.casefold():
                            normalized_name = " ".join(new_name.casefold().split())
                            duplicate = next(
                                (
                                    existing
                                    for existing in goal_by_key.values()
                                    if existing.id != goal.id
                                    and " ".join(existing.name.casefold().split())
                                    == normalized_name
                                ),
                                None,
                            )
                            if duplicate is not None:
                                raise ValueError(
                                    f"цель «{new_name}» уже существует; уточните, "
                                    "какую из целей изменить"
                                )
                            goal.name = new_name[:160]
                            changed = True
                        if item.target_amount is not None:
                            await update_goal_target(
                                session,
                                deps.rate_service,
                                goal,
                                item,
                                local_now.date(),
                            )
                            changed = True
                        if not changed:
                            raise ValueError(
                                "не понял новое название или новую стоимость цели"
                            )
                        balance = await goal_balance(session, household.id, goal.id)
                        target = Decimal(goal.target_amount)
                        remaining = max(target - balance, Decimal("0"))
                        title = (
                            f"{escape(old_name)} → <b>{escape(goal.name)}</b>"
                            if old_name != goal.name
                            else f"<b>{escape(goal.name)}</b>"
                        )
                        lines.append(
                            f"• {goal.icon} {title}: цель "
                            f"{format_money(target)} ₸ · накоплено "
                            f"{format_money(balance)} ₸ · осталось "
                            f"{format_money(remaining)} ₸"
                        )
                        continue
                    goal = await resolve_savings_goal(
                        session,
                        deps.rate_service,
                        household,
                        item,
                        goal_by_key,
                        local_now.date(),
                    )
                    if interpretation.kind == "goal_target":
                        if item.target_amount is None:
                            raise ValueError("не хватает общей стоимости цели")
                        balance = await goal_balance(session, household.id, goal.id)
                        remaining = max(
                            Decimal(goal.target_amount) - balance,
                            Decimal("0"),
                        )
                        lines.append(
                            f"• {goal.icon} {escape(goal.name)}: "
                            f"{format_money(balance)} / "
                            f"<b>{format_money(goal.target_amount)} ₸</b> · "
                            f"осталось {format_money(remaining)} ₸"
                        )
                        continue
                    if item.amount is None or item.currency is None:
                        raise ValueError("не хватает суммы или валюты")
                    amount = Decimal(str(item.amount))
                    currency = normalize_currency(item.currency)
                    if interpretation.kind == "goal_contribution":
                        posted = await post_goal_contribution(
                            session,
                            deps.rate_service,
                            household,
                            cycle,
                            goal.key,
                            amount,
                            currency,
                            local_now,
                            user_id,
                        )
                    else:
                        posted = await post_goal_expense(
                            session,
                            deps.rate_service,
                            household,
                            cycle,
                            goal.key,
                            amount,
                            currency,
                            item.description or f"Расход из цели «{goal.name}»",
                            local_now,
                            user_id,
                        )
                    balance = await goal_balance(session, household.id, goal.id)
                    target = Decimal(goal.target_amount)
                    if target > 0:
                        remaining = max(target - balance, Decimal("0"))
                        progress = (
                            f"накоплено {format_money(balance)} / "
                            f"{format_money(target)} ₸ · осталось "
                            f"{format_money(remaining)} ₸"
                        )
                    else:
                        progress = (
                            f"накоплено {format_money(balance)} ₸ · "
                            "напишите общую стоимость цели"
                        )
                    lines.append(
                        f"• {goal.icon} {escape(goal.name)}: "
                        f"{format_money(posted.entry.original_amount)} "
                        f"{posted.entry.original_currency} → "
                        f"<b>{format_money(posted.entry.amount_kzt)} ₸</b> · {progress}"
                    )
                response = "\n".join(lines)
            else:
                raise ValueError("неизвестный тип финансовой операции")
        await message.reply(response, reply_markup=main_menu_keyboard())
    except RateUnavailableError:
        await message.reply(
            "Не нашёл официальный курс. Операция не проведена. Владелец может задать "
            "курс: <code>/rate XXX курс_к_тенге номинал</code>."
        )
    except (CurrencyError, InvalidOperation, ValueError) as exc:
        await message.reply(f"Не удалось провести операцию: {escape(str(exc))}")
    except Exception:
        await message.reply("❌ Нейросеть не смогла разобрать сообщение. Операция не проведена.")


def custom_goal_key(name: str) -> str:
    normalized = " ".join(name.casefold().split())
    return f"custom_{sha256(normalized.encode()).hexdigest()[:16]}"


async def resolve_savings_goal(
    session: AsyncSession,
    rate_service: RateService,
    household: Household,
    item: InterpretedExpenseItem,
    goal_by_key: dict[str, SavingsGoal],
    on_date: date,
) -> SavingsGoal:
    candidate = find_existing_savings_goal(item, goal_by_key)
    goal = None
    if candidate is not None:
        goal = await session.scalar(
            select(SavingsGoal).where(
                SavingsGoal.id == candidate.id,
                SavingsGoal.household_id == household.id,
                SavingsGoal.active.is_(True),
            )
        )
    requested_name = (item.goal_name or "").strip()
    if goal is None:
        requested_name = requested_name or (item.goal_key or "").strip()
        if not requested_name:
            raise ValueError("не понял название цели накопления")
        key = custom_goal_key(requested_name)
        goal = await session.scalar(
            select(SavingsGoal).where(
                SavingsGoal.household_id == household.id,
                SavingsGoal.key == key,
            )
        )
        if goal is None:
            goal = SavingsGoal(
                household_id=household.id,
                key=key,
                name=requested_name[:160],
                icon="🎯",
                goal_type="goal",
                target_amount=Decimal("0"),
                monthly_target=Decimal("0"),
                currency="KZT",
            )
            session.add(goal)
            await session.flush()
        else:
            goal.active = True
        goal_by_key[goal.key] = goal

    if item.target_amount is not None:
        await update_goal_target(session, rate_service, goal, item, on_date)
    return goal


def find_existing_savings_goal(
    item: InterpretedExpenseItem,
    goal_by_key: dict[str, SavingsGoal],
) -> SavingsGoal | None:
    goal = goal_by_key.get(item.goal_key or "")
    requested_name = (item.goal_name or "").strip()
    if goal is not None or not requested_name:
        return goal
    normalized_name = " ".join(requested_name.casefold().split())
    return next(
        (
            existing
            for existing in goal_by_key.values()
            if " ".join(existing.name.casefold().split()) == normalized_name
        ),
        None,
    )


async def load_existing_savings_goal(
    session: AsyncSession,
    household: Household,
    item: InterpretedExpenseItem,
    goal_by_key: dict[str, SavingsGoal],
) -> SavingsGoal:
    candidate = find_existing_savings_goal(item, goal_by_key)
    if candidate is None:
        requested = (item.goal_name or item.goal_key or "").strip()
        suffix = f" «{requested}»" if requested else ""
        raise ValueError(
            f"не нашёл существующую цель{suffix}; назовите её так, как она указана "
            "в разделе «Накопления»"
        )
    goal = await session.scalar(
        select(SavingsGoal).where(
            SavingsGoal.id == candidate.id,
            SavingsGoal.household_id == household.id,
            SavingsGoal.active.is_(True),
        )
    )
    if goal is None:
        raise ValueError("существующая цель больше недоступна")
    return goal


async def update_goal_target(
    session: AsyncSession,
    rate_service: RateService,
    goal: SavingsGoal,
    item: InterpretedExpenseItem,
    on_date: date,
) -> None:
    if item.target_amount is None:
        return
    target_currency = normalize_currency(item.target_currency or "KZT")
    target_quote = await rate_service.get_quote(session, target_currency, on_date)
    goal.target_amount = target_quote.to_kzt(Decimal(str(item.target_amount)))
    goal.currency = "KZT"


async def latest_expense_context(
    session: AsyncSession,
    household_id: str,
    user_id: int,
) -> str | None:
    entry = await session.scalar(
        select(LedgerEntry)
        .where(
            LedgerEntry.household_id == household_id,
            LedgerEntry.created_by_user_id == user_id,
            LedgerEntry.entry_type == "expense",
            LedgerEntry.status == "posted",
        )
        .order_by(LedgerEntry.created_at.desc())
        .limit(1)
    )
    if entry is None:
        return None
    category = await session.get(Category, entry.category_id)
    if not entry.receipt_id:
        return (
            f"Расход: {entry.description}; {entry.original_amount} "
            f"{entry.original_currency}; категория "
            f"{category.key if category else 'unknown'}"
        )
    receipt = await session.scalar(
        select(Receipt)
        .where(Receipt.id == entry.receipt_id)
        .options(selectinload(Receipt.items))
    )
    if receipt is None:
        return None
    items = sorted(receipt.items, key=lambda item: item.created_at)
    item_context = "; ".join(
        f"{index}. {item.raw_name}, {format_money(item.line_total)} "
        f"{receipt.original_currency or 'KZT'}"
        for index, item in enumerate(items, 1)
    )
    return f"Чек: {item_context}"


async def build_expense_response(
    session: AsyncSession,
    cycle_id: str,
    posted_items: list[tuple[PostedEntry, Category]],
) -> str:
    remaining_by_category = {
        category.id: (limit - spent, category.envelope_currency)
        for category, limit, spent in await allocation_and_spend(session, cycle_id)
    }
    lines = ["✅ <b>Расход записан</b>"]
    for posted, category in posted_items:
        entry = posted.entry
        rate = posted.source_rate.rate_kzt / posted.source_rate.nominal
        remaining, envelope_currency = remaining_by_category.get(
            category.id, (Decimal("0"), category.envelope_currency)
        )
        lines.append(
            f"• {escape(entry.description)}: {format_money(entry.original_amount)} "
            f"{entry.original_currency} → <b>{format_money(entry.amount_kzt)} ₸</b>"
        )
        if entry.original_currency != "KZT":
            lines.append(
                f"  Курс: 1 {entry.original_currency} = {format_money(rate)} ₸"
            )
        lines.append(
            f"  {category.icon} {category.name} · осталось "
            f"<b>{format_money(remaining)} {envelope_currency}</b>"
        )
    lines.append("Если что-то неверно, напишите или скажите: <i>«нет, это было молоко»</i>.")
    return "\n".join(lines)


async def correct_last_expense(
    message: Message,
    deps: TelegramDependencies,
    household: Household,
    user_id: int,
    text: str,
    interpretation: ExpenseInterpretation | None = None,
) -> None:
    async with deps.session_factory() as session:
        last_entry = await session.scalar(
            select(LedgerEntry)
            .where(
                LedgerEntry.household_id == household.id,
                LedgerEntry.created_by_user_id == user_id,
                LedgerEntry.entry_type == "expense",
                LedgerEntry.status == "posted",
            )
            .order_by(LedgerEntry.created_at.desc())
            .limit(1)
        )
        if last_entry is None:
            await message.reply("Не нашёл ваш последний расход для исправления.")
            return
        category_models = (
            await session.scalars(
                select(Category).where(
                    Category.household_id == household.id,
                    Category.active.is_(True),
                )
            )
        ).all()
        previous_category = await session.get(Category, last_entry.category_id)
        receipt = None
        receipt_items: list[ReceiptItem] = []
        if last_entry.receipt_id:
            receipt = await session.scalar(
                select(Receipt)
                .where(Receipt.id == last_entry.receipt_id)
                .options(selectinload(Receipt.items))
            )
            if receipt is not None:
                receipt_items = sorted(receipt.items, key=lambda item: item.created_at)

    if receipt_items:
        item_context = "; ".join(
            f"{index}. {item.raw_name}, {format_money(item.line_total)} "
            f"{receipt.original_currency if receipt else ''}"
            for index, item in enumerate(receipt_items, 1)
        )
        previous_context = f"Чек: {item_context}"
    else:
        previous_context = (
            f"Расход: {last_entry.description}; {last_entry.original_amount} "
            f"{last_entry.original_currency}; категория "
            f"{previous_category.key if previous_category else 'unknown'}"
        )
    categories = {category.key: category.name for category in category_models}
    interpretation = interpretation or await deps.expense_interpreter.interpret(
        text, categories, previous_context
    )
    if interpretation.kind != "correction" or not interpretation.items:
        await message.reply(
            "Не понял, что именно исправить. Например: <i>«нет, это было молоко»</i>."
        )
        return
    correction = interpretation.items[0]
    category_by_key = {category.key: category for category in category_models}

    try:
        async with deps.session_factory() as session, session.begin():
            locked_entry = await session.scalar(
                select(LedgerEntry).where(LedgerEntry.id == last_entry.id).with_for_update()
            )
            if locked_entry is None or locked_entry.status != "posted":
                await message.reply("Расход уже изменён или удалён.")
                return
            if locked_entry.receipt_id:
                locked_receipt = await session.scalar(
                    select(Receipt)
                    .where(Receipt.id == locked_entry.receipt_id)
                    .options(selectinload(Receipt.items))
                    .with_for_update()
                )
                if locked_receipt is None:
                    raise ValueError("Чек не найден")
                items = sorted(locked_receipt.items, key=lambda item: item.created_at)
                target_number = correction.target_item_number
                if len(items) > 1 and target_number is None:
                    await message.reply(
                        "В чеке несколько позиций. Укажите номер, например: "
                        "<i>«нет, позиция 2 — молоко»</i>."
                    )
                    return
                target_number = target_number or 1
                if not 1 <= target_number <= len(items):
                    raise ValueError("Неверный номер позиции чека")
                target = items[target_number - 1]
                before = {
                    "raw_name": target.raw_name,
                    "line_total": str(target.line_total),
                    "category_id": target.category_id,
                }
                if correction.description:
                    target.raw_name = correction.description[:500]
                if correction.amount is not None:
                    target.line_total = quantize(Decimal(str(correction.amount)))
                if correction.currency and len(items) > 1:
                    raise ValueError("Валюту можно менять только у чека с одной позицией")
                currency = normalize_currency(
                    correction.currency or locked_receipt.original_currency or "KZT"
                )
                if correction.category_key:
                    corrected_category = category_by_key.get(correction.category_key)
                    if corrected_category is None:
                        raise ValueError("Категория не найдена")
                    target.category_id = corrected_category.id
                    target.subcategory_id = None
                occurred_at = locked_receipt.purchased_at or locked_receipt.created_at
                local_date = occurred_at.astimezone(deps.settings.timezone).date()
                source_rate = await deps.rate_service.get_quote(session, currency, local_date)
                for item in items:
                    item_category = await session.get(Category, item.category_id)
                    if item_category is None:
                        raise ValueError("Категория позиции не найдена")
                    item.amount_kzt = source_rate.to_kzt(Decimal(item.line_total))
                    envelope_rate = await deps.rate_service.get_quote(
                        session, item_category.envelope_currency, local_date
                    )
                    item.envelope_amount = envelope_rate.from_kzt(item.amount_kzt)
                    item.envelope_currency = item_category.envelope_currency
                locked_receipt.original_currency = currency
                locked_receipt.original_total = quantize(
                    sum((Decimal(item.line_total) for item in items), Decimal("0"))
                )
                locked_receipt.total_kzt = source_rate.to_kzt(locked_receipt.original_total)
                locked_receipt.exchange_rate_id = source_rate.rate_id
                await rebuild_receipt_ledger(session, household, locked_receipt, user_id)
                session.add(
                    AuditLog(
                        household_id=household.id,
                        actor_user_id=user_id,
                        action="receipt_item_natural_correction",
                        entity_type="receipt_item",
                        entity_id=target.id,
                        before_data=before,
                        after_data={
                            "raw_name": target.raw_name,
                            "line_total": str(target.line_total),
                            "category_id": target.category_id,
                        },
                    )
                )
                corrected_category = await session.get(Category, target.category_id)
                response = (
                    f"✅ Исправил позицию {target_number}: "
                    f"<b>{escape(target.raw_name)}</b> — {format_money(target.line_total)} "
                    f"{currency} → {corrected_category.icon if corrected_category else ''} "
                    f"{corrected_category.name if corrected_category else 'категория'}.\n"
                    f"Новый итог чека: <b>{format_money(locked_receipt.original_total)} "
                    f"{currency}</b> · {format_money(locked_receipt.total_kzt)} ₸"
                )
            else:
                category = (
                    category_by_key.get(correction.category_key)
                    if correction.category_key
                    else await session.get(Category, locked_entry.category_id)
                )
                if category is None:
                    raise ValueError("Категория не найдена")
                cycle = await session.get_one(BudgetCycle, locked_entry.cycle_id)
                locked_entry.status = "reversed"
                new_posted = await post_expense(
                    session,
                    deps.rate_service,
                    household,
                    cycle,
                    category,
                    Decimal(str(correction.amount))
                    if correction.amount is not None
                    else Decimal(locked_entry.original_amount),
                    normalize_currency(correction.currency or locked_entry.original_currency),
                    correction.description or locked_entry.description,
                    locked_entry.occurred_at.astimezone(deps.settings.timezone),
                    user_id,
                )
                new_posted.entry.reversal_of_id = locked_entry.id
                session.add(
                    AuditLog(
                        household_id=household.id,
                        actor_user_id=user_id,
                        action="ledger_expense_natural_correction",
                        entity_type="ledger_entry",
                        entity_id=new_posted.entry.id,
                        before_data={
                            "entry_id": locked_entry.id,
                            "description": locked_entry.description,
                            "amount": str(locked_entry.original_amount),
                            "currency": locked_entry.original_currency,
                            "category_id": locked_entry.category_id,
                        },
                        after_data={
                            "description": new_posted.entry.description,
                            "amount": str(new_posted.entry.original_amount),
                            "currency": new_posted.entry.original_currency,
                            "category_id": category.id,
                        },
                    )
                )
                response = await build_expense_response(
                    session, cycle.id, [(new_posted, category)]
                )
        await message.reply(response, reply_markup=main_menu_keyboard())
    except RateUnavailableError:
        await message.reply("Не удалось исправить: нет актуального курса валюты.")
    except (CurrencyError, InvalidOperation, ValueError) as exc:
        await message.reply(f"Не удалось исправить расход: {escape(str(exc))}")


async def authorize_message(
    message: Message,
    deps: TelegramDependencies,
    owner_only: bool = False,
    notify: bool = True,
) -> tuple[Household, int] | None:
    if message.from_user is None:
        return None
    user_id = message.from_user.id
    async with deps.session_factory() as session:
        if message.chat.type == "private":
            row = (
                await session.execute(
                    select(Household, Member)
                    .join(Member, Member.household_id == Household.id)
                    .where(
                        Household.active.is_(True),
                        Member.telegram_user_id == user_id,
                        Member.active.is_(True),
                    )
                    .order_by(Member.created_at)
                    .limit(1)
                )
            ).first()
            household, member = row if row is not None else (None, None)
        else:
            household = await get_household_by_chat(session, message.chat.id)
            member = None
            if (
                household is None
                and message.chat.type in {"group", "supergroup"}
                and deps.settings.setup_mode
                and deps.settings.telegram_owner_user_id == user_id
            ):
                household = await seed_household(
                    session,
                    deps.settings,
                    message.chat.id,
                    user_id,
                )
                await get_current_cycle(
                    session,
                    household,
                    datetime.now(deps.settings.timezone).date(),
                    deps.settings.financial_cycle_start_day,
                )
                await session.commit()
            if household is not None:
                member = await session.scalar(
                    select(Member).where(
                        Member.household_id == household.id,
                        Member.telegram_user_id == user_id,
                        Member.active.is_(True),
                    )
                )
                if member is None and message.chat.type in {"group", "supergroup"}:
                    member = Member(
                        household_id=household.id,
                        telegram_user_id=user_id,
                        role="member",
                        display_name=message.from_user.full_name[:120],
                    )
                    session.add(member)
                    await session.commit()
        if household is None:
            if notify:
                if message.chat.type == "private":
                    await message.reply(
                        "Бюджет ещё не привязан. Добавьте бота в семейную группу и "
                        "отправьте там <code>/setup</code>."
                    )
                else:
                    await message.reply(
                        "Семейный бюджет в этой группе ещё не настроен. "
                        "Пусть владелец отправит <code>/setup</code>."
                    )
            return None
        if member is None:
            await message.reply("Вы не подключены к этому семейному бюджету.")
            return None
        if owner_only and member.role != "owner":
            await message.reply("Эта команда доступна только владельцу бюджета.")
            return None
        return household, user_id


async def authorize_callback(callback: CallbackQuery, deps: TelegramDependencies) -> bool:
    if callback.message is None:
        return False
    async with deps.session_factory() as session:
        household = await get_household_by_chat(session, callback.message.chat.id)
        if household is None:
            return False
        member = await session.scalar(
            select(Member).where(
                Member.household_id == household.id,
                Member.telegram_user_id == callback.from_user.id,
                Member.active.is_(True),
            )
        )
        return member is not None


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
