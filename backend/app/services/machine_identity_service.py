"""Tenant- and employee-scoped machine credentials, separate from user login."""
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.permissions import effective_agent_owner_id, is_scoped_business_admin, require_agent_owner_or_admin
from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.external_principal import ExternalPrincipal
from app.models.machine_credential import MachineCredential
from app.models.tenant import Tenant
from app.models.user import User

MACHINE_SCOPES = frozenset({'agent.describe'})


def _now():
    return datetime.now(timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


async def _owner_scope(db, agent):
    owner_id = effective_agent_owner_id(agent)
    owner = await db.get(User, owner_id, populate_existing=True) if owner_id else None
    if owner is None or not owner.is_active or owner.tenant_id != agent.tenant_id:
        raise HTTPException(409, detail={'code': 'machine_owner_unavailable'})
    epoch = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.tenant_id == agent.tenant_id,
        AuditLog.agent_id == agent.id, AuditLog.action == 'agent:handover'))
    return owner.id, int(epoch or 0)


def _view(credential, principal, owner_scope, *, agent_available=True):
    effective = credential.status
    if effective == 'active':
        effective = ('revoked' if principal.status != 'active' else 'expired' if credential.expires_at <= _now()
            else 'unavailable' if not agent_available or owner_scope is None
            else 'needs_reissue' if owner_scope != (credential.owner_user_id, credential.owner_epoch) else 'active')
    return {'id': str(principal.id), 'credential_id': str(credential.id), 'agent_id': str(credential.agent_id),
        'issue_request_id': str(credential.issue_request_id),
        'name': principal.display_name, 'status': effective, 'scopes': list(credential.scopes),
        'expires_at': credential.expires_at, 'last_used_at': credential.last_used_at,
        'revoked_at': credential.revoked_at, 'created_at': credential.created_at}


def _request_frame(agent, actor, expected_user_id, expected_tenant_id):
    if actor.id != expected_user_id or agent.tenant_id != expected_tenant_id:
        raise HTTPException(409, detail={'code': 'machine_request_scope_changed'})


async def issue_machine_identity(db, *, agent_id, actor, name, expires_at, scopes, issue_request_id,
    expected_user_id, expected_tenant_id):
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _request_frame(agent, actor, expected_user_id, expected_tenant_id)
    if not is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id):
        raise HTTPException(403, detail={'code': 'machine_identity_issuer_required'})
    if not scopes or not set(scopes).issubset(MACHINE_SCOPES):
        raise HTTPException(422, detail={'code': 'machine_scope_unsupported'})
    if expires_at.tzinfo is None:
        raise HTTPException(422, detail={'code': 'machine_expiry_required'})
    if agent.deleted_at is not None or agent.deactivated_at is not None:
        raise HTTPException(409, detail={'code': 'machine_agent_unavailable'})
    owner_id, epoch = await _owner_scope(db, agent)
    request_hash = _hash(json.dumps({'tenant_id': str(agent.tenant_id), 'agent_id': str(agent.id),
        'actor_id': str(actor.id), 'name': name, 'expires_at': expires_at.astimezone(timezone.utc).isoformat(),
        'scopes': sorted(set(scopes))}, sort_keys=True, separators=(',', ':')))
    previous = await db.scalar(select(MachineCredential).where(MachineCredential.tenant_id == agent.tenant_id,
        MachineCredential.agent_id == agent.id, MachineCredential.issued_by_user_id == actor.id,
        MachineCredential.issue_request_id == issue_request_id))
    if previous is not None:
        if previous.request_hash != request_hash:
            raise HTTPException(409, detail={'code': 'machine_request_conflict'})
        principal = await db.get(ExternalPrincipal, previous.principal_id)
        return {**_view(previous, principal, (owner_id, epoch)), 'token': None, 'idempotent_replay': True,
            'secret_status': 'unavailable_after_issue', 'next_action': 'revoke_and_reissue'}
    if expires_at <= _now():
        raise HTTPException(422, detail={'code': 'machine_expiry_required'})
    principal_id, credential_id = uuid.uuid4(), uuid.uuid4()
    token = f'hmi_{agent.tenant_id.hex}_{credential_id.hex}_{secrets.token_urlsafe(32)}'
    principal = ExternalPrincipal(id=principal_id, tenant_id=agent.tenant_id, provider='a2a',
        installation_ref=f'machine:{principal_id}', subject_id=str(principal_id), display_name=name,
        profile_json={'kind': 'machine_service', 'agent_id': str(agent.id)}, status='active')
    db.add(principal)
    await db.flush()
    credential = MachineCredential(id=credential_id, tenant_id=agent.tenant_id, agent_id=agent.id,
        principal_id=principal.id, issued_by_user_id=actor.id, owner_user_id=owner_id, owner_epoch=epoch,
        issue_request_id=issue_request_id, request_hash=request_hash, token_hash=_hash(token),
        scopes=sorted(set(scopes)), status='active', expires_at=expires_at)
    db.add(credential)
    db.add(AuditLog(tenant_id=agent.tenant_id, agent_id=agent.id, user_id=actor.id,
        external_principal_id=principal.id, action='agent_machine_identity_issued', details={
            'credential_id': str(credential_id), 'scopes': credential.scopes, 'owner_epoch': epoch,
            'owner_user_id': str(owner_id), 'expires_at': expires_at.isoformat()}))
    await db.commit()
    return {**_view(credential, principal, (owner_id, epoch)), 'token': token, 'idempotent_replay': False,
        'secret_status': 'available_once'}


