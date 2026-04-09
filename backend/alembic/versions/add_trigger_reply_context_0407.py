"""Add reply_context JSONB to agent_triggers for channel-aware delivery.

Revision ID: add_trigger_reply_context_0407
Revises: add_unique_agent_tool_assignment_0402
Create Date: 2026-04-07
"""

from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "add_trigger_reply_context_0407"
down_revision: Union[str, None] = "add_unique_agent_tool_assignment_0402"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_triggers", sa.Column("reply_context", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_triggers", "reply_context")
