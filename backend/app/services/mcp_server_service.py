"""Server-first MCP extension surface — imperative shell over the MCP tables.

Reads and writes the Part 1 tables (``MCPServer``, ``MCPServerTool``,
``AgentMCPServerAssignment``) introduced to replace the legacy pack-derived MCP
grouping. Every query is tenant-scoped, and no DTO emitted here carries a
``pack`` / ``pack_name`` field — the public surface is server-first with stable
server identity. See docs/agent-extension-surface-skill-mcp.md §7.2–7.4, §8.2.
"""

from __future__ import annotations

import logging
import hashlib
import time
import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.capability_install import AgentCapabilityInstall
from app.models.external_capability import ExternalCapabilitySnapshot, ExternalExtensionActivation
from app.models.mcp_server import (
    AgentMCPServerAssignment,
    AgentMCPToolOverride,
    MCPServer,
    MCPServerTool,
)
from app.models.installed_plugin import AgentPluginAssignment, TenantInstalledPlugin
from app.models.tool import AgentTool, Tool
from app.services.mcp_backfill import (
    AgentToolState,
    McpToolRow,
    group_mcp_tools,
    plan_agent_assignment,
)
from app.services.mcp_backfill_service import backfill_tenant_mcp_servers
from app.services import mcp_oauth
from app.services.mcp_authz import assert_mcp_cloud_transport_allowed
from app.services.mcp_metadata_trust import (
    MCPMetadataValidationError,
    current_mcp_fingerprint,
    is_mcp_metadata_runtime_approved,
    prepare_mcp_metadata_candidate,
    public_mcp_review_facts,
    review_mcp_metadata,
)

logger = logging.getLogger(__name__)

MCP_TOOL_MODES = {"auto", "approval", "deny"}
SKILL_INSTALL_KINDS = {"platform_skill", "clawhub_skill", "external_skill_url"}


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _mcp_wire_server_key(row) -> str:
    raw = str(getattr(row, "server_key", None) or getattr(row, "name", None) or "server")
    key = "".join(ch.lower() if ch.isalnum() else "_" for ch in raw).strip("_")
    return key or "server"


def _mcp_wire_tool_name(row, tool_name: str) -> str:
    clean = str(tool_name or "").strip()
    if not clean:
        return ""
    if clean.startswith("mcp__"):
        return clean
    return f"mcp__{_mcp_wire_server_key(row)}__{clean}"


def _mcp_config_list(row, key: str) -> list[str]:
    config = getattr(row, "config_json", None)
    if not isinstance(config, dict):
        return []
    return _strings(config.get(key))


def _validate_tool_mode(value: str, *, field_name: str = "mode") -> str:
    mode = (value or "auto").strip().lower()
    if mode not in MCP_TOOL_MODES:
        raise ValueError(f"{field_name} must be one of: auto, approval, deny")
    return mode


