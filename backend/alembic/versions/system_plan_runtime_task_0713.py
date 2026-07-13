"""Add durable System Plan authoring RuntimeTask ownership.

Revision ID: system_plan_runtime_task_0713
Revises: hr_draft_recovery_0712
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op


revision = "system_plan_runtime_task_0713"
down_revision = "hr_draft_recovery_0712"
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
    "system_plan_run",
)
_LEGACY_RUNTIME_TASK_TYPES = _RUNTIME_TASK_TYPES[:-1]


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_RUNTIME_TASK_TYPES)})",
    )
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    op.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    op.execute("ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY")
    op.execute("DELETE FROM runtime_tasks WHERE task_type = 'system_plan_run'")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_LEGACY_RUNTIME_TASK_TYPES)})",
    )
    op.execute("ALTER TABLE runtime_tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_tasks FORCE ROW LEVEL SECURITY")
