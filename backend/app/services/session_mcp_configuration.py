"""Nonsecret MCP authorization/use readback for an originating question."""

import hashlib
import json
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.mcp_server import AgentMCPServerAssignment, AgentMCPToolOverride, MCPServer, MCPServerTool
from app.models.tool import AgentTool, Tool
from app.services import mcp_oauth
from app.services.mcp_metadata_trust import current_mcp_fingerprint, is_mcp_metadata_runtime_approved


async def mcp_configuration_snapshot(db, *, target, server_id, user=None, account_id=None):
    server = await db.scalar(
        select(MCPServer)
        .where(MCPServer.id == server_id, MCPServer.tenant_id == target.tenant_id, MCPServer.owner_user_id.is_(None))
        .execution_options(populate_existing=True)
    )
    if server is None:
        raise HTTPException(status_code=404, detail="MCP connection not found")
    from app.services.mcp_accounts import account_access, account_public, list_work_accounts

    account, grant = (
        await account_access(db, server=server, account_id=account_id or server.id, agent_id=target.id, user=user)
        if user
        else (server, None)
    )
    assignment = await db.scalar(
        select(AgentMCPServerAssignment)
        .where(
            AgentMCPServerAssignment.tenant_id == target.tenant_id,
            AgentMCPServerAssignment.agent_id == target.id,
            AgentMCPServerAssignment.mcp_server_id == server.id,
        )
        .execution_options(populate_existing=True)
    )
    rows = (
        await db.execute(
            select(Tool, AgentTool.enabled)
            .join(MCPServerTool, MCPServerTool.tool_id == Tool.id)
            .outerjoin(AgentTool, (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == target.id))
            .where(MCPServerTool.tenant_id == target.tenant_id, MCPServerTool.mcp_server_id == server.id)
            .order_by(Tool.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    overrides = (
        await db.scalars(
            select(AgentMCPToolOverride).where(
                AgentMCPToolOverride.agent_id == target.id, AgentMCPToolOverride.mcp_server_id == server.id
            )
        )
    ).all()
    override_modes = {row.tool_name: row.mode for row in overrides}
    mode = assignment.default_tool_mode if assignment else "deny"
    usage = {
        "enabled": bool(assignment and assignment.enabled),
        "default_tool_mode": mode,
        "always_load": bool(assignment and assignment.always_load),
    }
    facts = [
        {
            "id": str(tool.id),
            "fingerprint": current_mcp_fingerprint(tool),
            "approved": is_mcp_metadata_runtime_approved(tool),
            "enabled": bool(enabled and tool.enabled),
            "mode": override_modes.get(tool.mcp_tool_name, mode),
        }
        for tool, enabled in rows
    ]
    usable = (
        sum(1 for item in facts if item["approved"] and item["enabled"] and item["mode"] != "deny")
        if usage["enabled"]
        else 0
    )
    permitted_tool_count = usable
    if account.owner_user_id and not (grant and grant.enabled):
        usable = 0
    oauth = (account.config_json or {}).get("oauth") or {}
    token = mcp_oauth.decrypt_token_set(oauth.get("token"))
    refreshable = bool(
        token
        and token.refresh_token
        and oauth.get("token_endpoint")
        and oauth.get("client_id")
        and not oauth.get("refresh")
        and (not oauth.get("client_secret") or mcp_oauth.decrypt_value(oauth["client_secret"]))
    )
    if token and (not token.is_expired(time.time()) or refreshable):
        authorization = "configured"
    elif (oauth.get("refresh") or {}).get("status") in {"exchanging", "unknown"} or (oauth.get("pending") or {}).get(
        "status"
    ) == "exchanging":
        authorization = "unknown"
    elif oauth.get("pending"):
        authorization = "pending"
    else:
        authorization = "required"
    revision = hashlib.sha256(
        json.dumps(
            {
                "server": str(server.id),
                "updated_at": server.updated_at.isoformat(),
                "account": str(account.id),
                "account_updated_at": account.updated_at.isoformat(),
                "grant": [str(grant.id), grant.version, grant.enabled] if grant else None,
                "credential": hashlib.sha256(str(oauth.get("token") or "").encode()).hexdigest(),
                "authorization": authorization,
                "usage": usage,
                "tools": facts,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "server": {"id": str(server.id), "name": server.name},
        "revision": revision,
        "authorization": authorization,
        "usage": usage,
        "usable_tool_count": usable,
        "permitted_tool_count": permitted_tool_count,
        "account": {**account_public(account), "grant_version": grant.version if grant else 0},
        "accounts": await list_work_accounts(db, server=server, agent_id=target.id, user=user)
        if user
        else [account_public(server)],
        "account_use_enabled": not account.owner_user_id or bool(grant and grant.enabled),
        "can_grant_account": bool(user and account.owner_user_id == user.id),
        "can_authorize": bool(
            user
            and (
                account.owner_user_id == user.id
                if account.owner_user_id
                else user.role in {"platform_admin", "org_admin"}
            )
        ),
    }


async def validate_mcp_configuration_answer(db, *, payload, user, session_id):
    descriptor = payload["configuration_request"]
    from app.core.permissions import check_agent_access

    if user is None or str(user.id) != payload.get("requested_by_user_id"):
        raise HTTPException(status_code=403, detail="Original requester required")
    target, access = await check_agent_access(db, user, uuid.UUID(descriptor["target_agent_id"]))
    if access not in {"use", "manage"}:
        raise HTTPException(status_code=403, detail="Current employee access required")
    ready = payload.get("configuration_ready") or {}
    if not ready or target is None or target.deleted_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mcp_configuration_not_confirmed",
                "message": "Review the MCP authorization and employee use permission before continuing.",
            },
        )
    current = await mcp_configuration_snapshot(
        db,
        target=target,
        server_id=uuid.UUID(descriptor["mcp_server_id"]),
        user=user,
        account_id=uuid.UUID(ready["account_id"]) if ready.get("account_id") else None,
    )
    from app.services.mcp_accounts import work_binding_matches

    matches = await work_binding_matches(
        db,
        ready=ready,
        tenant_id=target.tenant_id,
        agent_id=target.id,
        session_id=session_id,
        user_id=user.id,
        account_id=current["account"]["id"],
    )
    if (
        current["revision"] != ready.get("revision")
        or not current["usable_tool_count"]
        or current["authorization"] != "configured"
        or not matches
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mcp_configuration_changed",
                "message": "MCP authorization or usage changed. Review the current connection before continuing.",
            },
        )
