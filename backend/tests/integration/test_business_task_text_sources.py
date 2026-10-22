"""Business task delivery consumes the current authority of original inputs."""

import json
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures('migrated_pg_url')
RESULT = 'BUSINESS_RESULT_DERIVED_FROM_PRIVATE_ORIGINAL'


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('withdrawn', [False, True])
async def test_business_task_file_returns_to_original_work_and_channel(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, withdrawn,  # noqa: F811
):
    await test_business_task_text_rechecks_its_actual_original_source(
        original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=withdrawn, unknown_effect=False, artifact_body='# Report\nPRIVATE_BUSINESS_INPUT\n')


async def _assert_business_file_return(*, client, db_factory, seed, run_id, original, headers,
    artifact_body, withdrawn, monkeypatch, tmp_path, model_client, canonical_download_user, owner_db_factory):
    from app.api import files, office
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.feishu_service import FeishuService
    from app.services.channel_delivery_outbox import _claimed
    from app.services.content_input_receipts import ContentInputReceiptError

    client._transport.app.include_router(files.router)
    client._transport.app.include_router(office.router)
    monkeypatch.setattr(files, '_load_download_user_from_jwt', canonical_download_user)
    monkeypatch.setattr(files.settings, 'AGENT_DATA_DIR', str(seed['data_root']))
    async with db_factory() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id)))
        outbox = await db.scalar(select(ChannelDeliveryOutbox).where(
            ChannelDeliveryOutbox.runtime_task_id == run_id,
            ChannelDeliveryOutbox.delivery_kind == 'terminal_result',
            ChannelDeliveryOutbox.terminal_status == 'completed'))
        observed = {'run_status': run.status, 'metadata': run.metadata_json,
            'artifacts': [{'id': str(a.id), 'path': a.path, 'session_id': str(a.session_id),
                'root_session_id': str(a.root_session_id), 'owner_user_id': str(a.owner_user_id),
                'snapshot': a.snapshot_json} for a in artifacts],
            'delivery_artifacts': outbox.artifact_ids_json if outbox else None}
        (tmp_path / 'business-file-publication.json').write_text(json.dumps(observed, indent=2, default=str))
        assert run.status == 'completed' and len(model_client.requests) == 3, observed
        assert len(artifacts) == 1, observed
        artifact = artifacts[0]
        assert outbox is not None and outbox.artifact_ids_json == [str(artifact.id)], observed
        assert artifact.owner_user_id == seed['user_id'] and artifact.root_session_id == seed['session_id'], observed
        artifact_id = str(artifact.id)
        saved = _claimed(outbox)
        service = ChannelDeliveryOutboxService(session_factory=db_factory)
        for changed in (
            replace(saved, artifact_ids=(uuid.uuid4(),)), replace(saved, session_id=uuid.uuid4()),
            replace(saved, user_id=uuid.uuid4()),
            replace(saved, metadata={**saved.metadata, 'business_task_id': str(uuid.uuid4())}),
        ):
            with pytest.raises(ContentInputReceiptError) as denied:
                await service._authorize_artifact_part(db, changed, artifact.id)
            assert denied.value.status_code == 403
    detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert [part['artifact_id'] for part in detail.json()['artifacts']] == [artifact_id], detail.text
    assert detail.json()['task']['result_visibility']['status'] == 'available', detail.text
    assert 'PRIVATE_BUSINESS_INPUT' in detail.json()['task']['last_result']
    own_logs = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}/logs", headers=headers)
    assert own_logs.status_code == 200 and 'PRIVATE_BUSINESS_INPUT' in own_logs.text, own_logs.text
    if not withdrawn:
        from app.models.user import User
        from app.core.security import create_access_token

        inspector_id = uuid.uuid4()
        async with owner_db_factory() as db:
            db.add(User(id=inspector_id, tenant_id=seed['tenant_id'], username=f'inspector-{inspector_id.hex}',
                email=f'{inspector_id}@fixture.test', display_name='Company inspector', password_hash='synthetic-unused',
                role='org_admin', is_active=True))
            await db.commit()
        inspector_headers = {'Authorization': 'Bearer ' + create_access_token(str(inspector_id), 'org_admin', str(seed['tenant_id']))}
        inspection = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}",
            params={'operator_view': 'true', 'operator_reason': 'Inspect task state'}, headers=inspector_headers)
        assert inspection.status_code == 200 and inspection.json()['task']['authority_source'] == 'scoped_business_admin', inspection.text
        assert inspection.json()['artifacts'] == []
        assert 'PRIVATE_BUSINESS_INPUT' not in inspection.text
        assert inspection.json()['task']['result_visibility']['status'] == 'denied'
        listed = await client.get(f"/agents/{seed['agent_id']}/tasks/", headers=inspector_headers)
        assert listed.status_code == 200 and 'PRIVATE_BUSINESS_INPUT' not in listed.text, listed.text
        private_logs = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}/logs", headers=inspector_headers)
        assert private_logs.status_code == 403 and 'PRIVATE_BUSINESS_INPUT' not in private_logs.text, private_logs.text
        private = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download", headers=inspector_headers)
        assert private.status_code == 403, private.text
        private_preview = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/content", headers=inspector_headers)
        assert private_preview.status_code == 403, private_preview.text
        office_preview = await client.get(f"/agents/{seed['agent_id']}/office/artifacts/{artifact_id}/preview", headers=inspector_headers)
        assert office_preview.status_code == 403, office_preview.text
    (seed['agent_root'] / 'workspace/business-report.md').write_text('EDITED_AFTER_COMPLETION')
    if withdrawn:
        revoked = await client.post(f"/chat/attachments/{original['original_attachment']['id']}/withdraw", headers=headers,
            json={'expected_content_sha256': original['original_attachment']['content_sha256']})
        assert revoked.status_code == 200, revoked.text
        inaccessible = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}", headers=headers)
        assert inaccessible.status_code == 200 and inaccessible.json()['artifacts'] == [], inaccessible.text
        assert inaccessible.json()['artifact_delivery']['status'] == 'denied'
        assert inaccessible.json()['task']['result_visibility']['status'] == 'denied'
        assert 'PRIVATE_BUSINESS_INPUT' not in inaccessible.text
    downloaded = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download", headers=headers)
    assert downloaded.status_code == (403 if withdrawn else 200), downloaded.text
    preview = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/content", headers=headers)
    assert preview.status_code == (403 if withdrawn else 200), preview.text
    if not withdrawn:
        assert downloaded.content == artifact_body.encode()
        assert preview.json()['content'] == artifact_body and preview.json()['workspace_changed'] is True
    texts, sent_files = [], []

    async def text_transport(self, _app_id, _secret, recipient, *args, **kwargs):
        texts.append(recipient)
        return {'data': {'message_id': 'synthetic-business-file-text'}}

    async def file_transport(self, _app_id, _secret, recipient, file_path, **kwargs):
        sent_files.append({'recipient': recipient, 'bytes': Path(file_path).read_bytes()})
        return {'data': {'message_id': 'synthetic-business-file-ack'}}

    monkeypatch.setattr(FeishuService, 'send_message', text_transport)
    monkeypatch.setattr(FeishuService, 'upload_and_send_file', file_transport)
    service = ChannelDeliveryOutboxService(session_factory=db_factory)
    counts = await service.drain_once(worker_id='business-file')
    assert texts == ([] if withdrawn else ['ou-business-owner']), counts
    assert sent_files == ([] if withdrawn else [{'recipient': 'ou-business-owner', 'bytes': artifact_body.encode()}]), counts
    assert (await service.drain_once(worker_id='business-file'))['claimed'] == 0
    assert len(model_client.requests) == 3
    if not withdrawn:
        renamed = await client.patch(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}",
            headers=headers, json={'title': 'Renamed after completion'})
        assert renamed.status_code == 200, renamed.text
        reopened = await client.get(f"/agents/{seed['agent_id']}/files/artifacts/{artifact_id}/download", headers=headers)
        assert reopened.status_code == 200 and reopened.content == artifact_body.encode(), reopened.text
        from app.models.task import TaskLog

        async with db_factory() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            legacy_id = uuid.uuid4()
            db.add(RuntimeTask(id=legacy_id, task_type='business_task', status='failed',
                tenant_id=run.tenant_id, parent_agent_id=run.parent_agent_id, child_agent_id=run.parent_agent_id,
                root_user_id=run.root_user_id, root_runtime_task_id=legacy_id,
                root_idempotency_key=f'legacy-attempt:{legacy_id}', config_snapshot_hash=run.config_snapshot_hash,
                policy_snapshot_hash=run.policy_snapshot_hash,
                metadata_json={'business_task_id': run.metadata_json['business_task_id'],
                    'outcome': {'result': 'LEGACY_OUTPUT_WITHOUT_SOURCE_RECEIPT'}}))
            db.add(TaskLog(tenant_id=run.tenant_id, task_id=uuid.UUID(run.metadata_json['business_task_id']),
                content='LEGACY_OUTPUT_WITHOUT_SOURCE_RECEIPT'))
            await db.commit()
        recovered = await client.get(f"/agents/{seed['agent_id']}/tasks/{run.metadata_json['business_task_id']}", headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['task']['result_visibility']['status'] == 'available'
        assert 'PRIVATE_BUSINESS_INPUT' in recovered.json()['task']['last_result']
        assert recovered.json()['log_visibility']['status'] == 'unavailable' and recovered.json()['logs'] == []
        assert 'LEGACY_OUTPUT_WITHOUT_SOURCE_RECEIPT' not in recovered.text
        assert [part['artifact_id'] for part in recovered.json()['artifacts']] == [artifact_id]


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('home,requester_state', [('none', 'active'), ('foreign', 'active'),
    ('none', 'role_revoked'), ('foreign', 'role_revoked'), ('none', 'inactive'), ('foreign', 'home_disabled')])
async def test_business_task_uses_the_selected_company_of_its_exact_requester(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, home, requester_state,  # noqa: F811
):
    from fastapi import Request
    from app.core.security import decode_access_token
    from app.database import get_db
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    original_seed = _seed_run
    canonical = {}

    async def selected_seed(*args, **kwargs):
        seed = await original_seed(*args, **kwargs)
        async with owner_sessionmaker() as db:
            user = await db.get(User, seed['user_id'])
            home_id = None
            if home == 'foreign':
                home_id = uuid.uuid4()
                db.add(Tenant(id=home_id, name='Requester home', slug=f'requester-home-{home_id.hex}'))
                await db.flush()
            user.tenant_id, user.role = home_id, 'platform_admin'
            owner_id = uuid.uuid4()
            db.add(User(id=owner_id, tenant_id=seed['tenant_id'], username=f'owner-{owner_id.hex}',
                email=f'{owner_id}@fixture.test', display_name='Company owner', password_hash='synthetic-unused',
                role='member', is_active=True))
            await db.flush()
            (await db.get(Agent, seed['agent_id'])).owner_user_id = owner_id
            await db.commit()
        canonical.update(user_id=seed['user_id'], tenant_id=home_id, agent_id=seed['agent_id'], owner_id=owner_id)
        return seed

    monkeypatch.setattr(sys.modules[__name__], '_seed_run', selected_seed)

    async def selected_scope(request: Request):
        claims = decode_access_token(request.headers['authorization'].removeprefix('Bearer '))
        scope = request.headers.get('x-tenant-id') if claims['role'] == 'platform_admin' else claims.get('tid')
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, uuid.UUID(scope))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    original_api[1]._transport.app.dependency_overrides[get_db] = selected_scope
    await test_business_task_text_rechecks_its_actual_original_source(
        original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        withdrawn=False, unknown_effect=False, delivery_channel='web', requester_state=requester_state)
    async with owner_sessionmaker() as db:
        user = await db.get(User, canonical['user_id'])
        assert user.tenant_id == canonical['tenant_id'] and user.role == ('member' if requester_state == 'role_revoked' else 'platform_admin')
        assert user.is_active is (requester_state != 'inactive')
        assert (await db.get(Agent, canonical['agent_id'])).owner_user_id == canonical['owner_id'] != user.id


