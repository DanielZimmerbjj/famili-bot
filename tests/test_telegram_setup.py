from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from family_bot.config import Settings
from family_bot.models import Base, BudgetCycle, Household, Member
from family_bot.telegram.handlers import (
    BALANCE_COMMAND_RE,
    HELP_COMMAND_RE,
    SETUP_COMMAND_RE,
    authorize_message,
)


class FakeMessage:
    def __init__(self, chat_id: int, user_id: int, chat_type: str = "group") -> None:
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = SimpleNamespace(id=user_id, full_name=f"User {user_id}")
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


@pytest.mark.parametrize(
    "text",
    (
        "/setup",
        "/setup.",
        "/setup! ",
        "/setup@zimmersfamili_bot.",
        "/НАСТРОИТЬ…",
    ),
)
def test_setup_command_accepts_harmless_punctuation(text: str) -> None:
    assert SETUP_COMMAND_RE.fullmatch(text)


def test_other_simple_commands_accept_harmless_punctuation() -> None:
    assert BALANCE_COMMAND_RE.fullmatch("/balance.")
    assert BALANCE_COMMAND_RE.fullmatch("остаток?")
    assert HELP_COMMAND_RE.fullmatch("/start@zimmersfamili_bot!")
    assert not SETUP_COMMAND_RE.fullmatch("/setup now")


async def test_owner_first_group_message_automatically_seeds_budget() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    deps = SimpleNamespace(settings=settings, session_factory=factory)
    message = FakeMessage(chat_id=-100777, user_id=42)

    authorization = await authorize_message(message, deps)

    assert authorization is not None
    household, user_id = authorization
    assert household.telegram_chat_id == -100777
    assert user_id == 42
    assert message.replies == []
    async with factory() as session:
        assert await session.scalar(select(func.count(Household.id))) == 1
        assert await session.scalar(select(func.count(Member.id))) == 1
        assert await session.scalar(select(func.count(BudgetCycle.id))) == 1
    await engine.dispose()


async def test_non_owner_cannot_automatically_seed_group() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    deps = SimpleNamespace(settings=settings, session_factory=factory)
    message = FakeMessage(chat_id=-100777, user_id=99)

    assert await authorize_message(message, deps) is None
    assert message.replies == [
        "Семейный бюджет в этой группе ещё не настроен. "
        "Пусть владелец отправит <code>/setup</code>."
    ]
    async with factory() as session:
        assert await session.scalar(select(func.count(Household.id))) == 0
    await engine.dispose()


async def test_any_participant_is_automatically_added_in_bound_group() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_owner_user_id=42,
        setup_mode=True,
    )
    deps = SimpleNamespace(settings=settings, session_factory=factory)
    assert await authorize_message(FakeMessage(-100777, 42), deps) is not None

    participant_message = FakeMessage(-100777, 99)
    authorization = await authorize_message(participant_message, deps)

    assert authorization is not None
    household, user_id = authorization
    assert user_id == 99
    assert participant_message.replies == []
    async with factory() as session:
        participant = await session.scalar(
            select(Member).where(
                Member.household_id == household.id,
                Member.telegram_user_id == 99,
            )
        )
        assert participant is not None
        assert participant.role == "member"
    await engine.dispose()
