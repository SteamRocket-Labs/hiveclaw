"""Digital Employee (Agent) models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Agent(Base):
    """Digital employee (Agent) instance.

    agent_type: 'native' (platform-hosted) or 'openclaw' (remote OpenClaw bot).
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role_description: Mapped[str] = mapped_column(String(500), default="")
    bio: Mapped[str | None] = mapped_column(Text)
    welcome_message: Mapped[str | None] = mapped_column(Text, default=None)

    # Ownership
    creator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))

    # Agent type: 'native' (platform-hosted LLM) or 'openclaw' (remote OpenClaw bot)
    agent_type: Mapped[str] = mapped_column(String(20), default="native", nullable=False)
    # API key hash for OpenClaw gateway authentication
    api_key_hash: Mapped[str | None] = mapped_column(String(128))
    # Last time OpenClaw polled the gateway (online status indicator)
    openclaw_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Agent classification and security zone
    agent_class: Mapped[str] = mapped_column(String(30), default="internal_tenant", nullable=False)
    security_zone: Mapped[str] = mapped_column(String(30), default="standard", nullable=False)

    # Runtime
    status: Mapped[str] = mapped_column(
        Enum("draft", "creating", "running", "idle", "stopped", "error", name="agent_status_enum", create_constraint=False),
        default="creating",
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(String(100))
    container_port: Mapped[int | None] = mapped_column(Integer)

    # LLM config
    primary_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))
    fallback_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))

    # Token usage stats (tracking only — enforcement is at User level)
    tokens_used_today: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used_month: Mapped[int] = mapped_column(Integer, default=0)
    last_daily_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_monthly_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_used_total: Mapped[int] = mapped_column(Integer, default=0)
    context_window_size: Mapped[int] = mapped_column(Integer, default=100)
    max_tool_rounds: Mapped[int] = mapped_column(Integer, default=200, server_default="200")
    execution_mode: Mapped[str] = mapped_column(String(30), default="standard", nullable=False, server_default="standard")
    smart_model_routing: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Trigger limits (per-agent, configurable from Settings UI)
    max_triggers: Mapped[int] = mapped_column(Integer, default=20)
    min_poll_interval_min: Mapped[int] = mapped_column(Integer, default=5)
    webhook_rate_limit: Mapped[int] = mapped_column(Integer, default=5)


    # Template
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_templates.id"))

    # Heartbeat is platform-managed; columns remain for backward-compatible API/DB shape.
    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    heartbeat_interval_minutes: Mapped[int] = mapped_column(Integer, default=45)
    heartbeat_active_hours: Mapped[str] = mapped_column(String(20), default="00:00-23:59")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Timezone (IANA format, e.g. "Asia/Shanghai"). None = inherit from tenant.
    timezone: Mapped[str | None] = mapped_column(String(50), default=None, nullable=True)

    parent_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True,
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True,
    )
    channel_perms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    config_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    creator: Mapped["User"] = relationship("User", back_populates="created_agents", foreign_keys=[creator_id])
    permissions: Mapped[list["AgentPermission"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    channel_config: Mapped["ChannelConfig | None"] = relationship(back_populates="agent", uselist=False)
    primary_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[primary_model_id])
    fallback_model: Mapped["LLMModel | None"] = relationship(foreign_keys=[fallback_model_id])

    @property
    def agent_kind(self) -> str:
        """Compatibility shim for legacy main/sub-agent semantics."""
        return "sub" if self.parent_agent_id else "main"


class AgentPermission(Base):
    """Access permission for a digital employee."""

    __tablename__ = "agent_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(
        Enum("company", "department", "user", name="permission_scope_enum"),
        nullable=False,
    )
    # scope_id: null for company, user_id for user scope
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # access_level: 'use' = task/chat/tool/skill/workspace only, 'manage' = full access
    access_level: Mapped[str] = mapped_column(String(20), default="use", nullable=False)

    agent: Mapped["Agent"] = relationship(back_populates="permissions")


class AgentTemplate(Base):
    """Digital employee template for quick creation / Role Template (ARCHITECTURE.md §6.2)."""

    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="🤖")
    category: Mapped[str] = mapped_column(String(50), default="general")
    soul_template: Mapped[str] = mapped_column(Text, default="")
    default_skills: Mapped[list] = mapped_column(JSON, default=[])
    is_builtin: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Role Template extensions (ARCHITECTURE.md §6.2)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"))
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("llm_models.id"))
    config_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")


# Import for relationship resolution
from app.models.task import Task  # noqa: E402, F401
from app.models.channel_config import ChannelConfig  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
from app.models.llm import LLMModel  # noqa: E402, F401
