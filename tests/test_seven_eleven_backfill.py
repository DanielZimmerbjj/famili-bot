from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.models import (
    Base,
    BudgetCycle,
    Category,
    Household,
    LedgerEntry,
    Receipt,
    ReceiptItem,
)
from family_bot.services.receipts import backfill_seven_eleven_receipts


async def test_backfills_existing_seven_eleven_receipt_idempotently() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        household = Household(name="Family", telegram_chat_id=-1001)
        session.add(household)
        await session.flush()
        groceries = Category(
            household_id=household.id,
            key="groceries_household",
            name="Groceries",
            envelope_currency="THB",
            default_limit=Decimal("10000"),
        )
        seven_eleven = Category(
            household_id=household.id,
            key="seven_eleven",
            name="7-Eleven",
            envelope_currency="THB",
            default_limit=Decimal("3000"),
        )
        session.add_all([groceries, seven_eleven])
        await session.flush()
        cycle = BudgetCycle(
            household_id=household.id,
            start_date=date(2026, 8, 5),
            end_date=date(2026, 9, 4),
        )
        session.add(cycle)
        await session.flush()
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-1001,
            telegram_message_id=10,
            created_by_user_id=42,
            status="posted",
            merchant="CP ALL, 7-Eleven",
        )
        session.add(receipt)
        await session.flush()
        item = ReceiptItem(
            receipt_id=receipt.id,
            category_id=groceries.id,
            raw_name="Milk",
            line_total=Decimal("124"),
        )
        entry = LedgerEntry(
            household_id=household.id,
            cycle_id=cycle.id,
            category_id=groceries.id,
            receipt_id=receipt.id,
            entry_type="expense",
            description="Receipt",
            original_amount=Decimal("124"),
            original_currency="THB",
            amount_kzt=Decimal("1749.64"),
            envelope_amount=Decimal("124"),
            envelope_currency="THB",
        )
        session.add_all([item, entry])
        await session.flush()

        assert await backfill_seven_eleven_receipts(session, household) == 1
        assert item.category_id == seven_eleven.id
        assert item.subcategory_id is None
        assert entry.category_id == seven_eleven.id
        assert await backfill_seven_eleven_receipts(session, household) == 0

    await engine.dispose()
