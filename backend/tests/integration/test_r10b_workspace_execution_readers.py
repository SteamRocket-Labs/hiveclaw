"""Workspace bytes use the output recipient's reader for every tool/run kind."""

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create
from tests.integration.test_r10a_workspace_source_admission import _unverifiable_history

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _execute(n, tool, path):
    arguments = ({"pattern": "SOURCE", "root": path} if tool == "grep_search" else
        {"command": "cat derived-source.md", "timeout": 10} if tool == "run_command" else
        {"language": "python", "code": "print('read')", "timeout": 10} if tool == "execute_code" else
        {"path": path, **({"selector": "paragraph"} if tool == "office_document_query" else {})})
    return await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)


def _local_process(monkeypatch, *, absent=(), present=None):
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult

    executions = []

    async def execute(_command, *, work_dir, **_kwargs):
        for path in absent:
            assert not (work_dir / path).exists(), f"EXCLUDED_INPUT_REACHED_PROCESS: {path}"
        for path, body in (present or {}).items():
            assert (work_dir / path).read_text() == body
        process = await asyncio.create_subprocess_exec("/bin/ls", "-1", cwd=work_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        assert process.returncode == 0 and stderr == b""
        executions.append(work_dir)
        return CodeExecutionResult(stdout=stdout.decode(), exit_code=0)

    monkeypatch.setattr(code_exec, "execute_agent_command", execute)
    return executions


async def _enable_commands(db, n):
    from app.models.capability_policy import CapabilityPolicy

    for capability in ("workspace.command.execute", "workspace.code.execute", "workspace.file.read"):
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
    await db.commit()


async def _set_run_kind(db, n, kind):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    task = await db.get(RuntimeTask, n.run)
    task.task_type = kind
    if kind == "delegation":
        parent, session = uuid.uuid4(), uuid.uuid4()
        db.add(Agent(id=parent, tenant_id=n.tenant, creator_id=n.user, name="Parent employee"))
        await db.flush()
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=parent, user_id=n.user))
        await db.flush()
        task.parent_agent_id, task.parent_session_id, task.root_session_id = parent, str(session), str(session)
        child = await db.get(ChatSession, n.session)
        child.parent_session_id, child.root_session_id = session, session
    await db.commit()


async def _personal_source(db, n, *, member_grant):
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.services.content_input_receipts import latest_workspace_receipt

    source = n.root.parent / "personal-source.md"
    source.write_text("PRIVATE_SOURCE")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant, scope_type="person", scope_id=n.foreign,
        source_kind="text", source_sha256=digest, canonical_md_path=source.name,
        canonical_md_sha256=digest, title="Private source", status="ready")
    db.add(document)
    if not member_grant:
        db.add(KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",
            resource_id=document.id, document_id=document.id, grantee_type="agent", grantee_id=n.agent,
            requester_user_id=n.user, session_id=str(n.session), purpose="interactive_session", permission="read",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    if member_grant:
        db.add(KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",
            resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=n.user,
            created_by_user_id=n.foreign, permission="read"))
    receipt = await latest_workspace_receipt(db, n.tenant, n.agent, "workspace/derived-source.md")
    receipt.dependencies_json = [{"source_kind": "knowledge_document", "source_id": str(document.id),
        "source_version": digest}]
    await db.commit()


@pytest.mark.parametrize("tool", ["grep_search", "run_command"])
@pytest.mark.parametrize("member_grant", [False, True])
async def test_personal_execution_inputs_use_member_grant(
    native_workspace, owner_sessionmaker, monkeypatch, tool, member_grant,  # noqa: F811
):
    n = native_workspace
    path = "workspace/derived-source.md"
    await _create(n, path, "DERIVED_SOURCE_BODY")
    async with owner_sessionmaker() as db:
        await _enable_commands(db, n)
        await _personal_source(db, n, member_grant=member_grant)
    executions = _local_process(monkeypatch, absent=() if member_grant else ("derived-source.md",),
        present={"derived-source.md": "DERIVED_SOURCE_BODY"} if member_grant else {})
    result = await _execute(n, tool, path)
    if tool == "grep_search":
        assert ("DERIVED_SOURCE_BODY" in str(result)) == member_grant, result
    else:
        assert len(executions) == 1, result
    assert "content_source_access_denied" not in str(result), result


