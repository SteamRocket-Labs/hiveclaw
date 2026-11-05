"""Approval authority and working-copy source continuity at outward boundaries."""
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create


@pytest.mark.parametrize('owner', [True, False])
@pytest.mark.parametrize('bypass', ['allowed_tools', 'hook'])
async def test_outward_preflight_requires_authorized_approver(native_workspace, owner_sessionmaker, monkeypatch, owner, bypass):  # noqa: F811
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.user import User
    from app.runtime.ccplus_contracts import PermissionProfileV1
    from app.services.approval_service import can_resolve_approval
    from app.tools import governance

    n = native_workspace
    if not owner:
        async with owner_sessionmaker() as db:
            agent = await db.get(Agent, n.agent)
            agent.owner_user_id = n.foreign
            agent.creator_id = n.foreign
            await db.commit()
    async def hook(**kwargs):
        return None
    if bypass == 'hook':
        monkeypatch.setattr(governance, '_apply_permission_request_hook', hook)
    context = governance.ToolGovernanceContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
        tool_name='send_feishu_message', arguments={'message': 'Private draft'}, session_id=str(n.session),
        permission_profile=PermissionProfileV1(allowed_tools=('send_feishu_message',) if bypass == 'allowed_tools' else ()))
    requests = []
    async def company(**kwargs):
        requests.append(kwargs)
        return {'approval_id': str(uuid.uuid4())}
    deps = governance.GovernanceDependencies(lambda _: 'standard', lambda *args: None, lambda **kw: None, company)
    result = await governance.request_action_preflight_approval(context, deps, reason_code='preflight_ask')
    assert result is not None, 'OUTWARD_ACTION_ALLOWED_WITHOUT_APPROVAL'
    assert ('session_permission_required' if owner else 'approval_required') in str(result)
    assert len(requests) == (0 if owner else 1)
    if not owner:
        async with owner_sessionmaker() as db:
            approval = ApprovalRequest(id=uuid.uuid4(), tenant_id=n.tenant, agent_id=n.agent,
                action_type='tool.action_preflight.send_feishu_message', details={'origin': {'type': 'action_preflight'}})
            assert not await can_resolve_approval(db, agent=await db.get(Agent, n.agent),
                user=await db.get(User, n.user), approval=approval)


@pytest.mark.parametrize('owner_kind', ['self', 'root', 'foreign', 'none'])
async def test_quarantine_preserves_owners_and_base_grant_boundary(native_workspace, owner_sessionmaker, owner_kind):  # noqa: F811
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.workspace_resource import WorkspaceResourceManifest
    n = native_workspace
    path = 'workspace/quarantined.md'
    await _create(n, path, 'QUARANTINED_BODY')
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.authority_state = 'quarantined'
        manifest.owner_user_id = n.user if owner_kind == 'self' else n.foreign if owner_kind in {'root', 'foreign'} else None
        manifest.root_session_id = n.session if owner_kind == 'root' else None
        db.add(ResourcePermission(id=uuid.uuid4(), tenant_id=n.tenant, principal_type='user', principal_id=n.user,
            resource_type='workspace_file', resource_id=workspace_resource_id(n.agent, path), actions=['read', 'write'], conditions={}))
        await db.commit()
    response = await n.client.get(f'/agents/{n.agent}/files/content', params={'path': path})
    assert response.status_code == (200 if owner_kind in {'self', 'root'} else 403), response.text
    response = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path},
        json={'content': 'Updated', 'expected_current_hash': hashlib.sha256(b'QUARANTINED_BODY').hexdigest()})
    assert response.status_code == (200 if owner_kind in {'self', 'root'} else 403), response.text


@pytest.mark.parametrize('receipt', [None, SimpleNamespace(content_sha256='old', dependencies_json=[])])
async def test_browser_working_copy_does_not_require_matching_receipt(receipt):
    from app.services.content_input_receipts import validate_browser_workspace_receipt
    await validate_browser_workspace_receipt(None, uuid.uuid4(), None, receipt, 'current')


async def test_invalid_workspace_path_returns_typed_denial():
    from app.services.content_input_receipts import authorize_workspace_receipt_read
    result = await authorize_workspace_receipt_read(SimpleNamespace(tool_name='read_file', arguments={'path': '../outside'}))
    assert 'content_source_unavailable' in str(result)


