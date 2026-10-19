"""Tenant-level channel configuration (ARCHITECTURE.md Phase 6).

One bot per tenant per channel type. Replaces the per-agent ChannelConfig
for enterprise deployments. Messages are routed by sender identity to the
corresponding employee's Main Agent.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.channel_secret_storage import EncryptedChannelJSON, EncryptedChannelSecret


class TenantChannelConfig(Base):
    """Enterprise-level channel configuration — one bot per company."""

    __tablename__ = "tenant_channel_configs"

    @property
    def is_configured(self) -> bool:
        return bool(self.is_active and self.app_id and self.app_secret)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel_type", name="uq_tenant_channel_tenant_type"),
        Index("uq_company_dingtalk_messaging_app", "app_id", unique=True,
            postgresql_where=text("channel_type = 'dingtalk' AND is_active AND extra_config -> 'messaging_enabled' = 'true'::jsonb")),
    )

    # Bot credentials (same field names as ChannelConfig for consistency)
    app_id: Mapped[str | None] = mapped_column(String(255))
    app_secret: Mapped[str | None] = mapped_column(EncryptedChannelSecret())
    encrypt_key: Mapped[str | None] = mapped_column(EncryptedChannelSecret())
    verification_token: Mapped[str | None] = mapped_column(EncryptedChannelSecret())

    # Extensible config (connection_mode, bot_secret for WeCom, etc.)
    extra_config: Mapped[dict] = mapped_column(
        EncryptedChannelJSON(postgres_jsonb=True), nullable=False, server_default="{}"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class CompanyChannelBindingChallenge(Base):
    """One self-issued proof linking an authenticated member to a verified sender."""

    __tablename__ = "company_channel_binding_challenges"
    __table_args__ = (
        CheckConstraint("provider IN ('dingtalk','wecom')", name="ck_company_channel_challenge_provider"),
        CheckConstraint("(consumed_at IS NULL) = (principal_id IS NULL)", name="ck_company_channel_challenge_receipt"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True),
        ForeignKey("tenant_channel_configs.id", ondelete="SET NULL"), nullable=True)
    identity_link_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_member_user_links.id"), nullable=False)
    link_version: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_history_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    installation_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("external_principals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
