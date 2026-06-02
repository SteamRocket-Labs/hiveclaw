"""Pack Catalog and Agent Capability APIs."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.mcp_registry_service import delete_tenant_mcp_server, import_tenant_mcp_server, list_tenant_mcp_servers
from app.services.pack_service import (
    get_capability_summary,
    get_session_runtime_summary,
)

router = APIRouter(tags=["packs"])


class TenantMcpImportIn(BaseModel):
    server_id: str | None = None
    mcp_url: str | None = None
    server_name: str | None = None
    config: dict | None = None


@router.get("/agents/{agent_id}/capability-summary")
async def get_agent_capability_summary(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Comprehensive capability summary: kernel tools, packs, policies, pending approvals."""
    await check_agent_access(db, current_user, agent_id)
    return await get_capability_summary(db, agent_id)


@router.get("/chat/sessions/{session_id}/runtime-summary")
async def get_session_runtime(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Runtime summary for a chat session: activated packs, used tools, blocked capabilities."""
    # Resolve agent_id from session and verify access
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await check_agent_access(db, current_user, session.agent_id)
    return await get_session_runtime_summary(db, session_id)


@router.get("/enterprise/mcp-servers")
async def list_enterprise_mcp_servers(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List tenant-managed MCP servers and their exposed tools."""
    if not current_user.tenant_id:
        return []
    return await list_tenant_mcp_servers(db, current_user.tenant_id)


@router.post("/enterprise/mcp-servers/import")
async def import_enterprise_mcp_server(
    data: TenantMcpImportIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Import an MCP server for the whole tenant and assign it to tenant agents."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await import_tenant_mcp_server(
            db,
            current_user.tenant_id,
            server_id=data.server_id,
            mcp_url=data.mcp_url,
            server_name=data.server_name,
            config=data.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/enterprise/mcp-servers/{server_key}")
async def delete_enterprise_mcp_server(
    server_key: str,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a tenant-managed MCP server and unassign its tools."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        await delete_tenant_mcp_server(db, current_user.tenant_id, server_key=server_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted", "server_key": server_key}
