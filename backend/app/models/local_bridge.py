"""Local Agent Bridge pairing and connection records."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LocalAgentBridgePairingSession(Base):
    """Short-lived device-flow session created by a local Hive Connect CLI."""

    __tablename__ = "local_agent_bridge_pairing_sessions"
    __table_args__ = (
        UniqueConstraint("pairing_code_hash", name="uq_local_bridge_pairing_code_hash"),
        UniqueConstraint("device_code_hash", name="uq_local_bridge_device_code_hash"),
        Index("ix_local_bridge_pairing_status", "status"),
        Index("ix_local_bridge_pairing_agent_id", "agent_id"),
        Index("ix_local_bridge_pairing_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("local_agent_bridge_connections.id"),
        nullable=True,
    )
    pairing_code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    device_code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    device_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    device_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LocalAgentBridgeConnection(Base):
    """A tenant/user-scoped bearer token for one local bridge device.

    `agent_id` is kept nullable for legacy agent-bound bridge tokens. The
    primary Local Agent Channel binding is user-scoped.
    """

    __tablename__ = "local_agent_bridge_connections"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_local_bridge_connection_token_hash"),
        Index("ix_local_bridge_connections_agent_status", "agent_id", "status"),
        Index("ix_local_bridge_connections_user_status", "user_id", "status"),
        Index("ix_local_bridge_connections_tenant_id", "tenant_id"),
        Index("ix_local_bridge_connections_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    device_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_ip: Mapped[str | None] = mapped_column(String(64))
    last_seen_user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
