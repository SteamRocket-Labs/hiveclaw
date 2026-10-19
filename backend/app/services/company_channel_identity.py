"""Self-issued company channel proof, separate from directory administration."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.external_principal import ExternalPrincipal, ExternalPrincipalBindingEvent
from app.models.identity import IdentityProvider
from app.models.org import OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.tenant_channel_config import CompanyChannelBindingChallenge, TenantChannelConfig
from app.models.user import User
from app.services.channel_ingress_inbox import channel_installation_ref
from app.services.identity_links import _bindings_for_providers, link_version, recheck_stored_link

COMMAND_PREFIX = '/hive-connect '


def company_channel_ready(config):
    extra = (config.extra_config or {}) if config else {}
    if not config or not config.is_configured or extra.get('messaging_enabled') is not True:
        return False
    if config.channel_type == 'wecom':
        import base64
        agent_id, key = str(extra.get('wecom_agent_id') or ''), config.encrypt_key or ''
        try:
            valid_key = len(key) == 43 and len(base64.b64decode(key + '=', validate=True)) == 32
        except ValueError:
            valid_key = False
        return bool(config.verification_token and valid_key and agent_id.isascii() and agent_id.isdecimal() and int(agent_id) > 0)
    return config.channel_type == 'dingtalk' and bool(str(extra.get('corp_id') or '').strip())


async def current_company_config(db, tenant_id, provider, *, lock=False):
    if provider not in {'wecom', 'dingtalk'}:
        raise HTTPException(404, detail={'code': 'company_channel_provider_unavailable'})
    if lock:
        from app.api.tenant_channels import _lock_config
        await _lock_config(db, tenant_id, provider)
    return await db.scalar(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == tenant_id,
        TenantChannelConfig.channel_type == provider).execution_options(populate_existing=True))


async def _current_link(db, *, config, link, user_id):
    if not company_channel_ready(config) or link is None or link.tenant_id != config.tenant_id or link.user_id != user_id:
        return None
    user = await db.scalar(select(User).where(User.id == user_id, User.tenant_id == config.tenant_id,
        User.is_active.is_(True)).execution_options(populate_existing=True))
    provider = await db.scalar(select(IdentityProvider).where(IdentityProvider.id == link.provider_id,
        IdentityProvider.tenant_id == config.tenant_id, IdentityProvider.provider_type == config.channel_type,
        IdentityProvider.is_active.is_(True)).execution_options(populate_existing=True))
    if user is None or provider is None or provider.config != {'connection_id': str(config.id), 'app_id': config.app_id}:
        return None
    member = await db.scalar(select(OrgMember).where(OrgMember.id == link.org_member_id,
        OrgMember.tenant_id == config.tenant_id, OrgMember.status == 'active').execution_options(populate_existing=True))
    bindings = await _bindings_for_providers(db, [provider.id], actor_id=user.id)
    valid, _reason = recheck_stored_link(link, member=member, user=user, provider=provider, bindings=bindings)
    return user if valid and link.subject_kind == 'provider_user_id' else None


async def _member_links(db, *, config, user_id):
    links = list(await db.scalars(select(OrgMemberUserLink).where(OrgMemberUserLink.tenant_id == config.tenant_id,
        OrgMemberUserLink.user_id == user_id, OrgMemberUserLink.status == 'active',
        OrgMemberUserLink.subject_kind == 'provider_user_id').execution_options(populate_existing=True)))
    return [link for link in links if await _current_link(db, config=config, link=link, user_id=user_id)]


async def company_principal(db, config, subject_id):
    return await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == config.tenant_id,
        ExternalPrincipal.provider == config.channel_type, ExternalPrincipal.tenant_channel_config_id == config.id,
        ExternalPrincipal.installation_ref == channel_installation_ref(config, fallback=''),
        ExternalPrincipal.subject_id == subject_id).execution_options(populate_existing=True))


async def _has_other_holder(db, principal, user_id):
    if principal.linked_user_id not in (None, user_id):
        return True
    history = await db.scalar(select(ExternalPrincipalBindingEvent.id).where(
        ExternalPrincipalBindingEvent.external_principal_id == principal.id,
        or_(ExternalPrincipalBindingEvent.previous_user_id != user_id,
            ExternalPrincipalBindingEvent.new_user_id != user_id)).limit(1))
    return history is not None


async def _binding_history_version(db, principal):
    event_ids = list(await db.scalars(select(ExternalPrincipalBindingEvent.id).where(
        ExternalPrincipalBindingEvent.external_principal_id == principal.id).order_by(ExternalPrincipalBindingEvent.id))) if principal else []
    return hashlib.sha256('\n'.join(map(str, event_ids)).encode()).hexdigest()


async def issue_company_challenge(db, *, user, provider):
    config = await current_company_config(db, user.tenant_id, provider, lock=True)
    if not company_channel_ready(config):
        raise HTTPException(409, detail={'code': 'company_channel_setup_required'})
    links = await _member_links(db, config=config, user_id=user.id)
    if len(links) != 1:
        raise HTTPException(409, detail={'code': 'company_channel_identity_ambiguous' if links else 'company_channel_directory_link_required'})
    link = await db.scalar(select(OrgMemberUserLink).where(OrgMemberUserLink.id == links[0].id).with_for_update()
        .execution_options(populate_existing=True))
    if not await _current_link(db, config=config, link=link, user_id=user.id):
        raise HTTPException(409, detail={'code': 'company_channel_identity_changed'})
    principal = await company_principal(db, config, link.match_subject)
    if principal is not None:
        if principal.status != 'active':
            raise HTTPException(409, detail={'code': 'company_channel_installation_revoked'})
        if await _has_other_holder(db, principal, user.id):
            raise HTTPException(409, detail={'code': 'company_channel_identity_history_conflict'})
    token = secrets.token_urlsafe(32)
    challenge = CompanyChannelBindingChallenge(tenant_id=user.tenant_id, user_id=user.id, config_id=config.id,
        identity_link_id=link.id, link_version=link_version(link), provider=provider,
        binding_history_version=await _binding_history_version(db, principal),
        installation_ref=channel_installation_ref(config, fallback=''), subject_id=link.match_subject,
        token_sha256=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    db.add(challenge)
    await db.flush()
    return {'challenge_id': str(challenge.id), 'command': COMMAND_PREFIX + token,
        'expires_at': challenge.expires_at.isoformat(), 'tenant_id': str(user.tenant_id), 'user_id': str(user.id),
        'subject_id': link.match_subject}


async def company_binding_proof(db, *, principal, user_id):
    if principal.provider not in {'dingtalk', 'wecom'} or principal.channel_config_id is not None or not principal.tenant_channel_config_id:
        return None
    config = await db.get(TenantChannelConfig, principal.tenant_channel_config_id, populate_existing=True)
    if not company_channel_ready(config) or principal.tenant_id != config.tenant_id or principal.provider != config.channel_type:
        return None
    if principal.installation_ref != channel_installation_ref(config, fallback=''):
        return None
    proof = await db.scalar(select(CompanyChannelBindingChallenge).where(
        CompanyChannelBindingChallenge.tenant_id == principal.tenant_id,
        CompanyChannelBindingChallenge.principal_id == principal.id, CompanyChannelBindingChallenge.user_id == user_id,
        CompanyChannelBindingChallenge.config_id == config.id,
        CompanyChannelBindingChallenge.consumed_at.is_not(None)).order_by(CompanyChannelBindingChallenge.consumed_at.desc()).limit(1))
    if (proof is None or proof.provider != principal.provider or proof.subject_id != principal.subject_id
        or proof.installation_ref != principal.installation_ref):
        return None
    link = await db.get(OrgMemberUserLink, proof.identity_link_id, populate_existing=True)
    if link is None or link_version(link) != proof.link_version or link.match_subject != principal.subject_id:
        return None
    user = await _current_link(db, config=config, link=link, user_id=user_id)
    return (user, proof) if user is not None else None


async def redeem_company_challenge(db, *, config, subject_id, text):
    """Called only after the concrete provider transport authenticated the sender."""
    if not text.startswith(COMMAND_PREFIX):
        return None
    token = text[len(COMMAND_PREFIX):]
    if not token or any(char.isspace() for char in token) or len(token) > 100:
        return {'state': 'verification_invalid'}
    proof = await db.scalar(select(CompanyChannelBindingChallenge).where(
        CompanyChannelBindingChallenge.tenant_id == config.tenant_id,
        CompanyChannelBindingChallenge.config_id == config.id,
        CompanyChannelBindingChallenge.token_sha256 == hashlib.sha256(token.encode()).hexdigest()).with_for_update()
        .execution_options(populate_existing=True))
    if (proof is None or proof.provider != config.channel_type or proof.subject_id != subject_id
        or proof.installation_ref != channel_installation_ref(config, fallback='') or not company_channel_ready(config)):
        return {'state': 'verification_invalid'}
    if proof.consumed_at is not None:
        principal = await db.get(ExternalPrincipal, proof.principal_id, populate_existing=True)
        if principal is not None and principal.status != 'active':
            return {'state': 'installation_revoked'}
        verified = await company_binding_proof(db, principal=principal, user_id=proof.user_id) if principal else None
        return {'state': 'bound' if verified and principal.linked_user_id == proof.user_id else 'reconnect_required'}
    if proof.expires_at <= datetime.now(timezone.utc):
        return {'state': 'verification_expired'}
    link = await db.scalar(select(OrgMemberUserLink).where(OrgMemberUserLink.id == proof.identity_link_id).with_for_update()
        .execution_options(populate_existing=True))
    user = await _current_link(db, config=config, link=link, user_id=proof.user_id)
    if user is None or link_version(link) != proof.link_version or link.match_subject != subject_id:
        return {'state': 'identity_changed'}
    from app.services.external_principal_service import _link_verified_external_principal, resolve_or_create_external_principal

    from app.services.external_principal_service import ExternalPrincipalRevokedError
    try:
        resolution = await resolve_or_create_external_principal(db, tenant_id=config.tenant_id,
            provider=config.channel_type, installation_ref=proof.installation_ref, tenant_channel_config_id=config.id,
            subject_id=subject_id, display_name=user.display_name or user.username, profile={})
    except ExternalPrincipalRevokedError:
        return {'state': 'installation_revoked'}
    principal = resolution.principal
    if await _binding_history_version(db, principal) != proof.binding_history_version:
        return {'state': 'verification_changed'}
    if await _has_other_holder(db, principal, user.id):
        return {'state': 'binding_history_conflict'}
    proof.principal_id, proof.consumed_at = principal.id, datetime.now(timezone.utc)
    await db.flush()
    bound = await _link_verified_external_principal(db, tenant_id=config.tenant_id, principal_id=principal.id,
        user_id=user.id, actor_user_id=user.id, binding_method='company_channel_challenge',
        reason='Authenticated member challenge redeemed by the matching provider sender')
    if not bound.actor.authority_bound:
        raise PermissionError('company_channel_binding_changed')
    return {'state': 'bound', 'principal_id': str(principal.id)}


async def current_company_identity(db, *, user, provider):
    config = await current_company_config(db, user.tenant_id, provider)
    result = {'tenant_id': str(user.tenant_id), 'user_id': str(user.id), 'provider': provider, 'principal_id': None}
    owned = list(await db.scalars(select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == user.tenant_id,
        ExternalPrincipal.provider == provider, ExternalPrincipal.linked_user_id == user.id,
        ExternalPrincipal.binding_method == 'company_channel_challenge')))
    result['disconnectable'] = [{'principal_id': str(row.id), 'subject_id': row.subject_id} for row in owned]
    if not company_channel_ready(config):
        return {**result, 'state': 'setup_required'}
    links = await _member_links(db, config=config, user_id=user.id)
    if len(links) != 1:
        return {**result, 'state': 'identity_ambiguous' if links else 'directory_link_required'}
    principal = await company_principal(db, config, links[0].match_subject)
    if principal is None:
        return {**result, 'state': 'verification_required', 'subject_id': links[0].match_subject}
    if principal.status != 'active':
        return {**result, 'state': 'installation_revoked', 'subject_id': principal.subject_id}
    if await _has_other_holder(db, principal, user.id):
        return {**result, 'state': 'identity_history_conflict', 'subject_id': principal.subject_id}
    from app.services.external_principal_service import load_external_runtime_actor
    actor = await load_external_runtime_actor(db, tenant_id=user.tenant_id, principal_id=principal.id, expected_user_id=user.id)
    state = 'bound' if actor.authority_bound else 'installation_revoked' if principal.status != 'active' else 'reconnect_required'
    return {**result, 'state': state, 'subject_id': principal.subject_id, 'principal_id': str(principal.id)}
