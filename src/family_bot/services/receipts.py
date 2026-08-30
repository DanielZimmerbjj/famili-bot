from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from html import escape
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from family_bot.config import Settings
from family_bot.constants import SEVEN_ELEVEN_CATEGORY_KEY
from family_bot.models import (
    BudgetCycle,
    Category,
    CategoryAlias,
    ExchangeRate,
    Household,
    LedgerEntry,
    Receipt,
    ReceiptImage,
    ReceiptItem,
    Subcategory,
    utcnow,
)
from family_bot.services.ledger import allocation_and_spend, post_expense
from family_bot.services.money import normalize_currency, quantize
from family_bot.services.rates import RateService, RateUnavailableError
from family_bot.services.receipt_ai import (
    ExtractedItem,
    ReceiptExtraction,
    ReceiptExtractionError,
    ReceiptExtractor,
)
from family_bot.services.receipt_images import ReceiptImageError, normalize_receipt_image

logger = logging.getLogger(__name__)

REVIEW_WARNING_PREFIX = "budget_review:"
MAX_RECEIPT_AGE = timedelta(days=62)
MAX_RECEIPT_FUTURE_SKEW = timedelta(days=1)


class ReceiptValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReceiptEnqueueResult:
    receipt: Receipt
    outcome: str


def normalize_purchased_at(
    extracted_at: datetime | None,
    received_at: datetime,
    local_timezone: tzinfo,
) -> datetime:
    """Keep OCR date mistakes from breaking exchange-rate lookup and posting."""
    if received_at.tzinfo is None:
        received_local = received_at.replace(tzinfo=local_timezone)
    else:
        received_local = received_at.astimezone(local_timezone)
    if extracted_at is None:
        return received_local

    if extracted_at.tzinfo is None:
        candidate = extracted_at.replace(tzinfo=local_timezone)
    else:
        candidate = extracted_at.astimezone(local_timezone)

    # Thai receipts can print years in the Buddhist calendar (Gregorian + 543).
    if 2400 <= candidate.year <= 2700:
        try:
            candidate = candidate.replace(year=candidate.year - 543)
        except ValueError:
            return received_local

    earliest = received_local - MAX_RECEIPT_AGE
    latest = received_local + MAX_RECEIPT_FUTURE_SKEW
    if not earliest <= candidate <= latest:
        return received_local
    return candidate


class ReceiptService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def enqueue(
        self,
        session: AsyncSession,
        household: Household,
        cycle_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
        file_id: str,
        file_unique_id: str,
        mime_type: str,
        media_group_id: str | None,
    ) -> ReceiptEnqueueResult:
        existing_image = await session.scalar(
            select(ReceiptImage).where(
                ReceiptImage.telegram_file_unique_id == file_unique_id,
            )
        )
        if existing_image is not None:
            existing_receipt = await session.get(Receipt, existing_image.receipt_id)
            if existing_receipt is None:
                raise RuntimeError("Receipt image points to a missing receipt")
            if existing_receipt.status == "posted":
                return ReceiptEnqueueResult(existing_receipt, "already_posted")
            if existing_receipt.status in {"failed", "needs_review", "rate_pending", "reversed"}:
                existing_receipt.cycle_id = cycle_id
                existing_receipt.telegram_chat_id = chat_id
                existing_receipt.telegram_message_id = message_id
                existing_receipt.created_by_user_id = user_id
                existing_receipt.status = "retrying"
                existing_receipt.retry_count = 0
                existing_receipt.next_attempt_at = utcnow()
                existing_receipt.error_message = None
                existing_receipt.merchant = None
                existing_receipt.purchased_at = None
                existing_receipt.original_currency = None
                existing_receipt.original_total = None
                existing_receipt.total_kzt = None
                existing_receipt.exchange_rate_id = None
                existing_receipt.model_name = None
                existing_receipt.extraction = None
                existing_receipt.overall_confidence = None
                existing_receipt.posted_at = None
                existing_image.telegram_file_id = file_id
                existing_image.mime_type = mime_type
                return ReceiptEnqueueResult(existing_receipt, "requeued")
            return ReceiptEnqueueResult(existing_receipt, "already_queued")

        receipt: Receipt | None = None
        if media_group_id:
            receipt = await session.scalar(
                select(Receipt).where(
                    Receipt.telegram_chat_id == chat_id,
                    Receipt.telegram_media_group_id == media_group_id,
                )
            )
        is_new = receipt is None
        if receipt is None:
            delay = 4 if media_group_id else 0
            receipt = Receipt(
                household_id=household.id,
                cycle_id=cycle_id,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                telegram_media_group_id=media_group_id,
                created_by_user_id=user_id,
                status="received",
                next_attempt_at=utcnow() + timedelta(seconds=delay),
            )
            session.add(receipt)
            await session.flush()
        elif receipt.status == "received":
            receipt.next_attempt_at = utcnow() + timedelta(seconds=4)

        page_order = int(
            await session.scalar(
                select(func.count(ReceiptImage.id)).where(ReceiptImage.receipt_id == receipt.id)
            )
            or 0
        )
        session.add(
            ReceiptImage(
                receipt_id=receipt.id,
                telegram_file_id=file_id,
                telegram_file_unique_id=file_unique_id,
                page_order=page_order,
                mime_type=mime_type,
            )
        )
        await session.flush()
        return ReceiptEnqueueResult(receipt, "created" if is_new else "page_added")


class ReceiptWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        extractor: ReceiptExtractor,
        rate_service: RateService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bot = bot
        self.extractor = extractor
        self.rate_service = rate_service
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        logger.info("Receipt worker started")
        recovered = False
        while not self.stop_event.is_set():
            try:
                if not recovered:
                    await self._recover_inflight()
                    recovered = True
                receipt_id = await self._claim_next()
                if receipt_id is not None:
                    await self._process(receipt_id)
                    continue
                await self._wait(self.settings.receipt_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Receipt worker iteration failed; it will continue")
                await self._wait(max(self.settings.receipt_poll_seconds, 1.0))
        logger.info("Receipt worker stopped")

    async def _wait(self, timeout: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=timeout)

    async def stop(self) -> None:
        self.stop_event.set()

    async def _recover_inflight(self) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(Receipt)
                .where(Receipt.status == "analyzing")
                .values(status="retrying", next_attempt_at=utcnow())
            )

    async def _claim_next(self) -> str | None:
        async with self.session_factory() as session, session.begin():
            statement = (
                select(Receipt)
                .where(
                    Receipt.status.in_(("received", "retrying")),
                    Receipt.next_attempt_at <= utcnow(),
                    Receipt.retry_count < self.settings.receipt_retry_limit,
                )
                .order_by(Receipt.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            receipt = await session.scalar(statement)
            if receipt is None:
                return None
            receipt.status = "analyzing"
            receipt.error_message = None
            return receipt.id

    async def _process(self, receipt_id: str) -> None:
        try:
            async with self.session_factory() as session:
                receipt = await session.scalar(
                    select(Receipt)
                    .where(Receipt.id == receipt_id)
                    .options(selectinload(Receipt.images))
                )
                if receipt is None:
                    return
                image_payloads = await self._download_images(session, receipt)
                categories, category_models, subcategory_models = await self._categories(
                    session, receipt.household_id
                )
                aliases = await self._aliases(session, receipt.household_id, category_models)
                extraction, model_name = await self.extractor.extract(
                    image_payloads, categories, aliases
                )
                await self._validate_and_post(
                    session,
                    receipt,
                    extraction,
                    model_name,
                    category_models,
                    subcategory_models,
                )
                await session.commit()
        except ReceiptValidationError as exc:
            await self._mark_review(receipt_id, str(exc))
        except RateUnavailableError as exc:
            await self._mark_review(receipt_id, f"Нет курса: {exc}", status="rate_pending")
        except ReceiptExtractionError as exc:
            logger.error(
                "Receipt %s extraction failed (retryable=%s): %s",
                receipt_id,
                exc.retryable,
                exc,
            )
            await self._mark_failed(receipt_id, str(exc), force_terminal=not exc.retryable)
        except Exception as exc:
            logger.exception("Receipt %s processing failed", receipt_id)
            await self._mark_failed(receipt_id, str(exc))
        else:
            try:
                await self._send_confirmation(receipt_id)
            except Exception:
                logger.exception(
                    "Receipt %s was posted but its Telegram confirmation failed", receipt_id
                )

    async def _download_images(
        self, session: AsyncSession, receipt: Receipt
    ) -> list[tuple[bytes, str]]:
        payloads: list[tuple[bytes, str]] = []
        storage_dir = Path(self.settings.receipt_storage_path) / receipt.id
        storage_dir.mkdir(parents=True, exist_ok=True)
        for image in sorted(receipt.images, key=lambda item: item.page_order):
            stream = BytesIO()
            await self.bot.download(image.telegram_file_id, destination=stream)
            data = stream.getvalue()
            try:
                normalized_data, normalized_mime, extension = normalize_receipt_image(data)
            except ReceiptImageError as exc:
                raise ReceiptValidationError(str(exc)) from exc
            digest = hashlib.sha256(data).hexdigest()
            duplicate = await session.scalar(
                select(ReceiptImage).where(
                    ReceiptImage.sha256 == digest,
                    ReceiptImage.receipt_id != receipt.id,
                )
            )
            if duplicate is not None:
                duplicate_receipt = await session.get(Receipt, duplicate.receipt_id)
                if duplicate_receipt is not None and duplicate_receipt.status == "posted":
                    raise ReceiptValidationError(
                        "Этот чек уже был учтён ранее; повторно расход не списан"
                    )
            path = storage_dir / f"{image.page_order + 1}{extension}"
            path.write_bytes(normalized_data)
            image.sha256 = digest
            image.storage_path = str(path)
            image.mime_type = normalized_mime
            payloads.append((normalized_data, normalized_mime))
        await session.flush()
        return payloads

    async def _categories(
        self, session: AsyncSession, household_id: str
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, Category], dict[str, Subcategory]]:
        category_rows = (
            await session.scalars(
                select(Category)
                .where(Category.household_id == household_id, Category.active.is_(True))
                .options(selectinload(Category.subcategories))
            )
        ).all()
        category_models = {category.key: category for category in category_rows}
        subcategory_models = {
            f"{category.key}:{subcategory.key}": subcategory
            for category in category_rows
            for subcategory in category.subcategories
        }
        contract = {
            category.key: tuple(subcategory.key for subcategory in category.subcategories)
            for category in category_rows
        }
        return contract, category_models, subcategory_models

    async def _aliases(
        self,
        session: AsyncSession,
        household_id: str,
        categories: dict[str, Category],
    ) -> dict[str, tuple[str, str | None]]:
        aliases = (
            await session.scalars(
                select(CategoryAlias).where(CategoryAlias.household_id == household_id)
            )
        ).all()
        category_keys = {category.id: key for key, category in categories.items()}
        return {
            alias.normalized_name: (category_keys[alias.category_id], None)
            for alias in aliases
            if alias.category_id in category_keys
        }

    async def _validate_and_post(
        self,
        session: AsyncSession,
        receipt: Receipt,
        extraction: ReceiptExtraction,
        model_name: str,
        categories: dict[str, Category],
        subcategories: dict[str, Subcategory],
    ) -> None:
        if extraction.document_type != "receipt":
            raise ReceiptValidationError("На изображении не найден чек")
        if extraction.total <= 0:
            raise ReceiptValidationError("Не удалось прочитать положительный итог чека")
        if not extraction.items:
            raise ReceiptValidationError("Не удалось прочитать позиции чека")
        currency = normalize_currency(extraction.currency)
        items, uncertain_names = prepare_receipt_items(
            extraction.items,
            extraction.merchant,
            categories,
            subcategories,
            self.settings.receipt_review_confidence,
        )
        if extraction.overall_confidence < self.settings.receipt_review_confidence:
            uncertain_names = list(dict.fromkeys([*uncertain_names, "весь чек"]))

        allocated_totals = allocate_receipt_total(
            items,
            extraction.total,
            extraction.discount,
            extraction.tax,
        )

        purchased_at = normalize_purchased_at(
            extraction.purchased_at,
            receipt.created_at,
            self.settings.timezone,
        )
        household = await session.get(Household, receipt.household_id)
        if household is None:
            raise RuntimeError("Household disappeared during receipt processing")

        grouped: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        source_rate = await self.rate_service.get_quote(session, currency, purchased_at.date())
        await session.execute(delete(ReceiptItem).where(ReceiptItem.receipt_id == receipt.id))
        for item, allocated_total in zip(items, allocated_totals, strict=True):
            category = categories[item.category_key]
            subcategory = None
            if item.subcategory_key:
                subcategory = subcategories.get(f"{item.category_key}:{item.subcategory_key}")
            amount_kzt = source_rate.to_kzt(allocated_total)
            envelope_rate = await self.rate_service.get_quote(
                session, category.envelope_currency, purchased_at.date()
            )
            envelope_amount = envelope_rate.from_kzt(amount_kzt)
            session.add(
                ReceiptItem(
                    receipt_id=receipt.id,
                    category_id=category.id,
                    subcategory_id=subcategory.id if subcategory else None,
                    raw_name=item.raw_name[:500],
                    display_name_ru=item.name_ru[:500],
                    quantity=quantize(item.quantity),
                    unit_price=quantize(item.unit_price) if item.unit_price is not None else None,
                    line_total=allocated_total,
                    amount_kzt=amount_kzt,
                    envelope_amount=envelope_amount,
                    envelope_currency=category.envelope_currency,
                    confidence=Decimal(str(item.confidence)),
                )
            )
            grouped[item.category_key] += allocated_total

        for category_key, amount in grouped.items():
            await post_expense(
                session=session,
                rate_service=self.rate_service,
                household=household,
                cycle=await session.get_one(BudgetCycle, receipt.cycle_id),
                category=categories[category_key],
                amount=amount,
                currency=currency,
                description=f"{extraction.merchant or 'Чек'} · {category_key}",
                occurred_at=purchased_at,
                user_id=receipt.created_by_user_id,
                receipt_id=receipt.id,
            )

        receipt.status = "posted"
        receipt.merchant = extraction.merchant
        receipt.purchased_at = purchased_at.astimezone(UTC)
        receipt.original_currency = currency
        receipt.original_total = quantize(extraction.total)
        receipt.total_kzt = source_rate.to_kzt(extraction.total)
        receipt.exchange_rate_id = source_rate.rate_id
        receipt.model_name = model_name
        receipt.schema_version = self.extractor.schema_version
        stored_warnings = list(extraction.warnings)
        if uncertain_names:
            stored_warnings.append(REVIEW_WARNING_PREFIX + ", ".join(uncertain_names[:5]))
        stored_extraction = extraction.model_copy(
            update={"items": items, "warnings": stored_warnings}
        )
        receipt.extraction = stored_extraction.model_dump(mode="json")
        receipt.overall_confidence = Decimal(str(extraction.overall_confidence))
        receipt.posted_at = utcnow()
        receipt.error_message = None

    async def _send_confirmation(self, receipt_id: str) -> None:
        async with self.session_factory() as session:
            receipt = await session.scalar(
                select(Receipt).where(Receipt.id == receipt_id).options(selectinload(Receipt.items))
            )
            if receipt is None or receipt.status != "posted":
                return
            category_ids = {item.category_id for item in receipt.items if item.category_id}
            categories = (
                await session.scalars(select(Category).where(Category.id.in_(category_ids)))
            ).all()
            names = {category.id: f"{category.icon} {category.name}" for category in categories}
            lines = [
                f"✅ <b>{escape(receipt.merchant or 'Чек')}</b> · "
                f"{format_money(receipt.original_total)} {receipt.original_currency}"
            ]
            extraction_warnings = (
                receipt.extraction.get("warnings", []) if receipt.extraction else []
            )
            review_warning = next(
                (
                    str(warning)[len(REVIEW_WARNING_PREFIX) :]
                    for warning in extraction_warnings
                    if str(warning).startswith(REVIEW_WARNING_PREFIX)
                ),
                None,
            )
            if review_warning:
                lines.append(
                    "⚠️ Проверьте сомнительные позиции: "
                    f"<b>{escape(review_warning)}</b>. "
                    "Если что-то неверно, исправьте текстом или голосом."
                )
            sorted_items = sorted(receipt.items, key=lambda item: item.created_at)
            for index, item in enumerate(sorted_items, 1):
                lines.append(
                    f"{index}. {escape(item.display_name)} — {format_money(item.line_total)} "
                    f"{receipt.original_currency} → "
                    f"{names.get(item.category_id, 'Категория')}"
                )
            if receipt.exchange_rate_id:
                rate = await session.get(ExchangeRate, receipt.exchange_rate_id)
                if rate is not None:
                    per_unit = Decimal(rate.rate_kzt) / Decimal(rate.nominal)
                    lines.append(
                        f"Курс: 1 {receipt.original_currency} = "
                        f"{format_money(per_unit)} ₸ ({rate.provider})"
                    )
            lines.append(f"Итого в учёте: <b>{format_money(receipt.total_kzt)} ₸</b>")
            affected_category_ids = {item.category_id for item in sorted_items if item.category_id}
            remaining_rows = await allocation_and_spend(session, receipt.cycle_id)
            for category, limit, spent in remaining_rows:
                if category.id not in affected_category_ids:
                    continue
                lines.append(
                    f"{category.icon} {category.name}: осталось "
                    f"<b>{format_money(limit - spent)} {category.envelope_currency}</b>"
                )
            lines.append("Неверно? Ответьте текстом или голосом: <i>«нет, позиция 1 — молоко»</i>.")
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Верно", callback_data=f"receipt:confirm:{receipt.id}"
                        ),
                        InlineKeyboardButton(
                            text="✏️ Исправить", callback_data=f"receipt:edit:{receipt.id}"
                        ),
                        InlineKeyboardButton(
                            text="🗑 Удалить", callback_data=f"receipt:delete:{receipt.id}"
                        ),
                    ]
                ]
            )
            await self.bot.send_message(
                receipt.telegram_chat_id,
                "\n".join(lines),
                reply_to_message_id=receipt.telegram_message_id,
                reply_markup=keyboard,
            )

    async def _mark_review(
        self, receipt_id: str, message: str, status: str = "needs_review"
    ) -> None:
        async with self.session_factory() as session, session.begin():
            receipt = await session.get(Receipt, receipt_id)
            if receipt is None:
                return
            receipt.status = status
            receipt.error_message = message[:2000]
            chat_id = receipt.telegram_chat_id
            message_id = receipt.telegram_message_id
        try:
            await self.bot.send_message(
                chat_id,
                f"⚠️ Чек пока не проведён: {escape(message)}",
                reply_to_message_id=message_id,
            )
        except Exception:
            logger.exception("Could not notify Telegram about receipt %s review", receipt_id)

    async def _mark_failed(
        self, receipt_id: str, message: str, *, force_terminal: bool = False
    ) -> None:
        should_notify = False
        chat_id = 0
        message_id = 0
        async with self.session_factory() as session, session.begin():
            receipt = await session.get(Receipt, receipt_id)
            if receipt is None:
                return
            receipt.retry_count += 1
            receipt.error_message = message[:2000]
            chat_id = receipt.telegram_chat_id
            message_id = receipt.telegram_message_id
            if force_terminal:
                receipt.retry_count = self.settings.receipt_retry_limit
            if receipt.retry_count >= self.settings.receipt_retry_limit:
                receipt.status = "failed"
                should_notify = True
            else:
                receipt.status = "retrying"
                receipt.next_attempt_at = utcnow() + timedelta(seconds=2**receipt.retry_count * 10)
        if should_notify:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Повторить",
                            callback_data=f"receipt:retry:{receipt_id}",
                        )
                    ]
                ]
            )
            try:
                await self.bot.send_message(
                    chat_id,
                    "❌ Не удалось обработать чек. Он сохранён и не списан. Бот продолжает работу.",
                    reply_to_message_id=message_id,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Could not notify Telegram about receipt %s failure", receipt_id)


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "0"
    decimal = Decimal(value)
    if decimal == decimal.to_integral():
        return f"{decimal:,.0f}".replace(",", " ")
    return f"{decimal:,.2f}".replace(",", " ")


def prepare_chargeable_items(
    items: list[ExtractedItem],
    categories: dict[str, Category],
    subcategories: dict[str, Subcategory],
    confidence_threshold: float,
) -> tuple[list[ExtractedItem], list[str]]:
    """Make every paid row postable while retaining uncertainty for the user."""
    prepared: list[ExtractedItem] = []
    uncertain_names: list[str] = []
    fallback_key = "buffer"

    for source_item in items:
        # A freebie or fully discounted row is useful extraction context, but it
        # must not create a zero-value expense or block the rest of the receipt.
        if source_item.line_total <= 0:
            continue

        item = source_item
        uncertain = item.confidence < confidence_threshold
        if item.category_key not in categories:
            if fallback_key not in categories:
                raise ReceiptValidationError(f"Неизвестная категория для позиции {item.name_ru}")
            item = item.model_copy(update={"category_key": fallback_key, "subcategory_key": None})
            uncertain = True
        elif item.subcategory_key and (
            f"{item.category_key}:{item.subcategory_key}" not in subcategories
        ):
            item = item.model_copy(update={"subcategory_key": None})
            uncertain = True

        prepared.append(item)
        if uncertain:
            uncertain_names.append(item.name_ru)

    if not prepared:
        raise ReceiptValidationError("В чеке не найдено оплаченных позиций")
    return prepared, list(dict.fromkeys(uncertain_names))


def is_seven_eleven_merchant(merchant: str | None) -> bool:
    if not merchant:
        return False
    normalized = merchant.casefold()
    if "เซเว่น" in normalized:
        return True
    compact = "".join(character for character in normalized if character.isalnum())
    return any(marker in compact for marker in ("7eleven", "seveneleven", "711", "cpall"))


async def backfill_seven_eleven_receipts(
    session: AsyncSession,
    household: Household,
) -> int:
    """Move previously posted 7-Eleven receipts into the dedicated envelope."""
    category = await session.scalar(
        select(Category).where(
            Category.household_id == household.id,
            Category.key == SEVEN_ELEVEN_CATEGORY_KEY,
        )
    )
    if category is None:
        return 0

    receipts = (
        await session.scalars(
            select(Receipt).where(
                Receipt.household_id == household.id,
                Receipt.status == "posted",
            )
        )
    ).all()
    receipt_ids = [
        receipt.id for receipt in receipts if is_seven_eleven_merchant(receipt.merchant)
    ]
    if not receipt_ids:
        return 0

    item_result = await session.execute(
        update(ReceiptItem)
        .where(
            ReceiptItem.receipt_id.in_(receipt_ids),
            ReceiptItem.category_id.is_distinct_from(category.id),
        )
        .values(category_id=category.id, subcategory_id=None)
    )
    await session.execute(
        update(LedgerEntry)
        .where(
            LedgerEntry.receipt_id.in_(receipt_ids),
            LedgerEntry.status == "posted",
            LedgerEntry.entry_type == "expense",
            LedgerEntry.category_id.is_distinct_from(category.id),
        )
        .values(category_id=category.id)
    )
    return int(item_result.rowcount or 0)


def prepare_receipt_items(
    items: list[ExtractedItem],
    merchant: str | None,
    categories: dict[str, Category],
    subcategories: dict[str, Subcategory],
    confidence_threshold: float,
) -> tuple[list[ExtractedItem], list[str]]:
    """Route every paid 7-Eleven row to its dedicated envelope."""
    if not is_seven_eleven_merchant(merchant):
        return prepare_chargeable_items(
            items,
            categories,
            subcategories,
            confidence_threshold,
        )
    if SEVEN_ELEVEN_CATEGORY_KEY not in categories:
        raise ReceiptValidationError("Не настроена категория 7-Eleven")

    prepared = [
        item.model_copy(
            update={
                "category_key": SEVEN_ELEVEN_CATEGORY_KEY,
                "subcategory_key": None,
            }
        )
        for item in items
        if item.line_total > 0
    ]
    if not prepared:
        raise ReceiptValidationError("В чеке не найдено оплаченных позиций")
    return prepared, []


def allocate_receipt_total(
    items: list[ExtractedItem],
    total: Decimal,
    discount: Decimal,
    tax: Decimal,
) -> list[Decimal]:
    """Reconcile receipt-level discount/tax and allocate the paid total across items."""
    item_total = sum((item.line_total for item in items), Decimal("0"))
    if total <= 0 or item_total <= 0:
        raise ReceiptValidationError("Итог чека и сумма позиций должны быть положительными")

    tolerance = max(Decimal("1"), abs(total) * Decimal("0.005"))
    direct_difference = abs(total - item_total)
    adjusted_difference = abs(total - (item_total - discount + tax))
    if min(direct_difference, adjusted_difference) > tolerance:
        raise ReceiptValidationError(f"Сумма позиций {item_total} не сходится с итогом {total}")

    allocations = [quantize(total * item.line_total / item_total) for item in items]
    residual = quantize(total) - sum(allocations, Decimal("0"))
    if residual:
        largest_index = max(range(len(items)), key=lambda index: items[index].line_total)
        allocations[largest_index] = quantize(allocations[largest_index] + residual)
    return allocations
