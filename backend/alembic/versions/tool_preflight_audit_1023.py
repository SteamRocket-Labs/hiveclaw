"""Persist governed tool preflight activity without dropping its event type."""

from alembic import op
import sqlalchemy as sa

revision = "tool_preflight_audit_1023"
down_revision = "private_browser_control_1023"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'action_preflight'")
    op.execute("ALTER TABLE agent_activity_logs DROP CONSTRAINT IF EXISTS ck_agent_activity_logs_legacy_action_type")


def downgrade():
    op.execute("LOCK TABLE agent_activity_logs IN ACCESS EXCLUSIVE MODE")
    populated = op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM agent_activity_logs WHERE action_type::text = 'action_preflight')"
    ))
    if populated:
        raise RuntimeError("Retain action_preflight audit evidence; downgrade requires a compatible reader")
    # The retained PostgreSQL label must not create rows the previous reader cannot decode.
    op.create_check_constraint(
        "ck_agent_activity_logs_legacy_action_type", "agent_activity_logs", "action_type::text <> 'action_preflight'"
    )
