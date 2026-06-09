"""Retire the supervision task type (drop the 4 supervision columns from tasks).

Claude Code has no "supervision" concept — a recurring watch-dog reminder is just
a trigger, which the trigger daemon already owns. The orphaned supervision reminder
service and the second ``Task`` type were removed, so the ``Task`` board is now
todo-only. This migration drops the four supervision-specific columns, each guarded
by ``DROP COLUMN IF EXISTS`` so it is idempotent and safe on databases that never
had them. The ``type`` column keeps the ``task_type_enum`` name with no DB CHECK
constraint (``create_constraint=False``), so the value is a plain string and no
enum DDL is required.

Revision ID: retire_task_supervision_0608
Revises: retire_agent_objectives_0608
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "retire_task_supervision_0608"
down_revision: Union[str, None] = "retire_agent_objectives_0608"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SUPERVISION_COLUMNS = (
    "supervision_target_user_id",
    "supervision_target_name",
    "supervision_channel",
    "remind_schedule",
)


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = :table_name"), {"table_name": table_name})
    return result.scalar() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists("tasks"):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS retired_supervision_tasks_0608 (
                    id UUID PRIMARY KEY,
                    agent_id UUID,
                    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    row_data JSONB NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO retired_supervision_tasks_0608 (id, agent_id, row_data)
                SELECT id, agent_id, to_jsonb(tasks.*)
                FROM tasks
                WHERE type::text = 'supervision'
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        conn.execute(text("DELETE FROM tasks WHERE type::text = 'supervision'"))
    for column in _SUPERVISION_COLUMNS:
        conn.execute(text(f"ALTER TABLE tasks DROP COLUMN IF EXISTS {column}"))


def downgrade() -> None:
    # No-op: this is a subsystem retirement. The supervision columns and their
    # application code were deleted, so there is no behaviour to restore by
    # recreating the columns. Resurrecting it is out of scope for a rollback.
    pass
