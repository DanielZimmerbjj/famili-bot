import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, Category, Receipt, ReceiptImage, ReceiptItem
from family_bot.services.cycles import get_current_cycle, seed_default_household
from family_bot.services.receipts import (
    ReceiptService,
    ReceiptWorker,
    receipt_progress_text,
)


async def queue_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_allowed_chat_id=-100123,
        telegram_owner_user_id=42,
    )
    return engine, factory, settings


async def test_album_pages_are_added_without_being_reported_as_duplicates() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        service = ReceiptService(settings)
        first = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=10,
            user_id=42,
            file_id="file-1",
            file_unique_id="unique-1",
            mime_type="image/jpeg",
            media_group_id="album",
        )
        second = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=11,
            user_id=42,
            file_id="file-2",
            file_unique_id="unique-2",
            mime_type="image/jpeg",
            media_group_id="album",
        )
        duplicate = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=12,
            user_id=42,
            file_id="file-1",
            file_unique_id="unique-1",
            mime_type="image/jpeg",
            media_group_id="album",
        )
        assert first.receipt.id == second.receipt.id == duplicate.receipt.id
        assert first.outcome == "created"
        assert second.outcome == "page_added"
        assert duplicate.outcome == "already_queued"
    await engine.dispose()


async def test_receipt_keeps_message_used_for_progress_updates() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        result = await ReceiptService(settings).enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=10,
            user_id=42,
            file_id="file-1",
            file_unique_id="unique-1",
            mime_type="image/jpeg",
            media_group_id=None,
            progress_message_id=777,
        )

        assert result.receipt.progress_message_id == 777
    await engine.dispose()


async def test_worker_edits_receipt_progress_without_breaking_on_telegram_error() -> None:
    engine, factory, settings = await queue_context()
    bot = AsyncMock()
    worker = ReceiptWorker(settings, factory, bot, object(), object())  # type: ignore[arg-type]
    receipt = Receipt(
        household_id="household",
        cycle_id="cycle",
        telegram_chat_id=-100123,
        telegram_message_id=10,
        progress_message_id=777,
        created_by_user_id=42,
    )

    await worker._update_progress(receipt, 55, "распознаю чек")

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=-100123,
        message_id=777,
        text="⏳ [█████░░░░░] 55% · распознаю чек",
    )
    bot.edit_message_text.side_effect = RuntimeError("telegram unavailable")
    await worker._update_progress(receipt, 80, "проверяю суммы")
    await engine.dispose()


def test_receipt_progress_text_has_clear_terminal_states() -> None:
    assert receipt_progress_text(10, "чек принят") == (
        "⏳ [█░░░░░░░░░] 10% · чек принят"
    )
    assert receipt_progress_text(100, "чек обработан", done=True).startswith(
        "✅ [██████████] 100%"
    )
    assert receipt_progress_text(100, "обработка не удалась", done=True).startswith(
        "❌ [██████████] 100%"
    )
    assert receipt_progress_text(100, "запрос не обработан", done=True).startswith(
        "❌ [██████████] 100%"
    )


async def test_resending_failed_receipt_requeues_it_and_updates_reply_target() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        service = ReceiptService(settings)
        first = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=20,
            user_id=42,
            file_id="old-file",
            file_unique_id="same-image",
            mime_type="image/jpeg",
            media_group_id=None,
        )
        first.receipt.status = "failed"
        first.receipt.retry_count = settings.receipt_retry_limit
        first.receipt.error_message = "old extraction error"

        repeated = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=21,
            user_id=99,
            file_id="fresh-file-id",
            file_unique_id="same-image",
            mime_type="image/jpeg",
            media_group_id=None,
        )

        assert repeated.receipt.id == first.receipt.id
        assert repeated.outcome == "requeued"
        assert repeated.receipt.status == "retrying"
        assert repeated.receipt.retry_count == 0
        assert repeated.receipt.error_message is None
        assert repeated.receipt.telegram_message_id == 21
        assert repeated.receipt.created_by_user_id == 99
        saved_image = await session.scalar(
            select(ReceiptImage).where(ReceiptImage.receipt_id == repeated.receipt.id)
        )
        assert saved_image is not None
        assert saved_image.telegram_file_id == "fresh-file-id"
    await engine.dispose()


async def test_resending_posted_receipt_never_requeues_or_double_posts() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        service = ReceiptService(settings)
        first = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=30,
            user_id=42,
            file_id="posted-file",
            file_unique_id="posted-image",
            mime_type="image/jpeg",
            media_group_id=None,
        )
        first.receipt.status = "posted"

        repeated = await service.enqueue(
            session=session,
            household=household,
            cycle_id=cycle.id,
            chat_id=-100123,
            message_id=31,
            user_id=42,
            file_id="posted-file-new-id",
            file_unique_id="posted-image",
            mime_type="image/jpeg",
            media_group_id=None,
        )

        assert repeated.outcome == "already_posted"
        assert repeated.receipt.status == "posted"
        assert repeated.receipt.telegram_message_id == 30
    await engine.dispose()