async def list_machine_identities(db, *, agent_id, actor):
    agent = await require_agent_owner_or_admin(db, actor, agent_id)
    try:
        owner_scope = await _owner_scope(db, agent)
    except HTTPException:
        owner_scope = None
    rows = (await db.execute(select(MachineCredential, ExternalPrincipal).join(ExternalPrincipal,
        ExternalPrincipal.id == MachineCredential.principal_id).where(MachineCredential.tenant_id == agent.tenant_id,
        MachineCredential.agent_id == agent.id, ExternalPrincipal.tenant_id == agent.tenant_id,
        ExternalPrincipal.provider == 'a2a').order_by(MachineCredential.created_at.desc(), MachineCredential.id))).all()
    return {'agent_id': str(agent.id), 'can_issue': is_scoped_business_admin(actor, resource_tenant_id=agent.tenant_id),
        'supported_scopes': sorted(MACHINE_SCOPES), 'identities': [_view(c, p, owner_scope,
            agent_available=agent.deactivated_at is None) for c, p in rows]}


async def revoke_machine_identity(db, *, agent_id, principal_id, actor, expected_user_id, expected_tenant_id):
    agent = await require_agent_owner_or_admin(db, actor, agent_id, lock=True)
    _request_frame(agent, actor, expected_user_id, expected_tenant_id)
    credentials = list(await db.scalars(select(MachineCredential).where(MachineCredential.tenant_id == agent.tenant_id,
        MachineCredential.agent_id == agent.id, MachineCredential.principal_id == principal_id)
        .order_by(MachineCredential.id).with_for_update().execution_options(populate_existing=True)))
    principal = await db.get(ExternalPrincipal, principal_id, populate_existing=True)
    if not credentials or principal is None or principal.tenant_id != agent.tenant_id or principal.provider != 'a2a':
        raise HTTPException(404, detail='Machine identity not found')
    changed = principal.status != 'revoked' or any(c.status != 'revoked' for c in credentials)
    now = _now()
    for credential in credentials:
        credential.status = 'revoked'
        credential.revoked_at = credential.revoked_at or now
    principal.status = 'revoked'
    principal.revoked_at = principal.revoked_at or now
    if changed:
        db.add(AuditLog(tenant_id=agent.tenant_id, agent_id=agent.id, user_id=actor.id,
            external_principal_id=principal.id, action='agent_machine_identity_revoked',
            details={'credential_ids': [str(c.id) for c in credentials]}))
    await db.commit()
    return {'id': str(principal.id), 'agent_id': str(agent.id), 'status': 'revoked', 'revoked_at': principal.revoked_at}


@dataclass(frozen=True)
class MachineAuthContext:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    principal_id: uuid.UUID
    credential_id: uuid.UUID
    scopes: tuple[str, ...]


async def authenticate_machine(db, *, token, agent_id, scope):
    denied = HTTPException(401, detail='Invalid or expired machine credential', headers={'WWW-Authenticate': 'Bearer'})
    try:
        prefix, tenant_hex, key_hex, secret = token.split('_', 3)
        if prefix != 'hmi' or len(tenant_hex) != 32 or len(key_hex) != 32 or len(secret) != 43:
            raise ValueError('Invalid credential syntax')
        tenant_id, key_id = uuid.UUID(hex=tenant_hex), uuid.UUID(hex=key_hex)
    except (ValueError, AttributeError):
        raise denied from None
    # Candidate routing selects only an exact credential row; it is not authority.
    await pin_rls_tenant_context(db, tenant_id)
    candidate = await db.get(MachineCredential, key_id, populate_existing=True)
    if candidate is None or candidate.tenant_id != tenant_id or not hmac.compare_digest(candidate.token_hash, _hash(token)):
        raise denied
    if candidate.agent_id != agent_id or scope not in set(candidate.scopes):
        raise HTTPException(403, detail={'code': 'machine_scope_denied'})
    agent = await db.scalar(select(Agent).join(Tenant, Tenant.id == Agent.tenant_id).where(Agent.id == agent_id,
        Agent.tenant_id == tenant_id, Tenant.is_active.is_(True), Agent.deleted_at.is_(None),
        Agent.deactivated_at.is_(None)).with_for_update(read=True, of=Agent).execution_options(populate_existing=True))
    if agent is None:
        raise denied
    credential = await db.scalar(select(MachineCredential).where(MachineCredential.id == key_id,
        MachineCredential.tenant_id == tenant_id).with_for_update().execution_options(populate_existing=True))
    if credential is None or not hmac.compare_digest(credential.token_hash, _hash(token)):
        raise denied
    if credential.agent_id != agent.id or scope not in set(credential.scopes):
        raise HTTPException(403, detail={'code': 'machine_scope_denied'})
    principal = await db.get(ExternalPrincipal, credential.principal_id, populate_existing=True)
    try:
        owner_scope = await _owner_scope(db, agent)
    except HTTPException:
        raise denied from None
    if (credential.status != 'active' or credential.expires_at <= _now()
        or principal is None or principal.tenant_id != tenant_id or principal.provider != 'a2a'
        or principal.linked_user_id is not None or principal.status != 'active'
        or owner_scope != (credential.owner_user_id, credential.owner_epoch)):
        raise denied
    credential.last_used_at = _now()
    return MachineAuthContext(tenant_id, agent.id, principal.id, credential.id, tuple(credential.scopes)), agent
