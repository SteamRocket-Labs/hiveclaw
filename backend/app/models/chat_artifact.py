"""Durable chat artifact references for session transcript delivery."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChatArtifact(Base):
    """A stable, user-facing artifact attached to a chat message.

    The workspace file remains the source of truth. This row is the durable
    transcript reference that lets chat history reopen the same artifact without
    asking users to browse the workspace manually.
    """

    __tablename__ = "chat_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "session_id",
            "runtime_task_id",
            "path",
            "snapshot_hash",
            name="uq_chat_artifacts_agent_session_run_path_snapshot",
        ),
        Index("ix_chat_artifacts_message_id", "message_id"),
        Index("ix_chat_artifacts_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_messages.id"), nullable=False)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modified_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="download")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="workspace_write")
    snapshot_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
