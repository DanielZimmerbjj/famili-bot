from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, LedgerEntry, SavingsGoal
from family_bot.services.cycles import seed_household
from family_bot.services.expense_ai import ExpenseInterpretation, InterpretedExpenseItem
from family_bot.services.ledger import goal_balance
from family_bot.services.rates import RateService
from family_bot.telegram.handlers import handle_natural_operation


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

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

    await handle_natural_operation(
        message, deps, household, 42, "получил зарплату 840000 тенге"
    )

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
