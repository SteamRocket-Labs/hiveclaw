"""Successor sources survive working-copy edits and current authority changes."""

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    native_workspace, _prepare_write, _admitted_write_messages,  # noqa: F401
)
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create
from tests.integration.source_withholding_helpers import legacy_model_requests

# Shared integration fixture imported above.
# ruff: noqa: F811
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _step(n, db, tool, arguments, messages, index):
    from app.services.llm_client import LLMMessage
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from app.services.session_semantic_history import load_session_semantic_history

    if tool == "grep_search":
        from app.models.capability_policy import CapabilityPolicy
        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant,
            CapabilityPolicy.capability == "workspace.file.read"))
        if policy is None:
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read", allowed=True, requires_approval=False))
            await db.commit()

    invocation, arguments, turn = await _prepare_write(db, n, tool_name=tool, arguments=arguments,
        messages=messages, round_index=index)
    trace = {}
    result = await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
    events = await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": arguments, "status": "done", "result": str(result),
        "artifacts": list(getattr(result, "artifacts", ())), "tool_call_id": invocation.provider_tool_use_id,
        "provider_request_id": invocation.provider_request_id, "runtime_task_id": str(n.run),
        "tool_execution_evidence": _execution_evidence(trace)})
    await db.refresh(invocation)
    sources, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
        ref=f"session-tool-result:{invocation.id}")
    history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
    canonical = next((row for row in history.messages if row.id == f"session-tool-result:{invocation.id}"), None)
    message = LLMMessage(role="tool", tool_call_id=invocation.provider_tool_use_id,
        content=canonical.content if canonical else str(result), source_ref=f"session-tool-result:{invocation.id}")
    return SimpleNamespace(result=result, invocation=invocation, sources=sources, complete=complete,
        message=message, events=events)


async def _document(n, db):
    from app.models.agent import Agent
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.models.security_audit import ResourcePermission
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    imported = await PersonalKnowledgeService(data_root=n.root.parent, extractor=None).ingest_markdown(db,
        tenant_id=n.tenant, owner_user_id=n.foreign, title="Private document D",
        markdown=f"# Input\nPRIVATE_DOCUMENT_D\n{uuid.uuid4()}\n", source_kind="paste", sensitivity="PL3_sensitive")
    document = await db.get(KnowledgeDocument, imported.document_id)
    grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",  # R10a reader ruling: agent-only grants no longer admit a member read
        resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=n.user,  # R10a reader ruling: agent-only grants no longer admit a member read
        created_by_user_id=n.foreign, permission="read")  # R10a reader ruling: agent-only grants no longer admit a member read
    runtime_grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",  # R10a reader ruling: agent-only grants no longer admit a member read
        resource_id=document.id, document_id=document.id, grantee_type="agent", grantee_id=n.agent,
        requester_user_id=n.user, session_id=str(n.session), purpose="interactive_session", permission="read",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), sensitivity_ceiling="PL3_sensitive")
    db.add_all([grant, runtime_grant])  # R10a reader ruling: agent-only grants no longer admit a member read
    agent = await db.get(Agent, n.agent)
    agent.owner_user_id = n.foreign
    db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
        resource_type="agent", resource_id=n.agent, actions=["use", "read", "execute", "read_workbench", "operator.inspect"], conditions={}))
    await db.commit()
    dependency = {"source_kind": "knowledge_document", "source_id": str(document.id),
        "source_version": document.canonical_md_sha256}
    return document, grant, dependency


async def _read_document(n, db, document, messages):
    from app.models.agent import Agent
    read = await _step(n, db, "read_personal_kb", {"document_id": str(document.id)}, messages, 1)
    (await db.get(Agent, n.agent)).owner_user_id = n.user
    await db.commit()
    return read