@pytest.mark.parametrize("tool", ["run_command", "execute_code"])
async def test_delegation_execution_excludes_unpublishable_chain_and_keeps_own_write(
    native_workspace, owner_sessionmaker, monkeypatch, tool,  # noqa: F811
):
    n = native_workspace
    await _create(n, "workspace/derived-source.md", "CHILD_OWN_WRITE")
    async with owner_sessionmaker() as db:
        await _set_run_kind(db, n, "delegation")
        await _enable_commands(db, n)
        await _unverifiable_history(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root)
        await db.commit()
    executions = _local_process(monkeypatch, absent=("old-native-output.md", "intermediate-report.md", "earlier-report.md"),
        present={"derived-source.md": "CHILD_OWN_WRITE"})
    result = await _execute(n, tool, "workspace")
    assert len(executions) == 1, result
    assert "derived-source.md" in str(result) and "earlier-report.md" not in str(result), result
    assert "CHILD_OWN_WRITE" in str(await _execute(n, "read_file", "workspace/derived-source.md"))


@pytest.mark.parametrize("run_kind", ["web_chat_turn", "delegation"])
async def test_depth_three_execution_preflight_is_batched_once(
    native_workspace, owner_sessionmaker, monkeypatch, run_kind,  # noqa: F811
):
    from app.services import content_input_receipts as receipts
    from app.services import learned_skill_sources

    n = native_workspace
    async with owner_sessionmaker() as db:
        await _set_run_kind(db, n, run_kind)
        await _enable_commands(db, n)
        path, _source = await _unverifiable_history(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root)
        await db.commit()
    calls = Counter()
    active = ContextVar("r10b_preflight", default=False)
    native_preload = receipts._preload_execution_sources
    native_validate = receipts._validate_workspace_dependency
    native_model = learned_skill_sources._validate_model_source
    native_gate = receipts.authorize_workspace_execution_inputs

    async def preload(*args, **kwargs):
        calls["batch_preload"] += 1
        return await native_preload(*args, **kwargs)

    async def validate(*args, **kwargs):
        if active.get():
            calls["workspace_validation"] += 1
        return await native_validate(*args, **kwargs)

    async def model(*args, **kwargs):
        if active.get():
            calls["model_validation"] += 1
        return await native_model(*args, **kwargs)

    async def gate(request):
        from dataclasses import replace

        # Select one depth-three input, while retaining the real tool runtime,
        # actor resolution, PostgreSQL, receipt closure and search implementation.
        request.context.workspace_authority_scope = replace(request.context.workspace_authority_scope,
            allowed_paths=frozenset({path}))
        token = active.set(True)
        try:
            return await native_gate(request)
        finally:
            active.reset(token)

    monkeypatch.setattr(receipts, "_preload_execution_sources", preload)
    monkeypatch.setattr(receipts, "_validate_workspace_dependency", validate)
    monkeypatch.setattr(learned_skill_sources, "_validate_model_source", model)
    monkeypatch.setattr(receipts, "authorize_workspace_execution_inputs", gate)
    result = await _execute(n, "grep_search", path)
    assert "COMPANY_SOURCE_MARKER" not in str(result), result
    assert calls == {"batch_preload": 1, "workspace_validation": 3, "model_validation": 1}, calls
    print("R10B_EXECUTION_COST", json.dumps({"task_type": run_kind, "depth": 3, **calls}))


