"""Project references use real HTTP, PostgreSQL RLS and canonical file/session authority."""

import hashlib
import os
import sys
import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")



@pytest.fixture
async def project_http(native_workspace, app_user_sessionmaker):  # noqa: F811 - imported fixture
    from app.api import projects, chat_sessions
    from app.core.security import get_current_user
    from app.models.user import User

    n = native_workspace
    app = n.client._transport.app
    app.include_router(projects.router)
    app.include_router(chat_sessions.router)
    actor = {"id": n.user}

    async def user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            return await db.get(User, actor['id'])

    app.dependency_overrides[get_current_user] = user

    async def request(method, suffix='', body=None):
        return await n.client.request(method, '/projects' + suffix, params={
            'expected_user_id': str(actor['id']), 'expected_tenant_id': str(n.tenant)}, json=body)

    return SimpleNamespace(n=n, actor=actor, request=request)


async def test_project_links_reopen_the_saved_file_and_original_works_without_copying(project_http, owner_sessionmaker):
    from app.models.workbench_project import WorkbenchProjectEntry
    from app.models.workspace_resource import WorkspaceResourceManifest

    p = project_http
    n = p.n
    path = 'workspace/project-evidence.md'
    saved = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={'content': 'INITIAL PROJECT BYTES'})
    assert saved.status_code == 200, saved.text
    project = uuid4()
    created = await p.request('POST', body={'id': str(project), 'title': 'Quarterly research'})
    assert created.status_code == 201, created.text
    assert created.json()['revision'] == 1
    replay = await p.request('POST', body={'id': str(project), 'title': 'Quarterly research'})
    assert replay.status_code == 201 and replay.json()['revision'] == 1
    forged = await p.request('POST', body={'id': str(uuid4()), 'title': 'Forged owner', 'owner_user_id': str(n.foreign)})
    assert forged.status_code == 422
    file_entry = uuid4()
    add_file = {'id': str(file_entry), 'expected_revision': 1, 'target_kind': 'workspace_file',
        'agent_id': str(n.agent), 'path': path, 'section': 'Evidence'}
    linked = await p.request('POST', f'/{project}/entries', add_file)
    assert linked.status_code == 201, linked.text
    assert linked.json()['revision'] == 2
    replay = await p.request('POST', f'/{project}/entries', add_file)
    assert replay.status_code == 201 and replay.json()['revision'] == 2
    session_entry = uuid4()
    linked_work = await p.request('POST', f'/{project}/entries', {'id': str(session_entry), 'expected_revision': 2,
        'target_kind': 'session', 'agent_id': str(n.agent), 'session_id': str(n.session), 'section': 'Work'})
    assert linked_work.status_code == 201, linked_work.text
    revision = linked_work.json()['revision']
    stale = await p.request('PATCH', f'/{project}', {'expected_revision': 1, 'title': 'Stale replacement'})
    assert stale.status_code == 409
    current = await p.request('GET', f'/{project}')
    assert current.status_code == 200, current.text
    assert current.json()['title'] == 'Quarterly research'
    assert current.json()['revision'] == revision
    assert {(e['target_kind'], e['state']) for e in current.json()['entries']} == {('workspace_file', 'linked'), ('session', 'linked')}
    assert 'INITIAL PROJECT BYTES' not in current.text
    changed = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={
        'content': 'HUMAN SAVED PROJECT VERSION', 'expected_current_hash': saved.json()['content_hash']})
    assert changed.status_code == 200, changed.text
    reloaded = (await p.request('GET', f'/{project}')).json()
    source = next(e for e in reloaded['entries'] if e['id'] == str(file_entry))
    opened = await n.client.get(f"/agents/{source['agent_id']}/files/content", params={'path': source['path']})
    assert opened.status_code == 200 and opened.json()['content'] == 'HUMAN SAVED PROJECT VERSION'
    assert hashlib.sha256((n.root / path).read_bytes()).hexdigest() == changed.json()['content_hash']
    async with owner_sessionmaker() as db:
        entry = await db.get(WorkbenchProjectEntry, file_entry)
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == n.agent,
            WorkspaceResourceManifest.path == path))
        assert entry.target_id == manifest.id
    removed = await p.request('PATCH', f'/{project}/entries/{file_entry}', {'expected_revision': revision, 'removed': True})
    assert removed.status_code == 200, removed.text
    assert (n.root / path).read_text() == 'HUMAN SAVED PROJECT VERSION'
    replay_removed = await p.request('POST', f'/{project}/entries', add_file)
    assert replay_removed.status_code == 201 and replay_removed.json()['removed'] is True
    archived = await p.request('PATCH', f'/{project}', {'expected_revision': removed.json()['revision'], 'archived': True})
    assert archived.status_code == 200, archived.text
    assert (await p.request('GET')).json()['projects'] == []
    assert (await p.request('GET', f'/{project}')).json()['archived'] is True


