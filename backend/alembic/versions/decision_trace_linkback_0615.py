"""Add decision trace linkback columns.

Revision ID: decision_trace_linkback_0615
Revises: force_all_tenant_rls_0615
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "decision_trace_linkback_0615"
down_revision = "force_all_tenant_rls_0615"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS decision_trace_id VARCHAR(128)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_decision_trace_id "
            "ON chat_messages (decision_trace_id)"
        )
        op.execute("ALTER TABLE session_feedback_events ADD COLUMN IF NOT EXISTS decision_trace_id VARCHAR(128)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_session_feedback_events_decision_trace_id "
            "ON session_feedback_events (decision_trace_id)"
        )
        return

    inspector = inspect(bind)
    for table, index_name in (
        ("chat_messages", "ix_chat_messages_decision_trace_id"),
        ("session_feedback_events", "ix_session_feedback_events_decision_trace_id"),
    ):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "decision_trace_id" not in columns:
            op.add_column(table, sa.Column("decision_trace_id", sa.String(length=128), nullable=True))
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if index_name not in indexes:
            op.create_index(index_name, table, ["decision_trace_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_session_feedback_events_decision_trace_id", table_name="session_feedback_events")
    op.drop_column("session_feedback_events", "decision_trace_id")
    op.drop_index("ix_chat_messages_decision_trace_id", table_name="chat_messages")
    op.drop_column("chat_messages", "decision_trace_id")