@pytest.mark.parametrize("tool", ["read_file", "grep_search"])
async def test_scoped_admin_cannot_consume_business_inspection_sources(
    native_workspace, owner_sessionmaker, tool,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from tests.integration.test_artifact_publication_transactions import stage_report

    n = native_workspace
    peer, session = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await _enable_commands(db, n)
        db.add(Agent(id=peer, tenant_id=n.tenant, name="Colleague", creator_id=n.foreign, owner_user_id=n.foreign))
        await db.flush()
        db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=peer, user_id=n.foreign))
        await db.flush()
        source = await stage_report(db, tenant=n.tenant, agent=peer, user=n.foreign, session=session,
            root=n.root.parent / str(peer), path="workspace/colleague.md")
        await db.flush()
        path = "workspace/derived-source.md"
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session, root=n.root,
            path=path, dependencies=[{"source_kind": "workspace_artifact", "source_id": str(source.id),
                "source_version": source.content_sha256}])
        await db.commit()
    result = await _execute(n, tool, path)
    assert "COMPANY_SOURCE_MARKER" not in str(result), result
    if tool == "read_file":
        assert "content_source_access_denied" in str(result), result


@pytest.mark.parametrize("tool", ["office_document_view", "office_document_query", "office_document_validate", "office_document_dump"])
async def test_office_rejects_changed_receipted_bytes_before_adapter(
    native_workspace, monkeypatch, tool,  # noqa: F811
):
    from app.services.office_document_service import OfficeDocumentService

    n = native_workspace
    path = "workspace/changed.docx"
    target = await _create(n, path, "RECEIPTED_BODY")
    target.write_text("UNRECEIPTED_BODY")
    invoked = []

    def adapter(*_args, **_kwargs):
        invoked.append(True)
        return {"text": target.read_text()}

    monkeypatch.setattr(OfficeDocumentService, "run_view", adapter)
    monkeypatch.setattr(OfficeDocumentService, "run_query", adapter)
    monkeypatch.setattr(OfficeDocumentService, "run_validate", adapter)
    monkeypatch.setattr(OfficeDocumentService, "run_dump", adapter)
    result = await _execute(n, tool, path)
    assert "UNRECEIPTED_BODY" not in str(result), result
    assert "content_source_changed" in str(result) and invoked == [], result


@pytest.mark.parametrize("path", ["memory/explicit/notes.docx", "memory/control/run.docx"])
async def test_office_keeps_its_workspace_boundary_before_memory_readers(monkeypatch, path):
    from types import SimpleNamespace
    from app.services import explicit_memory_authority
    from app.services.content_input_receipts import authorize_workspace_receipt_read

    async def unexpected(*_args):
        raise AssertionError("OFFICE_ENTERED_EXPLICIT_MEMORY_READER")

    monkeypatch.setattr(explicit_memory_authority, "authorize_explicit_memory_file_read", unexpected)
    request = SimpleNamespace(tool_name="office_document_dump", arguments={"path": path})
    assert await authorize_workspace_receipt_read(request) is None


async def test_fs_read_grep_has_incomplete_source_coverage(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.services.session_model_inputs import _resolve_origin_dependencies
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write

    n = native_workspace
    path = "workspace/grep-proof.md"
    await _create(n, path, "SOURCE_GREP_BODY")
    async with owner_sessionmaker() as db:
        invocation, arguments, turn = await _prepare_write(db, n, tool_name="fs_read",
            arguments={"mode": "grep", "path": path, "pattern": "SOURCE"},
            messages=await _admitted_write_messages(db, n, content="Search my file"))
        trace = {}
        result = await n.service.execute("fs_read", arguments, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace)
        assert "SOURCE_GREP_BODY" in str(result), result
        await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=invocation.id, provider_result_content=str(result), execution_evidence=_execution_evidence(trace))
        await db.commit()
        _sources, complete = await _resolve_origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
            ref=f"session-tool-result:{invocation.id}")
        assert complete is False
