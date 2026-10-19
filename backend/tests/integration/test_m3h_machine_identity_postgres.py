"""Machine credentials have real tenant scope without becoming user login tokens."""
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, func

from app.api import advanced, agents
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.external_principal import ExternalPrincipal
from app.models.machine_credential import MachineCredential
from app.models.user import User
from app.services.local_bridge_service import utcnow
from tests.integration.test_local_agent_channel_protocol import _seed

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture
async def machine_api(owner_sessionmaker, app_user_sessionmaker):
    from app.models import import_all_models
    import_all_models()
    tenant, owner, agent, _context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, owner)
        user.role = 'org_admin'
        await db.commit()
        token = create_access_token(str(user.id), user.role, str(user.tenant_id))
    app = FastAPI()
    app.include_router(agents.router)
    app.include_router(advanced.router)
    async def database():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local',
        headers={'Authorization': 'Bearer ' + token}) as api:
        yield {'tenant': tenant, 'owner': owner, 'agent': agent, 'user_token': token}, api


def _issue(seed):
    return {'name': 'Partner service', 'scopes': ['agent.describe'], 'expires_at': (utcnow() + timedelta(days=7)).isoformat(),
        'issue_request_id': str(uuid4()), 'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])}


async def test_admin_issues_once_machine_uses_exact_target_and_owner_revokes(machine_api, app_user_sessionmaker):
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/machine-identities"
    body = _issue(seed)
    created = await api.post(path, json=body)
    assert created.status_code == 201, created.text
    identity = created.json()
    token = identity['token']
    assert token.startswith('hmi_') and identity['secret_status'] == 'available_once'
    repeated = await api.post(path, json=body)
    assert repeated.status_code == 200 and repeated.json()['token'] is None
    assert repeated.json()['credential_id'] == identity['credential_id'] and repeated.json()['idempotent_replay']
    listed = await api.get(path)
    assert listed.status_code == 200 and token not in listed.text and 'token_hash' not in listed.text
    machine = await api.get(f"/agents/{seed['agent']}/machine-description", headers={'Authorization': 'Bearer ' + token})
    assert machine.status_code == 200 and machine.json()['agent']['id'] == str(seed['agent'])
    transport = machine.json()['interoperability']['a2a_tasks']
    assert transport['status'] == 'available' and transport['methods'] == {}
    assert transport['rpc_url'] == f"http://local/agents/{seed['agent']}/a2a/rpc"
    assert 'owner_user_id' not in machine.text and 'sponsor_user_id' not in machine.text
    personal = await api.get(f"/agents/{seed['agent']}", headers={'Authorization': 'Bearer ' + token})
    assert personal.status_code == 401
    revoked = await api.post(path + f"/{identity['id']}/revoke", json={
        'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
    assert revoked.status_code == 200
    machine = await api.get(f"/agents/{seed['agent']}/machine-description", headers={'Authorization': 'Bearer ' + token})
    assert machine.status_code == 401
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        credential, = list(await db.scalars(select(MachineCredential).where(MachineCredential.agent_id == seed['agent'])))
        assert credential.token_hash != token and credential.status == 'revoked'
        principal = await db.get(ExternalPrincipal, credential.principal_id)
        assert principal.provider == 'a2a' and principal.linked_user_id is None and principal.status == 'revoked'
        issued = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.agent_id == seed['agent'],
            AuditLog.action == 'agent_machine_identity_issued'))
        assert issued == 1


async def test_stale_admin_claim_does_not_issue_and_current_owner_can_revoke(machine_api, app_user_sessionmaker):
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/machine-identities"
    created = await api.post(path, json=_issue(seed))
    assert created.status_code == 201
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, seed['owner'])
        user.role = 'member'
        await db.commit()
    denied = await api.post(path, json=_issue(seed))
    assert denied.status_code == 403
    listed = await api.get(path)
    assert listed.status_code == 200 and not listed.json()['can_issue']
    identity_id = created.json()['id']
    body = {'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])}
    first = await api.post(path + f'/{identity_id}/revoke', json=body)
    again = await api.post(path + f'/{identity_id}/revoke', json=body)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        revoked = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.agent_id == seed['agent'],
            AuditLog.action == 'agent_machine_identity_revoked'))
        assert revoked == 1


