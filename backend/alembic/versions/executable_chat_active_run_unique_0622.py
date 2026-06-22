"""Expand active chat run guard to every executable session task type.

Revision ID: executable_chat_active_run_unique_0622
Revises: cc_codex_parity_goal_team_0622
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op


revision = "executable_chat_active_run_unique_0622"
down_revision = "cc_codex_parity_goal_team_0622"
branch_labels = None
depends_on = None

_EXECUTABLE_TYPES = "('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session")
    op.execute(
        f"""
UPDATE runtime_tasks AS rt
SET
    status = 'failed',
    completed_at = COALESCE(rt.completed_at, now()),
    result_summary = COALESCE(
        rt.result_summary,
        'Superseded by newer active executable chat run before unique active-run guard migration.'
    ),
    metadata_json = (
        COALESCE(rt.metadata_json, '{{}}'::json)::jsonb
        || jsonb_build_object('superseded_by_executable_chat_active_run_guard', true)
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
        WHERE task_type IN {_EXECUTABLE_TYPES}
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
        f"WHERE task_type IN {_EXECUTABLE_TYPES} "
        "AND status IN ('pending', 'running') "
        "AND parent_agent_id IS NOT NULL "
        "AND parent_session_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session")
    op.execute(
        """
UPDATE runtime_tasks AS rt
SET
    status = 'failed',
    completed_at = COALESCE(rt.completed_at, now()),
    result_summary = COALESCE(
        rt.result_summary,
        'Superseded by newer active web chat run before unique active-run guard downgrade.'
    ),
    metadata_json = (
        COALESCE(rt.metadata_json, '{}'::json)::jsonb
        || jsonb_build_object('superseded_by_active_run_guard_downgrade', true)
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