def test_pre_effect_failure_has_plain_model_message():
    from app.services.session_permission_runtime import _typed_execution_abort
    result = json.loads(_typed_execution_abort(invocation=SimpleNamespace(id=uuid.uuid4(), provider_tool_use_id='call'),
        reason_code='approved_tool_execution_failed', status='failed'))
    assert result['message'] == 'The approved action failed before it started.'


async def _read_with_inventory(n, db, *, tool, path):
    from app.services.session_model_inputs import _resolve_origin_dependencies
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _prepare_write, _admitted_write_messages
    from app.models.capability_policy import CapabilityPolicy
    capability = 'workspace.command.execute' if tool == 'run_command' else 'workspace.file.read'
    db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
    await db.commit()
    arguments = ({'pattern': 'SOURCE', 'root': path} if tool == 'grep_search' else
        {'command': 'cat derived-source.md'} if tool == 'run_command' else {'path': path})
    invocation, arguments, turn = await _prepare_write(db, n, tool_name=tool, arguments=arguments,
        messages=await _admitted_write_messages(db, n, content='Read my working copy'))
    trace = {}
    result = await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
    await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        invocation_id=invocation.id, provider_result_content=str(result), execution_evidence=_execution_evidence(trace))
    await db.commit()
    sources, complete = await _resolve_origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
        ref=f'session-tool-result:{invocation.id}')
    return result, sources, complete, invocation


@pytest.mark.parametrize('reader', ['read_file', 'grep_search', 'changed_read', 'sandbox'])
async def test_revoked_document_cannot_leave_after_working_copy_read(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, reader):  # noqa: F811
    from datetime import datetime, timedelta, timezone
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.services.content_input_receipts import (ContentInputReceiptError, latest_workspace_receipt,
        validate_runtime_input_dependencies)
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n = native_workspace
    path = 'workspace/derived-source.md'
    target = await _create(n, path, 'DERIVED_SOURCE_BODY')
    source = n.root.parent / 'document-source.md'
    source.write_text('SOURCE_DOCUMENT')
    version = hashlib.sha256(source.read_bytes()).hexdigest()
    document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant, scope_type='person', scope_id=n.foreign,
        source_kind='text', source_sha256=version, canonical_md_path=source.name,
        canonical_md_sha256=version, title='Private source', status='ready')
    grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type='document',
        resource_id=document.id, document_id=document.id, grantee_type='agent', grantee_id=n.agent,
        requester_user_id=n.user, session_id=str(n.session), purpose='interactive_session', permission='read',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    async with owner_sessionmaker() as db:
        db.add_all([document, grant])
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        receipt.dependencies_json = [{'source_kind': 'knowledge_document', 'source_id': str(document.id), 'source_version': version}]
        await db.commit()
        context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), workspace=n.root,
            session_id=str(n.session), runtime_task_id=str(n.run))
        if reader == 'changed_read':
            target.write_text('DERIVED_SOURCE_BODY with a member edit')
        if reader == 'sandbox':
            from app.services.agent_tool_domains import code_exec
            from app.services.code_execution.contracts import CodeExecutionResult
            async def process(_command, *, work_dir, **kwargs):
                return CodeExecutionResult(stdout=(work_dir / 'derived-source.md').read_text(), exit_code=0)
            monkeypatch.setattr(code_exec, 'execute_agent_command', process)
        result, sources, complete, invocation = await _read_with_inventory(n, db,
            tool='run_command' if reader == 'sandbox' else 'grep_search' if reader == 'grep_search' else 'read_file', path=path)
        assert 'DERIVED_SOURCE_BODY' in str(result), result
        assert complete, 'WORKING_COPY_READ_POISONED_INPUT_INVENTORY'
        assert any(row['source_kind'] == 'workspace_artifact' and row['source_id'] == str(receipt.id) for row in sources), sources
        # Recheck the actual captured inputs at the approved effect boundary.
        if reader != 'changed_read':
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name='send_email', arguments={}, context=context), sources)
        await db.commit()
        from app.services import agent_tools, runtime_task_worker
        from app.services.llm_client import LLMMessage
        from app.services.session_tool_runtime import complete_tool_invocation
        from app.services.session_permission_runtime import _execution_evidence
        from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _prepare_write
        from tests.integration.test_session_permission_execution_once import _mounted_client
        deliveries = []
        async def deliver(*args, **kwargs):
            deliveries.append(args)
            return 'Unexpected external delivery'
        async def notify(**kwargs):
            pass
        monkeypatch.setattr(agent_tools, '_handle_email_tool', deliver)
        monkeypatch.setattr(runtime_task_worker, 'notify_runtime_task_worker', notify)
        outward, arguments, turn = await _prepare_write(db, n, tool_name='send_email', round_index=2,
            arguments={'to': 'recipient@example.test', 'subject': 'Report', 'body': str(result)},
            messages=[LLMMessage(role='tool', tool_call_id=invocation.provider_tool_use_id, content=str(result),
                source_ref=f'session-tool-result:{invocation.id}')])
        trace = {}
        waiting = await n.service.execute('send_email', arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=outward.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        assert 'session_permission_required' in str(waiting), waiting
        await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=outward.id, provider_result_content=str(waiting), execution_evidence=_execution_evidence(trace))
        await db.commit()
        permission_id = outward.permission_item_id
        grant.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        with pytest.raises(ContentInputReceiptError):
            await validate_runtime_input_dependencies(db, ToolExecutionRequest(tool_name='send_email', arguments={}, context=context), sources)
        await db.commit()
        async with _mounted_client(app_user_sessionmaker, SimpleNamespace(user_id=n.user, tenant_id=n.tenant)) as client:
            resolved = await client.post(f'/api/agents/{n.agent}/sessions/{n.session}/permissions/{permission_id}/resolve',
                json={'action': 'allow_once'})
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()['run_status'] == 'resumable'
        await db.refresh(outward)
        assert deliveries == [] and outward.effect_state == 'prepared_not_started'
        from app.models.chat_transcript_event import ChatTranscriptEvent
        event = await db.get(ChatTranscriptEvent, outward.result_event_id)
        assert event.metadata_json['v2_payload']['outcome'] == 'failed'


