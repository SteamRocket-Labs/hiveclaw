"""Tenant-visible MCP registry helpers."""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment
from app.services.resource_discovery import import_mcp_direct, import_mcp_from_smithery


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "server"


def make_mcp_server_pack_name(server_name: str | None, server_url: str | None = None) -> str:
    if server_name:
        return f"mcp_server:{_slugify(server_name)}"
    if server_url:
        parsed = urlparse(server_url)
        host = parsed.netloc or parsed.path or server_url
        return f"mcp_server:{_slugify(host)}"
    return "mcp_server:unknown"


def build_mcp_server_registry(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        server_name = row.get("mcp_server_name") or "MCP Server"
        server_url = row.get("mcp_server_url") or ""
        key = (server_name, server_url)
        entry = grouped.setdefault(
            key,
            {
                "server_key": make_mcp_server_pack_name(server_name, server_url),
                "server_name": server_name,
                "server_url": server_url,
                "tool_count": 0,
                "agent_count": 0,
                "tools": set(),
                "agents": set(),
                "pack_name": make_mcp_server_pack_name(server_name, server_url),
            },
        )
        tool_name = row.get("tool_name")
        agent_name = row.get("agent_name")
        if tool_name:
            entry["tools"].add(tool_name)
        if agent_name:
            entry["agents"].add(agent_name)

    result = []
    for entry in grouped.values():
        tools = sorted(entry["tools"])
        agents = sorted(entry["agents"])
        result.append(
            {
                "server_key": entry["server_key"],
                "server_name": entry["server_name"],
                "server_url": entry["server_url"],
                "tool_count": len(tools),
                "agent_count": len(agents),
                "tools": tools,
                "agents": agents,
                "pack_name": entry["pack_name"],
            }
        )
    return sorted(result, key=lambda item: item["server_name"].lower())


async def list_tenant_mcp_servers(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    rows_query = (
        select(
            Tool.id.label("tool_id"),
            Tool.name.label("tool_name"),
            Tool.display_name.label("display_name"),
            Tool.mcp_server_name.label("mcp_server_name"),
            Tool.mcp_server_url.label("mcp_server_url"),
            Agent.id.label("agent_id"),
            Agent.name.label("agent_name"),
        )
        .join(AgentTool, AgentTool.tool_id == Tool.id)
        .join(Agent, Agent.id == AgentTool.agent_id)
        .where(Agent.tenant_id == tenant_id, Tool.type == "mcp")
    )
    result = await db.execute(rows_query)
    rows = [
        {
            "tool_id": str(row.tool_id),
            "tool_name": row.tool_name,
            "display_name": row.display_name,
            "mcp_server_name": row.mcp_server_name,
            "mcp_server_url": row.mcp_server_url,
            "agent_id": str(row.agent_id),
            "agent_name": row.agent_name,
        }
        for row in result.all()
    ]
    return build_mcp_server_registry(rows)


async def _assign_tools_to_tenant_agents(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_ids: list[uuid.UUID],
) -> None:
    result = await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id))
    agent_ids = [row[0] for row in result.all()]
    for tool_id in tool_ids:
        tool_result = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = tool_result.scalar_one_or_none()
        if tool:
            tool.tenant_id = tenant_id
        for agent_id in agent_ids:
            await ensure_agent_tool_assignment(
                db,
                agent_id=agent_id,
                tool_id=tool_id,
                enabled=True,
                source="system",
            )


async def import_tenant_mcp_server(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    server_id: str | None = None,
    mcp_url: str | None = None,
    server_name: str | None = None,
    config: dict | None = None,
) -> dict:
    agents_result = await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at.asc()))
    agent_ids = [row[0] for row in agents_result.all()]
    if not agent_ids:
        raise ValueError("This company needs at least one agent before importing MCP servers.")

    bootstrap_agent_id = agent_ids[0]
    if mcp_url:
        message = await import_mcp_direct(mcp_url, bootstrap_agent_id, server_name=server_name, api_key=(config or {}).get("api_key"))
        tool_query = select(Tool.id).where(Tool.type == "mcp", Tool.mcp_server_url == mcp_url)
    else:
        if not server_id:
            raise ValueError("server_id or mcp_url is required")
        message = await import_mcp_from_smithery(server_id, bootstrap_agent_id, config=config or None)
        clean_id = server_id.replace("/", "_").replace("@", "")
        tool_query = select(Tool.id).where(
            Tool.type == "mcp",
            Tool.name.like(f"mcp_{clean_id}%"),
        )

    tool_result = await db.execute(tool_query)
    tool_ids = [row[0] for row in tool_result.all()]
    await _assign_tools_to_tenant_agents(db, tenant_id, tool_ids)
    await db.commit()

    registry = await list_tenant_mcp_servers(db, tenant_id)
    if mcp_url:
        target = next((item for item in registry if item["server_url"] == mcp_url), None)
    else:
        prefix = f"mcp_{(server_id or '').replace('/', '_').replace('@', '')}"
        target = next((item for item in registry if any(tool.startswith(prefix) for tool in item["tools"])), None)
    return {
        "message": message,
        "server": target,
    }


async def delete_tenant_mcp_server(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    server_key: str,
) -> None:
    registry = await list_tenant_mcp_servers(db, tenant_id)
    target = next((item for item in registry if item["server_key"] == server_key), None)
    if not target:
        raise ValueError("MCP server not found")

    tenant_agents = select(Agent.id).where(Agent.tenant_id == tenant_id)
    tool_result = await db.execute(
        select(Tool.id).where(
            Tool.type == "mcp",
            Tool.mcp_server_name == target["server_name"],
            Tool.mcp_server_url == target["server_url"],
        )
    )
    tool_ids = [row[0] for row in tool_result.all()]
    if tool_ids:
        await db.execute(
            delete(AgentTool).where(
                AgentTool.agent_id.in_(tenant_agents),
                AgentTool.tool_id.in_(tool_ids),
            )
        )
        for tool_id in tool_ids:
            remaining = await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id))
            if not remaining.scalar_one_or_none():
                tool_row = await db.execute(select(Tool).where(Tool.id == tool_id))
                tool = tool_row.scalar_one_or_none()
                if tool:
                    await db.delete(tool)
    await db.commit()
