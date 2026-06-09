"""Retire the AgentObjective subsystem (drop agent_objectives table + RLS policy).

Claude Code has no "objective" concept — a schedule is just a trigger. The
objective-driven autonomous-wake subsystem and its focus.md projection were
removed, so the ``agent_objectives`` table is no longer created on fresh DBs.
Existing DBs still carry the table; this migration drops its RLS policy and the
table itself, guarded by an information_schema existence check so it is
idempotent and safe on databases that never had it.

Revision ID: retire_agent_objectives_0608
Revises: rename_long_task_intent_0608
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "retire_agent_objectives_0608"
down_revision: Union[str, None] = "rename_long_task_intent_0608"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists() -> bool:
    conn = op.get_bind()
    result = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_objectives'"))
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists():
        return
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS retired_agent_objectives_0608 (
                id UUID PRIMARY KEY,
                agent_id UUID,
                tenant_id UUID,
                archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                row_data JSONB NOT NULL
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO retired_agent_objectives_0608 (id, agent_id, tenant_id, row_data)
            SELECT id, agent_id, tenant_id, to_jsonb(agent_objectives.*)
            FROM agent_objectives
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    # Drop the RLS policy before the table (DROP TABLE would drop it too, but be explicit + idempotent).
    conn.execute(text("DROP POLICY IF EXISTS tenant_isolation_agent_objectives ON agent_objectives"))
    conn.execute(text("DROP TABLE IF EXISTS agent_objectives CASCADE"))


def downgrade() -> None:
    # No-op: this is a subsystem retirement. The objective model was deleted, so
    # there is no schema to recreate. Restoring it would require resurrecting the
    # removed application code, which is out of scope for a rollback.
    pass
