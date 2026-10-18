"""Selected project inputs use actual PostgreSQL, HTTP and canonical runtime source gates."""

import hashlib
import json
from uuid import uuid4

import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m2m_project_workbench_postgres import project_http, native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _project(p, owner_sessionmaker, *, other_agent=False):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession

    n = p.n
    source_agent, source_session = n.agent, n.session
    if other_agent:
        source_agent, source_session = uuid4(), uuid4()
        async with owner_sessionmaker() as db:
            db.add(Agent(id=source_agent, tenant_id=n.tenant, creator_id=n.user, name="Source employee"))
            await db.flush()
            db.add(ChatSession(id=source_session, tenant_id=n.tenant, agent_id=source_agent, user_id=n.user, title="Original source work"))
            await db.commit()
    path = "workspace/project-source.md"
    body = "CURRENT SAVED SOURCE\n" + "资料内容🌱" * 3000 + "\nEXACT_FINAL_SOURCE_TAIL"
    saved = await n.client.put(f"/agents/{source_agent}/files/content", params={"path": path}, json={"content": body})
    assert saved.status_code == 200, saved.text
    project, file_entry, work_entry = uuid4(), uuid4(), uuid4()
    created = await p.request("POST", body={"id": str(project), "title": "Selected context"})
    assert created.status_code == 201, created.text
    for revision, entry_id, kind, target in [(1, file_entry, "workspace_file", {"path": path}),
        (2, work_entry, "session", {"session_id": str(source_session)})]:
        linked = await p.request("POST", f"/{project}/entries", {"id": str(entry_id), "expected_revision": revision,
            "target_kind": kind, "agent_id": str(source_agent), **target, "section": "Research"})
        assert linked.status_code == 201, linked.text
    return {"project_id": str(project), "entry_ids": [str(file_entry), str(work_entry)], "source_agent": source_agent,
        "source_session": source_session, "path": path, "body": body}


async def _input(p, selected, app_user_sessionmaker):
    from app.services.session_model_round import bind_round_inputs
    from app.models.session_v2 import SessionTurnInput

    n = p.n
    input_id = uuid4()
    response = await n.client.post(f"/agents/{n.agent}/sessions/{n.session}/runs", json={"content": "Use these project sources",
        "parts": [{"type": "project_ref", "project_id": selected["project_id"], "entry_ids": selected["entry_ids"]}],
        "input_id": str(input_id), "idempotency_key": f"project-input:{input_id}"})
    assert response.status_code in {201, 202}, response.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=1)
        await db.commit()
        row = await db.get(SessionTurnInput, input_id)
        assert row.status in {"bound", "applied"} and row.bound_round_id
    return str(input_id)


async def _read(n, input_id, selected, **extra):
    raw = await n.service.execute("read_project_resource", {"input_id": input_id,
        "project_id": selected["project_id"], **extra}, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=f"turn-{n.run.hex}",
        tool_call_id=f"project-read-{uuid4()}", emit_runtime_hooks=False)
    return json.loads(str(raw))