async def test_project_access_does_not_grant_sources_and_revocation_withholds_metadata(project_http, owner_sessionmaker):
    from app.core.resource_authority import workspace_resource_id
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.chat_session import ChatSession

    p, n = project_http, project_http.n
    path = 'workspace/authorized-project-file.md'
    written = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={'content': 'SOURCE ACL BODY'})
    assert written.status_code == 200, written.text
    project, entry, grant = uuid4(), uuid4(), uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Shared project metadata'})).status_code == 201
    linked = await p.request('POST', f'/{project}/entries', {'id': str(entry), 'expected_revision': 1,
        'target_kind': 'workspace_file', 'agent_id': str(n.agent), 'path': path})
    assert linked.status_code == 201, linked.text
    async with owner_sessionmaker() as db:
        email = (await db.get(User, n.foreign)).email
        (await db.get(ChatSession, n.session)).title = 'PRIVATE_PROJECT_WORK_TITLE'
        db.add(AgentPermission(tenant_id=n.tenant, agent_id=n.agent, scope_type='user', scope_id=n.foreign, access_level='use'))
        await db.commit()
    work = await p.request('POST', f'/{project}/entries', {'id': str(uuid4()), 'expected_revision': 2,
        'target_kind': 'session', 'agent_id': str(n.agent), 'session_id': str(n.session), 'section': 'Work'})
    assert work.status_code == 201, work.text
    shared = await p.request('POST', f'/{project}/grants', {'id': str(grant), 'expected_revision': 3, 'email': email, 'role': 'reader'})
    assert shared.status_code == 201, shared.text
    p.actor['id'] = n.foreign
    current = await p.request('GET', f'/{project}')
    assert current.status_code == 200, current.text
    assert current.json()['entries'][0]['state'] == 'denied'
    assert path not in current.text and 'SOURCE ACL BODY' not in current.text and 'PRIVATE_PROJECT_WORK_TITLE' not in current.text
    assert all(row['state'] == 'denied' for row in current.json()['entries'])
    assert not current.json()['can_write'] and 'grants' not in current.json()
    direct = await n.client.get(f'/agents/{n.agent}/files/content', params={'path': path})
    assert direct.status_code == 403
    denied = await p.request('PATCH', f'/{project}', {'expected_revision': 4, 'title': 'Denied reader edit'})
    assert denied.status_code == 403
    source_grant = uuid4()
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(id=source_grant, tenant_id=n.tenant, principal_type='user', principal_id=n.foreign,
            resource_type='workspace_file', resource_id=workspace_resource_id(n.agent, path), actions=['read'], conditions={}))
        await db.commit()
    readable = await p.request('GET', f'/{project}')
    assert readable.status_code == 200 and readable.json()['entries'][0]['path'] == path
    assert (await n.client.get(f'/agents/{n.agent}/files/content', params={'path': path})).status_code == 200
    async with owner_sessionmaker() as db:
        await db.delete(await db.get(ResourcePermission, source_grant))
        await db.commit()
    revoked = await p.request('GET', f'/{project}')
    assert revoked.status_code == 200 and revoked.json()['entries'][0]['state'] == 'denied'
    assert path not in revoked.text and 'authorized-project-file.md' not in revoked.text
    p.actor['id'] = n.user
    revoked_project = await p.request('POST', f'/{project}/grants/{grant}/revoke', {'expected_revision': 4})
    assert revoked_project.status_code == 200, revoked_project.text
    p.actor['id'] = n.foreign
    denied_project = await p.request('GET', f'/{project}')
    assert denied_project.status_code == 403 and denied_project.headers['cache-control'] == 'private, no-store'
    assert (await p.request('GET')).json()['projects'] == []