async def _resolve_email(n, db, app_user_sessionmaker, monkeypatch, message, *, index, handover=False):
    from app.models.agent import Agent
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.security_audit import ResourcePermission
    from app.services import agent_tools, runtime_task_worker
    from tests.integration.test_session_permission_execution_once import _mounted_client

    deliveries = []
    async def deliver(*args, **kwargs):
        deliveries.append(args)
        return "Unexpected delivery"
    async def notify(**kwargs):
        pass
    monkeypatch.setattr(agent_tools, "_handle_email_tool", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    send = await _step(n, db, "send_email", {"to": "recipient@example.test", "subject": "Report",
        "body": "PRIVATE_DOCUMENT_D"}, [message], index)
    assert "session_permission_required" in str(send.result), send.result
    await db.commit()
    if handover:
        execute = agent_tools.execute_session_permission_tool
        async def handover_after_preflight(*args, **kwargs):
            callback = kwargs["pre_effect_callback"]
            async def before_effect(payload):
                agent = await db.get(Agent, n.agent)
                agent.owner_user_id = agent.creator_id = n.foreign
                db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
                    resource_type="agent", resource_id=n.agent, actions=["use", "read", "execute", "read_workbench"], conditions={}))
                await db.commit()
                await callback(payload)
            kwargs["pre_effect_callback"] = before_effect
            return await execute(*args, **kwargs)
        monkeypatch.setattr(agent_tools, "execute_session_permission_tool", handover_after_preflight)
    async with _mounted_client(app_user_sessionmaker, SimpleNamespace(user_id=n.user, tenant_id=n.tenant)) as client:
        response = await client.post(f"/api/agents/{n.agent}/sessions/{n.session}/permissions/{send.invocation.permission_item_id}/resolve",
            json={"action": "allow_once"})
        assert response.status_code == 200, response.text
    await db.refresh(send.invocation)
    event = await db.get(ChatTranscriptEvent, send.invocation.result_event_id)
    assert deliveries == [], "REVOKED_OR_STALE_AUTHORITY_DELIVERED"
    assert send.invocation.effect_state == "prepared_not_started"
    assert event.metadata_json["v2_payload"]["outcome"] == "failed"


async def test_ordinary_read_grep_edit_records_successor_and_download(native_workspace, owner_sessionmaker):
    from app.models.chat_artifact import ChatArtifact
    from app.services.content_input_receipts import latest_workspace_receipt

    n = native_workspace
    path = "workspace/ordinary-report.md"
    await _create(n, path, "Sales total: 42\n")
    async with owner_sessionmaker() as db:
        messages = await _admitted_write_messages(db, n, content="Read, grep, edit and deliver my report")
        steps = [("read_file", {"path": path}), ("grep_search", {"pattern": "Sales", "root": path}),
            ("edit_file", {"path": path, "old_text": "42", "new_text": "43"}),
            ("write_file", {"path": "workspace/final-report.md", "content": "Sales total: 43\n"})]
        for index, (tool, arguments) in enumerate(steps, 1):
            step = await _step(n, db, tool, arguments, messages, index)
            assert step.complete, f"ORDINARY_{tool}_INVENTORY_INCOMPLETE"
            messages = [*messages, step.message]
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, "workspace/final-report.md")
        assert receipt is not None and receipt.content_sha256 == hashlib.sha256(b"Sales total: 43\n").hexdigest()
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent,
            ChatArtifact.path == "workspace/final-report.md"))
        assert artifact is not None, "ORDINARY_FILE_CARD_UNAVAILABLE"
        response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/content")
        assert response.status_code == 200 and response.json()["content"] == "Sales total: 43\n", response.text


