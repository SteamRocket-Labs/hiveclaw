"""Owner-issued credential authority, independent of employee tool authority."""

import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.models.mcp_server import MCPAccountGrant, MCPServer, MCPWorkAccountBinding
from app.models.user import User

REGISTRATION_FIELDS = ("authorization_endpoint", "token_endpoint", "client_id", "redirect_uri", "scope")


def account_public(account):
    return {
        "id": str(account.id),
        "name": account.name,
        "owner_user_id": str(account.owner_user_id) if account.owner_user_id else None,
        "parent_server_id": str(account.parent_server_id) if account.parent_server_id else None,
        "auth_status": account.auth_status,
    }


def _active_in_tenant(user, tenant_id):
    return bool(user and user.is_active and (user.tenant_id == tenant_id or user.role == "platform_admin"))


async def account_owner(db, user, account_id, *, tenant_id):
    account = await db.scalar(
        select(MCPServer)
        .where(MCPServer.id == account_id, MCPServer.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise HTTPException(status_code=404, detail="MCP account not found")
    current_user = await db.get(User, user.id, populate_existing=True)
    if account.owner_user_id != user.id or not _active_in_tenant(current_user, tenant_id):
        raise HTTPException(
            status_code=403, detail="Account owner required; employee management is not credential authority"
        )
    return account


async def create_personal_account(db, *, user, tenant_id, server_id, account_id, name):
    parent = await db.scalar(
        select(MCPServer).where(
            MCPServer.id == server_id, MCPServer.tenant_id == tenant_id, MCPServer.owner_user_id.is_(None)
        )
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="Company connection definition not found")
    if not _active_in_tenant(user, tenant_id):
        raise HTTPException(status_code=403, detail="Current company identity required")
    existing = await db.get(MCPServer, account_id)
    if existing is not None:
        if (existing.owner_user_id, existing.parent_server_id, existing.name) != (user.id, server_id, name):
            raise HTTPException(status_code=409, detail="Account creation identity has different inputs")
        return account_public(existing)
    oauth = (parent.config_json or {}).get("oauth") or {}
    registration = oauth.get("pending") or oauth
    # Reuse the approved application registration, never its tokens or login attempt.
    inherited = {key: registration[key] for key in REGISTRATION_FIELDS if key in registration}
    account = MCPServer(
        id=account_id,
        tenant_id=tenant_id,
        owner_user_id=user.id,
        parent_server_id=parent.id,
        name=name,
        server_key=f"account-{uuid.uuid4().hex}",
        server_url=parent.server_url,
        transport=parent.transport,
        registry_source="personal",
        status="needs_auth",
        config_json={"oauth": inherited} if inherited else {},
    )
    try:
        async with db.begin_nested():
            db.add(account)
            await db.flush()
    except IntegrityError as exc:
        existing = await db.get(MCPServer, account_id)
        if existing is None or (existing.owner_user_id, existing.parent_server_id, existing.name) != (
            user.id,
            server_id,
            name,
        ):
            raise HTTPException(
                status_code=409, detail="Account creation identity conflicts; read back before retrying"
            ) from exc
        return account_public(existing)
    await db.commit()
    return account_public(account)


async def set_account_grant(db, *, account, agent_id, requester_id, enabled, expected_version):
    requester = await db.get(User, requester_id, populate_existing=True)
    if not _active_in_tenant(requester, account.tenant_id):
        raise HTTPException(status_code=404, detail="Current company requester not found")
    grant = await db.scalar(
        select(MCPAccountGrant)
        .where(
            MCPAccountGrant.account_id == account.id,
            MCPAccountGrant.agent_id == agent_id,
            MCPAccountGrant.requester_user_id == requester_id,
        )
        .execution_options(populate_existing=True)
    )
    if (
        grant is None
        and expected_version != 0
        or grant is not None
        and not (
            expected_version == grant.version or expected_version + 1 == grant.version and grant.enabled == enabled
        )
    ):
        raise HTTPException(
            status_code=409, detail="Account use grant changed; read its current version before deciding again"
        )
    if grant is None:
        grant = MCPAccountGrant(
            tenant_id=account.tenant_id,
            account_id=account.id,
            agent_id=agent_id,
            requester_user_id=requester_id,
            granted_by_user_id=account.owner_user_id,
            enabled=enabled,
        )
        db.add(grant)
    elif grant.enabled != enabled:
        grant.enabled = enabled
        grant.version += 1
    await db.commit()
    return {
        "id": str(grant.id),
        "enabled": grant.enabled,
        "version": grant.version,
        "account_id": str(account.id),
        "agent_id": str(agent_id),
        "requester_user_id": str(requester_id),
    }


async def list_work_accounts(db, *, server, agent_id, user):
    granted_ids = select(MCPAccountGrant.account_id).where(
        MCPAccountGrant.agent_id == agent_id,
        MCPAccountGrant.requester_user_id == user.id,
        MCPAccountGrant.enabled.is_(True),
    )
    accounts = (
        await db.scalars(
            select(MCPServer)
            .where(
                MCPServer.tenant_id == server.tenant_id,
                MCPServer.parent_server_id == server.id,
                or_(MCPServer.owner_user_id == user.id, MCPServer.id.in_(granted_ids)),
            )
            .order_by(MCPServer.name, MCPServer.id)
        )
    ).all()
    result = [
        {
            **account_public(server),
            "account_use_enabled": True,
            "can_grant_account": False,
            "can_authorize": user.role in {"platform_admin", "org_admin"},
        }
    ]
    for account in accounts:
        try:
            _, grant = await account_access(db, server=server, account_id=account.id, agent_id=agent_id, user=user)
        except HTTPException:
            continue
        result.append(
            {
                **account_public(account),
                "account_use_enabled": bool(grant and grant.enabled),
                "grant_version": grant.version if grant else 0,
                "can_grant_account": account.owner_user_id == user.id,
                "can_authorize": account.owner_user_id == user.id,
            }
        )
    return result


async def account_access(db, *, server, account_id, agent_id, user):
    account = await db.scalar(
        select(MCPServer)
        .where(MCPServer.tenant_id == server.tenant_id, MCPServer.id == account_id)
        .execution_options(populate_existing=True)
    )
    if account is None or (
        account.id != server.id and (not account.owner_user_id or account.parent_server_id != server.id)
    ):
        raise HTTPException(status_code=404, detail="MCP account does not belong to this connection")
    if not account.owner_user_id:
        return account, None
    owner = await db.get(User, account.owner_user_id, populate_existing=True)
    grant = await db.scalar(
        select(MCPAccountGrant)
        .where(
            MCPAccountGrant.account_id == account.id,
            MCPAccountGrant.agent_id == agent_id,
            MCPAccountGrant.requester_user_id == user.id,
        )
        .execution_options(populate_existing=True)
    )
    if not _active_in_tenant(owner, server.tenant_id) or not _active_in_tenant(user, server.tenant_id):
        raise HTTPException(
            status_code=403, detail="MCP account owner or requester is no longer active in this company"
        )
    if account.owner_user_id != user.id and not (grant and grant.enabled):
        raise HTTPException(status_code=403, detail="Account owner has not granted this requester and employee use")
    return account, grant


async def bind_work_account(db, *, server, account, grant, agent_id, session_id, user):
    if account.owner_user_id and not (grant and grant.enabled):
        raise HTTPException(status_code=409, detail="Account-use grant required")
    binding = await db.scalar(
        select(MCPWorkAccountBinding)
        .execution_options(populate_existing=True)
        .where(
            MCPWorkAccountBinding.session_id == session_id,
            MCPWorkAccountBinding.agent_id == agent_id,
            MCPWorkAccountBinding.server_id == server.id,
            MCPWorkAccountBinding.requester_user_id == user.id,
        )
    )
    if binding is None:
        binding = MCPWorkAccountBinding(
            tenant_id=server.tenant_id,
            session_id=session_id,
            agent_id=agent_id,
            server_id=server.id,
            requester_user_id=user.id,
        )
        db.add(binding)
    binding.account_id = account.id
    binding.grant_id = grant.id if grant else None
    binding.grant_version = grant.version if grant else None
    binding.authorization_id = ((account.config_json or {}).get("oauth") or {}).get("authorization_id")
    await db.flush()
    return binding


async def work_binding_matches(db, *, ready, tenant_id, agent_id, session_id, user_id, account_id):
    if not ready.get("binding_id"):
        return False
    binding = await db.get(MCPWorkAccountBinding, uuid.UUID(ready["binding_id"]), populate_existing=True)
    return bool(
        binding
        and binding.tenant_id == tenant_id
        and binding.agent_id == agent_id
        and binding.session_id == session_id
        and binding.requester_user_id == user_id
        and str(binding.account_id) == str(account_id)
    )


async def resolve_work_account(db, *, server, agent_id, context):
    if context is None:
        return server, None
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    try:
        principal = execution_principal_from_tool_context(context)
        if str(principal.tenant_id) != str(server.tenant_id) or str(context.agent_id) != str(agent_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=403, detail="MCP work identity is unavailable; no account was inferred"
        ) from exc
    try:
        session_id = uuid.UUID(str(principal.root_session_id)) if principal.root_session_id else None
    except ValueError:
        session_id = None
    requester = (
        await db.get(User, principal.requester_user_id, populate_existing=True) if principal.requester_user_id else None
    )
    if principal.requester_user_id and not _active_in_tenant(requester, server.tenant_id):
        raise HTTPException(
            status_code=403, detail="Authenticated MCP requester is no longer available in this company"
        )
    binding = (
        await db.scalar(
            select(MCPWorkAccountBinding)
            .execution_options(populate_existing=True)
            .where(
                MCPWorkAccountBinding.session_id == session_id,
                MCPWorkAccountBinding.agent_id == agent_id,
                MCPWorkAccountBinding.server_id == server.id,
                MCPWorkAccountBinding.requester_user_id == principal.requester_user_id,
            )
        )
        if session_id
        else None
    )
    if binding is None:
        personal_grant = await db.scalar(
            select(MCPAccountGrant.id)
            .join(MCPServer, MCPServer.id == MCPAccountGrant.account_id)
            .where(
                MCPServer.parent_server_id == server.id,
                MCPAccountGrant.agent_id == agent_id,
                MCPAccountGrant.requester_user_id == principal.requester_user_id,
                MCPAccountGrant.enabled.is_(True),
            )
            .limit(1)
        )
        if personal_grant:
            raise HTTPException(
                status_code=409,
                detail="Select the authorized account in the original work before using this connection",
            )
        return server, None
    if requester is None or not _active_in_tenant(requester, server.tenant_id) or binding.account_id is None:
        raise HTTPException(status_code=403, detail="Selected MCP account or requester is no longer available")
    from app.models.chat_session import ChatSession
    from app.core.permissions import authorize_session_action

    source = await db.get(ChatSession, session_id, populate_existing=True)
    if source is None or source.tenant_id != server.tenant_id:
        raise HTTPException(status_code=403, detail="Original work is no longer available")
    await authorize_session_action(
        db,
        requester,
        agent_id=source.agent_id,
        session_id=source.id,
        action="mcp_account_use",
        require_execution=True,
        require_writable=True,
    )
    account, grant = await account_access(
        db, server=server, account_id=binding.account_id, agent_id=agent_id, user=requester
    )
    if (
        account.owner_user_id
        and (not grant or not grant.enabled or binding.grant_id != grant.id or binding.grant_version != grant.version)
        or binding.authorization_id != ((account.config_json or {}).get("oauth") or {}).get("authorization_id")
    ):
        raise HTTPException(
            status_code=409, detail="MCP account authorization changed; review its selection in the original work"
        )
    return account, binding


def protect_account_credentials(context, *, account, token):
    if context is None:
        return
    from app.services.exact_secret_boundary import ExactSecretBoundary
    from app.services.mcp_oauth import decrypt_value

    oauth = (account.config_json or {}).get("oauth") or {}
    values = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "client_secret": decrypt_value(oauth.get("client_secret")),
    }
    boundary = ExactSecretBoundary.from_pairs(
        [(f"mcp-server://{account.id}/oauth/{key}", value) for key, value in values.items() if value]
    )
    context.exact_secret_boundary = ExactSecretBoundary.combine(
        getattr(context, "exact_secret_boundary", ExactSecretBoundary.empty()), boundary
    )


async def revoke_account(db, *, account):
    from app.services.mcp_server_service import _store_oauth

    oauth = (account.config_json or {}).get("oauth") or {}
    _store_oauth(
        account,
        {
            **{key: value for key, value in oauth.items() if key not in {"token", "pending", "refresh"}},
            "authorization_id": str(uuid.uuid4()),
        },
    )
    account.auth_status = "none"
    account.status = "needs_auth"
    grants = (await db.scalars(select(MCPAccountGrant).where(MCPAccountGrant.account_id == account.id))).all()
    for grant in grants:
        grant.enabled = False
        grant.version += 1
    await db.commit()
    return account_public(account)