async def test_project_tables_enforce_non_owner_tenant_rls_and_composite_project_binding(
    project_http, app_user_sessionmaker, owner_sessionmaker
):
    from app.models.workbench_project import WorkbenchProject, WorkbenchProjectEntry
    from sqlalchemy.exc import IntegrityError

    p, n = project_http, project_http.n
    project = uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Isolated project'})).status_code == 201
    from tests.services.test_session_input_control_v2 import _seed_session
    other_tenant, other_user, _agent, _session, _run = await _seed_session(owner_sessionmaker)
    other_project = uuid4()
    async with owner_sessionmaker() as db:
        db.add(WorkbenchProject(id=other_project, tenant_id=other_tenant, owner_user_id=other_user, title='Other tenant', revision=1))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        role = (await db.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user'))).one()
        assert role == (False, False)
        assert await db.get(WorkbenchProject, project) is not None
        await pin_rls_tenant_context(db, uuid4())
        assert list(await db.scalars(select(WorkbenchProject))) == []
        assert list(await db.scalars(select(WorkbenchProjectEntry))) == []
        await pin_rls_tenant_context(db, n.tenant)
        db.add(WorkbenchProjectEntry(tenant_id=n.tenant, project_id=other_project, target_kind='session', target_id=n.session, section=''))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    stale_account = await n.client.get('/projects', params={'expected_user_id': str(uuid4()), 'expected_tenant_id': str(n.tenant)})
    assert stale_account.status_code == 409


async def test_lost_database_commit_ack_is_unknown_and_the_same_project_id_is_not_recreated(project_http, monkeypatch, app_user_sessionmaker):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.exc import SQLAlchemyError
    from app.models.workbench_project import WorkbenchProject

    p, n = project_http, project_http.n
    project = uuid4()
    request = {'id': str(project), 'title': 'Only one committed project'}
    commit, lost = AsyncSession.commit, False

    async def commit_then_lose_ack(session):
        nonlocal lost
        await commit(session)
        if not lost:
            lost = True
            raise SQLAlchemyError('Synthetic lost commit ACK after real database commit')

    monkeypatch.setattr(AsyncSession, 'commit', commit_then_lose_ack)
    unknown = await p.request('POST', body=request)
    assert unknown.status_code == 503 and unknown.json()['detail']['outcome'] == 'unknown', unknown.text
    monkeypatch.setattr(AsyncSession, 'commit', commit)
    retried = await p.request('POST', body=request)
    assert retried.status_code == 201 and retried.json()['revision'] == 1
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert len(list(await db.scalars(select(WorkbenchProject).where(WorkbenchProject.id == project)))) == 1
    changed_purpose = await p.request('POST', body={**request, 'title': 'Another purpose'})
    assert changed_purpose.status_code == 409


async def test_projects_organize_multiple_employees_and_transfer_without_transferring_sources(project_http, owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.user import User

    p, n = project_http, project_http.n
    other = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=other, tenant_id=n.tenant, creator_id=n.user, name='Second project employee'))
        email = (await db.get(User, n.foreign)).email
        await db.commit()
    project = uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Cross employee project'})).status_code == 201
    revision = 1
    for agent in (n.agent, other):
        path = 'workspace/shared-project.txt'
        saved = await n.client.put(f'/agents/{agent}/files/content', params={'path': path}, json={'content': f'Canonical input from {agent}'})
        assert saved.status_code == 200, saved.text
        added = await p.request('POST', f'/{project}/entries', {'id': str(uuid4()), 'expected_revision': revision,
            'target_kind': 'workspace_file', 'agent_id': str(agent), 'path': path, 'section': 'Inputs'})
        assert added.status_code == 201, added.text
        revision = added.json()['revision']
    current = (await p.request('GET', f'/{project}')).json()
    assert {row['agent_id'] for row in current['entries']} == {str(n.agent), str(other)}
    transferred = await p.request('PATCH', f'/{project}', {'expected_revision': revision, 'owner_email': email})
    assert transferred.status_code == 200, transferred.text
    assert (await p.request('GET', f'/{project}')).status_code == 403
    p.actor['id'] = n.foreign
    new_owner = await p.request('GET', f'/{project}')
    assert new_owner.status_code == 200 and new_owner.json()['can_manage'] is True
    assert all(row['state'] == 'denied' and 'path' not in row for row in new_owner.json()['entries'])


