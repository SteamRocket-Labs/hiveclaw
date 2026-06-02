"""MCP backfill — imperative shell (DB I/O).

Reads legacy ``Tool(type="mcp")`` + ``AgentTool`` rows and writes the new MCP
server records. The transform is delegated to the pure functions in
``mcp_backfill`` so the parity properties proven there carry through. Idempotent
per tenant: a tenant that already has ``MCPServer`` rows is skipped, so the
backfill is safe to re-run (forward-fix path). See docs §10 (data foundation).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
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


async def _read_tenant_mcp(
    db: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[str, McpToolRow], dict[str, bool], dict[tuple[str, str], AgentToolState]]:
    """Read one tenant's MCP tools + per-agent access states (via agent association)."""
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
        .where(Agent.tenant_id == tenant_id, Tool.type == "mcp")
    )
    tool_rows: dict[str, McpToolRow] = {}
    is_default: dict[str, bool] = {}
    states: dict[tuple[str, str], AgentToolState] = {}
    for r in result.all():
        tid = str(r.id)
        if tid not in tool_rows:
            tool_rows[tid] = McpToolRow(
                tool_id=tid,
                mcp_tool_name=r.mcp_tool_name or "",
                display_name=r.display_name or "",
                mcp_server_name=r.mcp_server_name or "MCP Server",
                mcp_server_url=r.mcp_server_url or "",
                is_default=bool(r.is_default),
            )
            is_default[tid] = bool(r.is_default)
        states[(str(r.agent_id), tid)] = AgentToolState(
            agent_id=str(r.agent_id), tool_id=tid, has_row=True, enabled=bool(r.enabled)
        )
    return tool_rows, is_default, states


async def backfill_tenant_mcp_servers(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Backfill one tenant's MCP servers/tools/assignments/overrides. Idempotent."""
    existing = await db.execute(select(MCPServer.id).where(MCPServer.tenant_id == tenant_id).limit(1))
    if existing.scalar_one_or_none() is not None:
        return {"tenant_id": str(tenant_id), "skipped": True, "reason": "already backfilled"}

    tool_rows, is_default, states = await _read_tenant_mcp(db, tenant_id)
    if not tool_rows:
        return {"tenant_id": str(tenant_id), "servers": 0, "tools": 0, "assignments": 0, "overrides": 0}

    servers = group_mcp_tools(list(tool_rows.values()))
    n_tools = n_assign = n_override = 0
    for server in servers:
        mcp_server = MCPServer(
            tenant_id=tenant_id,
            name=server.name,
            server_key=server.server_key,
            server_url=server.server_url or None,
            registry_source="migration",
            status="disabled",
            auth_status="none",
            config_json={},
        )
        db.add(mcp_server)
        await db.flush()  # assign mcp_server.id for the FK below

        server_tool_ids = set(server.tool_ids)
        for tool_id, tool_name in zip(server.tool_ids, server.tool_names):
            db.add(
                MCPServerTool(
                    tenant_id=tenant_id,
                    mcp_server_id=mcp_server.id,
                    tool_id=uuid.UUID(tool_id),
                    mcp_tool_name=tool_name,
                    display_name=tool_rows[tool_id].display_name,
                )
            )
            n_tools += 1

        is_default_map = {tid: is_default.get(tid, False) for tid in server.tool_ids}
        agent_ids = {agent_id for (agent_id, tid) in states if tid in server_tool_ids}
        for agent_id in sorted(agent_ids):
            agent_states = {tid: states[(agent_id, tid)] for tid in server.tool_ids if (agent_id, tid) in states}
            assignment = plan_agent_assignment(server, agent_states, is_default_map, agent_id)
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
            n_assign += 1
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
                n_override += 1

    await db.commit()
    return {
        "tenant_id": str(tenant_id),
        "servers": len(servers),
        "tools": n_tools,
        "assignments": n_assign,
        "overrides": n_override,
    }
