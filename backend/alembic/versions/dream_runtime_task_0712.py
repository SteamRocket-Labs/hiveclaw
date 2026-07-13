"""Add durable Dream RuntimeTask ownership.

Revision ID: dream_runtime_task_0712
Revises: hr_provisioning_jobs_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "dream_runtime_task_0712"
down_revision = "hr_provisioning_jobs_0712"
branch_labels = None
depends_on = None


_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "heartbeat",
    "coordinator_worker",
    "harness_canary",
    "a2a_delegation",
    "approval_execution",
    "hr_provisioning",
    "dream",
)
_LEGACY_RUNTIME_TASK_TYPES = _RUNTIME_TASK_TYPES[:-1]


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _ensure_runtime_task_type_constraint() -> None:
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'runtime_tasks'::regclass
              AND conname = 'ck_runtime_tasks_task_type'
            """
        )
    )
    if definition and all(task_type in str(definition) for task_type in _RUNTIME_TASK_TYPES):
        return
    if definition:
        op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_RUNTIME_TASK_TYPES)})",
    )


def upgrade() -> None:
    # Legacy Dream cadence is file-backed; due-state rows are backfilled by
    # reconcile_due_dream_runtime_tasks, which can inspect those files. The DB
    # migration owns only the type contract and must remain RLS-safe.
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    _ensure_runtime_task_type_constraint()


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.execute("DELETE FROM runtime_tasks WHERE task_type = 'dream'")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_LEGACY_RUNTIME_TASK_TYPES)})",
    )
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")