async def test_editor_cannot_reshare_or_bypass_archived_project_state(project_http, owner_sessionmaker):
    from app.models.user import User

    p, n = project_http, project_http.n
    project, grant = uuid4(), uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Editor scope'})).status_code == 201
    async with owner_sessionmaker() as db:
        email = (await db.get(User, n.foreign)).email
    assert (await p.request('POST', f'/{project}/grants', {'id': str(grant), 'expected_revision': 1,
        'email': email, 'role': 'editor'})).status_code == 201
    p.actor['id'] = n.foreign
    renamed = await p.request('PATCH', f'/{project}', {'expected_revision': 2, 'title': 'Authorized editor change'})
    assert renamed.status_code == 200
    cannot_share = await p.request('POST', f'/{project}/grants', {'id': str(uuid4()), 'expected_revision': 3,
        'email': email, 'role': 'reader'})
    assert cannot_share.status_code == 403
    cannot_archive = await p.request('PATCH', f'/{project}', {'expected_revision': 3, 'archived': True})
    assert cannot_archive.status_code == 403
    p.actor['id'] = n.user
    archived = await p.request('PATCH', f'/{project}', {'expected_revision': 3, 'archived': True})
    assert archived.status_code == 200
    p.actor['id'] = n.foreign
    blocked = await p.request('PATCH', f'/{project}', {'expected_revision': 4, 'title': 'Forbidden archived edit'})
    assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'project_archived'


@pytest.mark.parametrize('alias', ['symlink', 'hardlink'])
async def test_project_alias_replacement_withholds_source_metadata_and_never_changes_target(project_http, alias):
    p, n = project_http, project_http.n
    path = 'workspace/independent-project-file.md'
    saved = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={'content': 'Independent original'})
    assert saved.status_code == 200, saved.text
    project = uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Alias boundary'})).status_code == 201
    linked = await p.request('POST', f'/{project}/entries', {'id': str(uuid4()), 'expected_revision': 1,
        'target_kind': 'workspace_file', 'agent_id': str(n.agent), 'path': path})
    assert linked.status_code == 201, linked.text
    protected = n.target.read_bytes()
    (n.root / path).unlink()
    if alias == 'symlink':
        (n.root / path).symlink_to(n.target)
    else:
        os.link(n.target, n.root / path)
    result = await p.request('GET', f'/{project}')
    assert result.status_code == 200 and result.json()['entries'][0]['state'] == 'denied'
    assert path not in result.text and 'PRIVATE_FOREIGN_FILE_BODY' not in result.text
    assert n.target.read_bytes() == protected