@pytest.mark.parametrize("cause", ["drift", "edit_inventory", "revoked_at_write"])
async def test_bot_successor_keeps_document_until_approved_send(native_workspace, owner_sessionmaker,
    app_user_sessionmaker, monkeypatch, cause):
    from app.services.content_input_receipts import latest_workspace_receipt

    n = native_workspace
    path = "workspace/report.md"
    if cause == "edit_inventory":
        from app.services.workspace_resource_authority import register_workspace_path
        target = n.root / path
        target.write_text("Initial report")
        async with owner_sessionmaker() as db:
            await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
                owner_user_id=n.user, root_session_id=n.session, source="user_upload")
            await db.commit()
    else:
        target = await _create(n, path, "Initial report")
    async with owner_sessionmaker() as db:
        document, grant, dependency = await _document(n, db)
        previous = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        inherited = None
        if previous is not None:
            _older, _old_grant, inherited = await _document(n, db)
            previous.dependencies_json = [inherited]
            await db.commit()
        messages = await _admitted_write_messages(db, n, content="Read D and edit my report")
        read = await _read_document(n, db, document, messages)
        assert "PRIVATE_DOCUMENT_D" in str(read.result) and read.complete, read.result
        assert dependency in read.sources
        messages = [*messages, read.message]
        if cause == "drift":
            target.write_text("Human changed report")
        if cause == "edit_inventory":
            edited = await _step(n, db, "edit_file", {"path": path, "old_text": "Initial", "new_text": "Edited"}, messages, 2)
            messages = [*messages, edited.message]
        if cause == "revoked_at_write":
            grant.revoked_at = datetime.now(timezone.utc)
            await db.commit()
        step = await _step(n, db, "edit_file", {"path": path, "old_text": "report", "new_text": "report PRIVATE_DOCUMENT_D"}, messages, 3)
        assert "Updated" in str(step.result), step.result
        successor = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        grant.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        # A fresh session cannot borrow the writing session's source grant.
        from app.models.chat_session import ChatSession
        later = ChatSession(id=uuid.uuid4(), tenant_id=n.tenant, agent_id=n.agent, user_id=n.user,
            title="Later approved send")
        db.add(later)
        from app.models.runtime_task import RuntimeTask
        old_run = await db.get(RuntimeTask, n.run)
        run_id = uuid.uuid4()
        run = RuntimeTask(id=run_id, tenant_id=n.tenant, parent_agent_id=n.agent, child_agent_id=n.agent, root_user_id=n.user,
            task_type=old_run.task_type, status="running", parent_session_id=str(later.id), child_session_id=str(later.id),
            root_session_id=str(later.id), root_runtime_task_id=run_id, prompt="Email the report",
            metadata_json={"turn_id": f"turn-{run_id.hex}"})
        db.add(run)
        await db.commit()
        later_n = SimpleNamespace(**{**vars(n), "session": later.id, "run": run.id})
        messages = await _admitted_write_messages(db, later_n, content="Email my report")
        read = await _step(later_n, db, "read_file", {"path": path}, messages, 1)
        assert "PRIVATE_DOCUMENT_D" in str(read.result) and read.complete
        await _resolve_email(later_n, db, app_user_sessionmaker, monkeypatch, read.message, index=2)
        assert successor.content_sha256 == hashlib.sha256(target.read_bytes()).hexdigest(), "BOT_WRITE_LOST_SUCCESSOR_RECEIPT"
        assert dependency in successor.dependencies_json, "BOT_WRITE_DROPPED_DOCUMENT_D"
        assert inherited is None or inherited in successor.dependencies_json, "BOT_WRITE_DROPPED_INHERITED_SOURCES"


async def test_process_with_unbound_input_keeps_restricted_successor(native_workspace, owner_sessionmaker, monkeypatch):
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.security_audit import ResourcePermission
    from app.core.resource_authority import workspace_resource_id
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.content_input_receipts import latest_workspace_receipt, ContentInputReceiptError, validate_browser_input_dependencies
    from app.models.user import User

    n = native_workspace
    n.target.write_text("FOREIGN_UNBOUND_BODY")
    async def process(_command, *, work_dir, **kwargs):
        content = (work_dir / "private-report.md").read_text()
        (work_dir / "unbound-result.md").write_text(content)
        return CodeExecutionResult(stdout=content, exit_code=0)
    monkeypatch.setattr(code_exec, "execute_agent_command", process)
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.command.execute", allowed=True, requires_approval=False))
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path), actions=["read"], conditions={}))
        await db.commit()
        messages = await _admitted_write_messages(db, n, content="Copy a granted input")
        step = await _step(n, db, "run_command", {"command": "cp private-report.md unbound-result.md"}, messages, 1)
        assert "FOREIGN_UNBOUND_BODY" in str(step.result), step.result
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, "workspace/unbound-result.md")
        assert receipt is not None, "UNBOUND_PROCESS_WRITE_LOST_RECEIPT"
        assert any(source["source_kind"] == "session_tool_invocation" and source["source_id"] == str(step.invocation.id)
            for source in receipt.dependencies_json)
        await db.commit()
        response = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": "workspace/unbound-result.md"})
        assert response.status_code == 200 and "FOREIGN_UNBOUND_BODY" in response.text
        with pytest.raises(ContentInputReceiptError):
            await validate_browser_input_dependencies(db, n.tenant, await db.get(User, n.user), receipt.dependencies_json)
        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent,
            ChatArtifact.path == "workspace/unbound-result.md"))
        assert artifact is None


