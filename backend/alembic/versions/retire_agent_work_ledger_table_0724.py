"""Retire the unused database Work Ledger authority without losing legacy rows.

The live Work Ledger is the governed file artifact consumed by the runtime,
resume path, tools, Memory T2 projection, and employee APIs.  The ORM table was
never wired to those paths.  Upgrades preserve any legacy table byte-for-byte
under a dated retired name; downgrades restore the original name.

Revision ID: retire_agent_work_ledger_table_0724
Revises: company_knowledge_promotion_intake_0724
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision = "retire_agent_work_ledger_table_0724"
down_revision = "company_knowledge_promotion_intake_0724"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_TABLE = "agent_work_ledgers"
RETIRED_TABLE = "retired_agent_work_ledgers_20260724"


def _table_exists(table_name: str) -> bool:
    return (
        op.get_bind()
        .execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:table_name"),
            {"table_name": table_name},
        )
        .scalar()
        is not None
    )


def _rename_table(source: str, target: str) -> None:
    source_exists = _table_exists(source)
    target_exists = _table_exists(target)
    if source_exists and target_exists:
        raise RuntimeError(f"cannot retire Work Ledger table while both {source!r} and {target!r} exist")
    if not source_exists:
        return
    op.execute(text(f'ALTER TABLE "{source}" RENAME TO "{target}"'))


def upgrade() -> None:
    _rename_table(ACTIVE_TABLE, RETIRED_TABLE)
    if _table_exists(RETIRED_TABLE):
        op.execute(
            text(
                f'COMMENT ON TABLE "{RETIRED_TABLE}" IS '
                "'Retired 2026-07-24: preserved legacy DB Work Ledger rows; "
                "live authority is the governed file Work Ledger.'"
            )
        )


def downgrade() -> None:
    _rename_table(RETIRED_TABLE, ACTIVE_TABLE)
    if _table_exists(ACTIVE_TABLE):
        op.execute(text(f'COMMENT ON TABLE "{ACTIVE_TABLE}" IS NULL'))
