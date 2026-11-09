"""Nonsecret settings projection over existing credential stores and writers."""
import uuid
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.agent import Agent
from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant
from app.models.email_account import EmailAccount, EmailAccountGrant
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config_service import decrypt_tool_config_secrets, resolve_tool_config, update_tenant_tool_config

router = APIRouter(tags=["vault"])


def _fields(schema):
    return [{"key": f["key"], "label": f.get("label") or ""} for f in (schema or {}).get("fields", [])
        if f.get("key") and (f.get("type") == "password" or f.get("format") == "password" or f.get("writeOnly"))]


def _row(kind, record, name, scope, fields, config, bots, *, field=None, version=None, removal=None):
    return {"kind": kind, "id": str(record.id), "name": name, "scope": scope, "fields": fields,
        "field": field, "version": version, "configured": bool(fields) and all(config.get(f["key"]) for f in fields),
        "bots": bots, "removal": removal or {"fallback": "configuration"}}


def _mcp_configured(server):
    from app.services.mcp_oauth import decrypt_token_set
    oauth = (server.config_json or {}).get("oauth") or {}
    token = decrypt_token_set(oauth.get("token"))
    return bool(token and (not token.is_expired(time.time()) or
        token.refresh_token and oauth.get("token_endpoint") and oauth.get("client_id")))


async def _agents(db, user, tenant):
    result = {}
    for agent in (await db.scalars(select(Agent).where(Agent.tenant_id == tenant,
        Agent.deleted_at.is_(None), Agent.deactivated_at.is_(None)))).all():
        try:
            _, access = await check_agent_access(db, user, agent.id)
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 410}:
                raise
            continue
        if access in {"use", "manage"}:
            result[agent.id] = (agent, access)
    return result


async def _mcp_removal(db, account, tenant):
    others = (await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant,
        MCPServer.owner_user_id == account.owner_user_id, MCPServer.parent_server_id == account.parent_server_id,
        MCPServer.id != account.id, MCPServer.auth_status == "configured"))).all()
    if any(_mcp_configured(other) for other in others):
        return {"fallback": "personal"}
    parent = await db.get(MCPServer, account.parent_server_id) if account.parent_server_id else None
    if parent and _mcp_configured(parent):
        return {"fallback": "company"}
    if parent and not (parent.config_json or {}).get("oauth") and parent.auth_status == "none":
        from app.services.mcp_authz import MCPAuthzError, resolve_mcp_api_key
        for tool in (await db.scalars(select(Tool).where(Tool.tenant_id == tenant, Tool.type == "mcp",
            Tool.mcp_server_url == parent.server_url))).all():
            try:
                if await resolve_mcp_api_key(db, tool=tool, tenant_id=tenant, agent_config={}):
                    return {"fallback": "company"}
            except MCPAuthzError:
                continue
    return {"fallback": "configuration"}


async def _account_removal(db, account, kind, user, tenant, tools):
    from app.services.custom_api_accounts import credential_schema
    grant_model = CustomApiAccountGrant if kind == "api" else EmailAccountGrant
    tool = tools.get(account.tool_id) if kind == "api" else next((t for t in tools.values() if t.name == "send_email"), None)
    fields = _fields(credential_schema(tool)) if kind == "api" and tool else [{"key": "auth_code"}]
    grants = (await db.scalars(select(grant_model).where(grant_model.tenant_id == tenant,
        grant_model.account_id == account.id, grant_model.requester_user_id == user.id, grant_model.enabled.is_(True)))).all()
    outcomes = set()
    if tool:
        for grant in grants:
            config = await resolve_tool_config(tool.name, tenant, agent_id=grant.agent_id, db=db)
            company = await resolve_tool_config(tool.name, tenant, db=db)
            configured = bool(fields) and all(config.get(f["key"]) for f in fields)
            outcomes.add("company" if configured and all(config.get(f["key"]) == company.get(f["key"]) for f in fields)
                else "existing" if configured else "configuration")
    fallback = next(iter(outcomes)) if len(outcomes) == 1 else "mixed" if outcomes else "configuration"
    return {"fallback": fallback, "review_required": True}, grants