@pytest.mark.parametrize("mode", ["plan", "dontAsk"])
async def test_outward_deny_explains_approval(mode, native_workspace):
    from app.runtime.ccplus_contracts import PermissionProfileV1
    from app.tools.governance import _emit_session_no_policy_result, ToolGovernanceContext
    n = native_workspace
    result = await _emit_session_no_policy_result(ToolGovernanceContext(agent_id=n.agent, user_id=n.user,
        tenant_id=str(n.tenant), tool_name="send_email", arguments={}, permission_profile=PermissionProfileV1(mode=mode)),
        capability="tool.action_preflight.send_email", reason="preflight_ask", action="deny", event_callback=None)
    assert "requires approval" in str(result) and "no enterprise capability policy" not in str(result), result


@pytest.mark.parametrize("mismatch", ["path", "agent", "user", "operator"])
async def test_execution_input_denials_are_typed(mismatch):
    from app.services.content_input_receipts import authorize_workspace_execution_inputs
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    agent, user = uuid.uuid4(), uuid.uuid4()
    scope = WorkspaceAuthorityScope(agent_id=agent, user_id=user, root_session_id=None,
        allowed_paths=frozenset({"workspace/report.md"}), operator_view=False, authority_source="owner")
    if mismatch in {"agent", "user"}:
        scope = replace(scope, **{f"{mismatch}_id": uuid.uuid4()})
    elif mismatch == "operator":
        scope = replace(scope, operator_view=True)
    request = ToolExecutionRequest(tool_name="office_document_apply", arguments={"path": "workspace/report.md",
        "resource_paths": ["../../etc/passwd"] if mismatch == "path" else []},
        context=ToolExecutionContext(agent_id=agent, user_id=user, tenant_id=str(uuid.uuid4()),
            workspace=Path("/tmp"), workspace_authority_scope=scope))
    result = await authorize_workspace_execution_inputs(request)
    assert "content_source_unavailable" in str(result) if mismatch == "path" else "content_source_access_denied" in str(result)


