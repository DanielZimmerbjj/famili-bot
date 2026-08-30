from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


Money = Numeric(24, 8)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Household(Base, TimestampMixin):
    __tablename__ = "households"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), default="Семья")
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Bangkok")
    base_currency: Mapped[str] = mapped_column(String(3), default="KZT")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    members: Mapped[list[Member]] = relationship(back_populates="household")
    categories: Mapped[list[Category]] = relationship(back_populates="household")


class Member(Base, TimestampMixin):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("household_id", "telegram_user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(20), default="member")
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    household: Mapped[Household] = relationship(back_populates="members")


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("household_id", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    icon: Mapped[str] = mapped_column(String(16), default="💳")
    envelope_currency: Mapped[str] = mapped_column(String(3), default="THB")
    default_limit: Mapped[Decimal] = mapped_column(Money)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    household: Mapped[Household] = relationship(back_populates="categories")
    subcategories: Mapped[list[Subcategory]] = relationship(back_populates="category")


class Subcategory(Base, TimestampMixin):
    __tablename__ = "subcategories"
    __table_args__ = (UniqueConstraint("category_id", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))

    category: Mapped[Category] = relationship(back_populates="subcategories")


class BudgetCycle(Base, TimestampMixin):
    __tablename__ = "budget_cycles"
    __table_args__ = (UniqueConstraint("household_id", "start_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")
    expected_income_kzt: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    mandatory_kzt: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    car_target_kzt: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    border_run_target_kzt: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BudgetAllocation(Base, TimestampMixin):
    __tablename__ = "budget_allocations"
    __table_args__ = (UniqueConstraint("cycle_id", "category_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cycle_id: Mapped[str] = mapped_column(ForeignKey("budget_cycles.id", ondelete="CASCADE"))
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="THB")


class IncomeSource(Base, TimestampMixin):
    __tablename__ = "income_sources"
    __table_args__ = (UniqueConstraint("household_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))
    expected_amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SavingsGoal(Base, TimestampMixin):
    __tablename__ = "savings_goals"
    __table_args__ = (UniqueConstraint("household_id", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    icon: Mapped[str] = mapped_column(String(16), default="🎯")
    goal_type: Mapped[str] = mapped_column(String(24), default="goal")
    target_amount: Mapped[Decimal] = mapped_column(Money)
    monthly_target: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    recurrence_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Receipt(Base, TimestampMixin):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("telegram_chat_id", "telegram_message_id"),
        Index("ix_receipts_work", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("budget_cycles.id", ondelete="CASCADE"))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    telegram_media_group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="received")
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    original_total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    total_kzt: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    exchange_rate_id: Mapped[str | None] = mapped_column(
        ForeignKey("exchange_rates.id", ondelete="SET NULL"), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extraction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    force_current_cycle: Mapped[bool] = mapped_column(Boolean, default=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_status: Mapped[str] = mapped_column(String(20), default="pending")
    confirmation_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmation_next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    images: Mapped[list[ReceiptImage]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )
    items: Mapped[list[ReceiptItem]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class ReceiptImage(Base, TimestampMixin):
    __tablename__ = "receipt_images"
    __table_args__ = (
        UniqueConstraint("receipt_id", "telegram_file_unique_id"),
        Index("ix_receipt_images_sha256", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"))
    telegram_file_id: Mapped[str] = mapped_column(String(512))
    telegram_file_unique_id: Mapped[str] = mapped_column(String(256))
    page_order: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(120), default="image/jpeg")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    receipt: Mapped[Receipt] = relationship(back_populates="images")


class ReceiptItem(Base, TimestampMixin):
    __tablename__ = "receipt_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"))
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    subcategory_id: Mapped[str | None] = mapped_column(
        ForeignKey("subcategories.id", ondelete="SET NULL"), nullable=True
    )
    raw_name: Mapped[str] = mapped_column(String(500))
    display_name_ru: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal("1"))
    unit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    printed_line_total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    line_total: Mapped[Decimal] = mapped_column(Money)
    amount_kzt: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    envelope_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    envelope_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 5), default=Decimal("0"))

    receipt: Mapped[Receipt] = relationship(back_populates="items")

    @property
    def display_name(self) -> str:
        """Russian user-facing name, with a fallback for receipts created before v1.1."""
        return self.display_name_ru or self.raw_name

    @property
    def display_line_total(self) -> Decimal:
        """Price printed on the receipt, falling back to the allocated legacy value."""
        return (
            self.printed_line_total
            if self.printed_line_total is not None
            else self.line_total
        )


class ExchangeRate(Base, TimestampMixin):
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency", "provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rate_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    nominal: Mapped[Decimal] = mapped_column(Money, default=Decimal("1"))
    rate_kzt: Mapped[Decimal] = mapped_column(Money)
    provider: Mapped[str] = mapped_column(String(64), default="NBK")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)


class LedgerEntry(Base, TimestampMixin):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_cycle_type", "cycle_id", "entry_type"),
        UniqueConstraint(
            "source_event_key",
            "source_item_index",
            name="uq_ledger_source_event_item",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("budget_cycles.id", ondelete="CASCADE"))
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("receipts.id", ondelete="SET NULL"), nullable=True
    )
    income_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("income_sources.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[str | None] = mapped_column(
        ForeignKey("savings_goals.id", ondelete="SET NULL"), nullable=True
    )
    entry_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(500))
    original_amount: Mapped[Decimal] = mapped_column(Money)
    original_currency: Mapped[str] = mapped_column(String(3))
    amount_kzt: Mapped[Decimal] = mapped_column(Money)
    envelope_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    envelope_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    exchange_rate_id: Mapped[str | None] = mapped_column(
        ForeignKey("exchange_rates.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_event_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_item_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="posted")
    reversal_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("ledger_entries.id", ondelete="SET NULL"), nullable=True
    )


class CategoryAlias(Base, TimestampMixin):
    __tablename__ = "category_aliases"
    __table_args__ = (UniqueConstraint("household_id", "normalized_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    normalized_name: Mapped[str] = mapped_column(String(500))
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    subcategory_id: Mapped[str | None] = mapped_column(
        ForeignKey("subcategories.id", ondelete="SET NULL"), nullable=True
    )
    confirmations: Mapped[int] = mapped_column(Integer, default=1)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    telegram_update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledRun(Base):
    __tablename__ = "scheduled_runs"
    __table_args__ = (UniqueConstraint("household_id", "run_date", "run_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    run_date: Mapped[date] = mapped_column(Date)
    run_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(120))
    entity_id: Mapped[str] = mapped_column(String(36))
    before_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
