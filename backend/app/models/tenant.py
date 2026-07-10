"""Tenant (Company) model — multi-tenancy isolation boundary."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Tenant(Base):
    """A company/organization that uses the platform."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    im_provider: Mapped[str] = mapped_column(
        Enum("feishu", "dingtalk", "wecom", "microsoft_teams", "web_only", name="im_provider_enum"),
        default="web_only",
        nullable=False,
    )
    im_config: Mapped[dict | None] = mapped_column(JSON, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Deprecated compatibility column. Heartbeat cadence is platform-managed.
    min_heartbeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=45)

    # Default timezone for all agents in this company (IANA format, e.g. "Asia/Shanghai")
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")

    # Trigger limits — defaults for new agents & floor values
    default_max_triggers: Mapped[int] = mapped_column(Integer, default=20)
    min_poll_interval_floor: Mapped[int] = mapped_column(Integer, default=5)
    max_webhook_rate_ceiling: Mapped[int] = mapped_column(Integer, default=5)

    # Default token quotas for new users
    default_tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    default_tokens_per_month: Mapped[int | None] = mapped_column(Integer)

    # Company-level hard token quotas and counters.
    quota_tokens_per_day: Mapped[int | None] = mapped_column(Integer)
    quota_tokens_per_month: Mapped[int | None] = mapped_column(Integer)
    tokens_used_today: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_month: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_total: Mapped[int] = mapped_column(Integer, default=0)
    tokens_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Desktop sync: global version counter bumped on any Desktop-visible resource change (ARCHITECTURE.md §6.6)
    sync_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")

    # Deprecated compatibility field. Native T3 Markdown is the only memory
    # backend; non-null legacy values are ignored by app.memory.backend.
    memory_backend: Mapped[str | None] = mapped_column(String(32), default=None, nullable=True)
