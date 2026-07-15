"""Tool and AgentTool models for dynamic tool management."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.agent_tool_config_storage import EncryptedAgentToolConfig


class Tool(Base):
    """A tool that can be assigned to agents.

    Types:
        - builtin: Hardcoded tools (file ops, task mgmt, feishu, web search, etc.)
        - mcp: External tools connected via Model Context Protocol
    """

    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("name", "tenant_id", name="uq_tools_name_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))  # "web_search", "list_files"
    display_name: Mapped[str] = mapped_column(String(200))  # "互联网搜索"
    description: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[str] = mapped_column(String(20), default="builtin")  # builtin | mcp
    category: Mapped[str] = mapped_column(String(50), default="general")  # file, task, communication, search, custom
    icon: Mapped[str] = mapped_column(String(10), default="🔧")

    # OpenAI function-calling parameters schema
    parameters_schema: Mapped[dict] = mapped_column(JSON, default=dict)

    # Runtime configuration (admin-editable settings)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # actual values, e.g. {"search_engine": "duckduckgo"}
    config_schema: Mapped[dict] = mapped_column(JSON, default=dict)  # UI schema describing configurable fields

    # MCP-specific fields
    mcp_server_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mcp_server_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mcp_tool_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # tool name on the MCP server
    # Raw external metadata is an administrator-only evidence surface. Runtime
    # schemas use ``description`` + ``parameters_schema`` after the MCP trust gate.
    mcp_raw_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_raw_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    mcp_metadata_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mcp_metadata_risk_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mcp_trust_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    mcp_trust_tier: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mcp_reviewed_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mcp_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    mcp_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # global toggle
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # auto-assigned to new agents

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentTool(Base):
    """Junction table: which tools are enabled for which agent."""

    __tablename__ = "agent_tools"
    __table_args__ = (UniqueConstraint("agent_id", "tool_id", name="uq_agent_tools_agent_tool"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # When MCP metadata quarantine forces ``enabled=False``, preserve the
    # assignment's prior intent so administrator review can restore exactly
    # that state without overriding an explicit agent-level disable.
    mcp_trust_requested_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The complete document is envelope-encrypted because third-party MCP
    # providers may use arbitrary credential field names.
    config: Mapped[dict] = mapped_column(EncryptedAgentToolConfig(), default=dict)
    source: Mapped[str] = mapped_column(String(20), default="system")  # "system" | "user_installed"
    installed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # agent that installed this tool
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
