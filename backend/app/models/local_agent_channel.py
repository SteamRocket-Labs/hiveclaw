"""Local Agent Channel models.

These tables model Hive as an outbound channel for local agent runners.  The
existing local bridge connection owns authentication; these rows own chat-like
sessions, durable messages, replayable events, and short-lived WS tickets.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LocalAgentChannel(Base):
    """Runtime presence for one user-bound local bridge connection."""

    __tablename__ = "local_agent_channels"
    __table_args__ = (
        UniqueConstraint("connection_id", name="uq_local_agent_channels_connection_id"),
        Index("ix_local_agent_channels_user_status", "owner_user_id", "status"),
        Index("ix_local_agent_channels_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_bridge_connections.id"), nullable=False
    )
    runtime_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    capabilities_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class LocalAgentChannelSession(Base):
    """A durable local-agent conversation surface backed by a host runner."""

    __tablename__ = "local_agent_channel_sessions"
    __table_args__ = (
        Index("ix_local_agent_channel_sessions_user_status", "owner_user_id", "status"),
        Index("ix_local_agent_channel_sessions_source_agent", "source_agent_id"),
        Index("ix_local_agent_channel_sessions_chat_session", "chat_session_id"),
        Index("ix_local_agent_channel_sessions_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    channel_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("local_agent_channels.id"))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_bridge_connections.id")
    )
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"))
    local_session_id: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class LocalAgentChannelMessage(Base):
    """A Hive-to-local or local-to-Hive message in a local agent session."""

    __tablename__ = "local_agent_channel_messages"
    __table_args__ = (
        Index("ix_local_agent_channel_messages_session_status", "session_id", "status"),
        Index("ix_local_agent_channel_messages_user_status", "owner_user_id", "status"),
        Index("ix_local_agent_channel_messages_source_agent", "source_agent_id"),
        Index("ix_local_agent_channel_messages_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_channel_sessions.id"), nullable=False
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    direction: Mapped[str] = mapped_column(String(32), nullable=False, default="hive_to_local")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LocalAgentChannelEvent(Base):
    """Replayable event emitted by Hive or the local runner."""

    __tablename__ = "local_agent_channel_events"
    __table_args__ = (
        Index("ix_local_agent_channel_events_session_created", "session_id", "created_at"),
        Index("ix_local_agent_channel_events_message", "message_id"),
        Index("ix_local_agent_channel_events_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_channel_sessions.id"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_channel_messages.id")
    )
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LocalAgentChannelWsTicket(Base):
    """Short-lived, single-use WebSocket ticket derived from a bridge token."""

    __tablename__ = "local_agent_channel_ws_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_hash", name="uq_local_agent_channel_ws_ticket_hash"),
        Index("ix_local_agent_channel_ws_tickets_connection", "connection_id"),
        Index("ix_local_agent_channel_ws_tickets_expires", "expires_at"),
        Index("ix_local_agent_channel_ws_tickets_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("local_agent_bridge_connections.id"), nullable=False
    )
    ticket_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