@pytest.mark.parametrize('field', ['expected_user_id', 'expected_tenant_id'])
async def test_issue_scope_change_has_no_side_effect(machine_api, app_user_sessionmaker, field):
    seed, api = machine_api
    body = _issue(seed)
    body[field] = str(uuid4())
    denied = await api.post(f"/agents/{seed['agent']}/machine-identities", json=body)
    assert denied.status_code == 409
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        assert await db.scalar(select(func.count(MachineCredential.id)).where(MachineCredential.agent_id == seed['agent'])) == 0


async def test_changed_issue_nonce_and_expired_readback_do_not_mint_new_keys(machine_api, monkeypatch):
    from app.services import machine_identity_service as service
    seed, api = machine_api
    path = f"/agents/{seed['agent']}/machine-identities"
    body = _issue(seed)
    created = await api.post(path, json=body)
    assert created.status_code == 201
    changed = await api.post(path, json={**body, 'name': 'A different service'})
    assert changed.status_code == 409
    future = utcnow() + timedelta(days=8)
    monkeypatch.setattr(service, '_now', lambda: future)
    repeated = await api.post(path, json=body)
    assert repeated.status_code == 200 and repeated.json()['status'] == 'expired' and repeated.json()['token'] is None
    fresh = await api.post(path, json={**body, 'issue_request_id': str(uuid4())})
    assert fresh.status_code == 422
    listed = await api.get(path)
    assert len(listed.json()['identities']) == 1


@pytest.mark.parametrize('invalidated', ['expiry', 'owner', 'employee', 'company'])
async def test_machine_authority_checks_current_real_state(machine_api, app_user_sessionmaker, invalidated):
    seed, api = machine_api
    created = await api.post(f"/agents/{seed['agent']}/machine-identities", json=_issue(seed))
    assert created.status_code == 201
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        if invalidated == 'expiry':
            credential = await db.scalar(select(MachineCredential).where(MachineCredential.agent_id == seed['agent']))
            credential.expires_at = utcnow() - timedelta(seconds=1)
        elif invalidated == 'owner':
            owner = await db.get(User, seed['owner'])
            owner.is_active = False
        elif invalidated == 'employee':
            agent = await db.get(Agent, seed['agent'])
            agent.deactivated_at = utcnow()
        else:
            tenant = await db.get(Tenant, seed['tenant'])
            tenant.is_active = False
        await db.commit()
    response = await api.get(f"/agents/{seed['agent']}/machine-description",
        headers={'Authorization': 'Bearer ' + created.json()['token']})
    assert response.status_code == 401
    if invalidated == 'employee':
        listed = await api.get(f"/agents/{seed['agent']}/machine-identities")
        assert listed.status_code == 200 and listed.json()['identities'][0]['status'] == 'unavailable'


async def test_machine_cannot_change_target_or_tenant_and_rls_hides_its_key(
    machine_api, owner_sessionmaker, app_user_sessionmaker,
):
    seed, api = machine_api
    created = await api.post(f"/agents/{seed['agent']}/machine-identities", json=_issue(seed))
    assert created.status_code == 201
    identity = created.json()
    other_tenant, _owner, other_agent, _context = await _seed(owner_sessionmaker)
    wrong = await api.get(f'/agents/{other_agent}/machine-description', headers={'Authorization': 'Bearer ' + identity['token']})
    assert wrong.status_code == 403
    async with tenant_scoped_session(other_tenant, session_factory=app_user_sessionmaker) as db:
        visible = await db.scalar(select(MachineCredential).where(MachineCredential.agent_id == seed['agent']))
        assert visible is None
    prefix, _tenant, key, secret = identity['token'].split('_', 3)
    forged = await api.get(f"/agents/{seed['agent']}/machine-description",
        headers={'Authorization': f'Bearer {prefix}_{other_tenant.hex}_{key}_{secret}'})
    assert forged.status_code == 401


