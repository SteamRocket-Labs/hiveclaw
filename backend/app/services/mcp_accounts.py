"""Owner-issued credential authority, independent of employee tool authority."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
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
        ).with_for_update()
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
    own = await personal_account(db, server=parent, user_id=user.id)
    if own is not None:
        return account_public(own)
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


async def list_work_accounts(db, *, server, agent_id, user):
    accounts = (
        await db.scalars(
            select(MCPServer)
            .where(
                MCPServer.tenant_id == server.tenant_id,
                MCPServer.parent_server_id == server.id,
                MCPServer.owner_user_id == user.id,
            )
            .order_by(MCPServer.name, MCPServer.id)
        )
    ).all()
    return [
        {**account_public(server), "can_authorize": user.role in {"platform_admin", "org_admin"}},
        *[{**account_public(account), "can_authorize": True} for account in accounts],
    ]


async def personal_account(db, *, server, user_id, connected_only=False):
    query = select(MCPServer).where(
        MCPServer.tenant_id == server.tenant_id,
        MCPServer.parent_server_id == server.id,
        MCPServer.owner_user_id == user_id,
    )
    if connected_only:
        query = query.where(MCPServer.auth_status == "configured")
    return await db.scalar(query.order_by(
        (MCPServer.auth_status == "configured").desc(), MCPServer.updated_at.desc(), MCPServer.id.desc()
    ).limit(1).execution_options(populate_existing=True))


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
    if owner and owner.tenant_id == server.tenant_id and _active_in_tenant(user, server.tenant_id) and not owner.is_active:
        raise HTTPException(status_code=403, detail="个人 MCP 凭据已吊销")
    if not _active_in_tenant(owner, server.tenant_id) or not _active_in_tenant(user, server.tenant_id):
        raise HTTPException(
            status_code=403, detail="MCP account owner or requester is no longer active in this company"
        )
    if account.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Personal connections can only act for their owner")
    return account, None


async def bind_work_account(db, *, server, account, grant, agent_id, session_id, user):
    if account.owner_user_id and account.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Personal connections can only act for their owner")
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
    # A session created for a system workflow does not make its owner a human requester.
    from app.models.runtime_task import RuntimeTask

    root_id = principal.root_runtime_task_id or getattr(context, "runtime_task_id", None)
    if root_id:
        root = await db.get(RuntimeTask, uuid.UUID(str(root_id)))
        if root is not None and (root.metadata_json or {}).get("headless_session_created"):
            return server, None
    requester = (
        await db.get(User, principal.requester_user_id, populate_existing=True) if principal.requester_user_id else None
    )
    if principal.requester_user_id and not _active_in_tenant(requester, server.tenant_id):
        raise HTTPException(
            status_code=403, detail="Authenticated MCP requester is no longer available in this company"
        )
    if requester is None:
        return server, None
    account = await personal_account(db, server=server, user_id=requester.id, connected_only=True)
    if account is not None:
        return await account_access(db, server=server, account_id=account.id, agent_id=agent_id, user=requester)
    return server, None


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