@pytest.mark.parametrize('tool', ['read_file', 'grep_search'])
async def test_owned_unreceipted_read_keeps_handoff_inventory_complete(native_workspace, owner_sessionmaker, tool):  # noqa: F811
    from app.services.workspace_resource_authority import register_workspace_path
    n = native_workspace
    path = 'workspace/bot-authored.md'
    (n.root / path).write_text('BOT_OWN_SOURCE')
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.user, root_session_id=n.session, source='agent_tool',
            content_hash=hashlib.sha256((n.root / path).read_bytes()).hexdigest())
        await db.commit()
        result, sources, complete, invocation = await _read_with_inventory(n, db, tool=tool, path=path)
        assert 'BOT_OWN_SOURCE' in str(result), result
        assert complete, 'UNRECEIPTED_OWN_FILE_BLOCKS_HANDOFFS'
        assert not any(row['source_kind'] == 'workspace_artifact' for row in sources), sources


@pytest.mark.parametrize('created_by', ['bot', 'member_edited'])
async def test_office_editor_authorizes_current_working_docx(native_workspace, owner_sessionmaker, created_by):  # noqa: F811
    from docx import Document
    from app.api import office, files
    from app.models.user import User
    from app.services.content_input_receipts import latest_workspace_receipt
    n = native_workspace
    from app.services.office_document_service import OfficeDocumentService
    from app.tools.handlers import office as office_tools
    class DocumentAdapter:
        # Keep the external OfficeCLI process outside this authority regression;
        # parse the actual produced docx bytes through the existing test dependency.
        def run(self, command, target, **kwargs):
            assert command == 'query'
            rows = [{'path': f'/body/p[{i + 1}]', 'text': paragraph.text}
                for i, paragraph in enumerate(Document(target).paragraphs)]
            return {'success': True, 'data': {'results': rows, 'matches': len(rows)}}
    def service(workspace, **kwargs):
        return OfficeDocumentService(workspace, adapter=DocumentAdapter(), **kwargs)
    n.monkeypatch.setattr(office, 'OfficeDocumentService', service)
    n.monkeypatch.setattr(office_tools, 'OfficeDocumentService', service)
    n.client._transport.app.include_router(office.router)
    path = 'workspace/current.docx'
    result = await n.service.execute('office_document_create', {'path': path, 'kind': 'docx'},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    assert (n.root / path).is_file(), result
    async with owner_sessionmaker() as db:
        assert await latest_workspace_receipt(db, n.tenant, n.agent, path) is None
        if created_by == 'member_edited':
            await files._write_file_bytes(n.agent, path, (n.root / path).read_bytes(),
                expected_current_hash=hashlib.sha256((n.root / path).read_bytes()).hexdigest(),
                current_user=await db.get(User, n.user), db=db)
            assert await latest_workspace_receipt(db, n.tenant, n.agent, path) is not None
    document = Document(n.root / path)
    document.paragraphs[0].text = 'Current editable paragraph'
    document.save(n.root / path)
    response = await n.client.get(f'/agents/{n.agent}/office/content', params={'path': path})
    assert response.status_code == 200, response.text
    assert response.json()['read_only'] is False
    assert response.json()['fields'][0]['value'] == 'Current editable paragraph'


async def test_restore_works_when_current_file_has_unreceipted_edits(native_workspace):  # noqa: F811
    n = native_workspace
    path, original = 'workspace/restore.md', 'Saved original'
    first = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path}, json={'content': original})
    assert first.status_code == 200, first.text
    second = await n.client.put(f'/agents/{n.agent}/files/content', params={'path': path},
        json={'content': 'Saved second', 'expected_current_hash': first.json()['content_hash']})
    assert second.status_code == 200, second.text
    history = await n.client.get(f'/agents/{n.agent}/files/versions', params={'path': path})
    version = next(row for row in history.json()['versions'] if row['content_hash'] == first.json()['content_hash'])
    (n.root / path).write_text('Current unreceipted edit')
    response = await n.client.post(f"/agents/{n.agent}/files/versions/{version['version_id']}/restore", params={'path': path},
        json={'expected_current_exists': True, 'expected_current_hash': hashlib.sha256((n.root / path).read_bytes()).hexdigest()})
    assert response.status_code == 200, response.text
    assert (n.root / path).read_text() == original


