"""Server-first MCP extension APIs.

The replacement for the legacy pack-derived MCP surface. ``/agents/{id}/extensions``
is the single normal frontend source of agent extension state (skills + MCP
servers); the enterprise routes manage tenant MCP server records with stable
server identity. Built on the Part 1 MCP tables — no DTO here exposes a
``pack`` / ``pack_name`` field. See docs/agent-extension-surface-skill-mcp.md
§7.2–7.4, §8.2.

The legacy pack-derived extension routes in ``app/api/packs.py`` have been
retired; this router is the active extension management surface.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, reject_non_executing_agent_access
from app.core.security import get_current_admin, get_current_user
from app.api.mcp_oauth import McpOAuthStartIn
from app.database import get_db
from app.models.user import User
from app.services.external_capabilities.mcp_source_adapter import stage_external_mcp_server_review
from app.services.mcp_server_service import (
    delete_tenant_server,
    get_agent_extensions,
    get_agent_mcp_servers,
    list_agent_mcp_server_tools,
    list_tenant_mcp_server_tools,
    list_tenant_servers,
    review_mcp_server_tool_metadata,
    set_agent_mcp_assignment,
    set_agent_mcp_tool_policy,
    trigger_tenant_backfill,
)

router = APIRouter(tags=["mcp-servers"])


class PersonalMcpAccountIn(BaseModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


@router.get("/mcp-accounts")
async def list_my_mcp_accounts(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.models.mcp_server import MCPServer
    from app.services.mcp_accounts import account_public

    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    application = aliased(MCPServer)
    accounts = (await db.execute(select(MCPServer, application.name).outerjoin(application,
        (application.id == MCPServer.parent_server_id) & (application.tenant_id == tenant)).where(
        MCPServer.tenant_id == tenant, MCPServer.owner_user_id == current_user.id
    ).order_by(MCPServer.name, MCPServer.id))).all()
    return [{**account_public(account), "app_name": app_name or account.name}
        for account, app_name in accounts if account.auth_status != "none"]


@router.post("/agents/{agent_id}/mcp-servers/{server_id}/accounts")
async def create_mcp_account(agent_id: uuid.UUID, server_id: uuid.UUID, body: PersonalMcpAccountIn,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.mcp_accounts import create_personal_account
    agent, access = await check_agent_access(db, current_user, agent_id)
    reject_non_executing_agent_access(access)
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Account name required")
    return await create_personal_account(db, user=current_user, tenant_id=agent.tenant_id, server_id=server_id,
        account_id=body.id, name=body.name.strip())


@router.get("/agents/{agent_id}/mcp-servers/{server_id}/accounts")
async def get_mcp_accounts(agent_id: uuid.UUID, server_id: uuid.UUID,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from app.models.mcp_server import MCPServer
    from app.services.mcp_accounts import list_work_accounts
    agent, _ = await check_agent_access(db, current_user, agent_id)
    server = await db.scalar(select(MCPServer).where(MCPServer.id == server_id,
        MCPServer.tenant_id == agent.tenant_id, MCPServer.owner_user_id.is_(None)))
    if server is None:
        raise HTTPException(status_code=404, detail="Company connection definition not found")
    return await list_work_accounts(db, server=server, agent_id=agent_id, user=current_user)


@router.post("/mcp-accounts/{account_id}/revoke")
async def revoke_mcp_account(account_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.services.mcp_accounts import account_owner, revoke_account
    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    account = await account_owner(db, current_user, account_id, tenant_id=tenant)
    return await revoke_account(db, account=account)


@router.get("/mcp-accounts/{account_id}/oauth")
async def personal_mcp_oauth_status(account_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.services.mcp_accounts import account_owner
    from app.services.mcp_server_service import get_mcp_oauth_status
    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    await account_owner(db, current_user, account_id, tenant_id=tenant)
    status = await get_mcp_oauth_status(db, tenant, account_id)
    registration = status.pop("registration", {})
    status["authorization_method"] = "oauth" if all(registration.get(key) for key in (
        "authorization_endpoint", "token_endpoint", "client_id", "redirect_uri")) else "token"
    return status


class PersonalMcpTokenIn(BaseModel):
    access_token: str = Field(repr=False)


@router.put("/mcp-accounts/{account_id}/token")
async def save_personal_mcp_token(account_id: uuid.UUID, body: PersonalMcpTokenIn,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.models.audit import AuditLog
    from app.models.mcp_server import MCPServer
    from app.services.mcp_accounts import account_owner
    from app.services.mcp_oauth import OAuthTokenSet, encrypt_token_set
    from app.services.mcp_server_service import _store_oauth

    if not body.access_token.strip() or len(body.access_token) > 16384:
        raise HTTPException(status_code=400, detail="访问令牌不能为空，且不能超过 16384 个字符。")
    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    account = await account_owner(db, current_user, account_id, tenant_id=tenant)
    parent = await db.get(MCPServer, account.parent_server_id)
    if parent is None or parent.tenant_id != tenant:
        raise HTTPException(status_code=404, detail="Connection not found")
    account.server_url, account.transport = parent.server_url, parent.transport
    _store_oauth(account, {"token": encrypt_token_set(OAuthTokenSet(body.access_token)),
        "authorization_id": str(uuid.uuid4())})
    account.auth_status = "configured"
    account.status = "connected"
    db.add(AuditLog(tenant_id=tenant, user_id=current_user.id, action="mcp.account.token_saved",
        details={"account_id": str(account.id), "server_id": str(parent.id)}))
    await db.commit()
    return {"server_id": str(account.id), "auth_status": "configured"}


@router.post("/mcp-accounts/{account_id}/oauth/start")
async def start_personal_mcp_oauth(account_id: uuid.UUID, body: McpOAuthStartIn | None = None,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.services.mcp_accounts import account_owner
    from app.services.mcp_oauth import OAuthError, OAuthFlowError
    from app.services.mcp_server_service import start_mcp_oauth
    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    account = await account_owner(db, current_user, account_id, tenant_id=tenant)
    try:
        if body is None:
            from app.models.mcp_server import MCPServer
            from app.services.mcp_accounts import REGISTRATION_FIELDS
            parent = await db.get(MCPServer, account.parent_server_id) if account.parent_server_id else None
            registration = ((parent.config_json or {}).get("oauth") or {}) if parent else {}
            registration = registration.get("pending") or registration
            values = {key: registration.get(key) for key in REGISTRATION_FIELDS}
            if not all(values[key] for key in REGISTRATION_FIELDS[:4]):
                raise OAuthFlowError("mcp_account_registration_changed", "unavailable", 409)
        else:
            values = body.model_dump()
        return await start_mcp_oauth(db, tenant, account_id, **values, requester_id=current_user.id)
    except OAuthFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=503, detail={"code": "oauth_encryption_unavailable"}) from exc


class AgentMcpAssignmentIn(BaseModel):
    enabled: bool
    default_tool_mode: Literal["auto", "approval", "deny"] = "auto"
    always_load: bool = False


class AgentMcpToolPolicyIn(BaseModel):
    mode: Literal["auto", "approval", "deny"]


class TenantMcpImportIn(BaseModel):
    server_id: str | None = None
    mcp_url: str | None = None
    server_name: str | None = None
    config: dict | None = None


class TenantMcpMetadataReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    canonical_description: str | None = Field(default=None, max_length=500)


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
        return await stage_external_mcp_server_review(
            db,
            current_user.tenant_id,
            created_by_user_id=current_user.id,
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


@router.get("/enterprise/mcp-servers/{server_id}/tools")
async def list_enterprise_mcp_server_metadata(
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Administrator-only raw evidence and canonical MCP metadata review state."""

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await list_tenant_mcp_server_tools(db, current_user.tenant_id, server_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/enterprise/mcp-servers/{server_id}/tools/{tool_name}/metadata-review")
async def review_enterprise_mcp_server_metadata(
    server_id: uuid.UUID,
    tool_name: str,
    data: TenantMcpMetadataReviewIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject one immutable MCP metadata fingerprint."""

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await review_mcp_server_tool_metadata(
            db,
            current_user.tenant_id,
            server_id,
            tool_name,
            reviewer_id=current_user.id,
            decision=data.decision,
            expected_fingerprint=data.expected_fingerprint,
            canonical_description=data.canonical_description,
        )
    except ValueError as exc:
        detail = str(exc)
        if "fingerprint changed" in detail:
            raise HTTPException(status_code=409, detail=detail)
        if "not found" in detail:
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=400, detail=detail)


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
    from app.services.agent_tool_assignment_service import lock_agent_tool_config_write

    agent = await lock_agent_tool_config_write(db, agent_id, current_user=current_user)
    try:
        return await set_agent_mcp_assignment(
            db,
            agent.tenant_id,
            agent_id,
            server_id,
            enabled=data.enabled,
            default_tool_mode=data.default_tool_mode,
            always_load=data.always_load,
            current_user=current_user,
            locked_agent=agent,
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
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access required")
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