async def _personal_rows(db, user, tenant, agents, tools, assignments):
    rows = []
    def bot_names(ids):
        return [{"id": str(i), "name": agents[i][0].name} for i in ids if i in agents]
    for account in (await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant,
        MCPServer.owner_user_id == user.id))).all():
        parent = await db.get(MCPServer, account.parent_server_id) if account.parent_server_id else None
        bots = bot_names([i for i in agents if parent and assignments.get((i, parent.id), True)])
        rows.append(_row("mcp", account, account.name, "personal", [{"key": "access_token", "label": ""}],
            {"access_token": _mcp_configured(account)}, bots, removal=await _mcp_removal(db, account, tenant)))
    from app.services.custom_api_accounts import credential_schema
    for model, grant_model, kind in ((CustomApiAccount, CustomApiAccountGrant, "api"), (EmailAccount, EmailAccountGrant, "email")):
        for account in (await db.scalars(select(model).where(model.tenant_id == tenant, model.owner_user_id == user.id))).all():
            tool = tools.get(account.tool_id) if kind == "api" else next((t for t in tools.values() if t.name == "send_email"), None)
            fields = _fields(credential_schema(tool)) if kind == "api" and tool else [{"key": "auth_code", "label": ""}]
            removal, grants = await _account_removal(db, account, kind, user, tenant, tools)
            bots = bot_names(list(dict.fromkeys(g.agent_id for g in grants)))
            rows.append(_row(kind, account, account.name, "personal", fields, account.config or {}, bots,
                version=account.version, removal=removal))
            if kind == "api" and tool:
                from app.services.custom_api_accounts import connector_revision, configured
                rows[-1].update(connection_revision=connector_revision(tool), configured=configured(account, tool))
    return rows


@router.get("/vault")
async def list_vault(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant = await resolve_and_pin_tenant_scope(db, current_user)
    agents = await _agents(db, current_user, tenant)
    tools = {t.id: t for t in (await db.scalars(select(Tool).where(or_(Tool.tenant_id == tenant, Tool.tenant_id.is_(None))))).all()}
    mcp_assignments = {(a.agent_id, a.mcp_server_id): a.enabled for a in
        (await db.scalars(select(AgentMCPServerAssignment).where(AgentMCPServerAssignment.tenant_id == tenant))).all()}
    rows = await _personal_rows(db, current_user, tenant, agents, tools, mcp_assignments)
    assignments = (await db.scalars(select(AgentTool).where(AgentTool.tenant_id == tenant))).all()
    for assignment in assignments:
        if assignment.agent_id not in agents or agents[assignment.agent_id][1] != "manage":
            continue
        tool = tools.get(assignment.tool_id)
        if tool is None or tool.type == "mcp":
            continue  # MCP account credentials use the requester-owned account writer.
        config = assignment.config or {}
        for field in _fields(tool.config_schema):
            after = await resolve_tool_config(tool.name, tenant, agent_id=assignment.agent_id, db=db,
                cleared_secret=("bot", field["key"]))
            rows.append(_row("bot", assignment, tool.display_name, "bot", [field], config,
                [{"id": str(assignment.agent_id), "name": agents[assignment.agent_id][0].name}], field=field["key"],
                removal={"fallback": "existing" if after.get(field["key"]) else "configuration"}))
    if current_user.role in {"org_admin", "platform_admin"}:
        from app.services.mcp_oauth import decrypt_value
        for server in (await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant, MCPServer.owner_user_id.is_(None)))).all():
            oauth = (server.config_json or {}).get("oauth") or {}
            if not oauth:
                continue
            fields = ["access_token"] + (["client_secret"] if oauth.get("client_secret") else [])
            for key in fields:
                rows.append(_row("company_mcp", server, server.name, "company", [{"key": key, "label": ""}],
                    {key: _mcp_configured(server) if key == "access_token" else bool(decrypt_value(oauth.get(key)))},
                    [{"id": str(i), "name": a.name} for i, (a, _) in agents.items()
                        if mcp_assignments.get((i, server.id), True)], field=key,
                    removal={"fallback": "authorization" if key == "client_secret" else "configuration"}))
        overrides = {r.tool_id: r for r in (await db.scalars(select(TenantToolConfig).where(TenantToolConfig.tenant_id == tenant))).all()}
        for tool in tools.values():
            override = overrides.get(tool.id)
            config = {**((tool.config or {}) if tool.tenant_id == tenant else {}), **((override.config or {}) if override else {})} if override or tool.tenant_id == tenant else None
            if config is None:
                continue
            for field in _fields(tool.config_schema):
                after = await resolve_tool_config(tool.name, tenant, db=db, cleared_secret=("company", field["key"]))
                bots = [{"id": str(a.agent_id), "name": agents[a.agent_id][0].name} for a in assignments
                    if a.tool_id == tool.id and a.agent_id in agents and a.enabled]
                rows.append(_row("company", tool, tool.display_name, "company", [field], config, bots, field=field["key"],
                    removal={"fallback": "existing" if after.get(field["key"]) else "configuration"}))
    return sorted(rows, key=lambda row: (row["scope"], row["name"], row["id"], row["field"] or ""))


