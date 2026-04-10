"""Provider-first LLM credentials and auth state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class LLMProviderConfig(Base):
    """Tenant-scoped provider credentials and auth metadata."""

    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_llm_provider_configs_tenant_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    auth_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="api_key")
    api_key_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    oauth_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oauth_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    models: Mapped[list["LLMModel"]] = relationship("LLMModel", back_populates="provider_config")

    @property
    def api_key(self) -> str | None:
        from app.services.secrets_provider import get_secrets_provider

        if not self.api_key_encrypted:
            return None
        return get_secrets_provider().decrypt(self.api_key_encrypted)

    @property
    def access_token(self) -> str | None:
        from app.services.secrets_provider import get_secrets_provider

        if not self.access_token_encrypted:
            return None
        return get_secrets_provider().decrypt(self.access_token_encrypted)

    @property
    def refresh_token(self) -> str | None:
        from app.services.secrets_provider import get_secrets_provider

        if not self.refresh_token_encrypted:
            return None
        return get_secrets_provider().decrypt(self.refresh_token_encrypted)
