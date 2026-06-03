"""Server-first MCP extension APIs.

The replacement for the legacy pack-derived MCP surface. ``/agents/{id}/extensions``
is the single normal frontend source of agent extension state (skills + MCP
servers); the enterprise routes manage tenant MCP server records with stable
server identity. Built on the Part 1 MCP tables — no DTO here exposes a
``pack`` / ``pack_name`` field. See docs/agent-extension-surface-skill-mcp.md
§7.2–7.4, §8.2.

This is purely additive: the legacy pack routes in ``app/api/packs.py`` are
removed in Part 6 once the frontend switches to these endpoints.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.user import User
from app.services.mcp_server_service import (
    delete_tenant_server,
    get_agent_extensions,
    get_agent_mcp_servers,
    import_and_register,
    list_agent_mcp_server_tools,
    list_tenant_servers,
    set_agent_mcp_assignment,
    set_agent_mcp_tool_policy,
    trigger_tenant_backfill,
)

router = APIRouter(tags=["mcp-servers"])


class AgentMcpAssignmentIn(BaseModel):
    enabled: bool
    default_tool_mode: Literal["auto", "approval", "deny"] = "auto"


class AgentMcpToolPolicyIn(BaseModel):
    mode: Literal["auto", "approval", "deny"]


class TenantMcpImportIn(BaseModel):
    server_id: str | None = None
    mcp_url: str | None = None
    server_name: str | None = None
    config: dict | None = None


@router.get("/agents/{agent_id}/extensions")
async def get_extensions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Single source of truth for an agent's extensions: skills + MCP servers."""
    await check_agent_access(db, current_user, agent_id)
    return await get_agent_extensions(db, agent_id)


@router.get("/enterprise/mcp-servers")
async def list_enterprise_mcp_servers(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List tenant MCP server records, server-first with stable identity."""
    if not current_user.tenant_id:
        return []
    return await list_tenant_servers(db, current_user.tenant_id)


@router.post("/enterprise/mcp-servers/import")
async def import_enterprise_mcp_server(
    data: TenantMcpImportIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Import an MCP server for the tenant, then register one server-first record."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await import_and_register(
            db,
            current_user.tenant_id,
            server_id=data.server_id,
            mcp_url=data.mcp_url,
            server_name=data.server_name,
            config=data.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/enterprise/mcp-servers/{server_id}")
async def delete_enterprise_mcp_server(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete one tenant MCP server record (by stable id) and its tools/assignments."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await delete_tenant_server(db, current_user.tenant_id, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/agents/{agent_id}/mcp-servers")
async def list_agent_mcp_servers(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the MCP servers assigned to a specific agent."""
    await check_agent_access(db, current_user, agent_id)
    return await get_agent_mcp_servers(db, agent_id)


@router.put("/agents/{agent_id}/mcp-servers/{server_id}")
async def update_agent_mcp_server(
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    data: AgentMcpAssignmentIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Assign or update one agent's connection to an MCP server."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    try:
        return await set_agent_mcp_assignment(
            db,
            agent.tenant_id,
            agent_id,
            server_id,
            enabled=data.enabled,
            default_tool_mode=data.default_tool_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/agents/{agent_id}/mcp-servers/{server_id}/tools")
async def list_agent_mcp_server_tool_policies(
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List advanced per-tool policy modes for one assigned MCP server."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    try:
        return await list_agent_mcp_server_tools(db, agent.tenant_id, agent_id, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/agents/{agent_id}/mcp-servers/{server_id}/tools/{tool_name}/policy")
async def update_agent_mcp_server_tool_policy(
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    tool_name: str,
    data: AgentMcpToolPolicyIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set an advanced per-tool policy override for one assigned MCP server."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    try:
        return await set_agent_mcp_tool_policy(
            db,
            agent.tenant_id,
            agent_id,
            server_id,
            tool_name,
            mode=data.mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/enterprise/mcp-servers/backfill")
async def backfill_enterprise_mcp_servers(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger the Part 2 MCP backfill for the current tenant. Idempotent."""
    if not current_user.tenant_id:
        return {"skipped": True, "reason": "no tenant assigned"}
    return await trigger_tenant_backfill(db, current_user.tenant_id)