async def test_worker_recovers_a_claimed_receipt_after_restart() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100123,
            telegram_message_id=20,
            created_by_user_id=42,
            status="analyzing",
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    worker = ReceiptWorker(settings, factory, object(), object(), object())  # type: ignore[arg-type]
    await worker._recover_inflight()
    async with factory() as session:
        status = await session.scalar(select(Receipt.status).where(Receipt.id == receipt_id))
        assert status == "retrying"
    await engine.dispose()


async def test_worker_continues_after_claim_error() -> None:
    engine, factory, settings = await queue_context()
    settings.receipt_poll_seconds = 0.01
    worker = ReceiptWorker(settings, factory, object(), object(), object())  # type: ignore[arg-type]
    worker._recover_inflight = AsyncMock()  # type: ignore[method-assign]
    worker._claim_next = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("database hiccup"), None]
    )

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if worker._claim_next.await_count >= 2:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    await worker.stop()
    await task

    assert worker._claim_next.await_count >= 2  # type: ignore[attr-defined]
    await engine.dispose()


async def test_terminal_failure_is_saved_even_if_telegram_notification_fails() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100123,
            telegram_message_id=30,
            created_by_user_id=42,
            status="analyzing",
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    worker = ReceiptWorker(settings, factory, bot, object(), object())  # type: ignore[arg-type]
    await worker._mark_failed(receipt_id, "invalid schema", force_terminal=True)

    async with factory() as session:
        saved = await session.get(Receipt, receipt_id)
        assert saved is not None
        assert saved.status == "failed"
        assert saved.retry_count == settings.receipt_retry_limit
        assert saved.error_message == "invalid schema"
    await engine.dispose()


async def test_review_receipt_can_be_retried_counted_current_or_dismissed() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100123,
            telegram_message_id=40,
            created_by_user_id=42,
            status="analyzing",
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    bot = AsyncMock()
    worker = ReceiptWorker(settings, factory, bot, object(), object())  # type: ignore[arg-type]
    await worker._mark_review(
        receipt_id,
        "старый финансовый месяц",
        allow_current_cycle=True,
    )

    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert callbacks == [
        f"receipt:current:{receipt_id}",
        f"receipt:retry:{receipt_id}",
        f"receipt:dismiss:{receipt_id}",
    ]
    await engine.dispose()


async def test_posted_receipt_confirmation_is_retried_after_telegram_failure() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100123,
            telegram_message_id=41,
            created_by_user_id=42,
            status="posted",
            confirmation_status="pending",
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    worker = ReceiptWorker(settings, factory, AsyncMock(), object(), object())  # type: ignore[arg-type]
    assert await worker._claim_confirmation() == receipt_id
    await worker._schedule_confirmation_retry(receipt_id, "Telegram unavailable")

    async with factory() as session:
        saved = await session.get(Receipt, receipt_id)
        assert saved is not None
        assert saved.confirmation_status == "retrying"
        assert saved.confirmation_retry_count == 1
        assert saved.error_message == "Telegram unavailable"
    await engine.dispose()


async def test_posted_receipt_is_reported_without_confirmation_button() -> None:
    engine, factory, settings = await queue_context()
    async with factory() as session, session.begin():
        household = await seed_default_household(session, settings)
        assert household is not None
        cycle = await get_current_cycle(session, household, date(2026, 8, 29), 5)
        category = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        assert category is not None
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100123,
            telegram_message_id=50,
            created_by_user_id=42,
            status="posted",
            merchant="7-Eleven",
            purchased_at=datetime(2026, 8, 29, tzinfo=UTC),
            original_currency="THB",
            original_total=Decimal("124"),
            total_kzt=Decimal("1749.64"),
            confirmation_status="sending",
            posted_at=datetime(2026, 8, 29, tzinfo=UTC),
        )
        session.add(receipt)
        await session.flush()
        session.add(
            ReceiptItem(
                receipt_id=receipt.id,
                category_id=category.id,
                raw_name="Milk",
                display_name_ru="Молоко",
                quantity=Decimal("1"),
                printed_line_total=Decimal("124"),
                line_total=Decimal("124"),
                amount_kzt=Decimal("1749.64"),
                envelope_amount=Decimal("124"),
                envelope_currency="THB",
                confidence=Decimal("0.99"),
            )
        )
        receipt_id = receipt.id

    bot = AsyncMock()
    worker = ReceiptWorker(settings, factory, bot, object(), object())  # type: ignore[arg-type]
    await worker._send_confirmation(receipt_id)

    sent = bot.send_message.await_args
    assert "Чек уже учтён автоматически" in sent.args[1]
    callbacks = [
        button.callback_data
        for row in sent.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == [
        f"receipt:edit:{receipt_id}",
        f"receipt:delete:{receipt_id}",
    ]
    await engine.dispose()
