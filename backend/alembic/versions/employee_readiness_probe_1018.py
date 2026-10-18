"""Admit durable no-inference employee readiness checks."""

from alembic import op
import sqlalchemy as sa

revision = "employee_readiness_probe_1018"
down_revision = "channel_prompt_identity_1018"
branch_labels = None
depends_on = None

_OLD_TYPES = ("web_chat_turn", "goal_continuation", "team_member", "advanced_plan", "workflow", "delegation",
    "business_task", "subagent", "trigger", "heartbeat", "coordinator_worker", "harness_canary", "a2a_delegation",
    "approval_execution", "hr_provisioning", "dream", "a2a_continuation")


def _constraint(types):
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.create_check_constraint("ck_runtime_tasks_task_type", "runtime_tasks",
        "task_type IN (" + ", ".join(repr(value) for value in types) + ")")


def upgrade():
    _constraint((*_OLD_TYPES, "readiness_probe"))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM runtime_tasks WHERE task_type='readiness_probe' LIMIT 1")):
        raise RuntimeError("Retain readiness receipts before downgrading; this migration does not delete or relabel work")
    _constraint(_OLD_TYPES)
