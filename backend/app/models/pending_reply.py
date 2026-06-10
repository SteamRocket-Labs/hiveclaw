"""Pending reply context — automatic cross-session context propagation.

When an agent sends an outbound message (Feishu, Slack, web, etc.) on behalf
of a user, this record captures the task context so that when the recipient
replies in a separate session, the agent knows WHY it sent the message and
WHAT to do with the reply.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PendingReplyContext(Base):
    """Cross-session context for outbound messages awaiting a reply."""

    __tablename__ = "pending_reply_contexts"
    __table_args__ = (
        Index("ix_pending_reply_lookup", "agent_id", "recipient_identity", "status"),
        Index("ix_pending_reply_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)

    # Recipient identification (normalized: "feishu:{user_id}", "web:{username}", etc.)
    recipient_channel: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient_identity: Mapped[str] = mapped_column(String(200), nullable=False)

    # What was sent
    outbound_message: Mapped[str] = mapped_column(Text, nullable=False)

    # Task context — WHY it was sent, WHAT to do with the reply
    task_summary: Mapped[str] = mapped_column(Text, nullable=False)
    originator_name: Mapped[str | None] = mapped_column(String(100))
    originator_identity: Mapped[str | None] = mapped_column(String(200))
    expected_action: Mapped[str | None] = mapped_column(Text)

    # Full structured context for maximum fidelity
    full_context_json: Mapped[dict | None] = mapped_column(JSONB, default=None)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
