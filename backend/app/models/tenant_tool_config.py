"""Per-tenant tool configuration — tenant-level override for global tool settings.

Config merge hierarchy:
  Tool.config (platform default) < TenantToolConfig.config (tenant override) < AgentTool.config (agent override)

This ensures each tenant has isolated API keys, feature toggles, and tool
settings. Without this layer, all tenants share the same Tool.config row.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantToolConfig(Base):
    """Per-tenant configuration override for a tool."""

    __tablename__ = "tenant_tool_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tool_id", name="uq_tenant_tool_config"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"), nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