async def test_missing_owned_file_keeps_actual_error(native_workspace, owner_sessionmaker):
    from app.services.workspace_resource_authority import register_workspace_path
    n = native_workspace
    path = "workspace/missing-human-file.md"
    async with owner_sessionmaker() as db:
        await register_workspace_path(db, tenant_id=n.tenant, agent_id=n.agent, path=path,
            owner_user_id=n.user, root_session_id=n.session, source="user_upload")
        await db.commit()
    result = await n.service.execute("read_file", {"path": path}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    assert "not found" in str(result).lower() and "workspace_input_read_unverified" not in str(result), result
    from app.services.content_input_receipts import settle_workspace_receipt_read
    from app.services.workspace_resource_authority import WorkspaceInputRead
    read = WorkspaceInputRead(path=path, receipt_id=None, revision=0, content_sha256="", dependencies_hash="")
    replacement, _evidence = await settle_workspace_receipt_read(request=SimpleNamespace(
        context=SimpleNamespace(), tool_name="read_file"), read=read, tool_call_id="missing", result=result)
    assert replacement is None, "MISSING_FILE_ERROR_REPLACED_BY_BINDING_503"


async def test_handover_revokes_pending_session_approval(native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    n = native_workspace
    async with owner_sessionmaker() as db:
        messages = await _admitted_write_messages(db, n, content="Email a summary")
        await _resolve_email(n, db, app_user_sessionmaker, monkeypatch, messages[0], index=1, handover=True)


@pytest.mark.parametrize("reader", ["shared_member", "operator"])
@pytest.mark.parametrize("surface", ["files", "download", "office", "projects"])
async def test_shared_readers_keep_private_document_boundary(native_workspace, owner_sessionmaker, monkeypatch, reader, surface):
    from docx import Document
    from app.api import office, projects
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.office_document_service import OfficeDocumentService

    n = native_workspace
    path = "workspace/shared.docx"
    await _create(n, path, "Temporary content")
    document = Document()
    document.add_paragraph("PRIVATE_DOCUMENT_D")
    document.save(n.root / path)
    class Adapter:
        def run(self, command, target, **kwargs):
            return {"success": True, "data": {"results": [{"path": "/body/p[1]", "text": "PRIVATE_DOCUMENT_D"}], "matches": 1}}
    monkeypatch.setattr(office, "OfficeDocumentService", lambda workspace, **kwargs: OfficeDocumentService(workspace, adapter=Adapter(), **kwargs))
    n.client._transport.app.include_router(office.router)
    n.client._transport.app.include_router(projects.router)
    async with owner_sessionmaker() as db:
        _document_row, grant, dependency = await _document(n, db)
        grant.revoked_at = datetime.now(timezone.utc)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        receipt.dependencies_json = [dependency]
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.owner_user_id, manifest.root_session_id = n.foreign, None
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path), actions=["read"], conditions={}))
        await db.commit()
    if surface == "projects":
        params = {"expected_user_id": str(n.user), "expected_tenant_id": str(n.tenant)}
        project_id = uuid.uuid4()
        created = await n.client.post("/projects", params=params, json={"id": str(project_id), "title": "Shared file"})
        assert created.status_code == 201, created.text
        response = await n.client.post(f"/projects/{project_id}/entries", params=params, json={"id": str(uuid.uuid4()),
            "expected_revision": 1, "target_kind": "workspace_file", "agent_id": str(n.agent), "path": path})
        assert response.status_code == 403, response.text
    else:
        params = {"path": path}
        if reader == "operator" and surface == "files":
            params.update(operator_view=True, operator_reason="Inspect shared file")
        url = f"/agents/{n.agent}/files/download" if surface == "download" else f"/agents/{n.agent}/{surface}/content"
        response = await n.client.get(url, params=params)
        assert response.status_code == 403, response.text
    # The same bytes remain available to the owner even with an obsolete receipt hash.
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        manifest.owner_user_id = n.user
        await db.commit()
    if surface == "projects":
        response = await n.client.post(f"/projects/{project_id}/entries", params=params, json={"id": str(uuid.uuid4()),
            "expected_revision": 1, "target_kind": "workspace_file", "agent_id": str(n.agent), "path": path})
        assert response.status_code == 201, response.text
    else:
        response = await n.client.get(url, params={"path": path})
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("route", ["approved_send", "delegation", "first_task", "artifact"])
async def test_incomplete_successor_remains_owned_but_cannot_leave(native_workspace, owner_sessionmaker,
    app_user_sessionmaker, monkeypatch, route):
    from app.models.chat_artifact import ChatArtifact
    from app.models.session_v2 import SessionModelResult
    from app.services.content_input_receipts import latest_workspace_receipt, validate_browser_input_dependencies, ContentInputReceiptError
    from app.services.employee_creation_sources import _owned_context_source
    from app.services.learned_skill_sources import model_source
    from app.services.llm_client import LLMMessage
    from app.services.runtime_result_sources import _published_owned_delegation_input
    from app.models.user import User

    n = native_workspace
    path = "workspace/incomplete.md"
    async with owner_sessionmaker() as db:
        document, _grant, dependency = await _document(n, db)
        messages = await _admitted_write_messages(db, n, content="Read D and write report")
        read = await _read_document(n, db, document, messages)
        assert read.complete and dependency in read.sources
        # Exercise successors of historical incomplete requests, before ingress withholding existed.
        with legacy_model_requests(monkeypatch):
            written = await _step(n, db, "write_file", {"path": path, "content": "PRIVATE_DOCUMENT_D"},
                [*messages, read.message, LLMMessage(role="system", content="Unbound external input")], 2)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        assert receipt is not None, "INCOMPLETE_BOT_WRITE_HAS_NO_RECEIPT"
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == written.invocation.provider_request_id))
        marker = model_source(model)
        assert dependency in receipt.dependencies_json and marker in receipt.dependencies_json
        await db.commit()
        readable = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": path})
        assert readable.status_code == 200 and "PRIVATE_DOCUMENT_D" in readable.text
        read = await _step(n, db, "read_file", {"path": path}, messages, 3)
        assert read.complete and "PRIVATE_DOCUMENT_D" in str(read.result)
        if route == "approved_send":
            await _resolve_email(n, db, app_user_sessionmaker, monkeypatch, read.message, index=4)
        elif route == "first_task":
            assert not await _owned_context_source(db, dependency=marker, agent_id=n.agent,
                tenant_id=n.tenant, session_id=str(n.session), user_id=n.user)
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, n.tenant, await db.get(User, n.user), receipt.dependencies_json)
        elif route == "delegation":
            assert not await _published_owned_delegation_input(db, marker, SimpleNamespace(child_agent_id=n.agent,
                tenant_id=n.tenant, child_session_id=str(n.session), id=n.run, root_user_id=n.user))
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, n.tenant, await db.get(User, n.user), receipt.dependencies_json)
        else:
            artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.agent_id == n.agent, ChatArtifact.path == path))
            assert artifact is None, "INCOMPLETE_CONTENT_PUBLISHED_AS_ARTIFACT"
