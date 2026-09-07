from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import (
    AuditLog,
    Base,
    Category,
    ExchangeRate,
    LedgerEntry,
    Receipt,
    ReceiptItem,
    SavingsGoal,
)
from family_bot.services.cycles import get_current_cycle, seed_household
from family_bot.services.expense_ai import ExpenseInterpretation, InterpretedExpenseItem
from family_bot.services.ledger import goal_balance, post_goal_contribution, post_income
from family_bot.services.rates import RateService
from family_bot.telegram.handlers import handle_natural_operation


class FakeMessage:
    def __init__(self, message_id: int = 1) -> None:
        self.replies: list[str] = []
        self.chat = SimpleNamespace(id=-100777)
        self.message_id = message_id

    async def reply(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class FakeInterpreter:
    def __init__(self, result: ExpenseInterpretation) -> None:
        self.result = result
        self.calls: list[str] = []

    async def interpret(self, text: str, *args: object, **kwargs: object) -> ExpenseInterpretation:
        self.calls.append(text)
        return self.result


async def make_budget() -> tuple[object, object, object, Settings]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    async with factory() as session, session.begin():
        household = await seed_household(session, settings, -100777, 42)
    return engine, factory, household, settings


async def test_free_text_income_is_classified_by_ai_and_posted() -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="income",
            items=[
                InterpretedExpenseItem(
                    description="зарплата",
                    amount=840000,
                    currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(message, deps, household, 42, "получил зарплату 840000 тенге")

    assert interpreter.calls == ["получил зарплату 840000 тенге"]
    assert message.replies and "Доход записан" in message.replies[0]
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count(LedgerEntry.id)).where(LedgerEntry.entry_type == "income")
            )
            == 1
        )
    await engine.dispose()


async def test_non_financial_text_is_not_posted() -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="other",
            items=[],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(message, deps, household, 42, "кто заберёт ребёнка?")

    assert interpreter.calls == ["кто заберёт ребёнка?"]
    assert message.replies and "Ничего не записал" in message.replies[0]
    async with factory() as session:
        assert await session.scalar(select(func.count(LedgerEntry.id))) == 0
    await engine.dispose()


async def test_same_telegram_message_never_posts_twice() -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="income",
            items=[
                InterpretedExpenseItem(
                    description="зарплата",
                    amount=840000,
                    currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=77)

    await handle_natural_operation(message, deps, household, 42, "зарплата 840000 тенге")
    await handle_natural_operation(message, deps, household, 42, "зарплата 840000 тенге")

    async with factory() as session:
        count = await session.scalar(
            select(func.count(LedgerEntry.id)).where(LedgerEntry.entry_type == "income")
        )
    assert count == 1
    assert len(interpreter.calls) == 1
    assert "уже учтено" in message.replies[-1]
    await engine.dispose()


async def test_natural_report_request_returns_full_report(monkeypatch) -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="report",
            items=[],
            overall_confidence=0.99,
        )
    )
    report_builder = AsyncMock(return_value="Полный семейный отчёт")
    monkeypatch.setattr("family_bot.telegram.handlers.build_report", report_builder)
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(message, deps, household, 42, "скинь отчёт")

    assert interpreter.calls == ["скинь отчёт"]
    assert message.replies == ["Полный семейный отчёт"]
    report_builder.assert_awaited_once()
    async with factory() as session:
        assert await session.scalar(select(func.count(LedgerEntry.id))) == 0
    await engine.dispose()


