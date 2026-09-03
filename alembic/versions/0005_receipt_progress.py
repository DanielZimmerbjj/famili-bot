"""Store the Telegram message used for receipt progress updates.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipts")
    }
    if "progress_message_id" not in columns:
        op.add_column(
            "receipts",
            sa.Column("progress_message_id", sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipts")
    }
    if "progress_message_id" in columns:
        op.drop_column("receipts", "progress_message_id")
