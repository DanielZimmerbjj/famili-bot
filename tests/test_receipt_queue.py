from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, Receipt
from family_bot.services.cycles import get_current_cycle, seed_default_household
from family_bot.services.receipts import ReceiptService, ReceiptWorker


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
        first, first_receipt, first_image = await service.enqueue(
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
        second, second_receipt, second_image = await service.enqueue(
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
        duplicate, duplicate_receipt, duplicate_image = await service.enqueue(
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
        assert first.id == second.id == duplicate.id
        assert (first_receipt, first_image) == (True, True)
        assert (second_receipt, second_image) == (False, True)
        assert (duplicate_receipt, duplicate_image) == (False, False)
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