async def test_unreceipted_input_allows_delegated_first_task_answer_and_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, migrated_pg_url,
):
    from pathlib import Path
    from app.config import get_settings
    from app.tools.service import ToolRuntimeService
    from app.services.workspace_resource_authority import register_workspace_path
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
    from tests.integration.test_delegation_final_delivery_postgres import (
        test_first_task_final_answer_and_pinned_file_survive_sealed_round_replay as exercise,
    )
    stream, execute = ScriptedLLMClient.stream, ToolRuntimeService.execute
    path = 'workspace/own-unreceipted-note.md'
    observed = []
    async def stream_with_working_copy(self, *args, **kwargs):
        for response in self.script[:1]:
            if any(call.get('id') == 'child-report-read' for call in response.get('tool_calls', [])):
                response['tool_calls'].append({'id': 'child-own-note-read', 'type': 'function',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': path})}})
        return await stream(self, *args, **kwargs)
    async def read_working_copy(self, name, arguments, **kwargs):
        if name == 'read_file' and arguments.get('path') == path:
            root = Path(get_settings().AGENT_DATA_DIR) / str(kwargs['agent_id'])
            (root / path).write_text('DELEGATED_REPORT from my own working note')
            from app.models.agent import Agent
            async with owner_sessionmaker() as db:
                agent = await db.get(Agent, kwargs['agent_id'])
                await register_workspace_path(db, tenant_id=agent.tenant_id, agent_id=agent.id, path=path,
                    owner_user_id=kwargs['user_id'], root_session_id=uuid.UUID(kwargs['session_id']),
                    source='agent_tool', content_hash=hashlib.sha256((root / path).read_bytes()).hexdigest())
                await db.commit()
            observed.append(path)
        return await execute(self, name, arguments, **kwargs)
    monkeypatch.setattr(ScriptedLLMClient, 'stream', stream_with_working_copy)
    monkeypatch.setattr(ToolRuntimeService, 'execute', read_working_copy)
    await exercise(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, migrated_pg_url)
    assert observed == [path]


async def test_unreceipted_input_keeps_delegated_approval_and_sandbox_artifact_usable(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.execution_context import ExecutionIdentity, ExecutionPrincipal
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult
    from app.models.chat_artifact import ChatArtifact
    from app.models.capability_policy import CapabilityPolicy
    from app.runtime.ccplus_contracts import PermissionProfileV1
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.delegation_permission_runtime import validate_permission_input_sources
    from app.services.llm_client import LLMMessage
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from app.services.workspace_resource_authority import register_workspace_path
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _prepare_write

    n = native_workspace
    path, output = 'workspace/own-note.md', 'workspace/result.md'
    (n.root / path).write_text('OWN_SOURCE_BODY')
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.user, root_session_id=n.session, source='agent_tool')
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability='workspace.command.execute', allowed=True, requires_approval=False))
        await db.commit()
        read, _sources, complete, read_invocation = await _read_with_inventory(n, db, tool='read_file', path=path)
        assert complete, 'OWN_INPUT_BLOCKS_DELEGATED_APPROVAL'
        invocation, arguments, turn = await _prepare_write(db, n, tool_name='run_command', round_index=2,
            arguments={'command': 'cp own-note.md result.md'}, messages=[LLMMessage(role='tool', content=str(read),
                tool_call_id=read_invocation.provider_tool_use_id, source_ref=f'session-tool-result:{read_invocation.id}')])
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == invocation.provider_request_id))
        frame = SimpleNamespace(principal=ExecutionPrincipal(tenant_id=n.tenant, source_agent_id=n.agent,
            requester_user_id=n.user, root_session_id=str(n.session), root_runtime_task_id=str(n.run)),
            execution_identity=ExecutionIdentity('delegated_user', n.user, 'Test member'), delegation_token=None,
            permission_profile=PermissionProfileV1(), budget_run_id=str(n.run))
        from app.database import pin_rls_tenant_context
        await pin_rls_tenant_context(db, n.tenant)
        await validate_permission_input_sources(db, authority=SimpleNamespace(tenant_id=n.tenant, agent_id=n.agent, principal_id=n.user),
            session=await db.get(ChatSession, n.session), invocation=invocation, model=model, execution={'authority_frame': frame})
        await db.commit()
        async def process(_command, *, work_dir, **kwargs):
            content = (work_dir / 'own-note.md').read_text()
            (work_dir / 'result.md').write_text(content)
            return CodeExecutionResult(stdout=content, exit_code=0)
        monkeypatch.setattr(code_exec, 'execute_agent_command', process)
        trace = {}
        result = await n.service.execute('run_command', arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        assert 'OWN_SOURCE_BODY' in str(result), result
        await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
            'name': 'run_command', 'args': arguments, 'status': 'done', 'result': str(result),
            'artifacts': list(getattr(result, 'artifacts', ())),
            'tool_call_id': invocation.provider_tool_use_id, 'provider_request_id': invocation.provider_request_id,
            'runtime_task_id': str(n.run), 'tool_execution_evidence': _execution_evidence(trace)})
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, output)
        assert receipt is not None, 'OWN_INPUT_BLOCKS_SANDBOX_OUTPUT_RECEIPT'
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent, ChatArtifact.path == output))
        assert artifact is not None, 'OWN_INPUT_BLOCKS_SANDBOX_ARTIFACT'
        opened = await n.client.get(f'/agents/{n.agent}/files/artifacts/{artifact.id}/content')
        assert opened.status_code == 200 and 'OWN_SOURCE_BODY' in opened.text, opened.text


@pytest.mark.parametrize('tool', ['read_file', 'grep_search'])
async def test_granted_unreceipted_file_does_not_become_owned_handoff_input(
    native_workspace, owner_sessionmaker, tool,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    n = native_workspace
    n.target.write_text('FOREIGN_SOURCE_BODY')
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type='user', principal_id=n.user,
            resource_type='workspace_file', resource_id=workspace_resource_id(n.agent, n.path),
            actions=['read'], conditions={}))
        await db.commit()
        result, _sources, complete, _invocation = await _read_with_inventory(n, db, tool=tool, path=n.path)
        assert 'FOREIGN_SOURCE_BODY' in str(result), result
        assert not complete, 'A read grant must not turn an unreceipted foreign file into owned context'
