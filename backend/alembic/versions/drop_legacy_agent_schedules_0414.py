"""Promote and drop legacy agent_schedules table.

Revision ID: drop_legacy_agent_schedules_0414
Revises: merge_channel_delivery_and_tenant_tool_heads_0413
Create Date: 2026-04-14
"""

from alembic import op

from app.db_legacy_schedule_migration import promote_legacy_schedules_to_triggers

revision = "drop_legacy_agent_schedules_0414"
down_revision = "merge_channel_delivery_and_tenant_tool_heads_0413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    promote_legacy_schedules_to_triggers(op.get_bind())


def downgrade() -> None:
    # Legacy schedule table removal is irreversible by design.
    pass
