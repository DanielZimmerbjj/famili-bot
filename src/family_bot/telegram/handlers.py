from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape
from io import BytesIO

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, ErrorEvent, Message
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from family_bot.config import Settings
from family_bot.constants import SEVEN_ELEVEN_CATEGORY_KEY
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
    cycle_close_breakdown,
    get_category,
    goal_balance,
    post_expense,
    post_goal_contribution,
    post_goal_expense,
    post_income,
)
from family_bot.services.money import CurrencyError, normalize_currency, quantize
from family_bot.services.rates import RateService, RateUnavailableError
from family_bot.services.receipt_images import infer_document_image_mime
from family_bot.services.receipts import (
    ReceiptService,
    format_money,
    is_seven_eleven_merchant,
    receipt_progress_text,
)
from family_bot.services.reports import build_chart, build_report, build_spending_answer
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
CLOSE_COMMAND_RE = simple_command_pattern(
    "close",
    "rollover",
    "закрыть",
    plain=(
        "закрыть",
        "закрыть месяц",
        "закрой месяц",
        "завершить месяц",
        "заверши месяц",
        "месяц закончен",
        "месяц закончился",
        "все, месяц закончен",
        "всё, месяц закончен",
        "все месяц закончен",
        "всё месяц закончен",
    ),
)
RECEIPT_CORRECTION_RE = re.compile(
    r"(?:\bчек\w*|\bпозици\w*|\bтовар\w*|\bмагазин\w*|\breceipt\b|"
    r"7[\s-]?(?:11|eleven)|seven[\s-]?eleven|севен[\s-]?элевен)",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


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
        progress = await message.reply(
            receipt_progress_text(15, "голосовое принял, загружаю")
        )
        try:
            stream = BytesIO()
            await message.bot.download(message.voice.file_id, destination=stream)
            await progress.edit_text(receipt_progress_text(40, "расшифровываю голос"))
            transcript = await deps.expense_interpreter.transcribe(
                stream.getvalue(),
                filename="voice.ogg",
                mime_type=message.voice.mime_type or "audio/ogg",
            )
            await progress.edit_text(
                receipt_progress_text(75, "распознал, разбираю запрос")
                + f"\n📝 <i>{escape(transcript)}</i>"
            )
            if CLOSE_COMMAND_RE.fullmatch(transcript.strip()):
                if user_id != deps.settings.telegram_owner_user_id:
                    await progress.edit_text(
                        receipt_progress_text(
                            100,
                            "запрос распознан, но нет прав на закрытие месяца",
                            done=True,
                        )
                    )
                    await message.reply("Закрыть месяц может только владелец.")
                    return
                await prompt_cycle_close(message, deps, household)
                await progress.edit_text(
                    receipt_progress_text(100, "голосовой запрос обработан", done=True)
                    + f"\n📝 <i>{escape(transcript)}</i>"
                )
                return
            await handle_natural_operation(message, deps, household, user_id, transcript)
            await progress.edit_text(
                receipt_progress_text(100, "голосовой запрос обработан", done=True)
                + f"\n📝 <i>{escape(transcript)}</i>"
            )
        except Exception as exc:
            try:
                await progress.edit_text(
                    receipt_progress_text(
                        100,
                        "голосовой запрос не обработан",
                        done=True,
                    )
                )
            except Exception:
                logger.warning("Could not update voice progress", exc_info=True)
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
        elif message.document and (
            inferred_mime := infer_document_image_mime(
                message.document.mime_type,
                message.document.file_name,
            )
        ):
            file_id = message.document.file_id
            file_unique_id = message.document.file_unique_id
            mime_type = inferred_mime
        else:
            await message.reply("Пришлите чек фотографией или изображением-файлом.")
            return

        progress = await message.reply(receipt_progress_text(10, "фото чека получил"))
        try:
            local_now = datetime.now(deps.settings.timezone)
            async with deps.session_factory() as session, session.begin():
                cycle = await get_current_cycle(
                    session,
                    household,
                    local_now.date(),
                    deps.settings.financial_cycle_start_day,
                )
                result = await deps.receipt_service.enqueue(
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
                    progress_message_id=progress.message_id,
                )
            if result.outcome == "created":
                response = receipt_progress_text(25, "чек сохранён, ждёт распознавания")
                response += f" · <code>{result.receipt.id[:8]}</code>"
            elif result.outcome == "page_added":
                response = receipt_progress_text(25, "страница добавлена, жду остальные")
                response += f" · <code>{result.receipt.id[:8]}</code>"
            elif result.outcome == "requeued":
                response = receipt_progress_text(25, "повторно запустил распознавание")
                response += f" · <code>{result.receipt.id[:8]}</code>"
            elif result.outcome == "already_posted":
                response = "ℹ️ Этот чек уже учтён. Повторно расход не списываю."
            else:
                response = "⏳ Этот чек уже находится в обработке."
            await progress.edit_text(response)
        except Exception:
            logger.exception("Could not enqueue Telegram receipt")
            try:
                await progress.edit_text(
                    "❌ Не удалось поставить чек в обработку. Попробуйте отправить ещё раз."
                )
            except Exception:
                logger.exception("Could not notify Telegram about receipt enqueue failure")

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
                if goal.goal_type == "reserve":
                    lines.append(
                        f"{goal.icon} {escape(goal.name)}: "
                        f"<b>{format_money(value)} {goal.currency}</b>"
                    )
                elif Decimal(goal.target_amount) > 0:
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
            "• <code>прошлый чек был не 7-Eleven, а Big C</code> — смена магазина.\n"
            "• <code>итог прошлого чека был 220 бат</code> — исправление суммы чека.\n"
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
        await prompt_cycle_close(message, deps, household)

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
        if not await authorize_callback(callback, deps, owner_only=True):
            return
        parts = callback.data.split(":")
        action = parts[1] if len(parts) >= 2 else ""
        if action == "goal" and len(parts) == 4:
            cycle_reference, goal_reference = parts[2], parts[3]
        elif action in {"rollover", "keep"} and len(parts) == 3:
            cycle_reference = parts[2]
            goal_reference = "car" if action == "rollover" else None
        else:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
        local_now = datetime.now(deps.settings.timezone)
        amount = Decimal("0")
        border_contribution = Decimal("0")
        mandatory_reserved = Decimal("0")
        destination_goal: SavingsGoal | None = None
        try:
            async with deps.session_factory() as session, session.begin():
                household = await get_household_by_chat(session, callback.message.chat.id)
                if household is None:
                    await callback.answer("Семья не найдена.", show_alert=True)
                    return
                cycle = await resolve_open_cycle_reference(
                    session,
                    household.id,
                    cycle_reference,
                )
                if cycle is None:
                    await callback.answer(
                        "Месяц не найден или уже закрыт.",
                        show_alert=True,
                    )
                    return
                if not cycle_can_close(local_now, cycle, deps.settings):
                    await callback.answer("Ещё рано закрывать этот месяц.", show_alert=True)
                    return
                pending = await pending_receipt_count(session, cycle.id)
                if pending:
                    await callback.answer(f"Есть непроведённые чеки: {pending}.", show_alert=True)
                    return
                breakdown = await cycle_close_breakdown(session, cycle)
                amount = breakdown.transferable_kzt
                border_contribution = breakdown.border_contribution_kzt
                mandatory_reserved = breakdown.mandatory_reserved_kzt
                if action in {"goal", "rollover"}:
                    destination_goal = await resolve_active_goal_reference(
                        session,
                        household.id,
                        goal_reference,
                    )
                    if destination_goal is None:
                        await callback.answer("Цель не найдена.", show_alert=True)
                        return
                else:
                    destination_goal = await resolve_active_goal_reference(
                        session,
                        household.id,
                        "reserve",
                    )
                    if destination_goal is None:
                        await callback.answer("Цель резерва не найдена.", show_alert=True)
                        return

                event_key = f"callback:{callback.id}"
                if border_contribution:
                    await post_goal_contribution(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        "border_run",
                        border_contribution,
                        "KZT",
                        local_now,
                        callback.from_user.id,
                        source_event_key=event_key,
                        source_item_index=0,
                    )
                if amount:
                    await post_goal_contribution(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        destination_goal.key,
                        amount,
                        "KZT",
                        local_now,
                        callback.from_user.id,
                        source_event_key=event_key,
                        source_item_index=1,
                    )
                cycle.status = "closed"
                cycle.closed_at = local_now.astimezone(UTC)
                destination = destination_goal.key
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
                            "mandatory_reserved_kzt": str(mandatory_reserved),
                            "border_contribution_kzt": str(border_contribution),
                            "remainder_kzt": str(amount),
                            "rollover_kzt": str(amount),
                            "destination": destination,
                        },
                    )
                )
        except RateUnavailableError:
            await callback.answer("Нет курса для расчёта остатка.", show_alert=True)
            return
        await callback.answer("Месяц закрыт")
        await callback.message.edit_reply_markup(reply_markup=None)
        if amount:
            await callback.message.reply(
                f"✅ {format_money(amount)} ₸ перенесено в «{escape(destination_goal.name)}». "
                f"На бордерран отложено {format_money(border_contribution)} ₸. "
                "Месяц закрыт."
            )
        else:
            await callback.message.reply(
                "✅ Месяц закрыт. После обязательств и бордеррана "
                "свободного остатка нет."
            )

    @router.callback_query(F.data.startswith("receipt:"))
    async def receipt_callback(callback: CallbackQuery) -> None:
        if callback.message is None or callback.from_user is None or not callback.data:
            return
        if not await authorize_callback(callback, deps):
            return
        _, action, receipt_id = callback.data.split(":", 2)
        if action == "confirm":
            # Compatibility for buttons already sent before confirmation was removed.
            await callback.answer("Чек уже был учтён автоматически")
            await callback.message.edit_reply_markup(reply_markup=None)
            return
        async with deps.session_factory() as session, session.begin():
            household = await get_household_by_chat(session, callback.message.chat.id)
            if household is None:
                await callback.answer("Семья не найдена", show_alert=True)
                return
            receipt = await session.scalar(
                select(Receipt)
                .where(
                    Receipt.id == receipt_id,
                    Receipt.household_id == household.id,
                )
                .options(selectinload(Receipt.items))
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
            if action == "current":
                if receipt.status == "posted":
                    await callback.answer("Чек уже проведён", show_alert=True)
                    return
                receipt.force_current_cycle = True
                receipt.status = "retrying"
                receipt.retry_count = 0
                receipt.next_attempt_at = datetime.now(UTC)
                receipt.error_message = None
                await callback.answer("Учту в текущем месяце")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.reply("🔄 Повторно обрабатываю чек как текущий…")
                return
            if action == "dismiss":
                if receipt.status == "posted":
                    await callback.answer(
                        "Проведённый чек нужно удалить кнопкой «Удалить».",
                        show_alert=True,
                    )
                    return
                receipt.status = "reversed"
                receipt.error_message = None
                session.add(
                    AuditLog(
                        household_id=receipt.household_id,
                        actor_user_id=callback.from_user.id,
                        action="receipt_dismissed",
                        entity_type="receipt",
                        entity_id=receipt.id,
                    )
                )
                await callback.answer("Чек не будет учитываться")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.reply("🗑 Чек пропущен и больше не блокирует месяц.")
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
                        f"{index}. {escape(item.display_name)} — "
                        f"{format_money(item.display_line_total)}"
                    )
                lines.append(
                    "\nОтветьте текстом или голосом, например:\n"
                    "<i>«нет, позиция 2 — молоко»</i>\n\n"
                    f"Или точно смените категорию:\n"
                    f"<code>/fix {receipt.id} номер мясо</code>"
                )
                await callback.answer()
                await callback.message.reply("\n".join(lines))

    @router.error()
    async def unexpected_update_error(event: ErrorEvent) -> bool:
        logger.exception("Unhandled Telegram update error", exc_info=event.exception)
        message = event.update.message
        if message is not None:
            try:
                await message.reply(
                    "❌ Не смог обработать это сообщение из-за внутренней ошибки. "
                    "Сообщение не потеряно; отправьте его ещё раз."
                )
            except Exception:
                logger.exception("Could not notify Telegram about update failure")
        return True

    return router


