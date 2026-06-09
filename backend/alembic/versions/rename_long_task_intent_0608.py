"""Rename plan intent long_task -> in_session_execution (exec/automation CC-alignment).

The plan **intent** ``long_task`` was renamed to ``in_session_execution``: it gates
in-session continuation by *risk*, not by task duration (CC has no long/short split).
This backfills already-persisted ``agent_plan_requests`` rows so historical intents
match the new vocabulary.

NOT touched (intentionally): the legacy handoff *target* word ``"long_task"`` carried
inside ``plan_json.handoff.target`` / ``handoff_payload`` — it stays registered for
compat so already-persisted plans still resolve to the continuation handler.

Revision ID: rename_long_task_intent_0608
Revises: mcp_assignment_always_load_0607
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "rename_long_task_intent_0608"
down_revision: Union[str, None] = "mcp_assignment_always_load_0607"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists() -> bool:
    conn = op.get_bind()
    result = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_plan_requests'"))
    return result.scalar() is not None


def upgrade() -> None:
    if not _table_exists():
        return
    op.get_bind().execute(
        text("UPDATE agent_plan_requests SET intent_type = 'in_session_execution' WHERE intent_type = 'long_task'")
    )


def downgrade() -> None:
    # Best-effort inverse. Note it cannot distinguish rows that were natively
    # 'in_session_execution' from migrated ones; acceptable for a rename rollback.
    if not _table_exists():
        return
    op.get_bind().execute(
        text("UPDATE agent_plan_requests SET intent_type = 'long_task' WHERE intent_type = 'in_session_execution'")
    )