async def _sealed_read(p, selected, input_id, app_user_sessionmaker, *, expected_status="ok"):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_human_input import input_parts_to_runtime_messages
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation
    from app.services.web_chat_runtime import _persist_tool_call
    from app.services.session_permission_runtime import _execution_evidence

    n = p.n
    arguments = {"input_id": input_id, "project_id": selected["project_id"], "entry_id": selected["entry_ids"][0]}
    call = f"project-source-{uuid4()}"
    turn = f"turn-{n.run.hex}"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        row = await db.get(SessionTurnInput, input_id)
        messages = _mid_run_items_to_user_messages(input_parts_to_runtime_messages([row]))
        request_id = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, messages=messages,
            tools=[{"type": "function", "function": {"name": "read_project_resource"}}], provider="openai", model="synthetic-no-provider")
        await seal_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request_id,
            response={"content": None, "tool_calls": [{"id": call, "type": "function", "function": {
                "name": "read_project_resource", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        await commit_sealed_model_round(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=turn, round_index=1, provider_request_id=request_id)
        invocation = await prepare_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, provider_request_id=request_id, provider_tool_use_id=call, tool_name="read_project_resource", arguments=arguments)
        await db.commit()
    trace = {}
    raw = await n.service.execute("read_project_resource", arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=call, emit_runtime_hooks=False, trace_metadata_sink=trace)
    assert json.loads(str(raw))["status"] == expected_status, raw
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": "read_project_resource", "args": arguments, "status": "done", "result": str(raw),
        "tool_call_id": call, "provider_request_id": request_id, "runtime_task_id": str(n.run),
        "tool_execution_evidence": _execution_evidence(trace)})
    return invocation.id


async def test_sealed_project_read_preserves_source_closure_and_revocation_for_derived_outputs(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    from app.models.workbench_project import WorkbenchProjectEntry
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.content_input_receipts import validate_runtime_input_dependencies, validate_browser_input_dependencies, ContentInputReceiptError
    from app.models.user import User
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    p = project_http
    selected = await _project(p, owner_sessionmaker, other_agent=True)
    selected["project_id"] = selected["project_id"].upper()
    input_id = await _input(p, selected, app_user_sessionmaker)
    invocation_id = await _sealed_read(p, selected, input_id, app_user_sessionmaker)
    request = ToolExecutionRequest(tool_name="write_file", arguments={}, context=ToolExecutionContext(
        agent_id=p.n.agent, user_id=p.n.user, tenant_id=p.n.tenant, session_id=str(p.n.session),
        runtime_task_id=str(p.n.run), workspace=p.n.root))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, p.n.tenant)
        sources, complete = await _origin_dependencies(db, tenant_id=p.n.tenant, agent_id=p.n.agent,
            ref=f"session-tool-result:{invocation_id}")
        assert complete and any(s["source_kind"] == "project_resource" for s in sources), sources
        await validate_runtime_input_dependencies(db, request, sources)
        await validate_browser_input_dependencies(db, p.n.tenant, await db.get(User, p.n.user), sources)
    async with owner_sessionmaker() as db:
        entry = await db.get(WorkbenchProjectEntry, selected["entry_ids"][0])
        manifest = await db.get(WorkspaceResourceManifest, entry.target_id)
        manifest.owner_user_id = p.n.foreign
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, p.n.tenant)
        with pytest.raises(ContentInputReceiptError, match="no longer authorized"):
            await validate_runtime_input_dependencies(db, request, sources)
        with pytest.raises(ContentInputReceiptError, match="no longer authorized"):
            await validate_browser_input_dependencies(db, p.n.tenant, await db.get(User, p.n.user), sources)


async def test_removed_unread_reference_does_not_revoke_other_selected_files_and_paging_detects_new_saved_bytes(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    p = project_http
    selected = await _project(p, owner_sessionmaker)
    input_id = await _input(p, selected, app_user_sessionmaker)
    first = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0], limit=256)
    assert first["status"] == "ok", first
    removed = await p.request("PATCH", f"/{selected['project_id']}/entries/{selected['entry_ids'][1]}",
        {"expected_revision": 3, "removed": True})
    assert removed.status_code == 200, removed.text
    surviving = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0], limit=256)
    assert surviving["status"] == "ok", surviving
    index = await _read(p.n, input_id, selected)
    assert json.loads(index["content"])["references"][1]["status"] == "unavailable", index
    saved = await p.n.client.put(f"/agents/{p.n.agent}/files/content", params={"path": selected["path"]},
        json={"content": "NEW HUMAN SAVED BYTES"})
    assert saved.status_code == 200, saved.text
    stale = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0], offset=256, expected_sha256=first["sha256"])
    assert stale["status"] == "stale_resource", stale
    current = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0], expected_sha256=stale["actual_sha256"])
    assert current["content"] == "NEW HUMAN SAVED BYTES", current