async def test_conversational_largest_spend_question_returns_short_answer(
    monkeypatch,
) -> None:
    engine, factory, household, settings = await make_budget()
    local_date = datetime.now(settings.timezone).date()
    occurred_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            local_date,
            settings.financial_cycle_start_day,
        )
        seven_eleven = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        assert seven_eleven is not None
        session.add(
            ExchangeRate(
                rate_date=local_date,
                currency="THB",
                nominal=Decimal("1"),
                rate_kzt=Decimal("14"),
                provider="NBK",
            )
        )
        await session.flush()
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                category_id=seven_eleven.id,
                entry_type="expense",
                description="Покупка в 7-Eleven",
                original_amount=Decimal("240"),
                original_currency="THB",
                amount_kzt=Decimal("3360"),
                envelope_amount=Decimal("240"),
                envelope_currency="THB",
                occurred_at=occurred_at,
                created_by_user_id=42,
            )
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="report",
            report_period="today",
            report_focus="largest_category",
            items=[],
            overall_confidence=0.99,
        )
    )
    full_report_builder = AsyncMock(return_value="Полный семейный отчёт")
    monkeypatch.setattr("family_bot.telegram.handlers.build_report", full_report_builder)
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=51)

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "бро, расскажи, на что я сегодня потратил большую часть денег",
    )

    assert len(message.replies) == 1
    assert "больше всего ушло" in message.replies[0]
    assert "7-Eleven" in message.replies[0]
    assert "240 THB" in message.replies[0]
    assert "Полный семейный отчёт" not in message.replies[0]
    full_report_builder.assert_not_awaited()
    await engine.dispose()


async def test_specific_category_question_returns_only_that_category() -> None:
    engine, factory, household, settings = await make_budget()
    local_date = datetime.now(settings.timezone).date()
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            local_date,
            settings.financial_cycle_start_day,
        )
        mobile = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "mobile",
            )
        )
        assert mobile is not None
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                category_id=mobile.id,
                entry_type="expense",
                description="Пополнение SIM-карты",
                original_amount=Decimal("200"),
                original_currency="THB",
                amount_kzt=Decimal("2776"),
                envelope_amount=Decimal("200"),
                envelope_currency="THB",
                occurred_at=datetime.now(UTC),
                created_by_user_id=42,
            )
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="report",
            report_period="current_cycle",
            report_focus="category_status",
            report_category_key="mobile",
            items=[],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=52)

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "Сколько я потратил на сим-карты в этом месяце?",
    )

    assert len(message.replies) == 1
    assert "SIM-карты" in message.replies[0]
    assert "200 THB" in message.replies[0]
    assert "осталось" in message.replies[0].lower()
    assert "расходы по категориям" not in message.replies[0]
    await engine.dispose()


async def test_conversational_yesterday_question_uses_yesterday_entries() -> None:
    engine, factory, household, settings = await make_budget()
    local_today = datetime.now(settings.timezone).date()
    yesterday_local_noon = datetime.combine(
        local_today - timedelta(days=1),
        time(hour=12),
        settings.timezone,
    )
    occurred_at = yesterday_local_noon.astimezone(UTC)
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            local_today,
            settings.financial_cycle_start_day,
        )
        seven_eleven = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        assert seven_eleven is not None
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                category_id=seven_eleven.id,
                entry_type="expense",
                description="Вчерашняя покупка",
                original_amount=Decimal("175"),
                original_currency="THB",
                amount_kzt=Decimal("2450"),
                envelope_amount=Decimal("175"),
                envelope_currency="THB",
                occurred_at=occurred_at,
                created_by_user_id=42,
            )
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="report",
            report_period="yesterday",
            report_focus="summary",
            items=[],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=53)

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "бро, сколько я потратил вчера денег?",
    )

    assert len(message.replies) == 1
    assert "Вчера потрачено" in message.replies[0]
    assert "2 450 ₸" in message.replies[0]
    assert "175 THB" in message.replies[0]
    assert "Ничего не записал" not in message.replies[0]
    await engine.dispose()


async def test_terse_seven_eleven_expense_without_description_is_posted() -> None:
    engine, factory, household, settings = await make_budget()
    local_date = datetime.now(settings.timezone).date()
    async with factory() as session, session.begin():
        session.add(
            ExchangeRate(
                rate_date=local_date,
                currency="THB",
                nominal=Decimal("1"),
                rate_kzt=Decimal("14"),
                provider="NBK",
            )
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="expense",
            merchant="7-Eleven",
            items=[
                InterpretedExpenseItem(
                    amount=240,
                    currency="THB",
                    category_key="seven_eleven",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=52)

    await handle_natural_operation(message, deps, household, 42, "240 бат 7/11")

    assert len(message.replies) == 1
    assert "Расход записан" in message.replies[0]
    assert "Покупка в 7-Eleven" in message.replies[0]
    assert "240 THB" in message.replies[0]
    async with factory() as session:
        entry = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.source_event_key == "message:-100777:52")
        )
        assert entry is not None
        assert entry.description == "Покупка в 7-Eleven"
        assert entry.category_id is not None
    await engine.dispose()


