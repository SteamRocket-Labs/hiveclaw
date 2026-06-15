"""Add decision trace linkback columns.

Revision ID: decision_trace_linkback_0615
Revises: force_all_tenant_rls_0615
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "decision_trace_linkback_0615"
down_revision = "force_all_tenant_rls_0615"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("decision_trace_id", sa.String(length=128), nullable=True))
    op.create_index("ix_chat_messages_decision_trace_id", "chat_messages", ["decision_trace_id"], unique=False)
    op.add_column("session_feedback_events", sa.Column("decision_trace_id", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_session_feedback_events_decision_trace_id",
        "session_feedback_events",
        ["decision_trace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_feedback_events_decision_trace_id", table_name="session_feedback_events")
    op.drop_column("session_feedback_events", "decision_trace_id")
    op.drop_index("ix_chat_messages_decision_trace_id", table_name="chat_messages")
    op.drop_column("chat_messages", "decision_trace_id")
