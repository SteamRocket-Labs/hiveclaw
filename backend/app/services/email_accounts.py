"""Mailbox credentials remain behind owner grants and the current work binding."""

import hashlib
import hmac
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.email_account import EmailAccount, EmailAccountGrant, EmailWorkAccountBinding
from app.models.user import User
from app.services.tool_config_service import mask_agent_tool_config_secrets, merge_agent_tool_config_secrets, resolve_tool_config

EMAIL_SCHEMA = {"fields": [{"key": "auth_code", "type": "password"}]}
_TRANSPORT_FIELDS = ("imap_host", "imap_port", "smtp_host", "smtp_port", "smtp_ssl")


def personal_config_schema():
    from app.services.email_service import EMAIL_PROVIDERS

    return {"fields": [{"key": "email_provider", "label": "Email provider", "type": "select", "default": "gmail",
        "options": [{"value": key, "label": value["label"]} for key, value in EMAIL_PROVIDERS.items()] + [{"value": "custom", "label": "Company mail server"}]},
        {"key": "email_address", "label": "Email address"}, {"key": "auth_code", "label": "App password", "type": "password"}]}


def _revision(config):
    from app.config import get_settings

    return hmac.new(get_settings().SECRET_KEY.encode(), json.dumps(config, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


async def _company_transport(db, *, tenant_id, agent_id):
    from app.models.agent import Agent
    from app.services.email_service import resolve_config

    source = await db.get(Agent, agent_id, populate_existing=True)
    if source is None or source.tenant_id != tenant_id or source.deleted_at or source.deactivated_at:
        raise HTTPException(409, "Company mail server source is no longer available")
    config, _ = await company_mailbox(db, tenant_id=tenant_id, agent_id=agent_id)
    if config.get("email_provider") != "custom":
        raise HTTPException(409, "Ask an employee manager to configure the company custom mail server first")
    try:
        resolved = resolve_config(config)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "Company mail server configuration needs review") from exc
    if not resolved.get("imap_host") or not resolved.get("smtp_host"):
        raise HTTPException(409, "Company mail server configuration is incomplete")
    return {key: resolved[key] for key in _TRANSPORT_FIELDS}


def account_public(account, grant=None):
    return {"id": str(account.id), "owner_user_id": str(account.owner_user_id), "name": account.name,
        "version": account.version, "is_configured": bool(account.config.get("email_address") and account.config.get("auth_code")),
        "account_use_enabled": bool(grant and grant.enabled), "grant_version": grant.version if grant else 0}


async def active_user(db, user_id, tenant_id):
    user = await db.get(User, user_id, populate_existing=True) if user_id else None
    if not user or not user.is_active or not (user.tenant_id == tenant_id or user.role == "platform_admin"):
        raise HTTPException(403, "Current company mailbox identity is unavailable")
    return user


async def owned_account(db, *, user, tenant_id, account_id):
    account = await db.scalar(select(EmailAccount).where(EmailAccount.id == account_id,
        EmailAccount.tenant_id == tenant_id).with_for_update().execution_options(populate_existing=True))
    if account is None:
        raise HTTPException(404, "Mailbox account not found")
    await active_user(db, user.id, tenant_id)
    if account.owner_user_id != user.id:
        raise HTTPException(403, "Mailbox owner required; employee management is not credential authority")
    return account


async def account_access(db, *, account_id, tenant_id, agent_id, requester_id):
    await active_user(db, requester_id, tenant_id)
    account = await db.get(EmailAccount, account_id, populate_existing=True)
    if account is None or account.tenant_id != tenant_id:
        raise HTTPException(404, "Mailbox account not found")
    await active_user(db, account.owner_user_id, tenant_id)
    grant = await db.scalar(select(EmailAccountGrant).where(EmailAccountGrant.account_id == account.id,
        EmailAccountGrant.agent_id == agent_id, EmailAccountGrant.requester_user_id == requester_id)
        .execution_options(populate_existing=True))
    if account.owner_user_id != requester_id and not (grant and grant.enabled):
        raise HTTPException(403, "Mailbox account has not been shared for this requester and employee")
    return account, grant


async def company_mailbox(db, *, tenant_id, agent_id):
    config = await resolve_tool_config("send_email", tenant_id=tenant_id, agent_id=agent_id, db=db)
    return config, _revision(config)


async def list_accounts(db, *, user, agent):
    await active_user(db, user.id, agent.tenant_id)
    config, revision = await company_mailbox(db, tenant_id=agent.tenant_id, agent_id=agent.id)
    result = [{"id": None, "owner_user_id": None, "name": "Company mailbox", "revision": revision,
        "is_configured": bool(config.get("email_address") and config.get("auth_code")), "account_use_enabled": True}]
    granted = select(EmailAccountGrant.account_id).where(EmailAccountGrant.agent_id == agent.id,
        EmailAccountGrant.requester_user_id == user.id, EmailAccountGrant.enabled.is_(True))
    accounts = (await db.scalars(select(EmailAccount).where(EmailAccount.tenant_id == agent.tenant_id,
        or_(EmailAccount.owner_user_id == user.id, EmailAccount.id.in_(granted))).order_by(EmailAccount.name, EmailAccount.id))).all()
    for account in accounts:
        try:
            account, grant = await account_access(db, account_id=account.id, tenant_id=agent.tenant_id,
                agent_id=agent.id, requester_id=user.id)
        except HTTPException:
            continue
        result.append(account_public(account, grant))
    return result


async def save_account(db, *, account, config, expected_version, source_agent_id=None):
    from app.core.permissions import require_agent_execution_access
    from app.services.email_service import EMAIL_PROVIDERS, _EMAIL_ADDRESS_RE

    allowed = {"email_provider", "email_address", "auth_code"}
    if any(key not in allowed and (key not in {*_TRANSPORT_FIELDS, "_transport_authority"} or value != account.config.get(key)) for key, value in config.items()):
        raise HTTPException(422, "Mailbox transport settings come from the approved company connection")
    merged = merge_agent_tool_config_secrets(config, account.config, EMAIL_SCHEMA)
    merged = {key: value for key, value in merged.items() if key in allowed}
    merged["email_provider"] = str(merged.get("email_provider") or "gmail")
    if (not isinstance(merged.get("email_address"), str) or not _EMAIL_ADDRESS_RE.fullmatch(merged["email_address"])
        or not isinstance(merged.get("auth_code"), str) or not merged["auth_code"]):
        raise HTTPException(422, "Mailbox address and app password are required")
    if merged["email_provider"] == "custom":
        previous = account.config.get("_transport_authority") or {}
        source_agent_id = source_agent_id or (uuid.UUID(previous["agent_id"]) if previous.get("agent_id") else None)
        if source_agent_id is None:
            raise HTTPException(409, "Choose the employee with the approved company custom mail server")
        owner = await active_user(db, account.owner_user_id, account.tenant_id)
        source, _access = await require_agent_execution_access(db, owner, source_agent_id)
        if source.tenant_id != account.tenant_id:
            raise HTTPException(404, "Company mail server not found")
        transport = await _company_transport(db, tenant_id=account.tenant_id, agent_id=source_agent_id)
        merged.update(transport)
        merged["_transport_authority"] = {"agent_id": str(source_agent_id), "revision": _revision(transport)}
    elif merged["email_provider"] not in EMAIL_PROVIDERS:
        raise HTTPException(422, "Choose a supported provider or an approved company mail server")
    if expected_version != account.version and not (expected_version + 1 == account.version and merged == account.config):
        raise HTTPException(409, "Mailbox changed; read the current settings before saving")
    if merged != account.config:
        account.config = merged
        account.version += 1
    await db.flush()
    return {**account_public(account), "config": mask_agent_tool_config_secrets(account.config, EMAIL_SCHEMA), "config_schema": personal_config_schema()}


async def set_grant(db, *, account, agent_id, requester_id, enabled, expected_version):
    await active_user(db, requester_id, account.tenant_id)
    grant = await db.scalar(select(EmailAccountGrant).where(EmailAccountGrant.account_id == account.id,
        EmailAccountGrant.agent_id == agent_id, EmailAccountGrant.requester_user_id == requester_id)
        .execution_options(populate_existing=True))
    if (grant is None and expected_version != 0 or grant is not None and not (
        expected_version == grant.version or expected_version + 1 == grant.version and grant.enabled == enabled)):
        raise HTTPException(409, "Mailbox use grant changed; read its current version before deciding again")
    if grant is None:
        grant = EmailAccountGrant(tenant_id=account.tenant_id, account_id=account.id,
            agent_id=agent_id, requester_user_id=requester_id, enabled=enabled)
        db.add(grant)
    elif grant.enabled != enabled:
        grant.enabled = enabled
        grant.version += 1
    await db.flush()
    return {"id": str(grant.id), "enabled": grant.enabled, "version": grant.version}


async def bind_account(db, *, agent, session_id, user, account_id, account_version=None, grant_version=None, company_revision=None):
    if account_id is None:
        config, revision = await company_mailbox(db, tenant_id=agent.tenant_id, agent_id=agent.id)
        if company_revision != revision:
            raise HTTPException(409, "Company mailbox changed; review it before continuing")
        account, grant = None, None
    else:
        account, grant = await account_access(db, account_id=account_id, tenant_id=agent.tenant_id,
            agent_id=agent.id, requester_id=user.id)
        if not grant or not grant.enabled:
            raise HTTPException(409, "Grant this employee use of the mailbox before choosing it")
        if (account.version, grant.version) != (account_version, grant_version):
            raise HTTPException(409, "Mailbox authorization changed; review it before continuing")
        config, revision = account.config, None
    if not config.get("email_address") or not config.get("auth_code"):
        raise HTTPException(409, "Mailbox configuration is incomplete")
    binding = await db.scalar(select(EmailWorkAccountBinding).where(EmailWorkAccountBinding.session_id == session_id,
        EmailWorkAccountBinding.agent_id == agent.id, EmailWorkAccountBinding.requester_user_id == user.id)
        .with_for_update().execution_options(populate_existing=True))
    if binding is None:
        binding = EmailWorkAccountBinding(tenant_id=agent.tenant_id, session_id=session_id,
            agent_id=agent.id, requester_user_id=user.id)
        db.add(binding)
    binding.account_id = account.id if account else None
    binding.account_version = account.version if account else None
    binding.grant_id = grant.id if grant else None
    binding.grant_version = grant.version if grant else None
    binding.company_revision = revision
    await db.flush()
    return binding


async def selected_config(db, *, binding, agent_id, user, session_id, tenant_id):
    if (binding.tenant_id, binding.agent_id, binding.requester_user_id, binding.session_id) != (tenant_id, agent_id, user.id, session_id):
        raise HTTPException(403, "Mailbox selection does not belong to this work")
    if binding.account_id is None:
        config, revision = await company_mailbox(db, tenant_id=tenant_id, agent_id=agent_id)
        if not binding.company_revision or revision != binding.company_revision:
            raise HTTPException(409, "Company mailbox changed; review the selection in the original work")
        return config
    account, grant = await account_access(db, account_id=binding.account_id, tenant_id=tenant_id,
        agent_id=agent_id, requester_id=user.id)
    if (not grant or not grant.enabled or (grant.id, grant.version, account.version) !=
        (binding.grant_id, binding.grant_version, binding.account_version)):
        raise HTTPException(409, "Mailbox authorization changed; review the selection in the original work")
    config = dict(account.config)
    if config.get("email_provider") == "custom":
        authority = config.get("_transport_authority") or {}
        if not authority.get("agent_id"):
            raise HTTPException(409, "Personal mailbox custom transport needs company review")
        transport = await _company_transport(db, tenant_id=tenant_id, agent_id=uuid.UUID(authority["agent_id"]))
        if _revision(transport) != authority.get("revision"):
            raise HTTPException(409, "Company mail server changed; review the mailbox configuration")
    return config


def binding_snapshot(binding):
    return {"binding_id": str(binding.id), "account_id": str(binding.account_id) if binding.account_id else None,
        "account_version": binding.account_version, "grant_id": str(binding.grant_id) if binding.grant_id else None,
        "grant_version": binding.grant_version, "company_revision": binding.company_revision}


async def validate_configuration_answer(db, *, question, payload, user):
    from app.core.permissions import require_agent_execution_access

    ready = (payload.get("configuration_saved") or {}).get("mailbox_selection")
    if not ready or not user:
        raise HTTPException(409, "Review and choose the mailbox in the original work before continuing")
    binding = await db.get(EmailWorkAccountBinding, uuid.UUID(ready["binding_id"]), populate_existing=True)
    if binding is None or binding_snapshot(binding) != ready:
        raise HTTPException(409, "Mailbox choice changed; review it before continuing")
    agent_id = uuid.UUID(payload["configuration_request"]["target_agent_id"])
    agent, _access = await require_agent_execution_access(db, user, agent_id)
    await active_user(db, user.id, agent.tenant_id)
    await selected_config(db, binding=binding, agent_id=agent_id, user=user,
        session_id=question.session_id, tenant_id=agent.tenant_id)


async def resolve_email_config(agent_id, context):
    if context is None:
        return await resolve_tool_config("send_email", agent_id=agent_id)
    from app.core.permissions import authorize_session_action, require_agent_execution_access
    from app.database import tenant_scoped_session
    from app.models.chat_session import ChatSession
    from app.services.runtime_task_authority import execution_principal_from_tool_context

    try:
        principal = execution_principal_from_tool_context(context)
        tenant_id = uuid.UUID(str(principal.tenant_id))
        session_id = uuid.UUID(str(principal.root_session_id)) if principal.root_session_id else None
        if context.agent_id != agent_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(403, "Authenticated mailbox work identity is unavailable") from exc
    async with tenant_scoped_session(tenant_id) as db:
        user = await active_user(db, principal.requester_user_id, tenant_id)
        await require_agent_execution_access(db, user, agent_id)
        binding = await db.scalar(select(EmailWorkAccountBinding).where(EmailWorkAccountBinding.session_id == session_id,
            EmailWorkAccountBinding.agent_id == agent_id, EmailWorkAccountBinding.requester_user_id == user.id)) if session_id else None
        if binding is None:
            grant = await db.scalar(select(EmailAccountGrant.id).where(EmailAccountGrant.agent_id == agent_id,
                EmailAccountGrant.requester_user_id == user.id, EmailAccountGrant.enabled.is_(True)).limit(1))
            if grant:
                raise HTTPException(409, "Select the mailbox in the original work before using email")
            config, _revision = await company_mailbox(db, tenant_id=tenant_id, agent_id=agent_id)
        else:
            source = await db.get(ChatSession, session_id, populate_existing=True)
            if source is None or source.tenant_id != tenant_id:
                raise HTTPException(403, "Original mailbox work is no longer available")
            await authorize_session_action(db, user, agent_id=source.agent_id, session_id=source.id,
                action="email_account_use", require_execution=True, require_writable=True)
            config = await selected_config(db, binding=binding, agent_id=agent_id, user=user,
                session_id=session_id, tenant_id=tenant_id)
    from app.services.exact_secret_boundary import ExactSecretBoundary

    context.exact_secret_boundary = ExactSecretBoundary.combine(context.exact_secret_boundary,
        ExactSecretBoundary.from_pairs([("email-account://auth_code", str(config["auth_code"]))] if config.get("auth_code") else []))
    return config