async def test_selected_reference_rechecks_authority_outside_the_visible_page(project_http, owner_sessionmaker):
    from app.models.workbench_project import WorkbenchProjectEntry
    from app.models.workspace_resource import WorkspaceResourceManifest

    p, n = project_http, project_http.n
    path = 'workspace/multiple-project-sections.md'
    assert (await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={'content': 'ONE CANONICAL FILE'})).status_code == 200
    project, first, second = uuid4(), uuid4(), uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Paged references'})).status_code == 201
    for revision, entry, section in ((1, first, 'Inputs'), (2, second, 'Review')):
        added = await p.request('POST', f'/{project}/entries', {'id': str(entry), 'expected_revision': revision,
            'target_kind': 'workspace_file', 'agent_id': str(n.agent), 'path': path, 'section': section})
        assert added.status_code == 201, added.text
    page = await n.client.get(f'/projects/{project}', params={'expected_user_id': str(n.user),
        'expected_tenant_id': str(n.tenant), 'limit': 1})
    assert page.status_code == 200 and page.json()['next_offset'] == 1 and len(page.json()['entries']) == 1
    other = await p.request('GET', f'/{project}/entries/{second}')
    assert other.status_code == 200 and other.json()['section'] == 'Review' and other.json()['path'] == path
    async with owner_sessionmaker() as db:
        targets = list(await db.scalars(select(WorkbenchProjectEntry.target_id).where(WorkbenchProjectEntry.project_id == project)))
        assert len(targets) == 2 and len(set(targets)) == 1
        manifest = await db.get(WorkspaceResourceManifest, targets[0])
        manifest.owner_user_id = n.foreign
        await db.commit()
    denied = await p.request('GET', f'/{project}/entries/{second}')
    assert denied.status_code == 200 and denied.json()['state'] == 'denied'
    assert path not in denied.text and 'ONE CANONICAL FILE' not in denied.text
    assert (n.root / path).read_text() == 'ONE CANONICAL FILE'


async def test_scoped_company_administrator_keeps_real_identity_and_project_ownership(project_http, owner_sessionmaker):
    from app.models.security_audit import SecurityAuditEvent
    from app.models.user import User

    p, n = project_http, project_http.n
    project = uuid4()
    assert (await p.request('POST', body={'id': str(project), 'title': 'Company administration'})).status_code == 201
    p.actor['id'] = n.foreign
    assert (await p.request('GET', f'/{project}')).status_code == 403
    async with owner_sessionmaker() as db:
        (await db.get(User, n.foreign)).role = 'org_admin'
        await db.commit()
    admin = await p.request('GET', f'/{project}')
    assert admin.status_code == 200 and admin.json()['can_manage'] is True
    assert admin.json()['owner_user_id'] == str(n.user)
    async with owner_sessionmaker() as db:
        events = list(await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.resource_id == project,
            SecurityAuditEvent.event_type == 'project.administrator_access')))
        assert events and all(event.actor_id == n.foreign and event.details['authority_source'] == 'scoped_business_admin' for event in events)
    cross_company = await n.client.get(f'/projects/{project}', params={'expected_user_id': str(n.foreign),
        'expected_tenant_id': str(uuid4())})
    assert cross_company.status_code == 403


