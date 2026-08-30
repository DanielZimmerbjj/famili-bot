"""Add receipt delivery state and ledger operation idempotency.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    receipt_columns = {column["name"] for column in inspector.get_columns("receipts")}
    receipt_additions = (
        ("force_current_cycle", sa.Boolean(), sa.false()),
        ("confirmation_status", sa.String(length=20), "'pending'"),
        ("confirmation_retry_count", sa.Integer(), "0"),
        ("confirmation_next_attempt_at", sa.DateTime(timezone=True), sa.func.now()),
        ("confirmation_sent_at", sa.DateTime(timezone=True), None),
    )
    for name, column_type, default in receipt_additions:
        if name in receipt_columns:
            continue
        op.add_column(
            "receipts",
            sa.Column(
                name,
                column_type,
                nullable=default is None,
                server_default=default,
            ),
        )

    # Existing posted receipts have already gone through the old one-shot
    # confirmation path. Do not resend their historical confirmations.
    op.execute(
        sa.text(
            """
            UPDATE receipts
               SET confirmation_status = 'sent',
                   confirmation_sent_at = COALESCE(posted_at, updated_at)
             WHERE status = 'posted'
            """
        )
    )

    ledger_columns = {column["name"] for column in inspector.get_columns("ledger_entries")}
    if "source_event_key" not in ledger_columns:
        op.add_column(
            "ledger_entries",
            sa.Column("source_event_key", sa.String(length=200), nullable=True),
        )
    if "source_item_index" not in ledger_columns:
        op.add_column(
            "ledger_entries",
            sa.Column("source_item_index", sa.Integer(), nullable=True),
        )

    constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints("ledger_entries")
    }
    if "uq_ledger_source_event_item" not in constraints:
        op.create_unique_constraint(
            "uq_ledger_source_event_item",
            "ledger_entries",
            ["source_event_key", "source_item_index"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints("ledger_entries")
    }
    if "uq_ledger_source_event_item" in constraints:
        op.drop_constraint(
            "uq_ledger_source_event_item",
            "ledger_entries",
            type_="unique",
        )

    ledger_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("ledger_entries")
    }
    for name in ("source_item_index", "source_event_key"):
        if name in ledger_columns:
            op.drop_column("ledger_entries", name)

    receipt_columns = {column["name"] for column in sa.inspect(bind).get_columns("receipts")}
    for name in (
        "confirmation_sent_at",
        "confirmation_next_attempt_at",
        "confirmation_retry_count",
        "confirmation_status",
        "force_current_cycle",
    ):
        if name in receipt_columns:
            op.drop_column("receipts", name)