async def test_ai_creates_dynamic_goal_with_target_and_contribution() -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="goal_contribution",
            items=[
                InterpretedExpenseItem(
                    description="Начинаю копить на ноутбук",
                    amount=300000,
                    currency="KZT",
                    goal_name="Ноутбук",
                    target_amount=1000000,
                    target_currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "ноутбук стоит миллион, отложил 300 тысяч тенге",
    )

    async with factory() as session:
        goal = await session.scalar(
            select(SavingsGoal).where(
                SavingsGoal.household_id == household.id,
                SavingsGoal.name == "Ноутбук",
            )
        )
        assert goal is not None
        assert goal.target_amount == 1000000
        assert await goal_balance(session, household.id, goal.id) == 300000
    assert message.replies and "осталось 700 000" in message.replies[0]
    await engine.dispose()


async def test_ai_renames_and_reprices_existing_goal_without_losing_savings() -> None:
    engine, factory, household, settings = await make_budget()
    rates = RateService()
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            datetime.now(UTC).date(),
            settings.financial_cycle_start_day,
        )
        macbook = SavingsGoal(
            household_id=household.id,
            key="custom_macbook",
            name="MacBook M5",
            icon="💻",
            goal_type="goal",
            target_amount=Decimal("1500000"),
            monthly_target=Decimal("0"),
            currency="KZT",
        )
        session.add(macbook)
        await session.flush()
        await post_goal_contribution(
            session,
            rates,
            household,
            cycle,
            macbook.key,
            Decimal("300000"),
            "KZT",
            datetime.now(UTC),
            42,
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="goal_update",
            items=[
                InterpretedExpenseItem(
                    goal_key="custom_macbook",
                    goal_name="MacBook M5",
                    new_goal_name="MacBook M6",
                    target_amount=1000000,
                    target_currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=rates,
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "измени цель MacBook M5 на MacBook M6, теперь он стоит миллион тенге",
    )

    async with factory() as session:
        goals = (
            await session.scalars(select(SavingsGoal).where(SavingsGoal.key == "custom_macbook"))
        ).all()
        assert len(goals) == 1
        assert goals[0].name == "MacBook M6"
        assert goals[0].target_amount == 1000000
        assert await goal_balance(session, household.id, goals[0].id) == 300000
        assert (
            await session.scalar(
                select(func.count(SavingsGoal.id)).where(SavingsGoal.name == "MacBook M5")
            )
            == 0
        )
    assert message.replies and "MacBook M5 → <b>MacBook M6</b>" in message.replies[0]
    assert "осталось 700 000" in message.replies[0]
    await engine.dispose()


async def test_goal_update_never_creates_a_missing_goal() -> None:
    engine, factory, household, settings = await make_budget()
    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="goal_update",
            items=[
                InterpretedExpenseItem(
                    goal_name="Несуществующий MacBook",
                    new_goal_name="MacBook M6",
                    target_amount=1000000,
                    target_currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage()

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "измени несуществующую цель на MacBook M6",
    )

    assert message.replies and "не нашёл существующую цель" in message.replies[0]
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count(SavingsGoal.id)).where(SavingsGoal.name == "MacBook M6")
            )
            == 0
        )
    await engine.dispose()


