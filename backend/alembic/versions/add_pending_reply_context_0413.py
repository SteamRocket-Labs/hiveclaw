"""Add pending_reply_contexts table for cross-session context propagation.

Revision ID: add_pending_reply_context_0413
Revises: add_delegation_activity_enum_0413
Create Date: 2026-04-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "add_pending_reply_context_0413"
down_revision = "add_delegation_activity_enum_0413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_reply_contexts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_channel", sa.String(30), nullable=False),
        sa.Column("recipient_identity", sa.String(200), nullable=False),
        sa.Column("outbound_message", sa.Text, nullable=False),
        sa.Column("task_summary", sa.Text, nullable=False),
        sa.Column("originator_name", sa.String(100)),
        sa.Column("originator_identity", sa.String(200)),
        sa.Column("expected_action", sa.Text),
        sa.Column("full_context_json", JSONB),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_pending_reply_lookup", "pending_reply_contexts", ["agent_id", "recipient_identity", "status"])
    op.create_index("ix_pending_reply_expires", "pending_reply_contexts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_reply_expires", table_name="pending_reply_contexts")
    op.drop_index("ix_pending_reply_lookup", table_name="pending_reply_contexts")
    op.drop_table("pending_reply_contexts")
