"""Server-first MCP extension surface — imperative shell over the MCP tables.

Reads and writes the Part 1 tables (``MCPServer``, ``MCPServerTool``,
``AgentMCPServerAssignment``) introduced to replace the legacy pack-derived MCP
grouping. Every query is tenant-scoped, and no DTO emitted here carries a
``pack`` / ``pack_name`` field — the public surface is server-first with stable
server identity. See docs/agent-extension-surface-skill-mcp.md §7.2–7.4, §8.2.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.capability_install import AgentCapabilityInstall
from app.models.mcp_server import (
    AgentMCPServerAssignment,
    AgentMCPToolOverride,
    MCPServer,
    MCPServerTool,
)
from app.models.tool import AgentTool, Tool
from app.services.mcp_backfill import (
    AgentToolState,
    McpToolRow,
    group_mcp_tools,
    plan_agent_assignment,
)
from app.services.mcp_backfill_service import backfill_tenant_mcp_servers

logger = logging.getLogger(__name__)

MCP_TOOL_MODES = {"auto", "approval", "deny"}
SKILL_INSTALL_KINDS = {"platform_skill", "clawhub_skill", "external_skill_url"}


def _validate_tool_mode(value: str, *, field_name: str = "mode") -> str:
    mode = (value or "auto").strip().lower()
    if mode not in MCP_TOOL_MODES:
        raise ValueError(f"{field_name} must be one of: auto, approval, deny")
    return mode


async def _require_tenant_server(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID) -> MCPServer:
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id, MCPServer.tenant_id == tenant_id))
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
    server_result = await db.execute(select(MCPServer).where(MCPServer.tenant_id == tenant_id).order_by(MCPServer.name))
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
            MCPServer.status,
            AgentMCPServerAssignment.enabled,
            AgentMCPServerAssignment.default_tool_mode,
            AgentMCPServerAssignment.always_load,
        )
        .join(AgentMCPServerAssignment, AgentMCPServerAssignment.mcp_server_id == MCPServer.id)
        .where(AgentMCPServerAssignment.agent_id == agent_id)
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

    return [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "enabled": bool(row.enabled),
            "tool_count": tool_counts.get(row.id, 0),
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


async def get_agent_extensions(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    """Single source of truth for an agent's extension state: skills + MCP servers."""
    return {
        "skills": _dedupe_skill_extensions(
            [*(await _list_agent_workspace_skills(agent_id)), *(await _list_installed_skill_extensions(db, agent_id))]
        ),
        "mcp_servers": await get_agent_mcp_servers(db, agent_id),
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
    await _require_tenant_server(db, tenant_id, server_id)
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
        select(MCPServerTool)
        .where(MCPServerTool.tenant_id == tenant_id, MCPServerTool.mcp_server_id == server_id)
        .order_by(MCPServerTool.display_name.asc(), MCPServerTool.mcp_tool_name.asc())
    )
    server_tools = list(tools_result.scalars().all())

    overrides_result = await db.execute(
        select(AgentMCPToolOverride).where(
            AgentMCPToolOverride.tenant_id == tenant_id,
            AgentMCPToolOverride.agent_id == agent_id,
            AgentMCPToolOverride.mcp_server_id == server_id,
        )
    )
    overrides = {override.tool_name: override.mode for override in overrides_result.scalars().all()}

    default_mode = _effective_tool_mode(assignment=assignment)
    return [
        {
            "tool_id": str(tool.tool_id) if tool.tool_id else None,
            "tool_name": tool.mcp_tool_name,
            "display_name": tool.display_name or tool.mcp_tool_name,
            "mode": overrides.get(tool.mcp_tool_name, default_mode),
            "effective_mode": _effective_tool_mode(
                assignment=assignment, override_mode=overrides.get(tool.mcp_tool_name)
            ),
        }
        for tool in server_tools
    ]


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
        "effective_mode": _effective_tool_mode(assignment=assignment, override_mode=mode),
    }


async def resolve_agent_mcp_tool_mode(db: AsyncSession, agent_id: uuid.UUID, tool) -> str | None:
    """Return the effective MCP mode for a concrete runtime Tool row.

    ``None`` means no new MCP server record links this tool yet, so callers may
    keep the legacy AgentTool fallback for un-backfilled tenants. ``deny`` means
    the call must be blocked. ``approval`` and ``auto`` are reachable modes.
    """
    tool_id = getattr(tool, "id", None)
    if tool_id is None or getattr(tool, "type", None) != "mcp":
        return None

    server_tools_result = await db.execute(select(MCPServerTool).where(MCPServerTool.tool_id == tool_id))
    server_tools = list(server_tools_result.scalars().all())
    if not server_tools:
        return None

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
    server_row = await db.execute(select(MCPServer).where(MCPServer.id == server_id, MCPServer.tenant_id == tenant_id))
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
) -> tuple[list[McpToolRow], dict[str, bool], dict[tuple[str, str], AgentToolState]]:
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
            AgentTool.agent_id,
            AgentTool.enabled,
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
    for r in result.all():
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
        states[(str(r.agent_id), tid)] = AgentToolState(
            agent_id=str(r.agent_id), tool_id=tid, has_row=True, enabled=bool(r.enabled)
        )
    return tool_rows, is_default, states


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
    tool_rows, is_default, states = await _read_tenant_mcp_tools_for_server(db, tenant_id, server_name, server_url)
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
            status="connected",
            auth_status="none",
            config_json={},
        )
        db.add(mcp_server)
        await db.flush()

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
    from app.services.resource_discovery import import_mcp_direct, import_mcp_from_smithery

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
        for agent_id in agent_ids:
            await ensure_agent_tool_assignment(db, agent_id=agent_id, tool_id=tool_id, enabled=True, source="system")
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
    from app.database import async_session
    from app.services.resource_discovery import import_mcp_direct, import_mcp_from_smithery

    config = dict(config or {})
    async with async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent is None or not agent.tenant_id:
            raise ValueError("Agent tenant not found")
        tenant_id = agent.tenant_id

    if mcp_url:
        api_key = config.pop("api_key", None)
        message = await import_mcp_direct(mcp_url, agent_id, server_name=server_name, api_key=api_key)
    else:
        if not server_id:
            raise ValueError("server_id or mcp_url is required")
        message = await import_mcp_from_smithery(server_id, agent_id, config or None, reauthorize=reauthorize)

    async with async_session() as db:
        query = (
            select(Tool.mcp_server_name, Tool.mcp_server_url)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(AgentTool.agent_id == agent_id, Tool.type == "mcp")
        )
        if mcp_url:
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
