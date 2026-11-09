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
from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant, CustomApiWorkAccountBinding
from app.models.email_account import EmailAccount, EmailAccountGrant, EmailWorkAccountBinding
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config_service import _secret_field_keys, decrypt_tool_config_secrets, resolve_tool_config, update_tenant_tool_config

router = APIRouter(tags=["vault"])


def _fields(schema):
    keys = _secret_field_keys(schema)
    return [{"key": f["key"], "label": f.get("label") or ""} for f in (schema or {}).get("fields", [])
        if f.get("key") in keys]


def _row(kind, record, name, scope, fields, config, bots, *, field=None, version=None, removal=None, read_only=False):
    return {"kind": kind, "id": str(record.id), "name": name, "scope": scope, "fields": fields,
        "field": field, "version": version, "configured": bool(fields) and all(config.get(f["key"]) for f in fields),
        "bots": bots, "read_only": read_only, "removal": removal or {"fallback": "configuration"}}


def _company_read_only(tool, field, company, assignments):
    return tool.tenant_id is None or tool.type != "custom_api" or field in company and any(
        field in (a.config or {}) and decrypt_tool_config_secrets(a.config, tool.config_schema).get(field) == company[field]
        for a in assignments if a.tool_id == tool.id)


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
    fields = _fields(credential_schema(tool)) if kind == "api" and tool else [] if kind == "api" else [{"key": "auth_code"}, {"key": "email_address"}]
    grants = (await db.scalars(select(grant_model).where(grant_model.tenant_id == tenant,
        grant_model.account_id == account.id, grant_model.requester_user_id == user.id, grant_model.enabled.is_(True)))).all()
    company = await resolve_tool_config(tool.name, tenant, db=db) if tool else {}
    fallback = "company" if fields and all(company.get(f["key"]) for f in fields) else "configuration"
    binding = CustomApiWorkAccountBinding if kind == "api" else EmailWorkAccountBinding
    used = await db.scalar(select(binding.id).where(binding.tenant_id == tenant, binding.account_id == account.id).limit(1))
    return {"fallback": fallback, "review_required": used is not None}, grants


