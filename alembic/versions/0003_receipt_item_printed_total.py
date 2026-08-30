"""Keep printed receipt prices separate from allocated paid amounts.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("receipt_items")
    }
    if "printed_line_total" not in columns:
        op.add_column(
            "receipt_items",
            sa.Column("printed_line_total", sa.Numeric(24, 8), nullable=True),
        )

    if bind.dialect.name != "postgresql":
        return

    # The extraction JSON retains the exact printed line totals. Match those
    # rows to stored items by their original receipt order so already-posted
    # receipts also display the real shelf prices after this deployment.
    op.execute(
        sa.text(
            """
            WITH ranked_items AS (
                SELECT
                    id,
                    receipt_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY receipt_id ORDER BY created_at, id
                    ) AS item_number
                FROM receipt_items
            ),
            extracted_items AS (
                SELECT
                    receipts.id AS receipt_id,
                    extracted.item_number,
                    extracted.item ->> 'line_total' AS printed_total
                FROM receipts
                CROSS JOIN LATERAL jsonb_array_elements(
                    COALESCE(receipts.extraction::jsonb -> 'items', '[]'::jsonb)
                ) WITH ORDINALITY AS extracted(item, item_number)
            )
            UPDATE receipt_items AS stored
            SET printed_line_total = extracted.printed_total::numeric
            FROM ranked_items AS ranked
            JOIN extracted_items AS extracted
              ON extracted.receipt_id = ranked.receipt_id
             AND extracted.item_number = ranked.item_number
            WHERE stored.id = ranked.id
              AND extracted.printed_total ~ '^[0-9]+([.][0-9]+)?$'
            """
        )
    )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipt_items")
    }
    if "printed_line_total" in columns:
        op.drop_column("receipt_items", "printed_line_total")