@pytest.mark.parametrize('provider', ['filesystem'], indirect=True)
@pytest.mark.parametrize('withdrawn,unknown_effect', [(False, False), (True, False), (False, True)])
async def test_business_task_text_rechecks_its_actual_original_source(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, withdrawn, unknown_effect,  # noqa: F811
    delivery_channel='feishu',
    requester_state='active',
    artifact_body=None,
    permission_handler=None,
    question_handler=None,
):
    import app.database as database
    import app.runtime.invoker as invoker
    from app.api import tasks, upload
    from app.config import get_settings
    from app.core.security import create_access_token
    from app.core.source_read_purpose import source_recipient_read
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.llm import LLMModel
    from app.models.plan_request import AgentPlanRequest
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.models.task import Task
    from app.models.user import User
    from app.schemas.schemas import TaskCreate
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from app.services.plan_authorization_lease import canonical_action_artifact, issue_plan_authorization_leases
    from app.services.plan_mode_core import compute_plan_hash
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_task_worker import _execute_claimed_business_task
    from app.services.secrets_provider import get_secrets_provider

    _, client = original_api
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(get_settings(), 'AGENT_DATA_DIR', str(seed['data_root']))
    monkeypatch.setattr(upload, 'WORKSPACE_ROOT', seed['data_root'])
    for module, name in ((agent_tools, 'WORKSPACE_ROOT'), (agent_context, 'PERSISTENT_DATA'),
        (agent_context, 'TOOL_WORKSPACE')):
        monkeypatch.setattr(module, name, seed['data_root'])
    monkeypatch.setattr('app.tools.workspace.WORKSPACE_ROOT', seed['data_root'])
    monkeypatch.setattr(agent_tools, '_TOOL_RUNTIME_SERVICE', None)
    monkeypatch.setattr(agent_manager, 'settings', get_settings())
    (seed['agent_root'] / 'workspace').rmdir()
    seed['agent_root'].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed['agent_id'])
        requester = await db.get(User, seed['user_id'])
        requester_role, requester_home = requester.role, requester.tenant_id
        await AgentManager().initialize_agent_files(db, agent, personality='Read authorized originals for business tasks.',
            original_author_id=seed['user_id'])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt('synthetic-business-task-provider-only')
        model.base_url, model.max_input_tokens = 'http://127.0.0.1:1', 256_000
        if artifact_body is not None and permission_handler is None:
            from app.models.capability_policy import CapabilityPolicy

            db.add(CapabilityPolicy(tenant_id=seed['tenant_id'], capability='workspace.file.write',
                allowed=True, requires_approval=False))
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, '__name__', '').startswith('app.'):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(tasks, 'notify_runtime_task_worker', no_wakeup)
    client._transport.app.include_router(tasks.router)
    headers = {'Authorization': 'Bearer ' + create_access_token(str(seed['user_id']), requester_role,
        str(requester_home) if requester_home else None)}
    if requester_role == 'platform_admin':
        headers['X-Tenant-Id'] = str(seed['tenant_id'])
    uploaded = await client.post('/chat/upload', headers=headers, files={'file': ('source.txt', b'PRIVATE_BUSINESS_INPUT')},
        data={'agent_id': str(seed['agent_id']), 'skip_personal_kb': 'true'})
    assert uploaded.status_code == 200, uploaded.text
    original = uploaded.json()
    original_id = uuid.UUID(original['original_attachment']['id'])
    body = TaskCreate(request_id=str(uuid.uuid4()), title='Original-based report',
        description='Read the authorized original and deliver the complete report.',
        confirmed_plan_session_id=str(seed['session_id'])).model_dump(mode='json')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        session = await db.get(ChatSession, seed['session_id'])
        if delivery_channel == 'feishu':
            config = ChannelConfig(id=uuid.uuid4(), tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], channel_type='feishu',
                app_id='synthetic-business-result', app_secret='synthetic-channel-only', is_configured=True)
            db.add(config)
            await db.flush()
            bound = await bind_authenticated_self_channel_principal(db, tenant_id=seed['tenant_id'], config=config,
                provider_subject_id='ou-business-owner', user_id=seed['user_id'], actor_user_id=seed['user_id'])
            session.delivery_target_json = {'channel': 'feishu', 'receive_id': 'ou-business-owner', 'receive_id_type': 'open_id',
                'chat_type': 'p2p', 'session_id': str(session.id), 'external_principal_id': str(bound.principal.id)}
        plan_json = {'schema': 'hive_plan.v1', 'intent_type': 'in_session_execution', 'title': body['title'],
            'objective': body['description'], 'steps': [{'order': 1, 'description': body['description']}],
            'success_criteria': ['Deliver the complete report.'], 'stop_conditions': ['The owner cancels.'],
            'handoff': {'target': 'continue_current_session'}, 'authorization_scopes': [{'action_kind': 'start_long_task',
                'target_ref': 'task:new', 'arguments': canonical_action_artifact(body), 'summary': body['description']}]}
        plan = AgentPlanRequest(tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], session_id=str(session.id),
            requested_by_user_id=seed['user_id'], confirmed_by_user_id=seed['user_id'], confirmed_at=datetime.now(UTC),
            source='web_chat', intent_type='in_session_execution', original_request=body['description'], status='confirmed',
            plan_version=1, plan_hash=compute_plan_hash(plan_json), plan_json=plan_json)
        db.add(plan)
        await db.flush()
        leases = await issue_plan_authorization_leases(db=db, plan=plan, confirming_user_id=seed['user_id'])
        assert len(leases) == 1
        body.update(confirmed_plan_id=str(plan.id), confirmed_plan_version=1, confirmed_plan_hash=plan.plan_hash)
        await db.commit()
    created = await client.post(f"/agents/{seed['agent_id']}/tasks/", headers=headers, json=body)
    assert created.status_code == 201, created.text
    run_id = uuid.UUID(created.json()['active_runtime_task_id'])
    repeated = await client.post(f"/agents/{seed['agent_id']}/tasks/", headers=headers, json=body)
    assert repeated.status_code == 201 and repeated.json()['id'] == created.json()['id']
    assert repeated.json()['active_runtime_task_id'] == str(run_id)
    if requester_state != 'active':
        from app.models.tenant import Tenant

        async with owner_sessionmaker() as db:
            user = await db.get(User, seed['user_id'])
            if requester_state == 'role_revoked':
                user.role = 'member'
            elif requester_state == 'inactive':
                user.is_active = False
            elif requester_state == 'home_disabled':
                (await db.get(Tenant, user.tenant_id)).is_active = False
            await db.commit()
    (seed['agent_root'] / original['workspace_path']).unlink()
    model_client = ScriptedLLMClient([{'tool_calls': [{'id': 'business-original-read', 'type': 'function', 'function': {
        'name': 'fs_read', 'arguments': json.dumps({'path': original['workspace_path'], 'original_attachment_id': str(original_id)})}}],
        'finish_reason': 'tool_calls'}, {'content': RESULT, 'finish_reason': 'stop'}])
    if artifact_body is not None:
        model_client.script.insert(1, {'tool_calls': [{'id': 'business-report-write', 'type': 'function', 'function': {
            'name': 'fs_write', 'arguments': json.dumps({'path': 'workspace/business-report.md', 'content': artifact_body})}}],
            'finish_reason': 'tool_calls'})
        model_client.script[-1]['content'] = RESULT + '\nPRIVATE_BUSINESS_INPUT\n\n[Report](workspace/business-report.md)'
    if question_handler is not None:
        model_client.script.insert(1, {'tool_calls': [{'id': 'business-question', 'type': 'function', 'function': {
            'name': 'ask_user_question', 'arguments': json.dumps({'questions': [{'question': 'Which output for PRIVATE_BUSINESS_INPUT?',
                'header': 'Output', 'options': [{'label': 'Report'}, {'label': 'Summary'}]}]})}}], 'finish_reason': 'tool_calls'})
    monkeypatch.setattr(invoker, 'create_llm_client', lambda **_kwargs: model_client)
    if unknown_effect:
        from app.kernel.contracts import ToolLifecyclePersistenceError
        from app.services.delegation_session_runtime import DelegationSessionRuntime

        persist = DelegationSessionRuntime.tool_call

        async def lose_result_receipt(self, data):
            if data.get('status') in {'done', 'completed'}:
                raise ToolLifecyclePersistenceError(tool_name=data['name'],
                    provider_tool_use_id=data['tool_call_id'], lifecycle=data['status'])
            return await persist(self, data)

        monkeypatch.setattr(DelegationSessionRuntime, 'tool_call', lose_result_receipt)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        run.status, run.claimed_by, run.claim_version, run.attempt_count = 'running', 'native-business-source', 1, 1
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=run_id, worker_id='native-business-source', claim_version=1)
    try:
        await _execute_claimed_business_task(run_id)
    finally:
        reset_runtime_task_fence(fence)
    (tmp_path / 'business-model-requests.json').write_text(json.dumps(model_client.requests, default=str))
    handler = permission_handler or question_handler
    if handler is not None:
        await handler(client=client, db_factory=app_user_sessionmaker, owner_db_factory=owner_sessionmaker,
            seed=seed, run_id=run_id, original=original, headers=headers, artifact_body=artifact_body,
            monkeypatch=monkeypatch, tmp_path=tmp_path, model_client=model_client,
            canonical_download_user=original_api[0].canonical_download_user)
        return
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        (tmp_path / 'business-runtime-result.json').write_text(json.dumps({
            'status': run.status, 'child_session_id': run.child_session_id, 'metadata': run.metadata_json}, default=str))
        if requester_state != 'active':
            assert run.status == 'skipped', run.metadata_json
            task = await db.get(Task, uuid.UUID(created.json()['id']))
            assert task.status == 'blocked' and task.created_by == seed['user_id']
            assert len(model_client.requests) == 0
            return
        if unknown_effect:
            assert run.status == 'needs_reconciliation', run.metadata_json
            task = await db.get(Task, uuid.UUID(created.json()['id']))
            assert task.last_execution_status == 'needs_reconciliation'
            tool = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id))
            assert tool.effect_state in {'effect_started', 'needs_reconciliation'}
            assert len(model_client.requests) == 1
    if artifact_body is not None:
        await _assert_business_file_return(client=client, db_factory=app_user_sessionmaker, seed=seed, run_id=run_id,
            original=original, headers=headers, artifact_body=artifact_body, withdrawn=withdrawn,
            monkeypatch=monkeypatch, tmp_path=tmp_path, model_client=model_client,
            canonical_download_user=original_api[0].canonical_download_user, owner_db_factory=owner_sessionmaker)
        return
    if unknown_effect:
        detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{created.json()['id']}", headers=headers)
        assert detail.status_code == 200 and detail.json()['task']['actions']['can_reconcile']
        assert not detail.json()['task']['actions']['can_retry']
        await _execute_claimed_business_task(run_id)
        assert len(model_client.requests) == 1
        revoked = await client.post(f'/chat/attachments/{original_id}/withdraw', headers=headers,
            json={'expected_content_sha256': original['original_attachment']['content_sha256']})
        assert revoked.status_code == 200, revoked.text
        calls = []

        async def status_transport(self, _app_id, _secret, recipient, *args, **kwargs):
            calls.append({'recipient': recipient, 'args': args, 'kwargs': kwargs})
            return {'data': {'message_id': 'synthetic-business-status-ack'}}

        monkeypatch.setattr(FeishuService, 'send_message', status_transport)
        service = ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker)
        counts = await service.drain_once(worker_id='business-status')
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            row = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
            observed = {'calls': calls, 'counts': counts, 'status': row.status, 'last_error': row.last_error}
            (tmp_path / 'business-status-source.json').write_text(json.dumps(observed, indent=2))
            assert len(calls) == 1 and row.status == 'delivered', observed
            assert calls[0]['recipient'] == 'ou-business-owner'
            assert 'reconciliation' in json.dumps(calls)
            assert not any(value in json.dumps(calls) for value in ('PRIVATE_BUSINESS_INPUT', RESULT, body['title']))
            from app.services.channel_delivery_outbox import _claimed

            saved = _claimed(row)
            for changed in (
                replace(saved, text='PRIVATE_BUSINESS_INPUT'),
                replace(saved, artifact_ids=(uuid.uuid4(),)),
                replace(saved, user_id=uuid.uuid4()),
                replace(saved, metadata={**saved.metadata, 'business_task_id': str(uuid.uuid4())}),
            ):
                with pytest.raises(ContentInputReceiptError) as denied:
                    await service._authorize_native_result_text(db, changed)
                assert denied.value.status_code == 403
            with pytest.raises(ContentInputReceiptError) as no_result:
                await service._authorize_native_result_text(db,
                    replace(saved, metadata={**saved.metadata, 'projection_kind': 'result'}))
            assert no_result.value.status_code == 503
            user = await db.get(User, seed['user_id'])
            user.is_active = False
            await db.flush()
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as inactive:
                await service._authorize_native_result_text(db, saved)
            assert inactive.value.status_code == 403
        assert len(calls) == 1
        assert len(model_client.requests) == 1
        return
    assert len(model_client.requests) == 2 and any(message.get('role') == 'tool' and 'PRIVATE_BUSINESS_INPUT' in str(message)
        for message in model_client.requests[-1]['messages'])
    if delivery_channel == 'web':
        detail = await client.get(f"/agents/{seed['agent_id']}/tasks/{created.json()['id']}", headers=headers)
        assert detail.status_code == 200 and detail.json()['task']['last_result'] == RESULT, detail.text
        assert detail.json()['task']['root_session_id'] == str(seed['session_id'])
        return
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        run = await db.get(RuntimeTask, run_id)
        assert run.status == 'completed', run.metadata_json
        row = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
        assert row is not None and row.metadata_json['source'] == 'business_task_runtime' and RESULT in row.text_content
        outbox_id = row.id
        user = await db.get(User, seed['user_id'])
        dependencies = [{'source_kind': 'original_attachment', 'source_id': str(original_id),
            'source_version': original['original_attachment']['content_sha256']}]
        with source_recipient_read(session_id=seed['session_id'], user_id=user.id):
            await _validate_input_dependencies(db, seed['tenant_id'], dependencies, user=user)
    if withdrawn:
        revoked = await client.post(f'/chat/attachments/{original_id}/withdraw', headers=headers,
            json={'expected_content_sha256': original['original_attachment']['content_sha256']})
        assert revoked.status_code == 200, revoked.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            with source_recipient_read(session_id=seed['session_id'], user_id=user.id):
                with pytest.raises(ContentInputReceiptError) as oracle:
                    await _validate_input_dependencies(db, seed['tenant_id'], dependencies, user=user)
            assert oracle.value.status_code == 403
    calls = []

    async def text_transport(self, _app_id, _secret, recipient, *args, **kwargs):
        calls.append({'recipient': recipient, 'args': args, 'kwargs': kwargs})
        return {'data': {'message_id': 'synthetic-business-ack'}}

    monkeypatch.setattr(FeishuService, 'send_message', text_transport)
    counts = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id='business-source')
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed['tenant_id'])
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        observed = {'withdrawn': withdrawn, 'calls': calls, 'counts': counts, 'status': row.status, 'last_error': row.last_error}
        (tmp_path / 'business-text-source.json').write_text(json.dumps(observed, indent=2))
        assert len(calls) == int(not withdrawn), observed
        assert row.status == ('dead_letter' if withdrawn else 'delivered'), observed
        if not withdrawn:
            assert RESULT in json.dumps(calls)