async def _company_review(db, tool, tenant, agent_id=None):
    if tool.type != "custom_api":
        return False
    from app.services.custom_api_accounts import company_config
    query = select(CustomApiWorkAccountBinding).where(CustomApiWorkAccountBinding.tenant_id == tenant,
        CustomApiWorkAccountBinding.tool_id == tool.id, CustomApiWorkAccountBinding.account_id.is_(None))
    if agent_id is not None:
        query = query.where(CustomApiWorkAccountBinding.agent_id == agent_id)
    for binding in (await db.scalars(query)).all():
        _, current = await company_config(db, tool=tool, agent_id=binding.agent_id)
        if binding.company_revision != current:
            return True
    return False


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
    for model, kind in ((CustomApiAccount, "api"), (EmailAccount, "email")):
        for account in (await db.scalars(select(model).where(model.tenant_id == tenant, model.owner_user_id == user.id))).all():
            tool = tools.get(account.tool_id) if kind == "api" else next((t for t in tools.values() if t.name == "send_email"), None)
            fields = _fields(credential_schema(tool)) if kind == "api" and tool else [] if kind == "api" else [{"key": "auth_code", "label": ""}]
            removal, grants = await _account_removal(db, account, kind, user, tenant, tools)
            bots = bot_names(list(dict.fromkeys(g.agent_id for g in grants)))
            rows.append(_row(kind, account, account.name, "personal", fields, account.config or {}, bots,
                version=account.version, removal=removal, read_only=not fields))
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
    overrides = {r.tool_id: r for r in (await db.scalars(select(TenantToolConfig).where(TenantToolConfig.tenant_id == tenant))).all()}
    company_configs = {}
    for assignment in assignments:
        if assignment.agent_id not in agents or agents[assignment.agent_id][1] != "manage":
            continue
        tool = tools.get(assignment.tool_id)
        if tool is None or tool.type == "mcp":
            continue  # MCP account credentials use the requester-owned account writer.
        if tool.id not in company_configs:
            override = overrides.get(tool.id)
            company_configs[tool.id] = decrypt_tool_config_secrets(
                {**(tool.config or {}), **((override.config or {}) if override else {})}, tool.config_schema)
        company = company_configs[tool.id]
        config = decrypt_tool_config_secrets(assignment.config, tool.config_schema)
        for field in _fields(tool.config_schema):
            if field["key"] not in config:
                continue
            if config[field["key"]] and field["key"] in company and config[field["key"]] == company[field["key"]]:
                continue
            rows.append(_row("bot", assignment, tool.display_name, "bot", [field], config,
                [{"id": str(assignment.agent_id), "name": agents[assignment.agent_id][0].name}], field=field["key"],
                removal={"fallback": "company" if company.get(field["key"]) else "configuration"}))
    if current_user.role in {"org_admin", "platform_admin"}:
        for server in (await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant, MCPServer.owner_user_id.is_(None)))).all():
            oauth = (server.config_json or {}).get("oauth") or {}
            if not oauth:
                continue
            rows.append(_row("company_mcp", server, server.name, "company", [{"key": "access_token", "label": ""}],
                {"access_token": _mcp_configured(server)}, [{"id": str(i), "name": a.name} for i, (a, _) in agents.items()
                    if mcp_assignments.get((i, server.id), True)], read_only=True))
        for tool in tools.values():
            if tool.type == "mcp":
                continue
            override = overrides.get(tool.id)
            config = {**((tool.config or {}) if tool.tenant_id == tenant else {}), **((override.config or {}) if override else {})} if override or tool.tenant_id == tenant else None
            if config is None:
                continue
            company = decrypt_tool_config_secrets(config, tool.config_schema)
            for field in _fields(tool.config_schema):
                bots = [{"id": str(a.agent_id), "name": agents[a.agent_id][0].name} for a in assignments
                    if a.tool_id == tool.id and a.agent_id in agents and a.enabled]
                rows.append(_row("company", tool, tool.display_name, "company", [field], config, bots, field=field["key"],
                    read_only=_company_read_only(tool, field["key"], company, assignments)))
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
    elif kind in {"api", "email"}:
        if kind == "api":
            from app.api.custom_api_connectors import _owned_api_account as owned, ApiAccountSaveIn as SaveIn, ApiAccountRevokeIn as RevokeIn, save_api_account as save, revoke_api_account as revoke
        else:
            from app.api.email_channel import _owned_mailbox as owned, MailboxSaveIn as SaveIn, MailboxRevokeIn as RevokeIn, save_email_account as save, revoke_email_account as revoke
        account = await owned(db, user, record_id)
        expected_version = version if removing else data.version
        if expected_version is None or expected_version < 1:
            raise HTTPException(409, "Read the current account before changing it")
        tool = await db.get(Tool, account.tool_id) if kind == "api" else await db.scalar(
            select(Tool).where(Tool.name == "send_email", or_(Tool.tenant_id == tenant, Tool.tenant_id.is_(None))).limit(1))
        removal, _ = await _account_removal(db, account, kind, user, tenant, {tool.id: tool} if tool else {})
        if removing:
            await revoke(record_id, RevokeIn(expected_version=expected_version), current_user=user, db=db)
        else:
            if kind == "api" and not data.connection_revision:
                raise HTTPException(409, "Read the current account before replacing it")
            extra = {"connection_revision": data.connection_revision} if kind == "api" else {}
            previous_version = account.version
            saved = await save(record_id, SaveIn(expected_version=expected_version, config=data.values, **extra), current_user=user, db=db)
            removal["review_required"] = removal["review_required"] and saved["version"] != previous_version
    elif kind in {"bot", "company"}:
        from app.api.tools import CategoryConfigIn, update_tool_config
        from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment, lock_agent_tool_config_write
        if kind == "bot":
            assignment = await db.scalar(select(AgentTool).where(AgentTool.id == record_id, AgentTool.tenant_id == tenant))
            if assignment is None:
                raise HTTPException(404, "Credential not found")
            agent = await lock_agent_tool_config_write(db, assignment.agent_id, current_user=user)
            await db.refresh(assignment, with_for_update=True)
            tool = await db.get(Tool, assignment.tool_id)
        else:
            await get_current_admin(user)
            tool = await db.scalar(select(Tool).where(Tool.id == record_id, or_(Tool.tenant_id == tenant, Tool.tenant_id.is_(None))))
            copies = (await db.scalars(select(AgentTool).where(AgentTool.tenant_id == tenant, AgentTool.tool_id == record_id))).all()
            override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tenant_id == tenant, TenantToolConfig.tool_id == record_id))
            company = decrypt_tool_config_secrets({**(tool.config or {}), **((override.config or {}) if override else {})}, tool.config_schema) if tool else {}
            if tool is not None and _company_read_only(tool, field, company, copies):
                raise HTTPException(403, "Manage this credential in company settings")
        if tool is None or tool.type == "mcp" or field not in _secret_field_keys(tool.config_schema):
            raise HTTPException(404, "Credential not found")
        if not removing and (set(data.values) != {field} or not data.values[field].strip()):
            raise HTTPException(422, "Enter the credential")
        values = {field: "" if removing else data.values[field]}
        if kind == "bot":
            if removing:
                await ensure_agent_tool_assignment(db, agent_id=assignment.agent_id, tool_id=tool.id,
                    config=lambda current: {key: value for key, value in current.items() if key != field},
                    locked_agent=agent, enabled=None, source=None, create=False)
                await db.commit()
            else:
                await update_tool_config(assignment.agent_id, tool.id,
                    CategoryConfigIn(config={**(assignment.config or {}), **values}), current_user=user, db=db)
            after = await resolve_tool_config(tool.name, tenant, agent_id=assignment.agent_id, db=db)
        else:
            config = dict((override.config or {}) if override else {})
            if removing:
                config.pop(field, None)
            else:
                config.update(values)
            # Removal passes only stored bytes; omit the schema so omission cannot restore the deleted key.
            await update_tenant_tool_config(db, tenant, tool.id, config=config, config_schema=None if removing else tool.config_schema)
            await db.commit()
            after = await resolve_tool_config(tool.name, tenant, db=db)
        removal = {"fallback": "company" if after.get(field) else "configuration",
            "review_required": await _company_review(db, tool, tenant, assignment.agent_id if kind == "bot" else None)}
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
    try:
        return await _change(kind, record_id, field, data, current_user, db)
    except ValidationError:
        raise HTTPException(422, "Enter the credential") from None


@router.delete("/vault/{kind}/{record_id}")
async def remove_vault(kind: str, record_id: uuid.UUID, field: str | None = Query(None), version: int | None = Query(None),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _change(kind, record_id, field, None, current_user, db, version=version)
