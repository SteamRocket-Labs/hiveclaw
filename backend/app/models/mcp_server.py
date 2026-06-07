"""MCP server records — first-class identity for installed MCP integrations.

Replaces the legacy pack-derived MCP grouping (``make_mcp_server_pack_name``)
with stable, tenant-scoped server records. Every table is tenant-scoped and
joins Hive's PostgreSQL row-level security: ``tenant_id`` is mandatory so the
``tenant_isolation_{table}`` policy can filter on a real column that physically
exists on the row. A child table reachable only by foreign key could not be
RLS-covered and would leak across tenants. See
docs/agent-extension-surface-skill-mcp.md §8.3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MCPServer(Base):
    """One installed MCP integration, managed as a single server-level object."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "server_key", name="uq_mcp_servers_tenant_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Stable, tenant-unique identity. Never uses the legacy ``mcp_server:*`` pack namespace.
    server_key: Mapped[str] = mapped_column(String(200), nullable=False)
    transport: Mapped[str | None] = mapped_column(String(40), nullable=True)  # sse | stdio | streamable_http
    server_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    registry_source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="manual"
    )  # smithery|direct|manual|system
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="disabled"
    )  # connected|needs_auth|failed|disabled
    auth_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="none"
    )  # none|configured|expired|error
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MCPServerTool(Base):
    """One tool exposed by an MCP server, linked to the runtime ``Tool`` row."""

    __tablename__ = "mcp_server_tools"
    __table_args__ = (
        UniqueConstraint("tenant_id", "mcp_server_id", "mcp_tool_name", name="uq_mcp_server_tools_tenant_server_tool"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tools.id", ondelete="CASCADE"), nullable=True, index=True
    )
    mcp_tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentMCPServerAssignment(Base):
    """Which agent is connected to which MCP server, plus the default tool mode."""

    __tablename__ = "agent_mcp_server_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "mcp_server_id", name="uq_agent_mcp_assignment_tenant_agent_server"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_tool_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")  # auto|approval|deny
    always_load: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentMCPToolOverride(Base):
    """Per-tool precision override for one agent's access to an MCP server."""

    __tablename__ = "agent_mcp_tool_overrides"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "agent_id", "mcp_server_id", "tool_name", name="uq_agent_mcp_override_tenant_agent_server_tool"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")  # auto|approval|deny
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
