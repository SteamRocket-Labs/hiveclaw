"""Add web chat final-message idempotency guard.

Revision ID: web_chat_final_message_idempotency_0702
Revises: retire_atlassian_rovo_0629
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op


revision = "web_chat_final_message_idempotency_0702"
down_revision = "retire_atlassian_rovo_0629"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_web_chat_final_decision_trace
        ON chat_messages (decision_trace_id)
        WHERE decision_trace_id LIKE 'web_chat_final:%'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_chat_messages_web_chat_final_decision_trace")