def compact_callback_reference(value: str) -> str:
    return value.replace("-", "").casefold()


def cycle_can_close(now: datetime, cycle: BudgetCycle, settings: Settings) -> bool:
    if now.date() > cycle.end_date:
        return True
    if now.date() < cycle.end_date:
        return False
    return (now.hour, now.minute) >= (
        settings.daily_report_hour,
        settings.daily_report_minute,
    )


async def resolve_open_cycle_reference(
    session: AsyncSession,
    household_id: str,
    reference: str,
) -> BudgetCycle | None:
    normalized = compact_callback_reference(reference)
    cycles = (
        await session.scalars(
            select(BudgetCycle)
            .where(
                BudgetCycle.household_id == household_id,
                BudgetCycle.status == "open",
            )
            .with_for_update()
        )
    ).all()
    matches = [
        cycle
        for cycle in cycles
        if compact_callback_reference(cycle.id).startswith(normalized)
    ]
    return matches[0] if len(matches) == 1 else None


async def resolve_active_goal_reference(
    session: AsyncSession,
    household_id: str,
    reference: str | None,
) -> SavingsGoal | None:
    if not reference:
        return None
    goals = (
        await session.scalars(
            select(SavingsGoal).where(
                SavingsGoal.household_id == household_id,
                SavingsGoal.active.is_(True),
            )
        )
    ).all()
    normalized = compact_callback_reference(reference)
    matches = [
        goal
        for goal in goals
        if goal.key.casefold() == reference.casefold()
        or compact_callback_reference(goal.id).startswith(normalized)
    ]
    return matches[0] if len(matches) == 1 else None