async def test_real_handover_and_transfer_back_do_not_revive_a_machine_grant(machine_api, app_user_sessionmaker):
    seed, api = machine_api
    created = await api.post(f"/agents/{seed['agent']}/machine-identities", json=_issue(seed))
    assert created.status_code == 201
    successor = uuid4()
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        db.add(User(id=successor, tenant_id=seed['tenant'], username=f'successor-{successor}',
            email=f'{successor}@machine-test.local', display_name='New accountable owner',
            password_hash='synthetic-unused-password', role='member', is_active=True))
        await db.commit()
    current = seed['owner']
    for next_owner in (successor, seed['owner']):
        transferred = await api.post(f"/agents/{seed['agent']}/handover", json={'new_owner_id': str(next_owner),
            'expected_owner_id': str(current), 'reason': 'Transfer real machine grant ownership', 'request_id': str(uuid4())})
        assert transferred.status_code == 200, transferred.text
        refused = await api.get(f"/agents/{seed['agent']}/machine-description",
            headers={'Authorization': 'Bearer ' + created.json()['token']})
        assert refused.status_code == 401
        current = next_owner
    listed = await api.get(f"/agents/{seed['agent']}/machine-identities")
    assert listed.json()['identities'][0]['status'] == 'needs_reissue'


async def test_actual_a2a_tab_issues_uses_and_revokes_machine_access(machine_api, app_user_sessionmaker, tmp_path):
    from app.api import a2a
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
    seed, _api = machine_api
    web = FastAPI()
    web.include_router(agents.router, prefix='/api')
    web.include_router(a2a.router, prefix='/api')
    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = database
    await _exercise_dom_bridge(web, {'user_id': seed['owner'], 'tenant_id': seed['tenant'], 'agent_id': seed['agent']},
        tmp_path, entry_file='e2e/machine-identity-native.tsx', extra_env={'HIVE_M3H_TOKEN': seed['user_token']})
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        credential, = list(await db.scalars(select(MachineCredential).where(MachineCredential.agent_id == seed['agent'])))
        assert credential.status == 'revoked' and credential.last_used_at is not None
        principal = await db.get(ExternalPrincipal, credential.principal_id)
        assert principal.linked_user_id is None


async def test_issuer_and_accountable_owner_remain_distinct(machine_api, app_user_sessionmaker):
    seed, api = machine_api
    issuer = uuid4()
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        db.add(User(id=issuer, tenant_id=seed['tenant'], username=f'issuer-{issuer}', email=f'{issuer}@machine-test.local',
            display_name='Company credential issuer', password_hash='synthetic-unused-password', role='org_admin', is_active=True))
        owner = await db.get(User, seed['owner'])
        owner.role = 'member'
        await db.commit()
        issuer_token = create_access_token(str(issuer), 'org_admin', str(seed['tenant']))
    body = {**_issue(seed), 'expected_user_id': str(issuer)}
    created = await api.post(f"/agents/{seed['agent']}/machine-identities", json=body,
        headers={'Authorization': 'Bearer ' + issuer_token})
    assert created.status_code == 201, created.text
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        credential = await db.scalar(select(MachineCredential).where(MachineCredential.agent_id == seed['agent']))
        assert credential.issued_by_user_id == issuer and credential.owner_user_id == seed['owner']
        audit = await db.scalar(select(AuditLog).where(AuditLog.agent_id == seed['agent'],
            AuditLog.action == 'agent_machine_identity_issued'))
        assert audit.user_id == issuer and audit.external_principal_id == credential.principal_id
        owner = await db.get(User, seed['owner'])
        owner.is_active = False
        await db.commit()
    unavailable = await api.get(f"/agents/{seed['agent']}/machine-identities", headers={'Authorization': 'Bearer ' + issuer_token})
    assert unavailable.status_code == 200 and unavailable.json()['identities'][0]['status'] == 'unavailable'
    async with tenant_scoped_session(seed['tenant'], session_factory=app_user_sessionmaker) as db:
        owner = await db.get(User, seed['owner'])
        owner.is_active = True
        await db.commit()
    revoked = await api.post(f"/agents/{seed['agent']}/machine-identities/{created.json()['id']}/revoke",
        json={'expected_user_id': str(seed['owner']), 'expected_tenant_id': str(seed['tenant'])})
    assert revoked.status_code == 200
