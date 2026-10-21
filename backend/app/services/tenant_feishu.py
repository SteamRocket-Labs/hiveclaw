"""Enterprise Feishu bot authority uses the current app-scoped directory proof."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.external_principal import ExternalPrincipalBindingEvent
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.user import User
from app.models.user_assistant_binding import UserAssistantBinding
from app.services.feishu_platform import resolve_feishu_platform


def _same_application(provider, config):
    identity = resolve_feishu_platform(provider.config)
    bot = resolve_feishu_platform(config.extra_config)
    return ((provider.config or {}).get("app_id") == config.app_id
        and (identity.region, identity.open_api_domain) == (bot.region, bot.open_api_domain))


async def resolve_tenant_feishu_identity(db, *, config, sender):
    """Never borrow an open_id from another app, tenant, legacy field or email."""
    if config is None or not config.is_active or config.channel_type != "feishu" or not sender.get("open_id"):
        return None
    row = (await db.execute(select(User, ExternalIdentity, IdentityProvider)
        .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
        .join(IdentityProvider, IdentityProvider.id == ExternalIdentity.provider_id)
        .where(User.tenant_id == config.tenant_id, User.is_active.is_(True),
            IdentityProvider.tenant_id == config.tenant_id, IdentityProvider.provider_type == "feishu",
            IdentityProvider.is_active.is_(True), ExternalIdentity.provider_open_id == sender["open_id"])
        .execution_options(populate_existing=True))).one_or_none()
    if row is None or not config.app_id or not _same_application(row[2], config):
        return None
    if any(sender.get(key) and getattr(row[1], column) and sender[key] != getattr(row[1], column)
        for key, column in (("user_id", "provider_user_id"), ("union_id", "provider_union_id"))):
        return None
    from app.services.channel_user_service import channel_user_service

    agreed = await channel_user_service.resolve_feishu_user(db, tenant_id=config.tenant_id,
        provider_open_id=sender["open_id"], provider_user_id=sender.get("user_id"), provider_union_id=sender.get("union_id"))
    if agreed is None or agreed.id != row[0].id:
        return None
    return row[0], row[1]


async def tenant_feishu_assistant(db, *, config, sender):
    proof = await resolve_tenant_feishu_identity(db, config=config, sender=sender)
    return await _tenant_member_assistant(db, tenant_id=config.tenant_id, user=proof[0]) if proof else None


async def tenant_feishu_target(db, *, config, sender, message, accepted_event=None):
    from app.core.permissions import check_agent_access, reject_non_executing_agent_access
    from app.services.feishu_group_entry import group_entry_settings

    proof = await resolve_tenant_feishu_identity(db, config=config, sender=sender)
    if proof is None:
        return None
    target_id = None
    if accepted_event is not None and accepted_event.result_session_id:
        from app.models.chat_session import ChatSession
        from app.services.channel_ingress_inbox import channel_installation_ref

        session = await db.get(ChatSession, accepted_event.result_session_id)
        if (session is None or session.tenant_id != config.tenant_id or session.agent_id != accepted_event.agent_id
            or accepted_event.installation_ref != channel_installation_ref(config, fallback="")
            or (session.audience_kind == "group" and (session.group_installation_ref != accepted_event.installation_ref
                or session.group_subject_id != message.get("chat_id") or message.get("chat_type") != "group"))
            or (session.audience_kind != "group" and (session.user_id != proof[0].id
                or session.external_principal_id != accepted_event.external_principal_id))):
            raise PermissionError("tenant_feishu_accepted_work_mismatch")
        target_id = session.agent_id
    else:
        routes = group_entry_settings(config)["routes"] if message.get("chat_type") == "group" else []
        route = next((row for row in routes if row["chat_id"] == message.get("chat_id")), None)
        if route is None:
            return await _tenant_member_assistant(db, tenant_id=config.tenant_id, user=proof[0])
        target_id = route["agent_id"]
    try:
        agent, access = await check_agent_access(db, proof[0], target_id)
        reject_non_executing_agent_access(access)
        return agent.id if agent.tenant_id == config.tenant_id else None
    except HTTPException:
        return None


async def _tenant_member_assistant(db, *, tenant_id, user):
    from app.core.permissions import check_agent_access, reject_non_executing_agent_access
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason

    agent = await db.scalar(select(Agent).join(UserAssistantBinding, UserAssistantBinding.agent_id == Agent.id)
        .where(UserAssistantBinding.tenant_id == tenant_id, UserAssistantBinding.user_id == user.id,
            UserAssistantBinding.status == "active", Agent.tenant_id == tenant_id))
    if agent is None or get_agent_lifecycle_block_reason(agent) is not None:
        return None
    try:
        _, access = await check_agent_access(db, user, agent.id)
        reject_non_executing_agent_access(access)
    except HTTPException:
        return None
    return agent.id


async def resolve_feishu_channel_principal(db, *, config, agent_id, tenant_id, subject_id, display_name, profile):
    from app.services.channel_ingress_context import current_channel_ingress_context
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import (
        ExternalPrincipalResolution, _runtime_actor, _synchronize_external_principal_session_users,
        resolve_or_create_external_principal,
    )

    enterprise = isinstance(config, TenantChannelConfig)
    if enterprise:
        ingress = current_channel_ingress_context()
        event = await db.get(ChannelIngressEvent, ingress.event_id) if ingress else None
        installation = channel_installation_ref(config, fallback="")
        if (ingress is None or event is None or event.status != "processing" or event.provider != "feishu"
            or event.handler_key != "feishu.event" or event.tenant_id != tenant_id or event.agent_id != agent_id
            or ingress.tenant_id != tenant_id or ingress.agent_id != agent_id or ingress.provider != "feishu"
            or ingress.installation_ref != installation or event.installation_ref != installation
            or str((event.metadata_json or {}).get("tenant_channel_config_id")) != str(config.id)):
            raise PermissionError("tenant_feishu_authenticated_ingress_required")
        body = (event.payload_json or {}).get("body", {})
        sender = (body.get("event", {}).get("sender", {}).get("sender_id") or {})
        config = await db.get(TenantChannelConfig, config.id, populate_existing=True)
        proof = await resolve_tenant_feishu_identity(db, config=config, sender=sender)
        if (proof is None or sender.get("open_id") != subject_id
            or await tenant_feishu_target(db, config=config, sender=sender, message=body.get("event", {}).get("message", {}), accepted_event=event) != agent_id):
            raise PermissionError("tenant_feishu_identity_or_assistant_changed")
    result = await resolve_or_create_external_principal(db, tenant_id=tenant_id, provider="feishu",
        installation_ref=channel_installation_ref(config, fallback=f"feishu:{agent_id}"),
        channel_config_id=None if enterprise else getattr(config, "id", None), tenant_channel_config_id=config.id if enterprise else None,
        subject_id=subject_id, display_name=display_name, profile=profile)
    if not enterprise:
        return result
    principal = result.principal
    user, identity = proof
    if principal.linked_user_id not in (None, user.id):
        raise PermissionError("tenant_feishu_binding_changed")
    if principal.linked_user_id is None:
        if await db.scalar(select(ExternalPrincipalBindingEvent.id)
            .where(ExternalPrincipalBindingEvent.external_principal_id == principal.id).limit(1)):
            raise PermissionError("tenant_feishu_explicit_reconnect_required")
        principal.linked_user_id = user.id
        principal.binding_method = "feishu_tenant_identity"
        principal.binding_verified_at = principal.linked_at = datetime.now(UTC)
        db.add(ExternalPrincipalBindingEvent(tenant_id=tenant_id, external_principal_id=principal.id,
            action="linked", previous_user_id=None, new_user_id=user.id,
            reason="Current enterprise bot app-scoped identity", metadata_json={"external_identity_id": str(identity.id),
                "binding_method": principal.binding_method}))
        await _synchronize_external_principal_session_users(db, principal=principal, user=user)
        await db.flush()
    actor = await _runtime_actor(db, principal, expected_user_id=user.id)
    if not actor.authority_bound:
        raise PermissionError("tenant_feishu_binding_unavailable")
    return ExternalPrincipalResolution(principal=principal, actor=actor)


async def reconnect_tenant_feishu_principal(db, *, principal_id, user):
    from app.models.external_principal import ExternalPrincipal
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import ExternalPrincipalAuthorityError, _link_verified_external_principal

    principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.id == principal_id,
        ExternalPrincipal.tenant_id == user.tenant_id).with_for_update())
    config = await db.get(TenantChannelConfig, principal.tenant_channel_config_id) if principal and principal.tenant_channel_config_id else None
    proof = await resolve_tenant_feishu_identity(db, config=config, sender={"open_id": principal.subject_id}) if principal else None
    if (proof is None or proof[0].id != user.id or principal.linked_user_id not in (None, user.id)
        or principal.installation_ref != channel_installation_ref(config, fallback="")):
        raise ExternalPrincipalAuthorityError("Current app-scoped identity does not prove this member")
    if not await _binding_history_matches_user(db, principal=principal, user_id=user.id):
        raise ExternalPrincipalAuthorityError("This identity's prior account requires scoped administrator reconciliation")
    return await _link_verified_external_principal(db, tenant_id=user.tenant_id, principal_id=principal.id,
        user_id=user.id, actor_user_id=user.id, binding_method="feishu_tenant_identity",
        reason="Authenticated member explicitly reconnected the enterprise bot")


async def _binding_history_matches_user(db, *, principal, user_id):
    rows = list((await db.execute(select(ExternalPrincipalBindingEvent.previous_user_id, ExternalPrincipalBindingEvent.new_user_id)
        .where(ExternalPrincipalBindingEvent.external_principal_id == principal.id,
            ExternalPrincipalBindingEvent.tenant_id == principal.tenant_id))).all())
    owners = {value for row in rows for value in row if value is not None}
    # A live directory remap and an employee grant cannot transfer old private work.
    return not rows or owners == {user_id}


async def current_member_feishu_status(db, *, user):
    from app.models.external_principal import ExternalPrincipal
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import load_external_runtime_actor

    result = {"user_id": user.id, "tenant_id": user.tenant_id, "state": "company_bot_unconfigured",
        "principal_id": None, "entries": [], "disconnectable": []}
    config = await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == user.tenant_id,
        TenantChannelConfig.channel_type == "feishu"))
    if config is None:
        return result
    saved_links = await db.scalars(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == user.tenant_id,
        ExternalPrincipal.provider == "feishu", ExternalPrincipal.tenant_channel_config_id == config.id,
        ExternalPrincipal.linked_user_id == user.id, ExternalPrincipal.status == "active"))
    result["disconnectable"] = [{"principal_id": row.id, "subject_id": row.subject_id} for row in saved_links]
    if not config.is_active:
        return {**result, "state": "company_bot_paused"}
    if not config.app_id or not config.app_secret or not (config.encrypt_key or config.verification_token):
        return {**result, "state": "company_bot_setup_required"}
    provider = await db.scalar(select(IdentityProvider).where(IdentityProvider.tenant_id == user.tenant_id,
        IdentityProvider.provider_type == "feishu"))
    if provider is None or not provider.is_active:
        return {**result, "state": "identity_provider_required"}
    if not _same_application(provider, config):
        return {**result, "state": "identity_app_mismatch"}
    identities = list(await db.scalars(select(ExternalIdentity).where(ExternalIdentity.provider_id == provider.id,
        ExternalIdentity.user_id == user.id).order_by(ExternalIdentity.id)))
    if not identities:
        return {**result, "state": "identity_required"}
    for identity in identities:
        if not identity.provider_open_id:
            continue
        proof = await resolve_tenant_feishu_identity(db, config=config, sender={"open_id": identity.provider_open_id})
        entry = {"subject_id": identity.provider_open_id, "principal_id": None, "state": "identity_conflict"}
        if proof is not None and proof[0].id == user.id:
            principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == user.tenant_id,
                ExternalPrincipal.provider == "feishu", ExternalPrincipal.tenant_channel_config_id == config.id,
                ExternalPrincipal.installation_ref == channel_installation_ref(config, fallback=""),
                ExternalPrincipal.subject_id == identity.provider_open_id))
            if principal is None:
                entry["state"] = "awaiting_first_message"
            elif (principal.linked_user_id in (None, user.id)
                and await _binding_history_matches_user(db, principal=principal, user_id=user.id)):
                entry["principal_id"] = principal.id
                if principal.status != "active":
                    entry["state"] = "installation_revoked"
                else:
                    actor = await load_external_runtime_actor(db, tenant_id=user.tenant_id,
                        principal_id=principal.id, expected_user_id=user.id)
                    entry["state"] = "bound" if actor.authority_bound else "reconnect_required"
        result["entries"].append(entry)
    if not result["entries"]:
        return {**result, "state": "identity_required"}
    result["state"] = result["entries"][0]["state"] if len(result["entries"]) == 1 else "multiple_identities"
    if len(result["entries"]) == 1:
        result["principal_id"] = result["entries"][0]["principal_id"]
    if await _tenant_member_assistant(db, tenant_id=user.tenant_id, user=user) is None:
        result["assistant_required"] = True
        if result["state"] == "awaiting_first_message":
            result["state"] = "assistant_required"
    return result