async def test_any_member_can_correct_latest_receipt_merchant_and_total() -> None:
    engine, factory, household, settings = await make_budget()
    occurred_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            occurred_at.date(),
            settings.financial_cycle_start_day,
        )
        seven_eleven = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "seven_eleven",
            )
        )
        groceries = await session.scalar(
            select(Category).where(
                Category.household_id == household.id,
                Category.key == "groceries_household",
            )
        )
        assert seven_eleven is not None
        assert groceries is not None
        thb_rate = ExchangeRate(
            rate_date=occurred_at.date(),
            currency="THB",
            nominal=Decimal("1"),
            rate_kzt=Decimal("14"),
            provider="NBK",
        )
        session.add(thb_rate)
        await session.flush()
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100777,
            telegram_message_id=900,
            created_by_user_id=42,
            status="posted",
            merchant="7-Eleven",
            purchased_at=occurred_at,
            original_currency="THB",
            original_total=Decimal("124"),
            total_kzt=Decimal("1736"),
            exchange_rate_id=thb_rate.id,
            posted_at=occurred_at,
        )
        session.add(receipt)
        await session.flush()
        item = ReceiptItem(
            receipt_id=receipt.id,
            category_id=seven_eleven.id,
            raw_name="Milk",
            display_name_ru="Молоко",
            quantity=Decimal("1"),
            printed_line_total=Decimal("124"),
            line_total=Decimal("124"),
            amount_kzt=Decimal("1736"),
            envelope_amount=Decimal("124"),
            envelope_currency="THB",
            confidence=Decimal("0.99"),
        )
        session.add(item)
        session.add(
            LedgerEntry(
                household_id=household.id,
                cycle_id=cycle.id,
                category_id=seven_eleven.id,
                receipt_id=receipt.id,
                entry_type="expense",
                description="7-Eleven · seven_eleven",
                original_amount=Decimal("124"),
                original_currency="THB",
                amount_kzt=Decimal("1736"),
                envelope_amount=Decimal("124"),
                envelope_currency="THB",
                exchange_rate_id=thb_rate.id,
                occurred_at=occurred_at,
                created_by_user_id=42,
            )
        )
        receipt_id = receipt.id
        seven_eleven_id = seven_eleven.id

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="correction",
            merchant="Big C",
            receipt_total=220,
            receipt_currency="THB",
            items=[],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=901)

    # A second household member corrects the receipt originally uploaded by user 42.
    await handle_natural_operation(
        message,
        deps,
        household,
        99,
        "прошлый чек был не 7-Eleven, а Big C, итог был 220 бат",
    )

    async with factory() as session:
        corrected_receipt = await session.get(Receipt, receipt_id)
        assert corrected_receipt is not None
        assert corrected_receipt.merchant == "Big C"
        assert corrected_receipt.original_total == Decimal("220")
        assert corrected_receipt.total_kzt == Decimal("3080")
        corrected_item = await session.scalar(
            select(ReceiptItem).where(ReceiptItem.receipt_id == receipt_id)
        )
        assert corrected_item is not None
        # A merchant rename does not guess a different spending purpose.
        assert corrected_item.category_id == seven_eleven_id
        assert corrected_item.line_total == Decimal("220")
        posted = (
            await session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.receipt_id == receipt_id,
                    LedgerEntry.status == "posted",
                )
            )
        ).all()
        assert len(posted) == 1
        assert posted[0].category_id == seven_eleven_id
        assert posted[0].source_event_key == "message:-100777:901"
    assert message.replies and "Исправил чек" in message.replies[0]
    assert "Big C" in message.replies[0]
    assert "Расходы и остатки в базе пересчитаны" in message.replies[0]
    await engine.dispose()


