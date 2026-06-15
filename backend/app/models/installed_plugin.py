"""Installed-plugin records — tenant-scoped identity for capability packs
installed via the plugin system (Step 5).

Generalizes the MCPServer install primitive (``app/models/mcp_server.py``) to any
capability pack. Every table is tenant-scoped and joins Hive's PostgreSQL RLS:
``tenant_id`` is mandatory so the ``tenant_isolation_{table}`` policy filters on a
real physical column — a child reachable only by FK could not be RLS-covered and
would leak across tenants. See docs/cc-tooling-alignment-and-plugin-system.md §3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantInstalledPlugin(Base):
    """One capability pack installed into a tenant (mirrors MCPServer)."""

    __tablename__ = "tenant_installed_plugins"
    __table_args__ = (UniqueConstraint("tenant_id", "plugin_key", name="uq_installed_plugins_tenant_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stable, tenant-unique identity = the pack manifest name (e.g. "web_pack").
    plugin_key: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="0.0.0")
    # Provenance (governed inclusion §6.7): builtin/local installable in v1; remote
    # kinds are recorded but fail-closed until signature + sandbox infra lands.
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="builtin")
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="enabled")  # enabled|disabled
    # Credential references and enable-time config (never raw secrets — those go to
    # encrypted_tool_config keyed by credential_requirements).
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Pinned dependency closure resolved at install time (governed inclusion).
    lockfile_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentPluginAssignment(Base):
    """Which agent has which installed plugin enabled (mirrors AgentMCPServerAssignment)."""

    __tablename__ = "agent_plugin_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "installed_plugin_id", name="uq_agent_plugin_assignment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installed_plugin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PluginHookRegistration(Base):
    """A declarative hook binding contributed by an installed plugin (governed
    inclusion §6.7).

    ``handler`` must come from the platform allowlist (validated at manifest read
    and at install time) — a plugin can NEVER bind raw Python/shell/import/webhook.
    A PRE_TOOL_USE enforce hook's ``modified_args`` must re-run schema / capability /
    ActionPreflight / approval; a hook can only narrow or block, never bypass.
    """

    __tablename__ = "plugin_hook_registrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "installed_plugin_id", "event", "handler", name="uq_plugin_hook_registration"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installed_plugin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    handler: Mapped[str] = mapped_column(String(120), nullable=False)  # platform-allowlisted handler name
    matcher_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="observe")  # observe|enforce
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PluginDependencyEdge(Base):
    """Resolved tenant plugin dependency graph.

    Each row says ``installed_plugin_id`` directly depends on
    ``dependency_plugin_id``. The lockfile on ``TenantInstalledPlugin`` keeps the
    full pinned closure; this table makes uninstall protection and graph
    inspection enforceable in SQL instead of buried inside JSON.
    """

    __tablename__ = "plugin_dependency_edges"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "installed_plugin_id",
            "dependency_plugin_id",
            name="uq_plugin_dependency_edge",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    installed_plugin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_installed_plugins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dependency_plugin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_installed_plugins.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dependency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    dependency_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="builtin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
