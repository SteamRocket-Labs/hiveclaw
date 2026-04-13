"""Add channel delivery target fields and activity enum values.

Revision ID: add_channel_delivery_targets_0413
Revises: add_delegation_activity_enum_0413
Create Date: 2026-04-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "add_channel_delivery_targets_0413"
down_revision = "add_delegation_activity_enum_0413"
branch_labels = None
depends_on = None

_NEW_ACTIVITY_VALUES = [
    "channel_delivery_success",
    "channel_delivery_failed",
    "channel_delivery_unavailable",
    "channel_capability_denied",
]


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("delivery_target_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("agent_schedules", sa.Column("delivery_target_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    for value in _NEW_ACTIVITY_VALUES:
        op.execute(f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_column("agent_schedules", "delivery_target_json")
    op.drop_column("chat_sessions", "delivery_target_json")
    # PostgreSQL does not support DROP VALUE for enum entries.
