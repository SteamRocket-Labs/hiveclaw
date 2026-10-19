"""Admit durable outgoing remote protocol tasks without relabeling old work."""

from alembic import op
import sqlalchemy as sa

revision = "remote_a2a_tasks_1020"
down_revision = "remote_a2a_connections_1020"
branch_labels = None
depends_on = None

_OLD_TYPES = (
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
    "a2a_continuation",
    "readiness_probe",
)
_OLD_SOURCES = (
    "subagent",
    "agent_team",
    "workflow",
    "trigger",
    "delegation",
    "a2a_delegation",
    "a2a_continuation",
    "runtime_budget",
    "approval",
    "employee_creation",
)


def _constraint(types):
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        "task_type IN (" + ", ".join(repr(value) for value in types) + ")",
    )


def upgrade():
    _constraint((*_OLD_TYPES, "remote_a2a"))
    _sources((*_OLD_SOURCES, "remote_a2a"))


def _sources(values):
    op.drop_constraint("ck_runtime_notification_outbox_source_kind", "runtime_notification_outbox", type_="check")
    op.create_check_constraint(
        "ck_runtime_notification_outbox_source_kind",
        "runtime_notification_outbox",
        "source_kind IN (" + ", ".join(repr(value) for value in values) + ")",
    )


def downgrade():
    op.execute("SELECT set_config('app.current_tenant_id', 'BYPASS', true)")
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM runtime_tasks WHERE task_type='remote_a2a' LIMIT 1")
    ) or op.get_bind().scalar(
        sa.text("SELECT 1 FROM runtime_notification_outbox WHERE source_kind='remote_a2a' LIMIT 1")
    ):
        raise RuntimeError("Retain remote task evidence before downgrading; work is never deleted or relabeled")
    _constraint(_OLD_TYPES)
    _sources(_OLD_SOURCES)
