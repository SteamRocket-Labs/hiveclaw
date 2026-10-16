"""An API credential cannot change the company-defined destination or action."""

import hashlib
import hmac
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant, CustomApiWorkAccountBinding
from app.models.tool import Tool
from app.models.user import User
from app.services.tool_config_service import mask_agent_tool_config_secrets, merge_agent_tool_config_secrets, resolve_tool_config


def revision(value):
    from app.config import get_settings

    return hmac.new(get_settings().SECRET_KEY.encode(), json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def connector_revision(tool):
    return revision([str(tool.id), tool.config, tool.config_schema, tool.parameters_schema])


def credential_schema(tool):
    from app.services.custom_api_connectors import _secret_field_for_auth

    scheme = (tool.config.get("auth") or {}).get("scheme", "none")
    return {"fields": _secret_field_for_auth(scheme)}


def configured(account, tool):
    keys = [field["key"] for field in credential_schema(tool)["fields"]]
    return bool(keys and all(account.config.get(key) for key in keys) and account.connector_revision == connector_revision(tool))


def account_public(account, tool, grant=None):
    action = tool.config.get("action") or {}
    return {"id": str(account.id) if account else None, "owner_user_id": str(account.owner_user_id) if account else None,
        "name": account.name if account else "Company account", "version": account.version if account else None,
        "is_configured": configured(account, tool) if account else False,
        "account_use_enabled": bool(grant and grant.enabled), "grant_version": grant.version if grant else 0,
        "connection_revision": connector_revision(tool),
        "connection": {"base_url": tool.config.get("base_url"), "method": action.get("method", "GET"), "path": action.get("path", "/")}}


async def active_user(db, user_id, tenant_id):
    user = await db.get(User, user_id, populate_existing=True) if user_id else None
    if not user or not user.is_active or not (user.tenant_id == tenant_id or user.role == "platform_admin"):
        raise HTTPException(403, "Current API account identity is unavailable")
    return user


async def load_connector(db, *, tenant_id, tool_id):
    tool = await db.get(Tool, tool_id, populate_existing=True)
    if not tool or tool.tenant_id != tenant_id or tool.type != "custom_api" or not tool.enabled:
        raise HTTPException(404, "Approved API connector not found")
    return tool


async def owned_account(db, *, user, tenant_id, account_id):
    await active_user(db, user.id, tenant_id)
    account = await db.scalar(select(CustomApiAccount).where(CustomApiAccount.id == account_id,
        CustomApiAccount.tenant_id == tenant_id).with_for_update().execution_options(populate_existing=True))
    if account is None:
        raise HTTPException(404, "API account not found")
    if account.owner_user_id != user.id:
        raise HTTPException(403, "API account owner required; employee management is not credential authority")
    return account


async def account_access(db, *, account_id, tool, agent_id, requester_id):
    await active_user(db, requester_id, tool.tenant_id)
    account = await db.get(CustomApiAccount, account_id, populate_existing=True)
    if account is None or (account.tenant_id, account.tool_id) != (tool.tenant_id, tool.id):
        raise HTTPException(404, "API account not found for this connector")
    await active_user(db, account.owner_user_id, tool.tenant_id)
    grant = await db.scalar(select(CustomApiAccountGrant).where(CustomApiAccountGrant.account_id == account.id,
        CustomApiAccountGrant.agent_id == agent_id, CustomApiAccountGrant.requester_user_id == requester_id)
        .execution_options(populate_existing=True))
    if account.owner_user_id != requester_id and not (grant and grant.enabled):
        raise HTTPException(403, "API account has not been shared for this requester and employee")
    return account, grant


async def company_config(db, *, tool, agent_id):
    config = await resolve_tool_config(tool.name, tenant_id=tool.tenant_id, agent_id=agent_id, db=db)
    return config, revision([connector_revision(tool), config])


async def list_accounts(db, *, user, agent, tool):
    await active_user(db, user.id, tool.tenant_id)
    config, current = await company_config(db, tool=tool, agent_id=agent.id)
    keys = [field["key"] for field in credential_schema(tool)["fields"]]
    result = [{**account_public(None, tool), "revision": current, "account_use_enabled": True,
        "is_configured": all(config.get(key) for key in keys)}]
    grants = select(CustomApiAccountGrant.account_id).where(CustomApiAccountGrant.agent_id == agent.id,
        CustomApiAccountGrant.requester_user_id == user.id, CustomApiAccountGrant.enabled.is_(True))
    accounts = (await db.scalars(select(CustomApiAccount).where(CustomApiAccount.tenant_id == tool.tenant_id,
        CustomApiAccount.tool_id == tool.id, or_(CustomApiAccount.owner_user_id == user.id, CustomApiAccount.id.in_(grants)))
        .order_by(CustomApiAccount.name, CustomApiAccount.id))).all()
    for account in accounts:
        try:
            account, grant = await account_access(db, account_id=account.id, tool=tool, agent_id=agent.id, requester_id=user.id)
        except HTTPException:
            continue
        result.append(account_public(account, tool, grant))
    return result


async def save_account(db, *, account, tool, config, expected_version, connection_revision):
    schema = credential_schema(tool)
    keys = {field["key"] for field in schema["fields"]}
    if not keys or set(config) - keys:
        raise HTTPException(422, "Only this approved connector's credential fields may be saved")
    if connection_revision != connector_revision(tool):
        raise HTTPException(409, "API connector changed; review its current destination and action")
    merged = merge_agent_tool_config_secrets(config, account.config, schema)
    merged = {key: value for key, value in merged.items() if key in keys}
    if not all(isinstance(merged.get(key), str) and merged[key] for key in keys):
        raise HTTPException(422, "API account credentials are incomplete")
    same = merged == account.config and account.connector_revision == connection_revision
    if expected_version != account.version and not (expected_version + 1 == account.version and same):
        raise HTTPException(409, "API account changed; read its current settings before saving")
    if not same:
        account.config, account.connector_revision = merged, connection_revision
        account.version += 1
    await db.flush()
    return {**account_public(account, tool), "config": mask_agent_tool_config_secrets(account.config, schema), "config_schema": schema}


async def set_grant(db, *, account, agent_id, requester_id, enabled, expected_version):
    await active_user(db, requester_id, account.tenant_id)
    grant = await db.scalar(select(CustomApiAccountGrant).where(CustomApiAccountGrant.account_id == account.id,
        CustomApiAccountGrant.agent_id == agent_id, CustomApiAccountGrant.requester_user_id == requester_id)
        .execution_options(populate_existing=True))
    if (grant is None and expected_version != 0 or grant is not None and not (
        expected_version == grant.version or expected_version + 1 == grant.version and grant.enabled == enabled)):
        raise HTTPException(409, "API account use grant changed; review its current version")
    if grant is None:
        grant = CustomApiAccountGrant(tenant_id=account.tenant_id, account_id=account.id,
            agent_id=agent_id, requester_user_id=requester_id, enabled=enabled)
        db.add(grant)
    elif grant.enabled != enabled:
        grant.enabled = enabled
        grant.version += 1
    await db.flush()
    return {"id": str(grant.id), "enabled": grant.enabled, "version": grant.version}


async def bind_account(db, *, agent, tool, session_id, user, account_id, account_version=None, grant_version=None, company_revision=None):
    if account_id is None:
        config, current = await company_config(db, tool=tool, agent_id=agent.id)
        if company_revision != current:
            raise HTTPException(409, "Company API account changed; review it before continuing")
        account, grant = None, None
        if not all(config.get(field["key"]) for field in credential_schema(tool)["fields"]):
            raise HTTPException(409, "Company API credentials are incomplete")
    else:
        account, grant = await account_access(db, account_id=account_id, tool=tool, agent_id=agent.id, requester_id=user.id)
        if not grant or not grant.enabled:
            raise HTTPException(409, "Grant this employee use of the API account before choosing it")
        if not configured(account, tool) or (account.version, grant.version) != (account_version, grant_version):
            raise HTTPException(409, "API account authorization changed; review its credentials and grant")
        current = None
    binding = await db.scalar(select(CustomApiWorkAccountBinding).where(CustomApiWorkAccountBinding.session_id == session_id,
        CustomApiWorkAccountBinding.agent_id == agent.id, CustomApiWorkAccountBinding.requester_user_id == user.id,
        CustomApiWorkAccountBinding.tool_id == tool.id).with_for_update().execution_options(populate_existing=True))
    if binding is None:
        binding = CustomApiWorkAccountBinding(tenant_id=agent.tenant_id, session_id=session_id,
            agent_id=agent.id, requester_user_id=user.id, tool_id=tool.id)
        db.add(binding)
    binding.account_id, binding.account_version = (account.id, account.version) if account else (None, None)
    binding.grant_id, binding.grant_version = (grant.id, grant.version) if grant else (None, None)
    binding.company_revision = current
    await db.flush()
    return binding


async def selected_config(db, *, binding, agent_id, tool, user, session_id):
    if (binding.tenant_id, binding.agent_id, binding.tool_id, binding.requester_user_id, binding.session_id) != (tool.tenant_id, agent_id, tool.id, user.id, session_id):
        raise HTTPException(403, "API account selection does not belong to this work")
    if binding.account_id is None:
        config, current = await company_config(db, tool=tool, agent_id=agent_id)
        if not binding.company_revision or current != binding.company_revision:
            raise HTTPException(409, "Company API account changed; review the selection in the original work")
        return config
    account, grant = await account_access(db, account_id=binding.account_id, tool=tool, agent_id=agent_id, requester_id=user.id)
    if not configured(account, tool) or not grant or not grant.enabled or (grant.id, grant.version, account.version) != (binding.grant_id, binding.grant_version, binding.account_version):
        raise HTTPException(409, "API account authorization changed; review the selection in the original work")
    return account.config


def binding_snapshot(binding):
    return {"binding_id": str(binding.id), "account_id": str(binding.account_id) if binding.account_id else None,
        "account_version": binding.account_version, "grant_id": str(binding.grant_id) if binding.grant_id else None,
        "grant_version": binding.grant_version, "company_revision": binding.company_revision}


async def validate_configuration_answer(db, *, question, payload, user):
    from app.core.permissions import require_agent_execution_access

    saved = (payload.get("configuration_saved") or {}).get("api_account_selection")
    if not saved or not user:
        raise HTTPException(409, "Review and choose the API account in the original work before continuing")
    binding = await db.get(CustomApiWorkAccountBinding, uuid.UUID(saved["binding_id"]), populate_existing=True)
    if binding is None or binding_snapshot(binding) != saved:
        raise HTTPException(409, "API account choice changed; review it before continuing")
    target = payload["configuration_request"]["target_agent_id"]
    agent, _access = await require_agent_execution_access(db, user, uuid.UUID(target))
    tool = await load_connector(db, tenant_id=agent.tenant_id, tool_id=binding.tool_id)
    if tool.name != payload["configuration_request"]["tool_name"]:
        raise HTTPException(409, "API connector choice changed")
    await selected_config(db, binding=binding, agent_id=agent.id, tool=tool, user=user, session_id=question.session_id)


async def resolve_account_config(db, *, tool, agent_id, context):
    from app.core.permissions import authorize_session_action, require_agent_execution_access
    from app.models.chat_session import ChatSession
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    try:
        principal = execution_principal_from_tool_context(context)
        session_id = uuid.UUID(str(principal.root_session_id)) if principal.root_session_id else None
        if uuid.UUID(str(principal.tenant_id)) != tool.tenant_id or context.agent_id != agent_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(403, "Authenticated API account work identity is unavailable") from exc
    user = await active_user(db, principal.requester_user_id, tool.tenant_id)
    await require_agent_execution_access(db, user, agent_id)
    binding = await db.scalar(select(CustomApiWorkAccountBinding).where(CustomApiWorkAccountBinding.session_id == session_id,
        CustomApiWorkAccountBinding.agent_id == agent_id, CustomApiWorkAccountBinding.requester_user_id == user.id,
        CustomApiWorkAccountBinding.tool_id == tool.id)) if session_id else None
    if binding is None:
        grant = await db.scalar(select(CustomApiAccountGrant.id).join(CustomApiAccount, CustomApiAccount.id == CustomApiAccountGrant.account_id)
            .where(CustomApiAccount.tool_id == tool.id, CustomApiAccountGrant.agent_id == agent_id,
                CustomApiAccountGrant.requester_user_id == user.id, CustomApiAccountGrant.enabled.is_(True)).limit(1))
        if grant:
            raise HTTPException(409, "Review the API account selection in the original work before using it")
        return (await company_config(db, tool=tool, agent_id=agent_id))[0]
    source = await db.get(ChatSession, session_id, populate_existing=True)
    if source is None or source.tenant_id != tool.tenant_id:
        raise HTTPException(403, "Original API account work is no longer available")
    await authorize_session_action(db, user, agent_id=source.agent_id, session_id=source.id,
        action="custom_api_account_use", require_execution=True, require_writable=True)
    return await selected_config(db, binding=binding, agent_id=agent_id, tool=tool, user=user, session_id=session_id)
