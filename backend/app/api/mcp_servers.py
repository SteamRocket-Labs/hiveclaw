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

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.user import User
from app.services.mcp_server_service import (
    get_agent_extensions,
    get_agent_mcp_servers,
    list_tenant_servers,
    set_agent_mcp_assignment,
    trigger_tenant_backfill,
)

router = APIRouter(tags=["mcp-servers"])


class AgentMcpAssignmentIn(BaseModel):
    enabled: bool
    default_tool_mode: str = "auto"


@router.get("/agents/{agent_id}/extensions")
async def get_extensions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Single source of truth for an agent's extensions: skills + MCP servers."""
    await check_agent_access(db, current_user, agent_id)
    return await get_agent_extensions(db, agent_id)


@router.get("/enterprise/mcp-servers/records")
async def list_enterprise_mcp_server_records(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List tenant MCP server records, server-first with stable identity."""
    if not current_user.tenant_id:
        return []
    return await list_tenant_servers(db, current_user.tenant_id)


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
    return await set_agent_mcp_assignment(
        db,
        agent.tenant_id,
        agent_id,
        server_id,
        enabled=data.enabled,
        default_tool_mode=data.default_tool_mode,
    )


@router.post("/enterprise/mcp-servers/backfill")
async def backfill_enterprise_mcp_servers(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger the Part 2 MCP backfill for the current tenant. Idempotent."""
    if not current_user.tenant_id:
        return {"skipped": True, "reason": "no tenant assigned"}
    return await trigger_tenant_backfill(db, current_user.tenant_id)