async def _require_tenant_server(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID, *, for_update: bool = False) -> MCPServer:
    statement = select(MCPServer).where(MCPServer.id == server_id, MCPServer.tenant_id == tenant_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    result = await db.execute(statement)
    server = result.scalar_one_or_none()
    if server is None:
        raise ValueError("MCP server not found")
    return server


def _effective_tool_mode(*, assignment: AgentMCPServerAssignment, override_mode: str | None = None) -> str:
    if not bool(assignment.enabled):
        return "deny"
    if override_mode:
        return _validate_tool_mode(override_mode)
    return _validate_tool_mode(getattr(assignment, "default_tool_mode", "auto"), field_name="default_tool_mode")


async def list_tenant_servers(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    """List a tenant's MCP servers, server-first, with tool/agent rollups."""
    server_result = await db.execute(select(MCPServer).where(MCPServer.tenant_id == tenant_id, MCPServer.owner_user_id.is_(None)).order_by(MCPServer.name))
    servers = server_result.scalars().all()
    if not servers:
        return []

    tool_count_result = await db.execute(
        select(MCPServerTool.mcp_server_id, func.count(MCPServerTool.id))
        .where(MCPServerTool.tenant_id == tenant_id)
        .group_by(MCPServerTool.mcp_server_id)
    )
    tool_counts = {row[0]: row[1] for row in tool_count_result.all()}

    assignment_result = await db.execute(
        select(
            AgentMCPServerAssignment.mcp_server_id,
            AgentMCPServerAssignment.enabled,
            Agent.id,
            Agent.name,
        )
        .join(Agent, Agent.id == AgentMCPServerAssignment.agent_id)
        .where(AgentMCPServerAssignment.tenant_id == tenant_id)
    )
    agents_by_server: dict[uuid.UUID, list[dict]] = {}
    enabled_counts: dict[uuid.UUID, int] = {}
    for server_id, enabled, agent_id, agent_name in assignment_result.all():
        agents_by_server.setdefault(server_id, []).append(
            {"id": str(agent_id), "name": agent_name, "enabled": bool(enabled)}
        )
        if enabled:
            enabled_counts[server_id] = enabled_counts.get(server_id, 0) + 1

    return [
        {
            "id": str(server.id),
            "name": server.name,
            "server_key": server.server_key,
            "status": server.status,
            "auth_status": server.auth_status,
            "transport": server.transport,
            "tool_count": tool_counts.get(server.id, 0),
            "agent_count": enabled_counts.get(server.id, 0),
            "agents": agents_by_server.get(server.id, []),
        }
        for server in servers
    ]


async def get_agent_mcp_servers(db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
    """List MCP servers assigned to one agent, with the server-level tool mode."""
    result = await db.execute(
        select(
            MCPServer.id,
            MCPServer.name,
            MCPServer.server_key,
            MCPServer.status,
            MCPServer.config_json,
            AgentMCPServerAssignment.enabled,
            AgentMCPServerAssignment.default_tool_mode,
            AgentMCPServerAssignment.always_load,
        )
        .join(AgentMCPServerAssignment, AgentMCPServerAssignment.mcp_server_id == MCPServer.id)
        .where(AgentMCPServerAssignment.agent_id == agent_id, MCPServer.owner_user_id.is_(None))
        .order_by(MCPServer.name)
    )
    rows = result.all()
    if not rows:
        return []

    tool_count_result = await db.execute(
        select(MCPServerTool.mcp_server_id, func.count(MCPServerTool.id))
        .where(MCPServerTool.mcp_server_id.in_([row.id for row in rows]))
        .group_by(MCPServerTool.mcp_server_id)
    )
    tool_counts = {row[0]: row[1] for row in tool_count_result.all()}
    tool_rows_result = await db.execute(
        select(MCPServerTool.mcp_server_id, MCPServerTool.mcp_tool_name)
        .where(MCPServerTool.mcp_server_id.in_([row.id for row in rows]))
        .order_by(MCPServerTool.mcp_tool_name.asc())
    )
    rows_by_id = {row.id: row for row in rows}
    tools_by_server: dict[uuid.UUID, list[str]] = {}
    for server_id, tool_name in tool_rows_result.all():
        row = rows_by_id.get(server_id)
        wire_name = _mcp_wire_tool_name(row, str(tool_name or "")) if row is not None else str(tool_name or "")
        if wire_name:
            tools_by_server.setdefault(server_id, []).append(wire_name)

    return [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "enabled": bool(row.enabled),
            "tool_count": tool_counts.get(row.id, 0),
            "tools": tools_by_server.get(row.id, []),
            "prompts": _mcp_config_list(row, "prompts"),
            "resources": _mcp_config_list(row, "resources"),
            "default_tool_mode": row.default_tool_mode,
            "always_load": bool(getattr(row, "always_load", False)),
        }
        for row in rows
    ]


async def _list_agent_workspace_skills(agent_id: uuid.UUID) -> list[dict]:
    """Read the agent's workspace skills as extension DTOs (best-effort)."""
    try:
        from app.skills.loader import WorkspaceSkillLoader
        from app.tools import ensure_workspace

        workspace = await ensure_workspace(agent_id)
        parsed = WorkspaceSkillLoader().load_from_workspace(workspace)
    except Exception as exc:
        logger.debug("Failed to load workspace skills for agent %s: %s", agent_id, exc)
        return []
    return [
        {
            "id": skill.metadata.name,
            "name": skill.metadata.name,
            "source": "workspace",
            "status": "available",
            "folder_name": (
                skill.file_path.parent.name
                if skill.file_path.parent.parent == workspace.resolve() / "skills"
                and skill.file_path.name.lower() == "skill.md"
                else None
            ),
        }
        for skill in parsed
    ]


async def _list_installed_skill_extensions(db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
    """Read persisted non-workspace skill installs into the extension skill DTO."""
    result = await db.execute(
        select(AgentCapabilityInstall)
        .where(AgentCapabilityInstall.agent_id == agent_id, AgentCapabilityInstall.kind.in_(SKILL_INSTALL_KINDS))
        .order_by(AgentCapabilityInstall.created_at.asc())
    )
    skills = []
    for record in result.scalars().all():
        source = getattr(record, "kind", "")
        normalized = getattr(record, "normalized_key", None) or getattr(record, "source_key", "")
        display_name = getattr(record, "display_name", None) or getattr(record, "source_key", None) or normalized
        skills.append(
            {
                "id": normalized,
                "name": display_name,
                "source": source,
                "status": getattr(record, "status", None) or "installed",
            }
        )
    return skills


def _dedupe_skill_extensions(skills: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for skill in skills:
        key = (str(skill.get("source") or ""), str(skill.get("id") or skill.get("name") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(skill)
    return deduped


def _isoformat_or_none(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


async def _list_agent_external_activations(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[dict]:
    """Read approved external snapshot activations into the agent extension DTO."""
    result = await db.execute(
        select(ExternalExtensionActivation, ExternalCapabilitySnapshot)
        .join(ExternalCapabilitySnapshot, ExternalCapabilitySnapshot.id == ExternalExtensionActivation.snapshot_id)
        .where(
            ExternalExtensionActivation.tenant_id == tenant_id,
            ExternalExtensionActivation.agent_id == agent_id,
            ExternalExtensionActivation.status == "active",
            ExternalExtensionActivation.activation_scope == "agent",
            ExternalCapabilitySnapshot.status == "approved",
        )
        .order_by(ExternalExtensionActivation.activated_at.desc())
    )
    return [
        {
            "id": str(activation.id),
            "snapshot_id": str(snapshot.id),
            "status": activation.status,
            "activation_scope": getattr(activation, "activation_scope", None) or "agent",
            "session_id": str(activation.session_id) if getattr(activation, "session_id", None) else None,
            "normalized_name": snapshot.normalized_name,
            "source_format": snapshot.source_format,
            "source_uri": snapshot.source_uri,
            "component_types": getattr(activation, "component_types_json", None) or {},
            "selected_components": getattr(activation, "selected_components_json", None) or [],
            "activation_result": getattr(activation, "activation_result_json", None) or {},
            "activated_at": _isoformat_or_none(getattr(activation, "activated_at", None)),
            "expires_at": _isoformat_or_none(getattr(activation, "expires_at", None)),
        }
        for activation, snapshot in result.all()
    ]


async def get_agent_extensions(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    """Single source of truth for an agent's extension state: skills + MCP + plugins."""
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    plugins: list[dict] = []
    external_activations: list[dict] = []
    if agent and agent.tenant_id:
        rows = (
            await db.execute(
                select(TenantInstalledPlugin, AgentPluginAssignment)
                .join(
                    AgentPluginAssignment,
                    AgentPluginAssignment.installed_plugin_id == TenantInstalledPlugin.id,
                )
                .where(
                    TenantInstalledPlugin.tenant_id == agent.tenant_id,
                    TenantInstalledPlugin.status == "enabled",
                    AgentPluginAssignment.tenant_id == agent.tenant_id,
                    AgentPluginAssignment.agent_id == agent_id,
                )
                .order_by(TenantInstalledPlugin.plugin_key)
            )
        ).all()
        plugins = [
            {
                "id": str(plugin.id),
                "plugin_key": plugin.plugin_key,
                "version": plugin.version,
                "status": plugin.status,
                "source_kind": plugin.source_kind,
                "enabled": bool(assignment.enabled),
            }
            for plugin, assignment in rows
        ]
        external_activations = await _list_agent_external_activations(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
        )
    return {
        "skills": _dedupe_skill_extensions(
            [*(await _list_agent_workspace_skills(agent_id)), *(await _list_installed_skill_extensions(db, agent_id))]
        ),
        "mcp_servers": await get_agent_mcp_servers(db, agent_id),
        "plugins": plugins,
        "external_activations": external_activations,
    }


async def set_agent_mcp_assignment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    enabled: bool,
    default_tool_mode: str = "auto",
    always_load: bool = False,
) -> dict:
    """Upsert one agent↔MCP server assignment (unique per tenant+agent+server)."""
    default_tool_mode = _validate_tool_mode(default_tool_mode, field_name="default_tool_mode")
    server = await _require_tenant_server(db, tenant_id, server_id, for_update=True)
    if server.owner_user_id is not None:
        raise ValueError("Personal accounts use owner-issued grants; assign the company connection separately")
    result = await db.execute(
        select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.agent_id == agent_id,
            AgentMCPServerAssignment.mcp_server_id == server_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        assignment = AgentMCPServerAssignment(
            tenant_id=tenant_id,
            agent_id=agent_id,
            mcp_server_id=server_id,
            enabled=enabled,
            default_tool_mode=default_tool_mode,
            always_load=always_load,
        )
        db.add(assignment)
    else:
        assignment.enabled = enabled
        assignment.default_tool_mode = default_tool_mode
        assignment.always_load = always_load
    if enabled:
        rows = (await db.execute(select(Tool, AgentTool).join(MCPServerTool, MCPServerTool.tool_id == Tool.id)
            .outerjoin(AgentTool, (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id))
            .where(MCPServerTool.tenant_id == tenant_id, MCPServerTool.mcp_server_id == server_id))).all()
        for tool, existing in rows:
            if existing is None:
                approved = is_mcp_metadata_runtime_approved(tool)
                db.add(AgentTool(tenant_id=tenant_id, agent_id=agent_id, tool_id=tool.id, enabled=approved,
                    mcp_trust_requested_enabled=None if approved else True, source="system", config={}))
    await db.commit()
    return {
        "id": str(assignment.id),
        "agent_id": str(agent_id),
        "server_id": str(server_id),
        "enabled": bool(assignment.enabled),
        "default_tool_mode": assignment.default_tool_mode,
        "always_load": bool(getattr(assignment, "always_load", False)),
    }


async def list_agent_mcp_server_tools(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
) -> list[dict]:
    """List one agent's per-tool policy modes for an MCP server."""
    await _require_tenant_server(db, tenant_id, server_id)
    assignment_result = await db.execute(
        select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.agent_id == agent_id,
            AgentMCPServerAssignment.mcp_server_id == server_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if assignment is None:
        raise ValueError("MCP server assignment not found")

    tools_result = await db.execute(
        select(MCPServerTool, Tool)
        .outerjoin(Tool, Tool.id == MCPServerTool.tool_id)
        .where(MCPServerTool.tenant_id == tenant_id, MCPServerTool.mcp_server_id == server_id)
        .order_by(MCPServerTool.display_name.asc(), MCPServerTool.mcp_tool_name.asc())
    )
    server_tools = list(tools_result.all())

    overrides_result = await db.execute(
        select(AgentMCPToolOverride).where(
            AgentMCPToolOverride.tenant_id == tenant_id,
            AgentMCPToolOverride.agent_id == agent_id,
            AgentMCPToolOverride.mcp_server_id == server_id,
        )
    )
    overrides = {override.tool_name: override.mode for override in overrides_result.scalars().all()}

    default_mode = _effective_tool_mode(assignment=assignment)
    result: list[dict] = []
    for server_tool, runtime_tool in server_tools:
        mode = overrides.get(server_tool.mcp_tool_name, default_mode)
        runtime_approved = bool(runtime_tool is not None and is_mcp_metadata_runtime_approved(runtime_tool))
        result.append(
            {
                "tool_id": str(server_tool.tool_id) if server_tool.tool_id else None,
                "tool_name": server_tool.mcp_tool_name,
                "display_name": server_tool.display_name or server_tool.mcp_tool_name,
                "mode": mode,
                "effective_mode": (
                    _effective_tool_mode(assignment=assignment, override_mode=overrides.get(server_tool.mcp_tool_name))
                    if runtime_approved
                    else "deny"
                ),
                "trust_status": getattr(runtime_tool, "mcp_trust_status", None) or "missing",
                "trust_tier": getattr(runtime_tool, "mcp_trust_tier", None) or "untrusted",
                "runtime_approved": runtime_approved,
            }
        )
    return result


async def list_tenant_mcp_server_tools(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
) -> list[dict]:
    """Return the administrator-only MCP metadata review surface."""

    await _require_tenant_server(db, tenant_id, server_id)
    rows = (
        await db.execute(
            select(MCPServerTool, Tool)
            .join(Tool, Tool.id == MCPServerTool.tool_id)
            .where(
                MCPServerTool.tenant_id == tenant_id,
                MCPServerTool.mcp_server_id == server_id,
                Tool.tenant_id == tenant_id,
                Tool.type == "mcp",
            )
            .order_by(MCPServerTool.display_name.asc(), MCPServerTool.mcp_tool_name.asc())
        )
    ).all()
    result: list[dict] = []
    for server_tool, runtime_tool in rows:
        facts = public_mcp_review_facts(runtime_tool)
        result.append(
            {
                "tool_id": str(runtime_tool.id),
                "tool_name": server_tool.mcp_tool_name,
                "display_name": server_tool.display_name or server_tool.mcp_tool_name,
                "canonical_description": runtime_tool.description,
                "canonical_schema": runtime_tool.parameters_schema or {},
                "raw_description": runtime_tool.mcp_raw_description or "",
                "raw_schema": runtime_tool.mcp_raw_schema or {},
                "metadata_fingerprint": facts["fingerprint"],
                "reviewed_fingerprint": facts["reviewed_fingerprint"],
                "activation": facts["activation"],
                "risk_flags": list(runtime_tool.mcp_metadata_risk_flags or []),
                "trust_status": runtime_tool.mcp_trust_status or "missing",
                "trust_tier": runtime_tool.mcp_trust_tier or "untrusted",
                "reviewed_by": str(runtime_tool.mcp_reviewed_by) if runtime_tool.mcp_reviewed_by else None,
                "reviewed_at": _isoformat_or_none(runtime_tool.mcp_reviewed_at),
                "runtime_approved": is_mcp_metadata_runtime_approved(runtime_tool),
            }
        )
    return result


async def review_mcp_server_tool_metadata(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    tool_name: str,
    *,
    reviewer_id: uuid.UUID,
    decision: str,
    expected_fingerprint: str,
    canonical_description: str | None = None,
) -> dict:
    """Review one exact metadata fingerprint and restore preserved assignments."""

    server = await _require_tenant_server(db, tenant_id, server_id)
    rows = (
        await db.execute(
            select(MCPServerTool, Tool)
            .join(Tool, Tool.id == MCPServerTool.tool_id)
            .where(
                MCPServerTool.tenant_id == tenant_id,
                MCPServerTool.mcp_server_id == server_id,
                MCPServerTool.mcp_tool_name == tool_name,
                Tool.tenant_id == tenant_id,
                Tool.type == "mcp",
            )
        )
    ).all()
    if len(rows) != 1:
        raise ValueError("MCP server tool not found")
    server_tool, runtime_tool = rows[0]
    live_fingerprint = current_mcp_fingerprint(runtime_tool)
    if not live_fingerprint or live_fingerprint != str(expected_fingerprint or ""):
        raise ValueError("MCP metadata fingerprint changed; reload before reviewing")
    validated_schema = None
    try:
        if decision == "approve":
            validated = prepare_mcp_metadata_candidate(
                server_name=runtime_tool.mcp_server_name or "MCP Server",
                server_url=runtime_tool.mcp_server_url or "",
                tool_name=runtime_tool.mcp_tool_name or server_tool.mcp_tool_name,
                raw_description=runtime_tool.mcp_raw_description or "",
                raw_schema=runtime_tool.mcp_raw_schema or {},
            )
            validated_schema = validated.canonical_schema
        review_mcp_metadata(
            runtime_tool,
            decision=decision,
            reviewer_id=reviewer_id,
            expected_fingerprint=expected_fingerprint,
            canonical_description=canonical_description,
        )
    except MCPMetadataValidationError as exc:
        raise ValueError(str(exc)) from exc
    if validated_schema is not None:
        runtime_tool.parameters_schema = validated_schema

    assignments = (await db.execute(select(AgentTool).where(AgentTool.tool_id == runtime_tool.id))).scalars().all()
    if decision == "approve":
        for assignment in assignments:
            requested = assignment.mcp_trust_requested_enabled
            if requested is not None:
                assignment.enabled = bool(requested)
                assignment.mcp_trust_requested_enabled = None
        server_tool.schema_hash = runtime_tool.mcp_metadata_fingerprint
        server.status = "connected" if is_mcp_metadata_runtime_approved(runtime_tool) else "pending_review"
    else:
        sibling_tools = (
            (
                await db.execute(
                    select(Tool)
                    .join(MCPServerTool, MCPServerTool.tool_id == Tool.id)
                    .where(
                        MCPServerTool.tenant_id == tenant_id,
                        MCPServerTool.mcp_server_id == server_id,
                        Tool.type == "mcp",
                    )
                )
            )
            .scalars()
            .all()
        )
        server.status = (
            "connected" if any(is_mcp_metadata_runtime_approved(tool) for tool in sibling_tools) else "pending_review"
        )
        for assignment in assignments:
            if assignment.mcp_trust_requested_enabled is None:
                assignment.mcp_trust_requested_enabled = bool(assignment.enabled)
            assignment.enabled = False

    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=reviewer_id,
            action="mcp.metadata.reviewed",
            details={
                "server_id": str(server_id),
                "tool_id": str(runtime_tool.id),
                "tool_name": tool_name,
                "decision": decision,
                "fingerprint": expected_fingerprint,
                "trust_tier": runtime_tool.mcp_trust_tier,
                "risk_flags": list(runtime_tool.mcp_metadata_risk_flags or []),
            },
        )
    )
    await db.commit()
    facts = public_mcp_review_facts(runtime_tool)
    return {
        "tool_id": str(runtime_tool.id),
        "tool_name": tool_name,
        "display_name": server_tool.display_name or tool_name,
        "canonical_description": runtime_tool.description,
        "metadata_fingerprint": facts["fingerprint"],
        "reviewed_fingerprint": facts["reviewed_fingerprint"],
        "activation": facts["activation"],
        "risk_flags": list(runtime_tool.mcp_metadata_risk_flags or []),
        "trust_status": runtime_tool.mcp_trust_status,
        "trust_tier": runtime_tool.mcp_trust_tier,
        "runtime_approved": is_mcp_metadata_runtime_approved(runtime_tool),
    }


async def set_agent_mcp_tool_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    tool_name: str,
    *,
    mode: str,
) -> dict:
    """Upsert one per-tool MCP policy override for an agent/server/tool."""
    mode = _validate_tool_mode(mode)
    await _require_tenant_server(db, tenant_id, server_id)
    assignment_result = await db.execute(
        select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.agent_id == agent_id,
            AgentMCPServerAssignment.mcp_server_id == server_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if assignment is None:
        raise ValueError("MCP server assignment not found")

    tool_result = await db.execute(
        select(MCPServerTool).where(
            MCPServerTool.tenant_id == tenant_id,
            MCPServerTool.mcp_server_id == server_id,
            MCPServerTool.mcp_tool_name == tool_name,
        )
    )
    server_tool = tool_result.scalar_one_or_none()
    if server_tool is None:
        raise ValueError("MCP server tool not found")

    runtime_tool = None
    if server_tool.tool_id is not None:
        runtime_tool = (
            await db.execute(
                select(Tool).where(
                    Tool.id == server_tool.tool_id,
                    Tool.tenant_id == tenant_id,
                    Tool.type == "mcp",
                )
            )
        ).scalar_one_or_none()
    if mode != "deny" and not (runtime_tool and is_mcp_metadata_runtime_approved(runtime_tool)):
        raise ValueError("MCP metadata approval required before enabling this tool")

    override_result = await db.execute(
        select(AgentMCPToolOverride).where(
            AgentMCPToolOverride.tenant_id == tenant_id,
            AgentMCPToolOverride.agent_id == agent_id,
            AgentMCPToolOverride.mcp_server_id == server_id,
            AgentMCPToolOverride.tool_name == tool_name,
        )
    )
    override = override_result.scalar_one_or_none()
    if override is None:
        override = AgentMCPToolOverride(
            tenant_id=tenant_id,
            agent_id=agent_id,
            mcp_server_id=server_id,
            tool_name=tool_name,
            mode=mode,
        )
        db.add(override)
    else:
        override.mode = mode
    await db.commit()
    return {
        "tool_id": str(server_tool.tool_id) if server_tool.tool_id else None,
        "tool_name": server_tool.mcp_tool_name,
        "display_name": server_tool.display_name or server_tool.mcp_tool_name,
        "mode": mode,
        "effective_mode": (
            _effective_tool_mode(assignment=assignment, override_mode=mode)
            if runtime_tool and is_mcp_metadata_runtime_approved(runtime_tool)
            else "deny"
        ),
        "trust_status": getattr(runtime_tool, "mcp_trust_status", None) or "missing",
        "trust_tier": getattr(runtime_tool, "mcp_trust_tier", None) or "untrusted",
        "runtime_approved": bool(runtime_tool and is_mcp_metadata_runtime_approved(runtime_tool)),
    }


async def resolve_agent_mcp_tool_mode(
    db: AsyncSession, agent_id: uuid.UUID, tool, *, server_id: uuid.UUID | None = None
) -> str | None:
    """Return the effective MCP mode for a concrete runtime Tool row.

    ``None`` means no new MCP server record links this tool yet, so callers may
    keep the legacy AgentTool fallback for un-backfilled tenants. ``deny`` means
    the call must be blocked. ``approval`` and ``auto`` are reachable modes.
    """
    tool_id = getattr(tool, "id", None)
    if tool_id is None or getattr(tool, "type", None) != "mcp":
        return None
    if not is_mcp_metadata_runtime_approved(tool):
        return "deny"

    from app.models.tenant_tool_config import TenantToolConfig
    from app.database import get_current_tenant_id

    company_enabled = (await db.execute(select(TenantToolConfig.enabled).where(
        TenantToolConfig.tenant_id == get_current_tenant_id(), TenantToolConfig.tool_id == tool_id,
    ))).scalar_one_or_none()
    if company_enabled is False:
        return "deny"

    query = select(MCPServerTool).where(MCPServerTool.tool_id == tool_id)
    if server_id is not None:
        query = query.where(MCPServerTool.mcp_server_id == server_id)
    server_tools_result = await db.execute(query)
    server_tools = list(server_tools_result.scalars().all())
    if not server_tools:
        return "deny" if server_id is not None else None

    for server_tool in server_tools:
        assignment_result = await db.execute(
            select(AgentMCPServerAssignment).where(
                AgentMCPServerAssignment.agent_id == agent_id,
                AgentMCPServerAssignment.mcp_server_id == server_tool.mcp_server_id,
            )
        )
        assignment = assignment_result.scalar_one_or_none()
        if assignment is None:
            continue
        if not bool(assignment.enabled):
            return "deny"

        override_result = await db.execute(
            select(AgentMCPToolOverride).where(
                AgentMCPToolOverride.agent_id == agent_id,
                AgentMCPToolOverride.mcp_server_id == server_tool.mcp_server_id,
                AgentMCPToolOverride.tool_name == server_tool.mcp_tool_name,
            )
        )
        override = override_result.scalar_one_or_none()
        return _effective_tool_mode(assignment=assignment, override_mode=getattr(override, "mode", None))

    return "deny"


async def trigger_tenant_backfill(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Run the Part 2 backfill for one tenant and return its summary."""
    return await backfill_tenant_mcp_servers(db, tenant_id)


async def delete_tenant_server(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID) -> dict:
    """Delete one MCP server record (by id, tenant-scoped) and everything it owns.

    Removes the server-first rows (``MCPServerTool`` / ``AgentMCPServerAssignment``
    / ``AgentMCPToolOverride``) and preserves the legacy delete's cleanup of the
    underlying runtime ``Tool(type="mcp")`` rows + their ``AgentTool`` rows for
    this tenant — matching what ``mcp_registry_service.delete_tenant_mcp_server``
    did, but keyed off the stable ``MCPServer.id`` instead of a pack-derived key.
    """
    server_row = await db.execute(select(MCPServer).where(
        MCPServer.id == server_id, MCPServer.tenant_id == tenant_id, MCPServer.owner_user_id.is_(None)
    ))
    server = server_row.scalar_one_or_none()
    if server is None:
        raise ValueError("MCP server not found")

    # Collect the underlying runtime Tool ids before the MCPServerTool rows go away.
    linked_tool_result = await db.execute(
        select(MCPServerTool.tool_id).where(
            MCPServerTool.tenant_id == tenant_id,
            MCPServerTool.mcp_server_id == server_id,
            MCPServerTool.tool_id.is_not(None),
        )
    )
    tool_ids = [row[0] for row in linked_tool_result.all() if row[0] is not None]

    # Server-first rows. MCPServerTool / AgentMCPServerAssignment / AgentMCPToolOverride
    # all FK mcp_servers.id ON DELETE CASCADE, but we delete explicitly so the same
    # transaction holds in environments where the cascade is not exercised.
    await db.execute(
        delete(AgentMCPToolOverride).where(
            AgentMCPToolOverride.tenant_id == tenant_id,
            AgentMCPToolOverride.mcp_server_id == server_id,
        )
    )
    await db.execute(
        delete(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.mcp_server_id == server_id,
        )
    )
    await db.execute(
        delete(MCPServerTool).where(
            MCPServerTool.tenant_id == tenant_id,
            MCPServerTool.mcp_server_id == server_id,
        )
    )

    # Legacy Tool cleanup (preserved from delete_tenant_mcp_server): drop this
    # tenant's AgentTool rows for the underlying tools, then delete the Tool rows
    # that are left without any AgentTool reference.
    if tool_ids:
        tenant_agents = select(Agent.id).where(Agent.tenant_id == tenant_id)
        await db.execute(
            delete(AgentTool).where(
                AgentTool.agent_id.in_(tenant_agents),
                AgentTool.tool_id.in_(tool_ids),
            )
        )
        for tool_id in tool_ids:
            remaining = await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id))
            if remaining.scalar_one_or_none() is None:
                tool_row = await db.execute(select(Tool).where(Tool.id == tool_id))
                tool = tool_row.scalar_one_or_none()
                if tool is not None:
                    await db.delete(tool)

    await db.delete(server)
    await db.commit()
    return {"status": "deleted", "server_id": str(server_id)}


async def _read_tenant_mcp_tools_for_server(
    db: AsyncSession, tenant_id: uuid.UUID, server_name: str, server_url: str
) -> tuple[list[McpToolRow], dict[str, bool], dict[tuple[str, str], AgentToolState], str]:
    """Read one freshly-imported server's ``Tool(type="mcp")`` rows + per-agent states.

    Scoped to a single ``(server_name, server_url)`` so the upsert stays per-server
    and does NOT touch other servers (unlike the full-tenant backfill).
    """
    result = await db.execute(
        select(
            Tool.id,
            Tool.mcp_tool_name,
            Tool.display_name,
            Tool.mcp_server_name,
            Tool.mcp_server_url,
            Tool.is_default,
            Tool.type,
            Tool.enabled.label("tool_enabled"),
            Tool.mcp_trust_status,
            Tool.mcp_metadata_fingerprint,
            Tool.mcp_reviewed_fingerprint,
            Tool.mcp_raw_description,
            Tool.mcp_raw_schema,
            Tool.config,
            AgentTool.agent_id,
            AgentTool.enabled,
            AgentTool.mcp_trust_requested_enabled,
        )
        .join(AgentTool, AgentTool.tool_id == Tool.id)
        .join(Agent, Agent.id == AgentTool.agent_id)
        .where(
            Agent.tenant_id == tenant_id,
            Tool.type == "mcp",
            Tool.mcp_server_name == server_name,
            Tool.mcp_server_url == server_url,
        )
    )
    tool_rows: list[McpToolRow] = []
    seen: set[str] = set()
    is_default: dict[str, bool] = {}
    states: dict[tuple[str, str], AgentToolState] = {}
    raw_rows = result.all()
    for r in raw_rows:
        tid = str(r.id)
        if tid not in seen:
            seen.add(tid)
            tool_rows.append(
                McpToolRow(
                    tool_id=tid,
                    mcp_tool_name=r.mcp_tool_name or "",
                    display_name=r.display_name or "",
                    mcp_server_name=r.mcp_server_name or "MCP Server",
                    mcp_server_url=r.mcp_server_url or "",
                    is_default=bool(r.is_default),
                )
            )
            is_default[tid] = bool(r.is_default)
        requested_enabled = getattr(r, "mcp_trust_requested_enabled", None)
        states[(str(r.agent_id), tid)] = AgentToolState(
            agent_id=str(r.agent_id), tool_id=tid, has_row=True,
            enabled=bool(r.enabled) if requested_enabled is None else bool(requested_enabled),
        )
    return tool_rows, is_default, states, _mcp_server_activation_status(raw_rows)


def _approval_view(row: Any) -> SimpleNamespace:
    return SimpleNamespace(
        type=getattr(row, "type", None),
        enabled=getattr(row, "tool_enabled", False),
        mcp_trust_status=getattr(row, "mcp_trust_status", None),
        mcp_metadata_fingerprint=getattr(row, "mcp_metadata_fingerprint", None),
        mcp_reviewed_fingerprint=getattr(row, "mcp_reviewed_fingerprint", None),
        mcp_server_name=getattr(row, "mcp_server_name", None),
        mcp_server_url=getattr(row, "mcp_server_url", None),
        mcp_tool_name=getattr(row, "mcp_tool_name", None),
        mcp_raw_description=getattr(row, "mcp_raw_description", None),
        mcp_raw_schema=getattr(row, "mcp_raw_schema", None),
        config=getattr(row, "config", None),
    )


def _mcp_server_activation_status(rows: list[Any]) -> str:
    mcp_rows = [row for row in rows if getattr(row, "type", None) == "mcp"]
    if mcp_rows and not any(is_mcp_metadata_runtime_approved(_approval_view(row)) for row in mcp_rows):
        return "pending_review"
    return "connected"


async def register_imported_server(
    db: AsyncSession, tenant_id: uuid.UUID, server_name: str, server_url: str
) -> dict | None:
    """Incrementally upsert the MCPServer record for one freshly-imported server.

    After the Tool rows exist (import created them), build the server-first
    records for *just this server*: the ``MCPServer`` row, its ``MCPServerTool``
    rows, and one ``AgentMCPServerAssignment`` per tenant agent — reusing
    ``group_mcp_tools`` + ``plan_agent_assignment`` from the backfill functional
    core. This is per-server, unlike ``backfill_tenant_mcp_servers`` which
    idempotent-skips a tenant that already has any server rows. Idempotent on
    ``(tenant_id, server_key)``: a re-run reuses the existing ``MCPServer`` and
    only adds the tools/assignments not yet present.
    """
    tool_rows, is_default, states, activation_status = await _read_tenant_mcp_tools_for_server(
        db, tenant_id, server_name, server_url
    )
    if not tool_rows:
        return None

    spec = group_mcp_tools(tool_rows)[0]

    existing_server = await db.execute(
        select(MCPServer).where(MCPServer.tenant_id == tenant_id, MCPServer.server_key == spec.server_key)
    )
    mcp_server = existing_server.scalar_one_or_none()
    if mcp_server is None:
        # The slug may already be taken by an unrelated server (same name, different
        # url). Pick a free key so the (tenant_id, server_key) constraint holds.
        taken_result = await db.execute(select(MCPServer.server_key).where(MCPServer.tenant_id == tenant_id))
        taken = {row[0] for row in taken_result.all()}
        from app.services.mcp_backfill import derive_server_key

        server_key = (
            spec.server_key if spec.server_key not in taken else derive_server_key(spec.name, spec.server_url, taken)
        )
        mcp_server = MCPServer(
            tenant_id=tenant_id,
            name=spec.name,
            server_key=server_key,
            server_url=spec.server_url or None,
            registry_source="direct",
            status=activation_status,
            auth_status="none",
            config_json={},
        )
        db.add(mcp_server)
        await db.flush()
    else:
        mcp_server.status = activation_status

    # Upsert MCPServerTool rows (skip the ones already linked).
    existing_tools_result = await db.execute(
        select(MCPServerTool.mcp_tool_name).where(
            MCPServerTool.tenant_id == tenant_id, MCPServerTool.mcp_server_id == mcp_server.id
        )
    )
    existing_tool_names = {row[0] for row in existing_tools_result.all()}
    tool_rows_by_id = {row.tool_id: row for row in tool_rows}
    for tool_id, tool_name in zip(spec.tool_ids, spec.tool_names):
        if tool_name in existing_tool_names:
            continue
        db.add(
            MCPServerTool(
                tenant_id=tenant_id,
                mcp_server_id=mcp_server.id,
                tool_id=uuid.UUID(tool_id),
                mcp_tool_name=tool_name,
                display_name=tool_rows_by_id[tool_id].display_name,
            )
        )

    # Upsert one assignment per agent that has a relationship to this server.
    existing_assign_result = await db.execute(
        select(AgentMCPServerAssignment.agent_id).where(
            AgentMCPServerAssignment.tenant_id == tenant_id,
            AgentMCPServerAssignment.mcp_server_id == mcp_server.id,
        )
    )
    existing_assigned_agents = {row[0] for row in existing_assign_result.all()}
    is_default_map = {tid: is_default.get(tid, False) for tid in spec.tool_ids}
    server_tool_ids = set(spec.tool_ids)
    agent_ids = {agent_id for (agent_id, tid) in states if tid in server_tool_ids}
    for agent_id in sorted(agent_ids):
        if uuid.UUID(agent_id) in existing_assigned_agents:
            continue
        agent_states = {tid: states[(agent_id, tid)] for tid in spec.tool_ids if (agent_id, tid) in states}
        assignment = plan_agent_assignment(spec, agent_states, is_default_map, agent_id)
        if assignment is None:
            continue
        db.add(
            AgentMCPServerAssignment(
                tenant_id=tenant_id,
                agent_id=uuid.UUID(agent_id),
                mcp_server_id=mcp_server.id,
                enabled=assignment.enabled,
                default_tool_mode="auto",
            )
        )
        for deny_name in assignment.deny_tool_names:
            db.add(
                AgentMCPToolOverride(
                    tenant_id=tenant_id,
                    agent_id=uuid.UUID(agent_id),
                    mcp_server_id=mcp_server.id,
                    tool_name=deny_name,
                    mode="deny",
                )
            )

    await db.commit()
    return {
        "id": str(mcp_server.id),
        "name": mcp_server.name,
        "server_key": mcp_server.server_key,
        "status": mcp_server.status,
        "auth_status": mcp_server.auth_status,
        "transport": mcp_server.transport,
        "server_url": mcp_server.server_url,
    }


async def import_and_register(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    server_id: str | None = None,
    mcp_url: str | None = None,
    server_name: str | None = None,
    config: dict | None = None,
) -> dict:
    """Import an MCP server's tools for the tenant, then register the server record.

    The Tool-creating half is the same flow the legacy import used: call the
    ``resource_discovery`` importer (direct URL or Smithery), then assign the new
    tools to every tenant agent. Once the tools exist, ``register_imported_server``
    builds the first-class ``MCPServer`` record. Returns the new server-first DTO.
    """
    from app.services.resource_discovery import MCPImportResult, import_mcp_direct, import_mcp_from_smithery

    requested_transport = (config or {}).get("transport") if isinstance(config, dict) else None
    if mcp_url or requested_transport:
        assert_mcp_cloud_transport_allowed(server_url=mcp_url, transport=requested_transport)

    agents_result = await db.execute(
        select(Agent.id).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at.asc())
    )
    agent_ids = [row[0] for row in agents_result.all()]
    if not agent_ids:
        raise ValueError("This company needs at least one agent before importing MCP servers.")

    bootstrap_agent_id = agent_ids[0]
    if mcp_url:
        message = await import_mcp_direct(
            mcp_url, bootstrap_agent_id, server_name=server_name, api_key=(config or {}).get("api_key")
        )
        tool_query = select(Tool.id).where(Tool.type == "mcp", Tool.mcp_server_url == mcp_url)
    else:
        if not server_id:
            raise ValueError("server_id or mcp_url is required")
        message = await import_mcp_from_smithery(server_id, bootstrap_agent_id, config=config or None)
        clean_id = server_id.replace("/", "_").replace("@", "")
        tool_query = select(Tool.id).where(Tool.type == "mcp", Tool.name.like(f"mcp_{clean_id}%"))

    if isinstance(message, MCPImportResult):
        tool_query = select(Tool.id).where(Tool.type == "mcp", Tool.id.in_(message.tool_ids))
    tool_query = tool_query.where(Tool.tenant_id == tenant_id)

    # Assign the freshly-created tools to every tenant agent (same as the legacy path).
    from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment

    tool_result = await db.execute(tool_query)
    tool_ids = [row[0] for row in tool_result.all()]
    server_name_for_records = server_url_for_records = None
    for tool_id in tool_ids:
        tool_lookup = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = tool_lookup.scalar_one_or_none()
        if tool is not None:
            tool.tenant_id = tenant_id
            server_name_for_records = tool.mcp_server_name or "MCP Server"
            server_url_for_records = tool.mcp_server_url or ""
        runtime_enabled = bool(tool is not None and is_mcp_metadata_runtime_approved(tool))
        for agent_id in agent_ids:
            await ensure_agent_tool_assignment(
                db,
                agent_id=agent_id,
                tool_id=tool_id,
                enabled=runtime_enabled,
                source="system",
                quarantine_for_mcp_trust=not runtime_enabled,
            )
    await db.commit()

    server_record = None
    if server_name_for_records is not None:
        server_record = await register_imported_server(
            db, tenant_id, server_name_for_records, server_url_for_records or ""
        )
    return {"message": message, "server": server_record}


async def import_mcp_for_agent_and_register(
    agent_id: uuid.UUID,
    *,
    server_id: str | None = None,
    mcp_url: str | None = None,
    server_name: str | None = None,
    config: dict | None = None,
    reauthorize: bool = False,
) -> str:
    """Agent-scoped MCP import path used by the ``import_mcp_server`` tool.

    The legacy resource discovery import creates runtime ``Tool`` rows and
    ``AgentTool`` rows for the current agent. This wrapper then registers the
    corresponding first-class MCPServer records for just the server(s) touched by
    that import, preserving agent-scoped install semantics instead of assigning
    the server to every tenant agent.
    """
    from app.database import tenant_scoped_session
    from app.services.resource_discovery import MCPImportResult, import_mcp_direct, import_mcp_from_smithery
    from app.services.tenant_resolver import resolve_tenant_for_agent

    config = dict(config or {})
    requested_transport = config.get("transport")
    if mcp_url or requested_transport:
        assert_mcp_cloud_transport_allowed(server_url=mcp_url, transport=requested_transport)
    tenant_id = await resolve_tenant_for_agent(agent_id)
    if tenant_id is None:
        raise ValueError("Agent tenant not found")

    if mcp_url:
        api_key = config.pop("api_key", None)
        message = await import_mcp_direct(mcp_url, agent_id, server_name=server_name, api_key=api_key)
    else:
        if not server_id:
            raise ValueError("server_id or mcp_url is required")
        message = await import_mcp_from_smithery(server_id, agent_id, config or None, reauthorize=reauthorize)

    async with tenant_scoped_session(tenant_id) as db:
        query = (
            select(Tool.mcp_server_name, Tool.mcp_server_url)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(AgentTool.agent_id == agent_id, Tool.type == "mcp", Tool.tenant_id == tenant_id)
        )
        if isinstance(message, MCPImportResult):
            query = query.where(Tool.id.in_(message.tool_ids))
        elif mcp_url:
            query = query.where(Tool.mcp_server_url == mcp_url)
        elif server_id:
            clean_id = server_id.replace("/", "_").replace("@", "")
            query = query.where(Tool.name.like(f"mcp_{clean_id}%"))
        rows = await db.execute(query)
        seen: set[tuple[str, str]] = set()
        registered = []
        for name, url in rows.all():
            server_name_for_records = name or server_name or "MCP Server"
            server_url_for_records = url or mcp_url or ""
            key = (server_name_for_records, server_url_for_records)
            if key in seen:
                continue
            seen.add(key)
            record = await register_imported_server(
                db,
                tenant_id,
                server_name_for_records,
                server_url_for_records,
            )
            if record:
                registered.append(record["name"])
        if registered:
            message += "\n\nRegistered MCP server records: " + ", ".join(sorted(registered))
        return message


# ── OAuth2 (Step 7) ───────────────────────────────────────────────────────────
# Tokens live server-side encrypted on MCPServer.config_json["oauth"]; the agent
# never sees or supplies them (守 mcp_authz). auth_status drives fail-closed.


_OAUTH_PENDING_SECONDS = 600


async def _locked_oauth_server(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID) -> MCPServer:
    result = await db.execute(
        select(MCPServer)
        .where(MCPServer.id == server_id, MCPServer.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    server = result.scalar_one_or_none()
    if server is None:
        raise mcp_oauth.OAuthFlowError("oauth_server_not_found", "unavailable", 404)
    return server


def _store_oauth(server: MCPServer, oauth: dict) -> None:
    server.config_json = {**(server.config_json or {}), "oauth": oauth}


def _attempt_result(server: MCPServer, attempt: dict) -> dict:
    outcome = attempt["outcome"]
    if outcome != "configured":
        raise mcp_oauth.OAuthFlowError(attempt["code"], outcome, 409 if outcome == "unknown" else 400)
    return {"server_id": str(server.id), "auth_status": server.auth_status, "attempt_id": attempt["id"]}


async def get_mcp_oauth_status(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID) -> dict:
    server = await _require_tenant_server(db, tenant_id, server_id)
    oauth = (server.config_json or {}).get("oauth") or {}
    pending = oauth.get("pending") or {}
    attempt = oauth.get("last_attempt")
    if pending:
        outcome = "unknown" if pending.get("status") == "exchanging" else "pending"
        if outcome == "pending" and pending.get("expires_at", 0) <= time.time():
            outcome = "expired"
        attempt = {"id": pending.get("id"), "outcome": outcome, "expires_at": pending.get("expires_at")}
    if attempt:
        attempt = {key: attempt.get(key) for key in ("id", "outcome", "expires_at", "code")}
    granted = (await db.execute(select(Agent.id, Agent.name).join(AgentMCPServerAssignment, AgentMCPServerAssignment.agent_id == Agent.id)
        .where(AgentMCPServerAssignment.tenant_id == tenant_id, AgentMCPServerAssignment.mcp_server_id == server_id,
               AgentMCPServerAssignment.enabled.is_(True), Agent.deleted_at.is_(None)).order_by(Agent.name, Agent.id))).all()
    registration = pending or oauth
    if server.owner_user_id is not None:
        from app.models.mcp_server import MCPAccountGrant
        parent = await db.get(MCPServer, server.parent_server_id) if server.parent_server_id else None
        registered = ((parent.config_json or {}).get("oauth") or {}) if parent else {}
        registration = registered.get("pending") or registered
        granted = (await db.execute(select(Agent.id, Agent.name).distinct().join(MCPAccountGrant, MCPAccountGrant.agent_id == Agent.id)
            .where(MCPAccountGrant.account_id == server.id, MCPAccountGrant.enabled.is_(True), Agent.deleted_at.is_(None))
            .order_by(Agent.name, Agent.id))).all()
    return {
        "server_id": str(server.id),
        "auth_status": server.auth_status,
        "attempt": attempt,
        "refresh_outcome": (oauth.get("refresh") or {}).get("status"),
        "registration": {key: registration.get(key) for key in ("authorization_endpoint", "token_endpoint", "client_id", "redirect_uri", "scope")},
        "client_secret_configured": bool(registration.get("client_secret")),
        "authorized_agents": [{"id": str(agent_id), "name": name} for agent_id, name in granted],
    }


async def start_mcp_oauth(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str | None = None,
    client_secret: str | None = None,
    requester_id: uuid.UUID,
) -> dict:
    for endpoint in (authorization_endpoint, token_endpoint):
        try:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
        except ValueError as exc:
            raise mcp_oauth.OAuthFlowError("oauth_endpoint_invalid", "unavailable", 422) from exc
    server = await _locked_oauth_server(db, tenant_id, server_id)
    if server.owner_user_id is not None:
        from app.services.mcp_accounts import REGISTRATION_FIELDS, _active_in_tenant
        from app.models.user import User
        owner = await db.get(User, requester_id)
        if requester_id != server.owner_user_id or not _active_in_tenant(owner, tenant_id):
            raise mcp_oauth.OAuthFlowError("mcp_account_owner_required", "unavailable", 403)
        parent = await db.get(MCPServer, server.parent_server_id) if server.parent_server_id else None
        if parent is None or parent.tenant_id != tenant_id or parent.owner_user_id is not None:
            raise mcp_oauth.OAuthFlowError("mcp_account_definition_unavailable", "unavailable", 409)
        approved_oauth = (parent.config_json or {}).get("oauth") or {}
        approved = approved_oauth.get("pending") or approved_oauth
        requested = dict(authorization_endpoint=authorization_endpoint, token_endpoint=token_endpoint,
            client_id=client_id, redirect_uri=redirect_uri, scope=scope)
        if not all(approved.get(key) for key in REGISTRATION_FIELDS[:4]) or any(
            (requested.get(key) or "") != (approved.get(key) or "") for key in REGISTRATION_FIELDS
        ):
            raise mcp_oauth.OAuthFlowError("mcp_account_registration_changed", "unavailable", 409)
        if client_secret is None:
            client_secret = mcp_oauth.decrypt_value(approved.get("client_secret"))
            if approved.get("client_secret") and client_secret is None:
                raise mcp_oauth.OAuthFlowError("oauth_encryption_unavailable", "unavailable", 503)
            client_secret = client_secret or ""
    verifier, challenge = mcp_oauth.generate_pkce_pair()
    # The locator only chooses an RLS scope; the exact stored random-state digest authorizes the callback.
    state = f"mcp1.{tenant_id.hex}.{server_id.hex}.{mcp_oauth.generate_state()}"
    expires_at = time.time() + _OAUTH_PENDING_SECONDS
    attempt_id = str(uuid.uuid4())
    oauth = dict((server.config_json or {}).get("oauth") or {})
    try:
        encrypted_verifier = mcp_oauth.encrypt_value(verifier)
        previous = oauth
        pending = oauth.get("pending") or {}
        if pending.get("client_id") == client_id and pending.get("token_endpoint") == token_endpoint:
            previous = pending
        if client_secret is None and previous.get("client_id") == client_id and previous.get("token_endpoint") == token_endpoint:
            client_secret = mcp_oauth.decrypt_value(previous.get("client_secret"))
            if previous.get("client_secret") and client_secret is None:
                raise mcp_oauth.OAuthError("OAuth encrypted storage is unavailable")
        encrypted_secret = mcp_oauth.encrypt_value(client_secret) if client_secret else None
    except mcp_oauth.OAuthError as exc:
        raise mcp_oauth.OAuthFlowError("oauth_encryption_unavailable", "unavailable", 503) from exc
    oauth["pending"] = {
        "id": attempt_id,
        "state_sha256": hashlib.sha256(state.encode()).hexdigest(),
        "status": "pending",
        "expires_at": expires_at,
        "requester_id": str(requester_id),
        "code_verifier": encrypted_verifier,
        **({"account_server_url": parent.server_url, "account_transport": parent.transport} if server.owner_user_id is not None else {}),
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "client_secret": encrypted_secret,
    }
    _store_oauth(server, oauth)
    await db.commit()
    url = mcp_oauth.build_authorization_url(
        authorization_endpoint=authorization_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scope=scope,
    )
    return {
        "authorization_url": url,
        "state": state,
        "server_id": str(server_id),
        "attempt_id": attempt_id,
        "expires_at": expires_at,
    }


async def complete_mcp_oauth(db: AsyncSession, state: str, code: str, error: str = "") -> dict:
    try:
        version, tenant, server_key, nonce = state.split(".")
        if version != "mcp1" or len(nonce) < 40:
            raise ValueError
        tenant_id, server_id = uuid.UUID(hex=tenant), uuid.UUID(hex=server_key)
    except (ValueError, AttributeError) as exc:
        raise mcp_oauth.OAuthFlowError("oauth_state_invalid", "unavailable") from exc
    await pin_rls_tenant_context(db, tenant_id)
    try:
        server = await _locked_oauth_server(db, tenant_id, server_id)
    except mcp_oauth.OAuthFlowError as exc:
        raise mcp_oauth.OAuthFlowError("oauth_state_invalid_or_superseded", "unavailable") from exc
    oauth = dict((server.config_json or {}).get("oauth") or {})
    digest = hashlib.sha256(state.encode()).hexdigest()
    previous = oauth.get("last_attempt") or {}
    if previous.get("state_sha256") == digest:
        return _attempt_result(server, previous)
    pending = dict(oauth.get("pending") or {})
    if pending.get("state_sha256") != digest:
        raise mcp_oauth.OAuthFlowError("oauth_state_invalid_or_superseded", "unavailable")
    if pending.get("status") == "exchanging":
        raise mcp_oauth.OAuthFlowError("oauth_exchange_in_progress_or_unknown", "unknown", 409)
    receipt = {
        "id": pending["id"],
        "state_sha256": digest,
        "expires_at": pending["expires_at"],
        "requester_id": pending["requester_id"],
    }
    if pending["expires_at"] <= time.time():
        receipt.update(outcome="expired", code="oauth_attempt_expired")
    elif error:
        receipt.update(outcome="cancelled" if error == "access_denied" else "failed", code="oauth_provider_denied")
    elif not code:
        raise mcp_oauth.OAuthFlowError("oauth_code_required", "pending")
    verifier = mcp_oauth.decrypt_value(pending.get("code_verifier"))
    client_secret = mcp_oauth.decrypt_value(pending.get("client_secret"))
    if "outcome" not in receipt and (not verifier or (pending.get("client_secret") and not client_secret)):
        raise mcp_oauth.OAuthFlowError("oauth_encryption_unavailable", "unavailable", 503)
    if "outcome" in receipt:
        _store_oauth(server, {**oauth, "pending": None, "last_attempt": receipt})
        await db.commit()
        return _attempt_result(server, receipt)
    # Commit the claim before a code can be redeemed. A crash cannot authorize a second redemption.
    _store_oauth(server, {**oauth, "pending": {**pending, "status": "exchanging"}})
    await db.commit()
    token_blob = None
    try:
        token = await mcp_oauth.exchange_code_for_token(
            token_endpoint=pending["token_endpoint"],
            client_id=pending["client_id"],
            code=code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=verifier,
            client_secret=client_secret,
        )
        token_blob = mcp_oauth.encrypt_token_set(token)
        receipt.update(outcome="configured", code="oauth_configured")
    except mcp_oauth.OAuthError as exc:
        outcome = "failed" if exc.outcome == "failed" else "unknown"
        receipt.update(outcome=outcome, code=f"oauth_exchange_{outcome}")
    try:
        server = await _locked_oauth_server(db, tenant_id, server_id)
    except mcp_oauth.OAuthFlowError as exc:
        raise mcp_oauth.OAuthFlowError("oauth_attempt_superseded", "unknown", 409) from exc
    oauth = dict((server.config_json or {}).get("oauth") or {})
    if (oauth.get("pending") or {}).get("id") != pending["id"]:
        raise mcp_oauth.OAuthFlowError("oauth_attempt_superseded", "unknown", 409)
    oauth.update(pending=None, last_attempt=receipt)
    if token_blob:
        if server.owner_user_id is not None:
            server.server_url = pending.get("account_server_url", server.server_url)
            server.transport = pending.get("account_transport", server.transport)
        oauth.update(
            token=token_blob,
            authorization_id=pending["id"],
            authorization_endpoint=pending.get("authorization_endpoint"),
            redirect_uri=pending["redirect_uri"],
            scope=pending.get("scope"),
            token_endpoint=pending["token_endpoint"],
            client_id=pending["client_id"],
            client_secret=pending.get("client_secret"),
            refresh=None,
        )
        server.auth_status = mcp_oauth.AUTH_CONFIGURED
        if server.status == "needs_auth":
            server.status = "connected"
    elif not mcp_oauth.decrypt_token_set(oauth.get("token")):
        server.auth_status = mcp_oauth.AUTH_ERROR
    _store_oauth(server, oauth)
    await db.commit()
    return _attempt_result(server, receipt)


async def resolve_mcp_oauth_bearer(
    db: AsyncSession, tenant_id: uuid.UUID | None, server_url: str, *, tool_id: uuid.UUID | None = None, server_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None, execution_context=None,
) -> tuple[str | None, str | None]:
    """No OAuth falls back to API-key auth; configured but unusable OAuth fails locally."""
    result = await db.execute(
        select(MCPServer)
        .where(MCPServer.tenant_id == tenant_id, MCPServer.server_url == server_url, MCPServer.owner_user_id.is_(None))
        .order_by(MCPServer.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    servers = list(result.scalars().all())
    if server_id is not None:
        servers = [server for server in servers if server.id == server_id]
        if not servers:
            return None, "MCP connection changed; review the current authorization."
    if not servers:
        return None, None
    if execution_context is None and all(
        not (server.config_json or {}).get("oauth") and server.auth_status == mcp_oauth.AUTH_NONE for server in servers
    ):
        return None, None
    if len(servers) > 1 and tool_id is not None:
        linked_ids = set(
            (
                await db.execute(
                    select(MCPServerTool.mcp_server_id).where(
                        MCPServerTool.tool_id == tool_id, MCPServerTool.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        servers = [server for server in servers if server.id in linked_ids]
    if len(servers) != 1:
        return None, "MCP connection is ambiguous; select one server assignment before authorizing this tool."
    from app.services.mcp_accounts import protect_account_credentials, resolve_work_account
    server, binding = await resolve_work_account(db, server=servers[0], agent_id=agent_id, context=execution_context)
    if server.server_url != server_url:
        return None, "MCP account endpoint changed; re-authorize and select the account again."

    async def resolved(token, *, recheck_authority=False):
        if recheck_authority and binding is not None:
            from fastapi import HTTPException
            current_account, current_binding = await resolve_work_account(db, server=servers[0], agent_id=agent_id, context=execution_context)
            if current_binding is None or current_account.id != server.id:
                raise HTTPException(status_code=409, detail="MCP work account changed during refresh; review its selection")
        protect_account_credentials(execution_context, account=server, token=token)
        if binding is not None:
            db.add(AuditLog(tenant_id=tenant_id, user_id=binding.requester_user_id, agent_id=binding.agent_id,
                action="mcp.account.credentials_resolved", details={"account_id": str(server.id),
                    "server_id": str(binding.server_id), "binding_id": str(binding.id),
                    "grant_id": str(binding.grant_id) if binding.grant_id else None,
                    "grant_version": binding.grant_version, "session_id": str(binding.session_id),
                    "runtime_task_id": str(getattr(execution_context, "runtime_task_id", None) or "") or None,
                    "status": "credential_resolved; external_execution_not_asserted"}))
            await db.commit()
        return token.access_token, None

    oauth = (server.config_json or {}).get("oauth") or {}
    token = mcp_oauth.decrypt_token_set(oauth.get("token"))
    if token is None:
        if oauth or server.auth_status != mcp_oauth.AUTH_NONE or server.owner_user_id is not None:
            return None, "MCP OAuth authorization is missing or unreadable; re-authorize the server."
        return None, None
    if not token.is_expired(time.time()):
        return await resolved(token)
    if not token.refresh_token:
        server.auth_status = mcp_oauth.AUTH_EXPIRED
        await db.commit()
        return None, "MCP OAuth token expired and has no refresh token; re-authorize the server."
    if oauth.get("refresh"):
        return None, "MCP OAuth refresh is in progress or its outcome is unknown; check status or re-authorize."
    client_secret = mcp_oauth.decrypt_value(oauth.get("client_secret"))
    if (
        not oauth.get("token_endpoint")
        or not oauth.get("client_id")
        or (oauth.get("client_secret") and not client_secret)
    ):
        return None, "MCP OAuth refresh configuration is unavailable; re-authorize the server."
    try:
        mcp_oauth.encrypt_value("oauth-storage-probe")
    except mcp_oauth.OAuthError:
        return None, "MCP OAuth encrypted storage is unavailable; restore it before refreshing."
    claim = str(uuid.uuid4())
    _store_oauth(server, {**oauth, "refresh": {"id": claim, "status": "exchanging"}})
    server_id = server.id
    await db.commit()
    refreshed_blob = None
    try:
        refreshed = await mcp_oauth.refresh_access_token(
            token_endpoint=oauth["token_endpoint"],
            client_id=oauth["client_id"],
            refresh_token=token.refresh_token,
            client_secret=client_secret,
        )
        refreshed_blob = mcp_oauth.encrypt_token_set(refreshed)
        outcome = "configured"
    except mcp_oauth.OAuthError as exc:
        outcome = "failed" if exc.outcome == "failed" else "unknown"
    server = await _locked_oauth_server(db, tenant_id, server_id)
    current = dict((server.config_json or {}).get("oauth") or {})
    if (current.get("refresh") or {}).get("id") != claim or current.get("token") != oauth.get("token"):
        return None, "MCP OAuth authorization changed during refresh; read the current authorization again."
    current["refresh"] = None if refreshed_blob else {"id": claim, "status": outcome}
    if refreshed_blob:
        current["token"] = refreshed_blob
    server.auth_status = mcp_oauth.AUTH_CONFIGURED if refreshed_blob else mcp_oauth.AUTH_EXPIRED
    _store_oauth(server, current)
    await db.commit()
    if not refreshed_blob:
        return None, "MCP OAuth refresh failed or its outcome is unknown; re-authorize the server."
    return await resolved(refreshed, recheck_authority=True)
