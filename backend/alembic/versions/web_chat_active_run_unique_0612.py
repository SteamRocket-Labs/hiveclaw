"""Enforce one active durable web-chat run per session.

Revision ID: web_chat_active_run_unique_0612
Revises: token_usage_events_0612
Create Date: 2026-06-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "web_chat_active_run_unique_0612"
down_revision: Union[str, None] = "token_usage_events_0612"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
UPDATE runtime_tasks AS rt
SET
    status = 'failed',
    completed_at = COALESCE(rt.completed_at, now()),
    result_summary = COALESCE(
        rt.result_summary,
        'Superseded by newer active web chat run before unique active-run guard migration.'
    ),
    metadata_json = (
        COALESCE(rt.metadata_json, '{}'::json)::jsonb
        || jsonb_build_object('superseded_by_active_run_guard', true)
    )::json
WHERE rt.id IN (
    SELECT id
    FROM (
        SELECT
            id,
            row_number() OVER (
                PARTITION BY parent_agent_id, parent_session_id
                ORDER BY started_at DESC NULLS LAST, created_at DESC, id DESC
            ) AS rn
        FROM runtime_tasks
        WHERE task_type = 'web_chat_turn'
          AND status IN ('pending', 'running')
          AND parent_agent_id IS NOT NULL
          AND parent_session_id IS NOT NULL
    ) ranked
    WHERE ranked.rn > 1
)
        """.strip()
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_tasks_active_web_chat_session "
        "ON runtime_tasks (parent_agent_id, parent_session_id) "
        "WHERE task_type = 'web_chat_turn' "
        "AND status IN ('pending', 'running') "
        "AND parent_agent_id IS NOT NULL "
        "AND parent_session_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session")