async def prompt_cycle_close(
    message: Message,
    deps: TelegramDependencies,
    household: Household,
) -> None:
    local_now = datetime.now(deps.settings.timezone)
    async with deps.session_factory() as session, session.begin():
        cycle = await session.scalar(
            select(BudgetCycle)
            .where(
                BudgetCycle.household_id == household.id,
                BudgetCycle.status == "open",
                BudgetCycle.end_date <= local_now.date(),
            )
            .order_by(BudgetCycle.end_date)
        )
        if cycle is None:
            current_cycle = await get_current_cycle(
                session,
                household,
                local_now.date(),
                deps.settings.financial_cycle_start_day,
            )
            if current_cycle.status == "closed":
                await message.reply("Этот финансовый месяц уже закрыт.")
            else:
                await message.reply(
                    f"Закрыть текущий месяц можно "
                    f"{current_cycle.end_date.strftime('%d.%m.%Y')}."
                )
            return
        if not cycle_can_close(local_now, cycle, deps.settings):
            await message.reply(
                f"Закрыть месяц можно сегодня после "
                f"{deps.settings.daily_report_hour:02d}:"
                f"{deps.settings.daily_report_minute:02d}."
            )
            return
        pending = await pending_receipt_count(session, cycle.id)
        if pending:
            await message.reply(
                f"Сначала разберите {pending} чек(а/ов), которые ещё не проведены."
            )
            return
        breakdown = await cycle_close_breakdown(session, cycle)
        amount = breakdown.transferable_kzt
        goals = (
            await session.scalars(
                select(SavingsGoal)
                .where(
                    SavingsGoal.household_id == household.id,
                    SavingsGoal.active.is_(True),
                    SavingsGoal.key != "reserve",
                )
                .order_by(SavingsGoal.goal_type, SavingsGoal.name)
            )
        ).all()

    goal_options = [(goal.id, goal.name, goal.icon) for goal in goals]
    if amount:
        text = (
            f"💰 Можно отложить: <b>{format_money(amount)} ₸</b>.\n"
            f"Обязательства: {format_money(breakdown.mandatory_reserved_kzt)} ₸. "
            f"Бордерран: {format_money(breakdown.border_contribution_kzt)} ₸.\n\n"
            "Куда переложить остаток?"
        )
    else:
        text = (
            "💰 Свободного остатка для переноса нет. "
            "Месяц можно закрыть без пополнения целей."
        )
    await message.reply(
        text,
        reply_markup=cycle_close_keyboard(
            cycle.id,
            goal_options,
            has_remainder=bool(amount),
        ),
    )


