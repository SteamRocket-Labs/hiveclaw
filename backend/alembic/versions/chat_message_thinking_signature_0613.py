"""Persist provider thinking signatures for chat history replay.

Revision ID: chat_message_thinking_signature_0613
Revises: session_feedback_events_0613
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "chat_message_thinking_signature_0613"
down_revision: Union[str, None] = "session_feedback_events_0613"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS thinking_signature TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS thinking_signature")
