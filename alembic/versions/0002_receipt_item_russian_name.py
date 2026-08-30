"""Add Russian user-facing receipt item names.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipt_items")
    }
    if "display_name_ru" not in columns:
        op.add_column(
            "receipt_items",
            sa.Column("display_name_ru", sa.String(length=500), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipt_items")
    }
    if "display_name_ru" in columns:
        op.drop_column("receipt_items", "display_name_ru")
