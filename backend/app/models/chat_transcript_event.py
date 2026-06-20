"""Replayable chat transcript events.

This table is the indexed runtime event stream for chat/session UI projection.
T0 remains the raw Markdown/XML evidence source; each row is bridged into T0
through `transcript_event_id` / `transcript_sequence` metadata.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChatTranscriptEvent(Base):
    """One durable event in a replayable chat/session transcript."""

    __tablename__ = "chat_transcript_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_transcript_events_session_sequence"),
        Index("ix_chat_transcript_events_session_sequence", "session_id", "sequence"),
        Index("ix_chat_transcript_events_run_id", "run_id"),
        Index("ix_chat_transcript_events_message_id", "message_id"),
        Index("ix_chat_transcript_events_listed_surface", "listed_surface"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True)
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True)
    root_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True)
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_messages.id"), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="direct_user")
    listed_surface: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parts_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
