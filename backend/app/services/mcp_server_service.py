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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.mcp_server import (
    AgentMCPServerAssignment,
    MCPServer,
    MCPServerTool,
)
from app.services.mcp_backfill_service import backfill_tenant_mcp_servers

logger = logging.getLogger(__name__)


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


async def get_agent_extensions(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    """Single source of truth for an agent's extension state: skills + MCP servers."""
    return {
        "skills": await _list_agent_workspace_skills(agent_id),
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
) -> dict:
    """Upsert one agent↔MCP server assignment (unique per tenant+agent+server)."""
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
        )
        db.add(assignment)
    else:
        assignment.enabled = enabled
        assignment.default_tool_mode = default_tool_mode
    await db.commit()
    return {
        "id": str(assignment.id),
        "agent_id": str(agent_id),
        "server_id": str(server_id),
        "enabled": bool(assignment.enabled),
        "default_tool_mode": assignment.default_tool_mode,
    }


async def trigger_tenant_backfill(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Run the Part 2 backfill for one tenant and return its summary."""
    return await backfill_tenant_mcp_servers(db, tenant_id)