async def handle_natural_operation(
    message: Message,
    deps: TelegramDependencies,
    household: Household,
    user_id: int,
    text: str,
) -> None:
    local_now = datetime.now(deps.settings.timezone)
    message_chat = getattr(getattr(message, "chat", None), "id", None)
    message_chat = message_chat or household.telegram_chat_id or 0
    message_id = getattr(message, "message_id", id(message))
    source_event_key = f"message:{message_chat}:{message_id}"
    try:
        async with deps.session_factory() as session:
            already_recorded = await session.scalar(
                select(LedgerEntry.id)
                .where(LedgerEntry.source_event_key == source_event_key)
                .limit(1)
            )
            if already_recorded is not None:
                await message.reply("ℹ️ Это сообщение уже учтено. Повторно ничего не записываю.")
                return
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
            previous_context = await latest_expense_context(session, household.id)
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
                source_event_key=source_event_key,
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
                report_focus = interpretation.report_focus or "full"
                if report_focus == "full":
                    report = await build_report(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        local_now.date(),
                    )
                else:
                    report = await build_spending_answer(
                        session,
                        household,
                        cycle,
                        local_now.date(),
                        interpretation.report_period or "current_cycle",
                        report_focus,
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
            if cycle.status != "open":
                raise ValueError(
                    "финансовый месяц уже закрыт; новую операцию можно записать "
                    "после начала следующего цикла"
                )
            if interpretation.kind == "expense":
                posted_expenses: list[tuple[PostedEntry, Category]] = []
                for item_index, item in enumerate(interpretation.items):
                    if (
                        item.amount is None
                        or item.currency is None
                        or item.category_key not in category_by_key
                    ):
                        raise ValueError("не хватает суммы, валюты или категории")
                    category = category_by_key[item.category_key]
                    description = (item.description or "").strip()
                    if not description:
                        description = (
                            f"Покупка в {interpretation.merchant}"
                            if interpretation.merchant
                            else category.name
                        )
                    posted = await post_expense(
                        session,
                        deps.rate_service,
                        household,
                        cycle,
                        category,
                        Decimal(str(item.amount)),
                        normalize_currency(item.currency),
                        description,
                        local_now,
                        user_id,
                        source_event_key=source_event_key,
                        source_item_index=item_index,
                    )
                    posted_expenses.append((posted, category))
                response = await build_expense_response(
                    session, cycle.id, posted_expenses
                )
            elif interpretation.kind == "income":
                lines = ["✅ <b>Доход записан</b>"]
                for item_index, item in enumerate(interpretation.items):
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
                        source_event_key=source_event_key,
                        source_item_index=item_index,
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
                for item_index, item in enumerate(interpretation.items):
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
                            source_event_key=source_event_key,
                            source_item_index=item_index,
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
                            source_event_key=source_event_key,
                            source_item_index=item_index,
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
    except IntegrityError:
        await message.reply("ℹ️ Это сообщение уже учтено. Повторно ничего не записываю.")
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
) -> str | None:
    entry = await session.scalar(
        select(LedgerEntry)
        .where(
            LedgerEntry.household_id == household_id,
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
    category_ids = {item.category_id for item in items if item.category_id}
    categories = (
        await session.scalars(select(Category).where(Category.id.in_(category_ids)))
    ).all()
    category_keys = {category.id: category.key for category in categories}
    item_context = "; ".join(
        f"{index}. {item.display_name}, {format_money(item.display_line_total)} "
        f"{receipt.original_currency or 'KZT'}, категория "
        f"{category_keys.get(item.category_id, 'unknown')}"
        for index, item in enumerate(items, 1)
    )
    return (
        f"Чек магазина {receipt.merchant or 'не указан'}; итог "
        f"{format_money(receipt.original_total)} {receipt.original_currency or 'KZT'}; "
        f"позиции: {item_context}"
    )


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


def correction_targets_receipt(
    text: str,
    interpretation: ExpenseInterpretation | None,
) -> bool:
    if RECEIPT_CORRECTION_RE.search(text):
        return True
    if interpretation is None:
        return False
    if interpretation.merchant or interpretation.receipt_total or interpretation.receipt_currency:
        return True
    return any(
        item.target_item_number is not None or item.target_item_name
        for item in interpretation.items
    )


def correction_has_changes(interpretation: ExpenseInterpretation) -> bool:
    if interpretation.merchant or interpretation.receipt_total or interpretation.receipt_currency:
        return True
    return any(
        item.description
        or item.amount is not None
        or item.currency
        or item.category_key
        for item in interpretation.items
    )


def resolve_corrected_receipt_item(
    items: list[ReceiptItem],
    correction: InterpretedExpenseItem,
) -> tuple[int, ReceiptItem]:
    if correction.target_item_number is not None:
        index = correction.target_item_number - 1
        if not 0 <= index < len(items):
            raise ValueError("Неверный номер позиции чека")
        return index + 1, items[index]
    if correction.target_item_name:
        target_name = " ".join(
            re.sub(r"[^\w]+", " ", correction.target_item_name.casefold()).split()
        )
        matches: list[tuple[int, ReceiptItem]] = []
        for index, item in enumerate(items, 1):
            names = (item.display_name, item.raw_name)
            normalized_names = [
                " ".join(re.sub(r"[^\w]+", " ", name.casefold()).split())
                for name in names
            ]
            if any(
                target_name == name or target_name in name or name in target_name
                for name in normalized_names
                if name
            ):
                matches.append((index, item))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("Название подходит к нескольким позициям; укажите номер")
        raise ValueError("Не нашёл такую позицию в последнем чеке; укажите её номер")
    if len(items) == 1:
        return 1, items[0]
    raise ValueError(
        "В чеке несколько позиций. Укажите номер или название, например: "
        "«позиция 2 — молоко»"
    )


def reallocate_receipt_total(items: list[ReceiptItem], new_total: Decimal) -> None:
    current_total = sum((Decimal(item.line_total) for item in items), Decimal("0"))
    if new_total <= 0 or current_total <= 0:
        raise ValueError("Итог чека и сумма позиций должны быть положительными")
    allocations = [
        quantize(new_total * Decimal(item.line_total) / current_total) for item in items
    ]
    residual = quantize(new_total) - sum(allocations, Decimal("0"))
    if residual:
        largest_index = max(
            range(len(items)), key=lambda index: Decimal(items[index].line_total)
        )
        allocations[largest_index] = quantize(allocations[largest_index] + residual)
    for item, allocation in zip(items, allocations, strict=True):
        item.line_total = allocation


async def correct_last_expense(
    message: Message,
    deps: TelegramDependencies,
    household: Household,
    user_id: int,
    text: str,
    interpretation: ExpenseInterpretation | None = None,
    source_event_key: str | None = None,
) -> None:
    receipt_request = correction_targets_receipt(text, interpretation)
    async with deps.session_factory() as session:
        category_models = (
            await session.scalars(
                select(Category).where(
                    Category.household_id == household.id,
                    Category.active.is_(True),
                )
            )
        ).all()
        receipt = None
        if receipt_request:
            receipt = await session.scalar(
                select(Receipt)
                .where(
                    Receipt.household_id == household.id,
                    Receipt.status == "posted",
                )
                .options(selectinload(Receipt.items))
                .order_by(Receipt.posted_at.desc(), Receipt.created_at.desc())
                .limit(1)
            )
        last_entry = None
        if receipt is not None:
            last_entry = await session.scalar(
                select(LedgerEntry)
                .where(
                    LedgerEntry.receipt_id == receipt.id,
                    LedgerEntry.entry_type == "expense",
                    LedgerEntry.status == "posted",
                )
                .order_by(LedgerEntry.created_at.desc())
                .limit(1)
            )
        else:
            last_entry = await session.scalar(
                select(LedgerEntry)
                .where(
                    LedgerEntry.household_id == household.id,
                    LedgerEntry.entry_type == "expense",
                    LedgerEntry.status == "posted",
                )
                .order_by(LedgerEntry.created_at.desc())
                .limit(1)
            )
            if last_entry is not None and last_entry.receipt_id:
                receipt = await session.scalar(
                    select(Receipt)
                    .where(Receipt.id == last_entry.receipt_id)
                    .options(selectinload(Receipt.items))
                )
        if receipt_request and receipt is None:
            await message.reply("Не нашёл последний проведённый чек для исправления.")
            return
        if last_entry is None and receipt is None:
            await message.reply("Не нашёл последний расход для исправления.")
            return

        receipt_items = (
            sorted(receipt.items, key=lambda item: item.created_at) if receipt else []
        )
        previous_category = (
            await session.get(Category, last_entry.category_id) if last_entry else None
        )

    if receipt_items and receipt is not None:
        category_key_by_id = {category.id: category.key for category in category_models}
        item_context = "; ".join(
            f"{index}. {item.display_name}, {format_money(item.display_line_total)} "
            f"{receipt.original_currency or 'KZT'}, категория "
            f"{category_key_by_id.get(item.category_id, 'unknown')}"
            for index, item in enumerate(receipt_items, 1)
        )
        previous_context = (
            f"Чек магазина {receipt.merchant or 'не указан'}; итог "
            f"{format_money(receipt.original_total)} {receipt.original_currency or 'KZT'}; "
            f"позиции: {item_context}"
        )
    elif last_entry is not None:
        previous_context = (
            f"Расход: {last_entry.description}; {last_entry.original_amount} "
            f"{last_entry.original_currency}; категория "
            f"{previous_category.key if previous_category else 'unknown'}"
        )
    else:
        previous_context = "none"

    categories = {category.key: category.name for category in category_models}
    interpretation = interpretation or await deps.expense_interpreter.interpret(
        text, categories, previous_context
    )
    if interpretation.kind != "correction" or not correction_has_changes(interpretation):
        await message.reply(
            "Не понял, что именно исправить. Например: "
            "<i>«прошлый чек был не 7-Eleven, а Big C»</i>."
        )
        return
    correction = interpretation.items[0] if interpretation.items else None
    category_by_key = {category.key: category for category in category_models}

    try:
        async with deps.session_factory() as session, session.begin():
            if receipt is not None:
                locked_receipt = await session.scalar(
                    select(Receipt)
                    .where(
                        Receipt.id == receipt.id,
                        Receipt.household_id == household.id,
                        Receipt.status == "posted",
                    )
                    .options(selectinload(Receipt.items))
                    .with_for_update()
                )
                if locked_receipt is None:
                    raise ValueError("Чек уже изменён или удалён")
                items = sorted(locked_receipt.items, key=lambda item: item.created_at)
                if not items:
                    raise ValueError("В чеке нет позиций")
                before_data = {
                    "merchant": locked_receipt.merchant,
                    "total": str(locked_receipt.original_total),
                    "currency": locked_receipt.original_currency,
                    "items": [
                        {
                            "id": item.id,
                            "name": item.display_name,
                            "line_total": str(item.line_total),
                            "category_id": item.category_id,
                        }
                        for item in items
                    ],
                }
                changes: list[str] = []
                old_merchant = locked_receipt.merchant
                if interpretation.merchant:
                    new_merchant = interpretation.merchant.strip()[:200]
                    if new_merchant and new_merchant != old_merchant:
                        locked_receipt.merchant = new_merchant
                        changes.append(
                            f"магазин: <b>{escape(old_merchant or 'не указан')}</b> → "
                            f"<b>{escape(new_merchant)}</b>"
                        )

                        seven_category = category_by_key.get(SEVEN_ELEVEN_CATEGORY_KEY)
                        if is_seven_eleven_merchant(new_merchant) and seven_category:
                            for item in items:
                                item.category_id = seven_category.id
                                item.subcategory_id = None
                            changes.append("все позиции перенесены в 🏪 7-Eleven")
                        elif is_seven_eleven_merchant(old_merchant):
                            fallback_category = category_by_key.get("groceries_household")
                            if fallback_category is None:
                                raise ValueError("Не найдена категория продуктов")
                            moved = 0
                            for item in items:
                                if seven_category is None or item.category_id == seven_category.id:
                                    item.category_id = fallback_category.id
                                    item.subcategory_id = None
                                    moved += 1
                            if moved:
                                changes.append(
                                    f"{moved} поз. перенесено в {fallback_category.icon} "
                                    f"{escape(fallback_category.name)}"
                                )

                target_number: int | None = None
                target: ReceiptItem | None = None
                if correction is not None and any(
                    (
                        correction.description,
                        correction.amount is not None,
                        correction.currency,
                        correction.category_key,
                    )
                ):
                    target_number, target = resolve_corrected_receipt_item(items, correction)
                    if correction.description:
                        target.display_name_ru = correction.description[:500]
                    if correction.amount is not None:
                        corrected_amount = quantize(Decimal(str(correction.amount)))
                        target.printed_line_total = corrected_amount
                        target.line_total = corrected_amount
                    if correction.currency and len(items) > 1:
                        raise ValueError(
                            "Валюту отдельной позиции нельзя менять внутри общего чека"
                        )
                    if correction.category_key:
                        corrected_category = category_by_key.get(correction.category_key)
                        if corrected_category is None:
                            raise ValueError("Категория не найдена")
                        target.category_id = corrected_category.id
                        target.subcategory_id = None

                if interpretation.receipt_total is not None:
                    corrected_total = quantize(Decimal(str(interpretation.receipt_total)))
                    reallocate_receipt_total(items, corrected_total)
                    corrected_currency = normalize_currency(
                        interpretation.receipt_currency
                        or locked_receipt.original_currency
                        or "KZT"
                    )
                    changes.append(
                        f"итог чека: <b>{format_money(corrected_total)} "
                        f"{corrected_currency}</b>"
                    )

                currency = normalize_currency(
                    interpretation.receipt_currency
                    or (correction.currency if correction else None)
                    or locked_receipt.original_currency
                    or "KZT"
                )
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
                if locked_receipt.extraction:
                    stored_extraction = dict(locked_receipt.extraction)
                    stored_extraction.update(
                        {
                            "merchant": locked_receipt.merchant,
                            "currency": currency,
                            "total": str(locked_receipt.original_total),
                        }
                    )
                    locked_receipt.extraction = stored_extraction
                await rebuild_receipt_ledger(
                    session,
                    household,
                    locked_receipt,
                    user_id,
                    source_event_key=source_event_key,
                )
                session.add(
                    AuditLog(
                        household_id=household.id,
                        actor_user_id=user_id,
                        action="receipt_natural_correction",
                        entity_type="receipt",
                        entity_id=locked_receipt.id,
                        before_data=before_data,
                        after_data={
                            "merchant": locked_receipt.merchant,
                            "total": str(locked_receipt.original_total),
                            "currency": locked_receipt.original_currency,
                            "items": [
                                {
                                    "id": item.id,
                                    "name": item.display_name,
                                    "line_total": str(item.line_total),
                                    "category_id": item.category_id,
                                }
                                for item in items
                            ],
                        },
                    )
                )
                if target is not None and target_number is not None:
                    corrected_category = await session.get(Category, target.category_id)
                    changes.append(
                        f"позиция {target_number}: <b>{escape(target.display_name)}</b> — "
                        f"{format_money(target.display_line_total)} {currency} → "
                        f"{corrected_category.icon if corrected_category else ''} "
                        f"{escape(corrected_category.name) if corrected_category else 'категория'}"
                    )
                response = "✅ <b>Исправил прошлый чек</b>"
                if changes:
                    response += "\n" + "\n".join(f"• {change}" for change in changes)
                response += (
                    f"\nНовый итог: <b>{format_money(locked_receipt.original_total)} "
                    f"{currency}</b> · {format_money(locked_receipt.total_kzt)} ₸. "
                    "Расходы и остатки в базе пересчитаны."
                )
            else:
                if last_entry is None or correction is None:
                    raise ValueError("Последний расход не найден")
                locked_entry = await session.scalar(
                    select(LedgerEntry).where(LedgerEntry.id == last_entry.id).with_for_update()
                )
                if locked_entry is None or locked_entry.status != "posted":
                    await message.reply("Расход уже изменён или удалён.")
                    return
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
                    source_event_key=source_event_key,
                    source_item_index=0,
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


async def authorize_callback(
    callback: CallbackQuery,
    deps: TelegramDependencies,
    *,
    owner_only: bool = False,
) -> bool:
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
        if member is None:
            return False
        if owner_only and member.role != "owner":
            await callback.answer("Это действие доступно только владельцу.", show_alert=True)
            return False
        return True


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
    *,
    source_event_key: str | None = None,
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
    for source_item_index, (category_id, items) in enumerate(grouped.items()):
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
                source_event_key=source_event_key,
                source_item_index=source_item_index if source_event_key else None,
            )
        )