async def test_typed_no_data_denial_preserves_safe_downstream_model_sources(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    from app.models.workbench_project import WorkbenchProjectEntry
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.content_input_receipts import validate_runtime_input_dependencies
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    p = project_http
    selected = await _project(p, owner_sessionmaker, other_agent=True)
    input_id = await _input(p, selected, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        entry = await db.get(WorkbenchProjectEntry, selected["entry_ids"][0])
        (await db.get(WorkspaceResourceManifest, entry.target_id)).owner_user_id = p.n.foreign
        await db.commit()
    invocation = await _sealed_read(p, selected, input_id, app_user_sessionmaker, expected_status="denied")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, p.n.tenant)
        sources, complete = await _origin_dependencies(db, tenant_id=p.n.tenant, agent_id=p.n.agent,
            ref=f"session-tool-result:{invocation}")
        assert complete and not any(dep["source_kind"] == "project_resource" for dep in sources), sources
        request = ToolExecutionRequest(tool_name="write_file", arguments={}, context=ToolExecutionContext(
            agent_id=p.n.agent, user_id=p.n.user, tenant_id=p.n.tenant, session_id=str(p.n.session),
            runtime_task_id=str(p.n.run), workspace=p.n.root))
        await validate_runtime_input_dependencies(db, request, sources)


async def test_human_revision_can_select_project_context_without_changing_the_immutable_original_command(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    from app.services.session_model_round import bind_round_inputs
    from app.models.session_v2 import SessionTurnInput, SessionCommand

    p = project_http
    n = p.n
    selected = await _project(p, owner_sessionmaker)
    input_id = uuid4()
    original = await n.client.post(f"/agents/{n.agent}/sessions/{n.session}/runs", json={"content": "Original queued task", "input_id": str(input_id)})
    assert original.status_code in {201, 202}, original.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id)
        original_hash = command.request_hash
    parts = [{"type": "text", "text": "Revised task with selected sources"},
        {"type": "project_ref", "project_id": selected["project_id"], "entry_ids": selected["entry_ids"]}]
    revised = await n.client.patch(f"/agents/{n.agent}/sessions/{n.session}/inputs/{input_id}",
        headers={"If-Match": '"1"'}, json={"content_parts": parts})
    assert revised.status_code == 200 and revised.json()["revision"] == 2, revised.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        await bind_round_inputs(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            run_id=n.run, turn_id=f"turn-{n.run.hex}", round_index=1)
        await db.commit()
        row = await db.get(SessionTurnInput, input_id)
        assert (await db.get(SessionCommand, row.command_id)).request_hash == original_hash
    consumed = await _read(n, str(input_id), selected, entry_id=selected["entry_ids"][0], limit=256)
    assert consumed["status"] == "ok" and consumed["content"] == selected["body"][:256], consumed


async def test_selected_cross_employee_source_is_read_losslessly_without_source_agent_impersonation(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    from app.services.content_input_receipts import validate_runtime_input_dependencies, ContentInputReceiptError, latest_workspace_receipt
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    p = project_http
    selected = await _project(p, owner_sessionmaker, other_agent=True)
    input_id = await _input(p, selected, app_user_sessionmaker)
    first = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0])
    assert first["status"] == "ok", first
    index = await _read(p.n, input_id, selected)
    assert index["status"] == "ok", index
    assert "Source employee" not in index["content"]  # No synthesized Agent identity.
    assert "project-source.md" in index["content"] and "Original source work" in index["content"]
    chunks, offset, digest = [], 0, None
    while True:
        page = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0], offset=offset, limit=4096,
            **({"expected_sha256": digest} if digest else {}))
        assert page["status"] == "ok", page
        chunks.append(page["content"])
        digest = page["sha256"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(chunks) == selected["body"]
    assert digest == hashlib.sha256(selected["body"].encode()).hexdigest()
    # The scoped reader must not leak permission into ordinary file tools or source validators.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, p.n.tenant)
        receipt = await latest_workspace_receipt(db, p.n.tenant, selected["source_agent"], selected["path"])
        request = ToolExecutionRequest(tool_name="read_file", arguments={}, context=ToolExecutionContext(
            agent_id=p.n.agent, user_id=p.n.user, tenant_id=p.n.tenant, session_id=str(p.n.session),
            runtime_task_id=str(p.n.run), workspace=p.n.root))
        with pytest.raises(ContentInputReceiptError, match="another employee"):
            await validate_runtime_input_dependencies(db, request, [{"source_kind": "workspace_artifact",
                "source_id": str(receipt.id), "source_version": receipt.content_sha256}])


async def test_selection_cannot_be_created_by_an_internal_or_runtime_result_input(project_http, owner_sessionmaker):  # noqa: F811
    from fastapi import HTTPException
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input

    p = project_http
    selected = await _project(p, owner_sessionmaker)
    async with owner_sessionmaker() as db:
        with pytest.raises(HTTPException) as denied:
            await submit_live_human_input(db=db, agent=await db.get(Agent, p.n.agent), user=await db.get(User, p.n.user),
                session=await db.get(ChatSession, p.n.session), content="Untrusted selection", source="forged_source_string",
                parts=[{"type": "project_ref", "project_id": selected["project_id"], "entry_ids": selected["entry_ids"]}])
        assert denied.value.status_code == 403 and denied.value.detail["code"] == "project_selection_origin_denied"


async def test_native_worker_consumes_a_selected_file_created_by_another_employee_and_delivers_in_the_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    import shlex
    import sys
    from pathlib import Path
    from uuid import UUID
    from sqlalchemy import select
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_artifact import ChatArtifact
    from app.services.agent_manager import AgentManager
    from tests.integration.test_m2c_managed_environment_postgres import _exercise_real_worker_environment
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    source = "REAL_NATIVE_SOURCE_原始员工资料"
    code = f"from pathlib import Path; Path('saved.txt').write_text('CANONICAL_ENVIRONMENT_FILE'); Path('project.md').write_text({source!r}); print('FIRST_COMMAND')"
    command = f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(code)}"

    async def consume(seed, client, url, environment_scope, current):
        from app.api import files, chat_sessions, projects
        from app.runtime import invoker
        from app.services.content_input_receipts import latest_workspace_receipt
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
        from app.services.web_chat_runtime import execute_web_chat_run
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        for router in (files.router, chat_sessions.router, projects.router):
            client._transport.app.include_router(router, prefix="/api")
        receiver, work, project, entry, work_entry, input_id = (uuid4() for _ in range(6))
        # Provision fixture identities through the same owner setup seam as
        # the source employee. All HTTP/runtime consumption stays NOBYPASSRLS.
        async with owner_sessionmaker() as db:
            original = await db.get(Agent, seed["agent_id"])
            agent = Agent(id=receiver, tenant_id=seed["tenant_id"], creator_id=seed["user_id"], owner_user_id=seed["user_id"],
                name="Project recipient", primary_model_id=original.primary_model_id, default_session_permission_mode="bypassPermissions")
            db.add(agent)
            await db.flush()
            await AgentManager().initialize_agent_files(db, agent, personality="Use selected project sources", original_author_id=seed["user_id"])
            from app.models.capability_policy import CapabilityPolicy

            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.file.write", allowed=True, requires_approval=False))
            db.add(ChatSession(id=work, tenant_id=seed["tenant_id"], agent_id=receiver, user_id=seed["user_id"], source_channel="web"))
            await db.commit()
        scope = {"expected_tenant_id": str(seed["tenant_id"]), "expected_user_id": str(seed["user_id"])}
        created = await client.post("/api/projects", params=scope, json={"id": str(project), "title": "Native cross-employee sources"})
        assert created.status_code == 201, created.text
        added = await client.post(f"/api/projects/{project}/entries", params=scope, json={"id": str(entry), "expected_revision": 1,
            "target_kind": "workspace_file", "agent_id": str(seed["agent_id"]), "path": "workspace/project.md", "section": "Sources"})
        assert added.status_code == 201, added.text
        linked_work = await client.post(f"/api/projects/{project}/entries", params=scope, json={"id": str(work_entry), "expected_revision": 2,
            "target_kind": "session", "agent_id": str(seed["agent_id"]), "session_id": str(seed["session_id"]), "section": "Original work"})
        assert linked_work.status_code == 201, linked_work.text
        started = await client.post(f"/api/agents/{receiver}/sessions/{work}/runs", json={"content": "Read the selected project source and derive a report in this work.",
            "input_id": str(input_id), "parts": [{"type": "project_ref", "project_id": str(project), "entry_ids": [str(entry), str(work_entry)]}]})
        assert started.status_code == 201, started.text
        run_id = UUID(started.json()["run_id"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            claims = await RuntimeTaskClaimService(db=db, worker_id="project-recipient", lease_seconds=600).claim_available()
            claim = next(row for row in claims if row.id == run_id)
            version = claim.claim_version
            await db.commit()
        model = ScriptedLLMClient([
            {"tool_calls": [{"id": "project-source-read", "type": "function", "function": {"name": "read_project_resource", "arguments": json.dumps({
                "input_id": str(input_id), "project_id": str(project), "entry_id": str(entry)})}}], "finish_reason": "tool_calls"},
            {"tool_calls": [{"id": "project-original-work-read", "type": "function", "function": {"name": "read_project_resource", "arguments": json.dumps({
                "input_id": str(input_id), "project_id": str(project), "entry_id": str(work_entry), "limit": 12000})}}], "finish_reason": "tool_calls"},
            {"tool_calls": [{"id": "project-derived-write", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({
                "path": "workspace/project-report.md", "content": "Derived from: " + source})}}], "finish_reason": "tool_calls"},
            {"content": "Project source consumed in this work.\nDELIVERABLE: workspace/project-report.md", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
        fence = set_runtime_task_fence(task_id=run_id, worker_id="project-recipient", claim_version=version)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(fence)
        observed = next(message for message in model.requests[1]["messages"] if message.get("tool_call_id") == "project-source-read")
        page = json.loads(observed["content"])
        assert page["status"] == "ok" and page["content"] == source, page
        observed_work = next(message for message in model.requests[2]["messages"] if message.get("tool_call_id") == "project-original-work-read")
        work_page = json.loads(observed_work["content"])
        assert work_page["status"] == "ok" and "FIRST_COMMAND" in work_page["content"], work_page
        assert any(tool["function"]["name"] == "read_project_resource" for tool in model.requests[0]["tools"])
        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="project-consumption-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert boundary["delivered"] == 1, boundary
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            report = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == run_id, ChatArtifact.agent_id == receiver,
                ChatArtifact.session_id == work, ChatArtifact.path == "workspace/project-report.md"))
            assert report is not None and report.snapshot_json["content_input_receipt_id"]
            receipt = await latest_workspace_receipt(db, seed["tenant_id"], receiver, report.path)
            assert any(dep["source_kind"] == "project_resource" for dep in receipt.dependencies_json), receipt.dependencies_json
            assert (await db.get(RuntimeTask, run_id)).status == "completed"
        read = await client.get(f"/api/agents/{receiver}/files/artifacts/{report.id}/content")
        assert read.status_code == 200 and source in read.json()["content"], read.text
        # Recheck the source through its real managed command environment. This
        # also fulfils the shared environment harness's recovery-lease contract.
        check_input = await client.post(f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs",
            json={"content": "Verify the original source is unchanged after the other employee consumed it."})
        assert check_input.status_code == 201, check_input.text
        check_run = UUID(check_input.json()["run_id"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            claims = await RuntimeTaskClaimService(db=db, worker_id="project-source-check", lease_seconds=600).claim_available()
            check_version = next(row for row in claims if row.id == check_run).claim_version
            await db.commit()
        verify_code = f"from pathlib import Path; import hashlib; body=Path('project.md').read_text(); assert body=={source!r}; print('UNCHANGED_SOURCE_SHA='+hashlib.sha256(body.encode()).hexdigest())"
        check_command = f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(verify_code)}"
        check_model = ScriptedLLMClient([
            {"tool_calls": [{"id": "project-recipient-completion", "type": "function", "function": {"name": "run_command",
                "arguments": json.dumps({"command": check_command, "timeout": 10})}}], "finish_reason": "tool_calls"},
            {"content": "Original source bytes verified.", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: check_model)
        fence = set_runtime_task_fence(task_id=check_run, worker_id="project-source-check", claim_version=check_version)
        try:
            await execute_web_chat_run(check_run)
        finally:
            reset_runtime_task_fence(fence)
        assert "UNCHANGED_SOURCE_SHA=" + hashlib.sha256(source.encode()).hexdigest() in json.dumps(check_model.requests[-1]["messages"])
        settled = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="project-source-check-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert settled["delivered"] == 1, settled
        (tmp_path / "project-worker-consumption.json").write_text(json.dumps({"source_agent_id": str(seed["agent_id"]),
            "receiver_agent_id": str(receiver), "original_work_id": str(work), "input_id": str(input_id), "project_id": str(project),
            "report_artifact_id": str(report.id), "controlled_model_transport": True}, indent=2))
        return (await client.get(url, params=environment_scope)).json()["environment"]

    await _exercise_real_worker_environment(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        initial_command=command, initial_artifact_paths=["workspace/project.md"], initial_delivery=True,
        recovery=consume, recovery_invocation_key="project-recipient-completion")


async def test_selected_scope_and_live_revocation_are_local_to_each_reference(
    project_http, owner_sessionmaker, app_user_sessionmaker  # noqa: F811
):
    from app.models.workbench_project import WorkbenchProjectEntry
    from app.models.workspace_resource import WorkspaceResourceManifest

    p = project_http
    selected = await _project(p, owner_sessionmaker, other_agent=True)
    input_id = await _input(p, selected, app_user_sessionmaker)
    forged = await _read(p.n, input_id, selected, entry_id=str(uuid4()))
    assert forged["status"] == "denied" and forged["reason_code"] == "project_reference_not_selected", forged
    async with owner_sessionmaker() as db:
        entry = await db.get(WorkbenchProjectEntry, selected["entry_ids"][0])
        manifest = await db.get(WorkspaceResourceManifest, entry.target_id)
        manifest.owner_user_id = p.n.foreign
        await db.commit()
    index = await _read(p.n, input_id, selected)
    assert index["status"] == "ok", index
    items = json.loads(index["content"])["references"]
    assert items[0] == {"entry_id": selected["entry_ids"][0], "status": "unavailable", "retryable": True}
    assert items[1]["status"] == "ok" and "Original source work" in index["content"]
    denied = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0])
    assert denied["status"] == "denied" and "CURRENT SAVED SOURCE" not in json.dumps(denied), denied
    # An organization role is not an Agent source grant.
    from app.models.user import User
    async with owner_sessionmaker() as db:
        (await db.get(User, p.n.user)).role = "org_admin"
        await db.commit()
    denied_admin = await _read(p.n, input_id, selected, entry_id=selected["entry_ids"][0])
    assert denied_admin["status"] == "denied", denied_admin