async def test_conversational_correction_moves_whole_seven_eleven_receipt_to_mobile() -> None:
    engine, factory, household, settings = await make_budget()
    occurred_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            datetime.now(settings.timezone).date(),
            settings.financial_cycle_start_day,
        )
        categories = (
            await session.scalars(select(Category).where(Category.household_id == household.id))
        ).all()
        category_by_key = {category.key: category for category in categories}
        seven_eleven = category_by_key["seven_eleven"]
        mobile = category_by_key["mobile"]
        rate = ExchangeRate(
            rate_date=occurred_at.date(),
            currency="THB",
            nominal=Decimal("1"),
            rate_kzt=Decimal("14"),
            provider="NBK",
        )
        session.add(rate)
        await session.flush()
        receipt = Receipt(
            household_id=household.id,
            cycle_id=cycle.id,
            telegram_chat_id=-100777,
            telegram_message_id=920,
            created_by_user_id=42,
            status="posted",
            merchant="7-Eleven",
            purchased_at=occurred_at,
            original_currency="THB",
            original_total=Decimal("252.50"),
            total_kzt=Decimal("3535"),
            exchange_rate_id=rate.id,
            posted_at=occurred_at,
        )
        session.add(receipt)
        await session.flush()
        session.add_all(
            [
                ReceiptItem(
                    receipt_id=receipt.id,
                    category_id=seven_eleven.id,
                    raw_name="AIS TOPUP",
                    display_name_ru="Пополнение SIM-карты",
                    quantity=Decimal("1"),
                    printed_line_total=Decimal("200"),
                    line_total=Decimal("200"),
                    amount_kzt=Decimal("2800"),
                    envelope_amount=Decimal("200"),
                    envelope_currency="THB",
                    confidence=Decimal("0.99"),
                ),
                ReceiptItem(
                    receipt_id=receipt.id,
                    category_id=seven_eleven.id,
                    raw_name="DTAC TOPUP",
                    display_name_ru="Пополнение второй SIM-карты",
                    quantity=Decimal("1"),
                    printed_line_total=Decimal("52.50"),
                    line_total=Decimal("52.50"),
                    amount_kzt=Decimal("735"),
                    envelope_amount=Decimal("52.50"),
                    envelope_currency="THB",
                    confidence=Decimal("0.99"),
                ),
                LedgerEntry(
                    household_id=household.id,
                    cycle_id=cycle.id,
                    category_id=seven_eleven.id,
                    receipt_id=receipt.id,
                    entry_type="expense",
                    description="7-Eleven · seven_eleven",
                    original_amount=Decimal("252.50"),
                    original_currency="THB",
                    amount_kzt=Decimal("3535"),
                    envelope_amount=Decimal("252.50"),
                    envelope_currency="THB",
                    exchange_rate_id=rate.id,
                    occurred_at=occurred_at,
                    created_by_user_id=42,
                ),
            ]
        )
        receipt_id = receipt.id
        mobile_id = mobile.id

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="correction",
            target_entry_ref="T1",
            correction_scope="whole_receipt",
            items=[
                InterpretedExpenseItem(
                    category_key="mobile",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=921)

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "А почему ты записал расходы в 7-Eleven, а не в SIM-карты? "
        "Для SIM-карты есть отдельная статья расходов.",
    )

    async with factory() as session:
        items = (
            await session.scalars(select(ReceiptItem).where(ReceiptItem.receipt_id == receipt_id))
        ).all()
        posted = (
            await session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.receipt_id == receipt_id,
                    LedgerEntry.status == "posted",
                )
            )
        ).all()
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "receipt_natural_correction")
        )
        assert {item.category_id for item in items} == {mobile_id}
        assert len(posted) == 1
        assert posted[0].category_id == mobile_id
        assert posted[0].original_amount == Decimal("252.50")
        assert audit is not None
    assert message.replies and "весь чек перенесён" in message.replies[0]
    assert "Две SIM-карты" in message.replies[0]
    await engine.dispose()


async def test_conversational_correction_can_update_income() -> None:
    engine, factory, household, settings = await make_budget()
    occurred_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        cycle = await get_current_cycle(
            session,
            household,
            datetime.now(settings.timezone).date(),
            settings.financial_cycle_start_day,
        )
        await post_income(
            session,
            RateService(),
            household,
            cycle,
            Decimal("840000"),
            "KZT",
            "Зарплата",
            occurred_at,
            42,
        )

    interpreter = FakeInterpreter(
        ExpenseInterpretation(
            kind="correction",
            target_entry_ref="T1",
            correction_scope="operation",
            items=[
                InterpretedExpenseItem(
                    description="Зарплата с премией",
                    amount=850000,
                    currency="KZT",
                    confidence=0.99,
                )
            ],
            overall_confidence=0.99,
        )
    )
    deps = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        rate_service=RateService(),
        expense_interpreter=interpreter,
    )
    message = FakeMessage(message_id=930)

    await handle_natural_operation(
        message,
        deps,
        household,
        42,
        "В последней зарплате была премия, исправь доход на 850 тысяч тенге",
    )

    async with factory() as session:
        entries = (
            await session.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.entry_type == "income")
                .order_by(LedgerEntry.created_at)
            )
        ).all()
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "ledger_income_natural_correction")
        )
        assert [entry.status for entry in entries] == ["reversed", "posted"]
        assert entries[-1].original_amount == Decimal("850000")
        assert entries[-1].reversal_of_id == entries[0].id
        assert audit is not None
    assert message.replies and "Исправил доход" in message.replies[0]
    await engine.dispose()
