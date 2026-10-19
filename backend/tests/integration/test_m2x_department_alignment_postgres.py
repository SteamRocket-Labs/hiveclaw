"""Observed company directories use the real explicit department authority path."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.org import OrgMember
from app.models.organization_identity import DepartmentDirectoryLink
from app.models.security_audit import ResourcePermission
from app.models.user import User
from tests.integration.test_m2w_company_directory_postgres import company_directory, _provider  # noqa: F401


def _single_department_provider(request, state):
    response = _provider(request, state)
    if request.url.path.endswith('/user/list'):
        ding = request.url.host == 'oapi.dingtalk.com'
        current = str(json.loads(request.content)['dept_id']) if ding else request.url.params['department_id']
        person = {'userid': 'same-id', 'name': 'Directory person', 'status': 1,
            'dept_id_list' if ding else 'department': [1, 2] if state.get('multi') else [2]}
        people = [person] if current == '2' or state.get('multi') else []
        return httpx.Response(200, json={'errcode': 0, 'result': {'list': people, 'has_more': False}} if ding
            else {'errcode': 0, 'userlist': people})
    return response


@pytest.mark.parametrize('provider', ['dingtalk', 'wecom'])
async def test_directory_mapping_cutover_permission_and_compare_restore(
    company_directory, owner_sessionmaker, app_user_sessionmaker, monkeypatch, provider,  # noqa: F811
):
    seed, client, state = company_directory, httpx.AsyncClient, {'calls': []}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(**{**kw, 'transport': httpx.MockTransport(lambda r: _single_department_provider(r, state))}))
    async with client(transport=httpx.ASGITransport(app=seed['app']), base_url='http://alignment') as api:
        synced = await api.post(f'/api/enterprise/org/sync?provider={provider}')
        assert synced.json()['status'] == 'complete', synced.text
        listing = (await api.get('/api/enterprise/org/department-links')).json()
        root, child = sorted(listing['org_departments'], key=lambda r: r['provider_department_id'] or '')
        assert root['proposal']['selectable'] is True, root
        assert root['provider'] == provider and root['provider_id'], listing
        early = await api.post('/api/enterprise/org/department-links', json={'selections': [{'action': 'create', 'org_department_id': child['org_department_id']}]})
        assert early.json()['rows'][0]['reason'] == 'missing_parent', early.text
        mapped = []
        for row in (root, child):
            response = await api.post('/api/enterprise/org/department-links', json={'selections': [{'action': 'create', 'org_department_id': row['org_department_id']}]})
            assert response.json()['counts']['department_mappings']['created'] == 1, response.text
            mapped.append(response.json()['rows'][0])
        target, mapping = mapped[1]['department_id'], mapped[1]['link_id']
        async with owner_sessionmaker() as db:
            member = await db.scalar(select(OrgMember).where(OrgMember.tenant_id == seed['tenant']))
            member_id, provider_id = member.id, member.provider_id
            link = await db.get(DepartmentDirectoryLink, UUID(mapping))
            assert str(link.provider_id) == str(provider_id) and link.provider == provider
            resource = uuid4()
            db.add(ResourcePermission(tenant_id=seed['tenant'], principal_type='department',
                principal_id=UUID(target), resource_type='agent_activity', resource_id=resource, actions=['read']))
            await db.commit()
        linked = await api.post('/api/enterprise/org/identity-links', json={'org_member_id': str(member_id),
            'provider_id': str(provider_id), 'user_id': str(seed['account']), 'subject_kind': 'provider_user_id'})
        assert linked.json()['applied'] is True, linked.text
        assigned = await api.post('/api/enterprise/org/department-cutover', json={'org_member_ids': [str(member_id)]})
        assert assigned.json()['counts']['user_assignments']['changed'] == 1, assigned.text
        listed = await api.get('/api/org/users', params={'department_id': target})
        assert str(seed['account']) in {r['id'] for r in listed.json()}
        from app.core.resource_authority import load_explicit_resource_grant_ids
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant'])
            user = await db.get(User, seed['account'])
            assert resource in await load_explicit_resource_grant_ids(db, user=user, resource_kind='agent_activity', action='read')
            assert (user.role, user.password_hash) == ('member', 'preserve-password')
        journal = (await api.get('/api/enterprise/org/cutover-journal')).json()
        assigned_journal = next(r for r in journal['rows'] if r['object_kind'] == 'user_department' and r['after']['department_id'] == target)
        restored = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [assigned_journal['id']]})
        assert restored.json()['counts']['user_assignments']['changed'] == 1, restored.text
        normal = await api.post(f'/api/enterprise/org/sync?provider={provider}')
        assert normal.json()['assignment_counts']['changed'] == 1, normal.text
        repaired = await api.post('/api/enterprise/org/department-assignments/repair', json={
            'user_id': str(seed['account']), 'department_id': mapped[0]['department_id']})
        assert repaired.json()['counts']['user_assignments']['changed'] == 1, repaired.text
        normal = await api.post(f'/api/enterprise/org/sync?provider={provider}')
        assert normal.json()['assignment_counts']['conflict'] == 1, normal.text
        stale = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [assigned_journal['id']]})
        assert stale.json()['rows'][0]['reason'] == 'rollback_conflict', stale.text
        revoked = await api.post('/api/enterprise/org/department-links', json={'selections': [{'action': 'revoke', 'link_id': mapping}]})
        assert revoked.json()['counts']['department_mappings']['revoked'] == 1, revoked.text


async def _map_provider(api, provider, *, root_target=None, child_target=None):
    response = await api.post(f'/api/enterprise/org/sync?provider={provider}')
    assert response.json()['status'] == 'complete', response.text
    directory = (await api.get('/api/enterprise/org/department-links')).json()
    rows = sorted((r for r in directory['org_departments'] if r['provider'] == provider), key=lambda r: r['provider_department_id'])
    mapped = []
    for row, target in zip(rows, (root_target, child_target), strict=True):
        selection = {'action': 'link' if target else 'create', 'org_department_id': row['org_department_id']}
        if target:
            selection['department_id'] = target
        result = await api.post('/api/enterprise/org/department-links', json={'selections': [selection]})
        assert result.json()['rows'][0]['outcome'] in {'created', 'mapped'}, result.text
        mapped.append(result.json()['rows'][0])
    return rows, mapped


async def test_distinct_providers_can_align_same_department_but_cannot_cross_consume(
    company_directory, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    seed, client, state = company_directory, httpx.AsyncClient, {'calls': []}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(**{**kw, 'transport': httpx.MockTransport(lambda r: _single_department_provider(r, state))}))
    async with client(transport=httpx.ASGITransport(app=seed['app']), base_url='http://alignment') as api:
        _ding, mapped = await _map_provider(api, 'dingtalk')
        wecom, _mapped = await _map_provider(api, 'wecom', root_target=mapped[0]['department_id'], child_target=mapped[1]['department_id'])
        async with owner_sessionmaker() as db:
            people = list(await db.scalars(select(OrgMember).where(OrgMember.tenant_id == seed['tenant'])))
        for person in people:
            linked = await api.post('/api/enterprise/org/identity-links', json={'org_member_id': str(person.id),
                'provider_id': str(person.provider_id), 'user_id': str(seed['account']), 'subject_kind': 'provider_user_id'})
            assert linked.json()['applied'] is True, linked.text
        assigned = await api.post('/api/enterprise/org/department-cutover', json={'org_member_ids': [str(p.id) for p in people]})
        assert assigned.json()['counts']['user_assignments']['changed'] == 1, assigned.text
        from app.models.identity import IdentityProvider
        from app.models.org import OrgDepartment
        from app.models.user import Department
        from app.services.identity_dry_run import build_identity_dry_run_report
        async with owner_sessionmaker() as db:
            feishu_provider = IdentityProvider(tenant_id=seed['tenant'], provider_type='feishu', name='Feishu', is_active=True, config={})
            db.add(feishu_provider)
            independent = Department(tenant_id=seed['tenant'], name='Independent department')
            db.add(independent)
            await db.flush()
            user = User(tenant_id=seed['tenant'], username=uuid4().hex, email=f'{uuid4().hex}@fixture.test',
                password_hash='synthetic', display_name='Feishu person', feishu_user_id='legacy-feishu-person', department_id=independent.id)
            legacy = OrgDepartment(tenant_id=seed['tenant'], feishu_id='2', name='Feishu mirror')
            db.add_all([user, legacy])
            await db.flush()
            member = OrgMember(tenant_id=seed['tenant'], provider_id=feishu_provider.id, feishu_user_id='legacy-feishu-person',
                name='Feishu person', department_id=legacy.id, observed_directory_department_ids=['2'])
            db.add(member)
            await db.commit()
            report = await build_identity_dry_run_report(db, tenant_id=seed['tenant'], actor_id=seed['admin'],
                snapshot={'complete': True, 'departments': [], 'members': []}, source_revision='provider-isolation')
            actual = [row for row in report['rows'] if row['object_id'] == str(member.id)]
            assert any(row['disposition'] == 'trusted' for row in actual), actual
            assert all(row['disposition'] != 'department_conflict' for row in actual), actual
        ding = next(p for p in people if str(p.provider_id) != wecom[1]['provider_id'])
        async with owner_sessionmaker() as db:
            member = await db.get(OrgMember, ding.id)
            member.department_id = UUID(wecom[1]['org_department_id'])
            (await db.get(User, seed['account'])).department_id = None
            await db.commit()
        denied = await api.post('/api/enterprise/org/department-cutover', json={'org_member_ids': [str(ding.id)]})
        assert denied.json()['rows'][0]['reason'] == 'provider_namespace', denied.text
        async with owner_sessionmaker() as db:
            assert (await db.get(User, seed['account'])).department_id is None


async def test_connection_revocation_blocks_assignment_and_reactivation_but_not_safe_revoke(
    company_directory, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    seed, client, state = company_directory, httpx.AsyncClient, {'calls': []}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(**{**kw, 'transport': httpx.MockTransport(lambda r: _single_department_provider(r, state))}))
    async with client(transport=httpx.ASGITransport(app=seed['app']), base_url='http://alignment') as api:
        _, mapped = await _map_provider(api, 'wecom')
        async with owner_sessionmaker() as db:
            person = await db.scalar(select(OrgMember).where(OrgMember.tenant_id == seed['tenant']))
        link = await api.post('/api/enterprise/org/identity-links', json={'org_member_id': str(person.id),
            'provider_id': str(person.provider_id), 'user_id': str(seed['account']), 'subject_kind': 'provider_user_id'})
        assert link.json()['applied'] is True
        configs = (await api.get('/api/tenant-channels')).json()
        config = next(c for c in configs if c['channel_type'] == 'wecom')
        paused = await api.put('/api/tenant-channels/wecom', json={'app_id': config['app_id'], 'is_active': False,
            'expected_revision': config['revision'], 'expected_tenant_id': str(seed['tenant']), 'expected_user_id': str(seed['admin'])})
        assert paused.status_code == 200, paused.text
        calls = len(state['calls'])
        assigned = await api.post('/api/enterprise/org/department-cutover', json={'org_member_ids': [str(person.id)]})
        assert assigned.json()['rows'][0]['reason'] == 'directory_connection_inactive', assigned.text
        listing = (await api.get('/api/enterprise/org/department-links')).json()
        assert all(r['link']['valid'] is False for r in listing['org_departments'])
        revoked = await api.post('/api/enterprise/org/department-links', json={'selections': [{'action': 'revoke', 'link_id': mapped[1]['link_id']}]})
        assert revoked.json()['counts']['department_mappings']['revoked'] == 1, revoked.text
        journal = (await api.get('/api/enterprise/org/cutover-journal')).json()
        revoke = next(r for r in journal['rows'] if r['object_id'] == mapped[1]['link_id'] and r['after']['status'] == 'revoked')
        refused = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [revoke['id']]})
        assert refused.json()['rows'][0]['reason'] == 'directory_connection_inactive', refused.text
        changed = await api.put('/api/tenant-channels/wecom', json={'app_id': 'another-company', 'app_secret': 'synthetic-new-corp',
            'is_active': True, 'expected_revision': paused.json()['revision'], 'expected_tenant_id': str(seed['tenant']), 'expected_user_id': str(seed['admin'])})
        assert changed.status_code == 200, changed.text
        refused = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [revoke['id']]})
        assert refused.json()['rows'][0]['reason'] == 'directory_namespace_changed', refused.text
        root_creation = next(r for r in journal['rows'] if r['object_id'] == mapped[0]['link_id'] and r['before'] is None)
        safe = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [root_creation['id']]})
        assert safe.json()['rows'][0]['outcome'] == 'restored', safe.text
        assert len(state['calls']) == calls
        async with owner_sessionmaker() as db:
            assert (await db.get(User, seed['account'])).department_id is None


async def test_multi_department_conflict_keeps_exact_account_and_newer_observation_for_recovery(
    company_directory, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    seed, client, state = company_directory, httpx.AsyncClient, {'calls': []}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(**{**kw, 'transport': httpx.MockTransport(lambda r: _single_department_provider(r, state))}))
    async with client(transport=httpx.ASGITransport(app=seed['app']), base_url='http://alignment') as api:
        _, mapped = await _map_provider(api, 'wecom')
        async with owner_sessionmaker() as db:
            person = await db.scalar(select(OrgMember).where(OrgMember.tenant_id == seed['tenant']))
        linked = await api.post('/api/enterprise/org/identity-links', json={'org_member_id': str(person.id),
            'provider_id': str(person.provider_id), 'user_id': str(seed['account']), 'subject_kind': 'provider_user_id'})
        assert linked.json()['applied'] is True
        state['multi'] = True
        synced = await api.post('/api/enterprise/org/sync?provider=wecom')
        assert synced.json()['assignment_counts']['conflict'] == 1, synced.text
        row = synced.json()['assignment_rows'][0]
        assert row['user_id'] == str(seed['account']) and row['reason'] == 'multiple_directory_departments', row
        explicit = await api.post('/api/enterprise/org/department-cutover', json={'org_member_ids': [str(person.id)]})
        assert explicit.json()['rows'][0]['user_id'] == str(seed['account']), explicit.text
        repaired = await api.post('/api/enterprise/org/department-assignments/repair', json={
            'user_id': str(seed['account']), 'department_id': mapped[1]['department_id']})
        assert repaired.json()['counts']['user_assignments']['changed'] == 1, repaired.text
        journal = (await api.get('/api/enterprise/org/cutover-journal')).json()
        repair = next(r for r in journal['rows'] if r['object_kind'] == 'user_department')
        again = await api.post('/api/enterprise/org/sync?provider=wecom')
        assert again.json()['assignment_counts']['conflict'] == 1, again.text
        rollback = await api.post('/api/enterprise/org/cutover-journal/rollback', json={'journal_ids': [repair['id']]})
        assert rollback.json()['counts']['user_assignments']['changed'] == 1, rollback.text
        async with owner_sessionmaker() as db:
            assert (await db.get(User, seed['account'])).department_id is None
            assert (await db.get(OrgMember, person.id)).observed_directory_department_ids == ['1', '2']
