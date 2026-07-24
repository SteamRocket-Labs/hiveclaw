"""Chat session model for grouping chat messages per participant-agent pair."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, ForeignKey, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.channel_secret_storage import EncryptedDeliveryTargetJSON


class ChatSession(Base):
    """A named session grouping chat messages between a user and an agent.

    source_channel: 'web' | 'feishu' | 'discord' | 'slack'
    external_conv_id: original channel conversation ID (e.g. 'feishu_p2p_ou_xxx').
                      Unique per agent — used for reliable find-or-create without in-process caching.
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("agent_id", "external_conv_id", name="uq_chat_sessions_agent_ext_conv"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    external_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_principals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New Session")
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    external_conv_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Participant identity (unified User/Agent identity)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("participants.id"), nullable=True
    )
    # For agent-to-agent sessions: the other agent in the conversation
    peer_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    delivery_target_json: Mapped[dict | None] = mapped_column(
        EncryptedDeliveryTargetJSON(postgres_jsonb=True),
        nullable=True,
    )
    session_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="human_chat")
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    runtime_source: Mapped[str] = mapped_column(String(64), nullable=False, default="web_chat")
    visibility_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="direct_user")
    listed_surface: Mapped[str] = mapped_column(String(64), nullable=False, default="chat", index=True)
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    root_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True, index=True
    )
    transcript_metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


# Keep isolated ChatSession imports mapper-safe for runtime_task_id flushes.
from app.models.runtime_task import RuntimeTask  # noqa: E402, F401
from app.models.external_principal import ExternalPrincipal  # noqa: E402, F401