class VaultReplaceIn(BaseModel):
    values: dict[str, str] = Field(repr=False)
    version: int | None = None
    connection_revision: str | None = None


async def _change(kind, record_id, field, data, user, db, *, version=None):
    tenant = await resolve_and_pin_tenant_scope(db, user)
    removing = data is None
    if kind == "mcp":
        from app.api.mcp_servers import PersonalMcpTokenIn, revoke_mcp_account, save_personal_mcp_token
        from app.services.mcp_accounts import account_owner
        account = await account_owner(db, user, record_id, tenant_id=tenant)
        removal = await _mcp_removal(db, account, tenant)
        if removing:
            await revoke_mcp_account(record_id, current_user=user, db=db)
        else:
            if set(data.values) != {"access_token"}:
                raise HTTPException(422, "Enter the account credential")
            await save_personal_mcp_token(record_id, PersonalMcpTokenIn(access_token=data.values["access_token"]), current_user=user, db=db)
    elif kind == "company_mcp":
        await get_current_admin(user)
        from app.services.mcp_server_service import _locked_oauth_server, _store_oauth
        from app.services.mcp_accounts import revoke_account
        from app.services.mcp_oauth import OAuthTokenSet, encrypt_token_set, encrypt_value
        server = await _locked_oauth_server(db, tenant, record_id)
        if server.owner_user_id is not None or field not in {"access_token", "client_secret"}:
            raise HTTPException(404, "Company credential not found")
        if not removing and (set(data.values) != {field} or not data.values[field].strip() or len(data.values[field]) > 16384):
            raise HTTPException(422, "Enter the credential")
        if removing and field == "access_token":
            await revoke_account(db, account=server)
        else:
            oauth = dict((server.config_json or {}).get("oauth") or {})
            if field == "access_token":
                oauth.update(token=encrypt_token_set(OAuthTokenSet(data.values[field])), authorization_id=str(uuid.uuid4()), pending=None, refresh=None)
                server.auth_status, server.status = "configured", "connected"
            elif removing:
                oauth.pop("client_secret", None)
            else:
                oauth["client_secret"] = encrypt_value(data.values[field])
            _store_oauth(server, oauth)
            await db.commit()
        removal = {"fallback": "authorization" if field == "client_secret" else "configuration"}
    elif kind in {"api", "email"}:
        if kind == "api":
            from app.api.custom_api_connectors import _owned_api_account, ApiAccountSaveIn, ApiAccountRevokeIn, save_api_account, revoke_api_account
            from app.services.custom_api_accounts import load_connector
            account = await _owned_api_account(db, user, record_id)
            tool = await load_connector(db, tenant_id=tenant, tool_id=account.tool_id)
            removal, _ = await _account_removal(db, account, kind, user, tenant, {tool.id: tool})
            if removing:
                await revoke_api_account(record_id, ApiAccountRevokeIn(expected_version=version or account.version), current_user=user, db=db)
            else:
                if data.version is None or not data.connection_revision:
                    raise HTTPException(409, "Read the current account before replacing it")
                await save_api_account(record_id, ApiAccountSaveIn(expected_version=data.version or account.version,
                    connection_revision=data.connection_revision, config=data.values), current_user=user, db=db)
        else:
            from app.api.email_channel import _owned_mailbox, MailboxSaveIn, MailboxRevokeIn, save_email_account, revoke_email_account
            account = await _owned_mailbox(db, user, record_id)
            mail_tool = await db.scalar(select(Tool).where(Tool.name == "send_email", or_(Tool.tenant_id == tenant, Tool.tenant_id.is_(None))).limit(1))
            removal, _ = await _account_removal(db, account, kind, user, tenant, {mail_tool.id: mail_tool} if mail_tool else {})
            if removing:
                await revoke_email_account(record_id, MailboxRevokeIn(expected_version=version or account.version), current_user=user, db=db)
            else:
                if data.version is None:
                    raise HTTPException(409, "Read the current account before replacing it")
                await save_email_account(record_id, MailboxSaveIn(expected_version=data.version or account.version,
                    config=data.values), current_user=user, db=db)
    elif kind in {"bot", "company"}:
        from app.api.tools import CategoryConfigIn, ToolUpdateIn, update_global_tool, update_tool_config
        from app.services.agent_tool_assignment_service import lock_agent_tool_config_write
        if kind == "bot":
            assignment = await db.scalar(select(AgentTool).where(AgentTool.id == record_id, AgentTool.tenant_id == tenant))
            if assignment is None:
                raise HTTPException(404, "Credential not found")
            await lock_agent_tool_config_write(db, assignment.agent_id, current_user=user)
            tool = await db.get(Tool, assignment.tool_id)
        else:
            await get_current_admin(user)
            tool = await db.scalar(select(Tool).where(Tool.id == record_id, or_(Tool.tenant_id == tenant, Tool.tenant_id.is_(None))))
        if tool is None or field not in {f["key"] for f in _fields(tool.config_schema)}:
            raise HTTPException(404, "Credential not found")
        if not removing and (set(data.values) != {field} or not data.values[field].strip()):
            raise HTTPException(422, "Enter the credential")
        values = {field: "" if removing else data.values[field]}
        if kind == "bot":
            await update_tool_config(assignment.agent_id, tool.id, CategoryConfigIn(config=values), current_user=user, db=db)
            after = await resolve_tool_config(tool.name, tenant, agent_id=assignment.agent_id, db=db)
        else:
            override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tenant_id == tenant, TenantToolConfig.tool_id == tool.id))
            if tool.tenant_id is None or override is not None and field in (override.config or {}):
                await update_tenant_tool_config(db, tenant, tool.id, config={**((override.config or {}) if override else {}), **values}, config_schema=tool.config_schema)
                await db.commit()
            else:
                await update_global_tool(tool.id, ToolUpdateIn(config={**(tool.config or {}), **values}), current_user=user, db=db)
            after = await resolve_tool_config(tool.name, tenant, db=db)
        removal = {"fallback": "existing" if after.get(field) else "configuration"}
    else:
        raise HTTPException(404, "Credential not found")
    return {"removed": removing, "removal": removal}


@router.put("/vault/{kind}/{record_id}")
async def replace_vault(kind: str, record_id: uuid.UUID, request: Request, field: str | None = Query(None),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        data = VaultReplaceIn.model_validate(await request.json())
    except (ValidationError, ValueError):
        # Validation responses must not echo credential bytes from the body.
        raise HTTPException(422, "Enter the credential") from None
    return await _change(kind, record_id, field, data, current_user, db)


@router.delete("/vault/{kind}/{record_id}")
async def remove_vault(kind: str, record_id: uuid.UUID, field: str | None = Query(None), version: int | None = Query(None, ge=1),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _change(kind, record_id, field, None, current_user, db, version=version)
