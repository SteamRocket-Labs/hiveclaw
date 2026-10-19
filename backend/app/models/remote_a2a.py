"""Configured remote machine endpoints; discovery does not attest execution."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.agent_tool_config_storage import EncryptedAgentToolConfig


class RemoteA2AConnection(Base):
    __tablename__ = "remote_a2a_connections"
    __table_args__ = (
        CheckConstraint("state IN ('configured','discovered','disabled')", name="ck_remote_a2a_connection_state"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    owner_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    configured_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    card_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    rpc_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    credential_config: Mapped[dict] = mapped_column(EncryptedAgentToolConfig, nullable=False, deferred=True)
    credential_configured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    allowed_networks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    max_response_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=16 * 1024 * 1024)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="configured")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_card: Mapped[dict | None] = mapped_column(JSONB)
    discovery_error: Mapped[str | None] = mapped_column(String(100))
    runtime_authorization: Mapped[dict | None] = mapped_column(JSONB)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