async def test_mounted_project_file_is_consumed_by_the_next_native_worker_in_the_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from tests.integration.test_m2c_managed_environment_postgres import _exercise_real_worker_environment
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput

    create = ("from pathlib import Path; Path('saved.txt').write_text('CANONICAL_ENVIRONMENT_FILE'); "
        "Path('project.md').write_text('PROJECT_INITIAL_NATIVE_BYTES'); print('FIRST_COMMAND')")
    initial = f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(create)}"

    async def consume(seed, client, url, environment_scope, current):
        from app.api import files, chat_sessions, projects, agents
        from app.runtime import invoker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
        from app.services.web_chat_runtime import execute_web_chat_run
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
        from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

        web = client._transport.app
        for router in (files.router, chat_sessions.router, projects.router, agents.router):
            web.include_router(router, prefix='/api')
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            original = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed['run_id'],
                ChatArtifact.path == 'workspace/project.md'))
            assert original is not None
            original_id, original_hash = original.id, original.snapshot_hash
        await _exercise_dom_bridge(web, seed, tmp_path, entry_file='e2e/project-workbench-native.tsx', extra_env={
            'HIVE_PROJECT_SESSION_ID': str(seed['session_id'])})
        output = (tmp_path / 'dom-native.log').read_text()
        project_id = UUID(re.search(r'HIVE_PROJECT_ID=([0-9a-f-]+)', output).group(1))
        input_id = UUID(re.search(r'HIVE_PROJECT_INPUT_ID=([0-9a-f-]+)', output).group(1))
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            assert (await db.get(SessionTurnInput, input_id)).session_id == seed['session_id']
            runs = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed['agent_id'],
                RuntimeTask.parent_session_id == str(seed['session_id']), RuntimeTask.task_type == 'web_chat_turn',
                RuntimeTask.id != seed['run_id'])))
            assert len(runs) == 1 and runs[0].root_user_id == seed['user_id']
            run_id = runs[0].id
            claims = await RuntimeTaskClaimService(db=db, worker_id='project-follow-up', lease_seconds=600).claim_available()
            version = next(row for row in claims if row.id == run_id).claim_version
            await db.commit()
        code = ("from pathlib import Path; value=Path('project.md').read_text(); "
            "assert value=='PROJECT_HUMAN_SAVED_决定性尾部'; print(value); "
            "Path('project-result.md').write_text('Derived project report: '+value)")
        command = f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(code)}"
        model = ScriptedLLMClient([
            {'tool_calls': [{'id': 'project-follow-up-command', 'type': 'function', 'function': {
                'name': 'run_command', 'arguments': json.dumps({'command': command, 'timeout': 10})}}], 'finish_reason': 'tool_calls'},
            {'content': 'Saved project version consumed.\nDELIVERABLE: workspace/project-result.md', 'finish_reason': 'stop'}])
        monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model)
        fence = set_runtime_task_fence(task_id=run_id, worker_id='project-follow-up', claim_version=version)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(fence)
        assert 'PROJECT_HUMAN_SAVED_决定性尾部' in json.dumps(model.requests[-1]['messages'], ensure_ascii=False)
        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed['data_root'])
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed['tenant_id'], worker_id='project-boundary', canonical_validator=processor.validate,
            process_callback=processor, task_types=('web_chat_turn',))
        assert boundary['delivered'] == 1, boundary
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            assert (await db.get(RuntimeTask, run_id)).status == 'completed'
            assert (await db.get(ChatArtifact, original_id)).snapshot_hash == original_hash
            report = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id,
                ChatArtifact.path == 'workspace/project-result.md'))
            assert report is not None and report.snapshot_json['content_input_receipt_id']
        delivered = await client.get(f"/api/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
        assert delivered.status_code == 200 and 'PROJECT_HUMAN_SAVED_决定性尾部' in delivered.json()['content']
        scope = {'expected_user_id': str(seed['user_id']), 'expected_tenant_id': str(seed['tenant_id'])}
        detail = (await client.get(f'/api/projects/{project_id}', params=scope)).json()
        added = await client.post(f'/api/projects/{project_id}/entries', params=scope, json={
            'id': str(uuid4()), 'expected_revision': detail['revision'], 'target_kind': 'workspace_file',
            'agent_id': str(seed['agent_id']), 'path': 'workspace/project-result.md', 'section': 'Delivery'})
        assert added.status_code == 201, added.text
        (tmp_path / 'project-native-observation.json').write_text(json.dumps({'project_id': str(project_id),
            'session_id': str(seed['session_id']), 'input_id': str(input_id), 'run_id': str(run_id),
            'original_artifact_id': str(original_id), 'original_snapshot_hash': original_hash,
            'derived_artifact_id': str(report.id), 'controlled_model_transport': True}, indent=2))
        return (await client.get(url, params=environment_scope)).json()['environment']

    await _exercise_real_worker_environment(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        initial_command=initial, initial_artifact_paths=['workspace/project.md'], initial_delivery=True,
        recovery=consume, recovery_invocation_key='project-follow-up-command')
