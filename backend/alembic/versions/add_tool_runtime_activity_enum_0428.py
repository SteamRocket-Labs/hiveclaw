"""Add tool runtime activity enum values.

Revision ID: add_tool_runtime_activity_enum_0428
Revises: add_agent_objectives_0427
Create Date: 2026-04-28
"""

from alembic import op


revision = "add_tool_runtime_activity_enum_0428"
down_revision = "add_agent_objectives_0427"
branch_labels = None
depends_on = None


_NEW_VALUES = [
    "tool_call_direct",
    "tool_call_approved",
]


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely. Old code simply will not
    # write these action types after downgrade.
    pass
