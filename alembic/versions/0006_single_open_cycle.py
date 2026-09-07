"""Keep exactly one manually open budget cycle per household.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_budget_cycles_one_open_per_household"


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    cycles = sa.table(
        "budget_cycles",
        sa.column("id", sa.String()),
        sa.column("household_id", sa.String()),
        sa.column("start_date", sa.Date()),
        sa.column("end_date", sa.Date()),
        sa.column("status", sa.String()),
        sa.column("closed_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    ledger = sa.table(
        "ledger_entries",
        sa.column("cycle_id", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    receipts = sa.table(
        "receipts",
        sa.column("cycle_id", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    audit = sa.table(
        "audit_log",
        sa.column("id", sa.String()),
        sa.column("household_id", sa.String()),
        sa.column("actor_user_id", sa.BigInteger()),
        sa.column("action", sa.String()),
        sa.column("entity_type", sa.String()),
        sa.column("entity_id", sa.String()),
        sa.column("before_data", sa.JSON()),
        sa.column("after_data", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    duplicate_households = bind.execute(
        sa.select(cycles.c.household_id)
        .where(cycles.c.status == "open")
        .group_by(cycles.c.household_id)
        .having(sa.func.count(cycles.c.id) > 1)
    ).scalars()
    for household_id in duplicate_households:
        open_cycles = bind.execute(
            sa.select(
                cycles.c.id,
                cycles.c.start_date,
                cycles.c.end_date,
            )
            .where(
                cycles.c.household_id == household_id,
                cycles.c.status == "open",
            )
            .order_by(cycles.c.start_date.desc())
        ).mappings().all()
        active = open_cycles[0]
        obsolete_ids = [row["id"] for row in open_cycles[1:]]
        moved_entries = bind.execute(
            sa.select(sa.func.count())
            .select_from(ledger)
            .where(ledger.c.cycle_id.in_(obsolete_ids))
        ).scalar_one()
        moved_receipts = bind.execute(
            sa.select(sa.func.count())
            .select_from(receipts)
            .where(receipts.c.cycle_id.in_(obsolete_ids))
        ).scalar_one()

        bind.execute(
            ledger.update()
            .where(ledger.c.cycle_id.in_(obsolete_ids))
            .values(cycle_id=active["id"], updated_at=now)
        )
        bind.execute(
            receipts.update()
            .where(receipts.c.cycle_id.in_(obsolete_ids))
            .values(cycle_id=active["id"], updated_at=now)
        )
        bind.execute(
            cycles.update()
            .where(cycles.c.id.in_(obsolete_ids))
            .values(status="closed", closed_at=now, updated_at=now)
        )
        bind.execute(
            audit.insert().values(
                id=str(uuid.uuid4()),
                household_id=household_id,
                actor_user_id=None,
                action="duplicate_open_cycles_repaired",
                entity_type="budget_cycle",
                entity_id=active["id"],
                before_data={
                    "open_cycles": [
                        {
                            "id": row["id"],
                            "start": str(row["start_date"]),
                            "end": str(row["end_date"]),
                        }
                        for row in open_cycles
                    ]
                },
                after_data={
                    "active_cycle": active["id"],
                    "closed_cycles": obsolete_ids,
                    "moved_ledger_entries": moved_entries,
                    "moved_receipts": moved_receipts,
                    "reason": "manual close is the only cycle rollover",
                },
                created_at=now,
            )
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("budget_cycles")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "budget_cycles",
            ["household_id"],
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
            sqlite_where=sa.text("status = 'open'"),
        )


def downgrade() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("budget_cycles")
    }
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="budget_cycles")
