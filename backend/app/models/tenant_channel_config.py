"""Tenant-level channel configuration (ARCHITECTURE.md Phase 6).

One bot per tenant per channel type. Replaces the per-agent ChannelConfig
for enterprise deployments. Messages are routed by sender identity to the
corresponding employee's Main Agent.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantChannelConfig(Base):
    """Enterprise-level channel configuration — one bot per company."""

    __tablename__ = "tenant_channel_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True,
    )
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "channel_type", name="uq_tenant_channel_tenant_type"),)

    # Bot credentials (same field names as ChannelConfig for consistency)
    app_id: Mapped[str | None] = mapped_column(String(255))
    app_secret: Mapped[str | None] = mapped_column(String(255))
    encrypt_key: Mapped[str | None] = mapped_column(String(255))
    verification_token: Mapped[str | None] = mapped_column(String(255))

    # Extensible config (connection_mode, bot_secret for WeCom, etc.)
    extra_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
