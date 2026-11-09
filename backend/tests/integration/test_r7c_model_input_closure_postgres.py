"""Own-run canonical evidence remains available to subsequent model requests."""

import json

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult
from tests.integration.test_r5j_private_session_model_inputs import t2_work  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("provider", ["openai", "minimax"])
async def test_native_kernel_context_and_own_tool_results_stay_bound(
    t2_work, app_user_sessionmaker, owner_sessionmaker, monkeypatch, provider,  # noqa: F811
):
    from app.runtime import invoker
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = t2_work
    from app.models.llm import LLMModel
    async with owner_sessionmaker() as db:
        model = await db.scalar(select(LLMModel).where(LLMModel.tenant_id == seed["tenant_id"]))
        model.provider = provider
        await db.commit()
    path = seed["package_path"] + "/summary.md"
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "r7c-read", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": path})}}], "finish_reason": "tool_calls"},
        {"content": "Own work is available.", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        origins = list(await db.scalars(select(InvocationSpan).where(
            InvocationSpan.runtime_task_id == seed["run_id"], InvocationSpan.span_type == "kernel_context")))
        assert origins and all(origin.metadata_json["context_origin"]["dependency_coverage"]["complete"]
            for origin in origins)
        entries = [entry for row in rows for entry in row.model_request_snapshot_json["input_bindings"]["messages"]]
        own = [entry for entry in entries if entry.get("source_ref", "").startswith((
            "session-tool-result:", "session-model-result:", "session-kernel-context:"))]
        assert own and all(entry["status"] == "bound" for entry in own), own


@pytest.mark.parametrize("tool", ["read_file", "edit_file", "grep_search", "list_files", "fs_list",
    "list_async_tasks", "reconcile_memory_write", "read_runtime_result"])
async def test_native_same_run_tools_are_available_to_the_next_request(
    native_workspace, app_user_sessionmaker, tool,  # noqa: F811
):
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts, _tool_execution_evidence
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import _persist_tool_call, conversation_from_history_messages
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write
    from tests.integration.test_m1g28_runtime_source_reads_postgres import _create

    n = native_workspace
    from app.models.capability_policy import CapabilityPolicy
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        for capability in ("workspace.file.read", "agent.memory.write"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    path = "workspace/r7c-owned.md"
    await _create(n, path, "R7C_AUTHORIZED_WORK")
    args = {
        "read_file": {"path": path},
        "edit_file": {"path": path, "old_text": "R7C_AUTHORIZED_WORK", "new_text": "R7C_EDITED_WORK"},
        "grep_search": {"root": "workspace", "pattern": "R7C_AUTHORIZED_WORK"},
        "list_files": {"path": "workspace"}, "fs_list": {"path": "workspace"},
        "list_async_tasks": {},
        "reconcile_memory_write": {"action": "status"},
        "read_runtime_result": {"result_ref": f"runtime-result://{n.run}/{'a' * 64}"},
    }[tool]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        call, _, turn = await _prepare_write(db, n, tool_name=tool, arguments=args,
            messages=await _admitted_write_messages(db, n))
    trace = {}
    value = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=call.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace))
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": args, "status": "done", "result": value,
        "tool_call_id": call.provider_tool_use_id, "provider_request_id": call.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _tool_execution_evidence(
            tool_name=tool, tool_args=args, trace_metadata=trace, machine_status="settled")})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        binding = await build_model_input_bindings(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, turn_id=turn, round_index=2, messages=messages,
            tools=[], provider="local", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        entry = next(item for item in binding["messages"] if item.get("source_ref") == f"session-tool-result:{call.id}")
        assistant = next(item for item in binding["messages"]
            if item.get("source_ref", "").startswith("session-model-result:"))
        assert assistant["dependency_status"] == "resolved"
        prepared = row.model_request_snapshot_json["input_bindings"]["messages"]
        assert prepared[assistant["message_index"]]["status"] == "bound", prepared[assistant["message_index"]]
        assert prepared[entry["message_index"]]["status"] == "bound", prepared[entry["message_index"]]
        assert entry["dependency_status"] == "resolved", entry
        assert any(message.get("content") == value for message in row.model_request_snapshot_json["wire_request"]["messages"])


@pytest.mark.parametrize("alteration", [None, "unknown", "changed_bytes", "other_requester"])
@pytest.mark.parametrize("tool", ["list_files", "fs_list"])
@pytest.mark.parametrize("historical_projection", [False, True])
async def test_permission_resume_retained_directory_observation_and_derived_reply(
    native_workspace, app_user_sessionmaker, tool, alteration, historical_projection,  # noqa: F811
):
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_session import ChatSession
    from app.services.llm_client import LLMMessage
    from app.services.session_model_inputs import build_model_input_bindings
    from app.services.session_model_round import prepare_model_request, commit_model_response
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation, mark_tool_effect_started
    from app.services.session_control_input import accept_tool_permission_response, apply_permission_response_control_input
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.models.user import User
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import _persist_tool_call, conversation_from_history_messages
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write
    from tests.integration.test_m1g28_runtime_source_reads_postgres import _create

    n = native_workspace
    await _create(n, "workspace/r7c-visible.md", "OWN_WORKSPACE_BODY")
    args = {"path": "workspace"}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        call, _, turn = await _prepare_write(db, n, tool_name=tool, arguments=args,
            messages=await _admitted_write_messages(db, n))
        # Persist the native waiting state and the authenticated user's actual
        # allow-once control; recovery must not apply to ordinary missing tags.
        from uuid import uuid4, uuid5
        await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=call.id, provider_result_content="", execution_evidence={
                "schema": "hive.tool_execution_evidence.v1", "status": "settled", "retryable": True,
                "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": f"permission-{call.id}",
                    "outcome": "require_approval", "input_hash": call.args_hash, "approval_id": str(uuid4())},
                "execution_frame": None})
        task = await db.get(RuntimeTask, n.run)
        task.status = "suspended"
        task.metadata_json = {**task.metadata_json, "interactive_pause": {
            "schema": "hive.tool_permission_wait.v1", "invocation_ids": [str(call.id)]}}
        await db.commit()
        await db.refresh(call)
        authority = await resolve_session_mutation_authority(db, user=await db.get(User, n.user),
            agent_id=n.agent, session_id=n.session, action="respond_tool_permission")
        control_id = uuid5(call.permission_item_id, "permission-response:allow_once")
        await accept_tool_permission_response(db, authority=authority, control_id=control_id,
            idempotency_key=f"permission:{call.permission_item_id}:allow_once", invocation_id=call.id,
            permission_item_id=call.permission_item_id, permission_request_version=call.permission_request_version,
            permission_authority_snapshot_hash=call.permission_authority_snapshot_hash,
            expected_run_id=n.run, decision="allow_once", response_schema="hive.tool_permission_response.v1")
        await apply_permission_response_control_input(db, authority=authority, control_id=control_id)
        await mark_tool_effect_started(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=call.id, effective_arguments=args, permission_control_id=control_id)
        await db.commit()
        await db.refresh(call)
        assert call.permission_state == "approved"
    trace = {}
    value = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=call.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace))
    assert "r7c-visible.md" in value
    capture = trace["native_directory_read_evidence"]
    assert capture["status"] == "captured"
    evidence = _execution_evidence(trace)
    if historical_projection:
        # Reproduce the persisted old permission-resume projection. The native
        # observation still exists; only its forwarding pointer was dropped.
        evidence.pop("native_directory_read_evidence", None)
        evidence.pop("native_empty_read_evidence", None)
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": args, "status": "done", "result": value,
        "tool_call_id": call.provider_tool_use_id, "provider_request_id": call.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": evidence})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        from uuid import UUID, uuid4
        proof = await db.get(InvocationSpan, UUID(capture["receipt_ref"].split(":", 1)[1]))
        if alteration == "unknown":
            await db.delete(proof)
        elif alteration == "changed_bytes":
            proof.metadata_json = {"native_directory_read_evidence": {
                **proof.metadata_json["native_directory_read_evidence"], "tool_result_hash": "0" * 64}}
        await db.commit()
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        session_id, run_id = n.session, n.run
        if alteration == "other_requester":
            # Use the other member's real private work, rather than corrupting
            # the authenticated requester of an existing admitted run.
            session_id, run_id = uuid4(), uuid4()
            turn = f"turn-{run_id.hex}"
            db.add(ChatSession(id=session_id, tenant_id=n.tenant, agent_id=n.agent,
                user_id=n.foreign, audience_kind="private"))
            await db.flush()
            db.add(RuntimeTask(id=run_id, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
                parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(session_id),
                child_session_id=str(session_id), root_user_id=n.foreign, root_session_id=str(session_id),
                root_runtime_task_id=run_id, prompt="Inspect my own work", metadata_json={"turn_id": turn}))
            await db.flush()
            messages = [LLMMessage(role="tool", tool_call_id=call.provider_tool_use_id, content=value,
                source_ref=f"session-tool-result:{call.id}")]
        binding = await build_model_input_bindings(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=session_id, messages=messages, wire_messages=_llm_messages_to_dicts(messages))
        entry = binding["messages"][0] if alteration == "other_requester" else next(
            item for item in binding["messages"] if item.get("source_ref") == f"session-tool-result:{call.id}")
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=session_id, run_id=run_id, turn_id=turn, round_index=2, messages=messages,
            tools=[], provider="local", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = row.model_request_snapshot_json
        output = json.dumps(snapshot["wire_request"], ensure_ascii=False)
        selected = snapshot["input_bindings"]["messages"][entry["message_index"]]
        if alteration in {"unknown", "changed_bytes"}:
            assert entry["dependency_status"] == "incomplete", entry
        if alteration:
            assert selected["status"] == "withheld"
            assert "r7c-visible.md" not in output
        else:
            assert selected["status"] == "bound" and "r7c-visible.md" in output
            assert entry["dependency_status"] == "resolved", entry
            await commit_model_response(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                run_id=n.run, turn_id=turn, round_index=2, provider_request_id=request,
                response={"content": "The directory contains r7c-visible.md.", "tool_calls": [], "finish_reason": "stop", "usage": {}})
            history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
            messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
            third = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
                session_id=n.session, run_id=n.run, turn_id=turn, round_index=3, messages=messages,
                tools=[], provider="local", model="synthetic-no-provider")
            derived = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == third))
            assert all(item["status"] == "bound" for item in derived.model_request_snapshot_json["input_bindings"]["messages"])


@pytest.mark.parametrize("tool", ["check_async_task", "list_async_tasks"])
async def test_owned_child_status_does_not_import_its_unknown_instruction(
    native_workspace, app_user_sessionmaker, tool,  # noqa: F811
):
    from uuid import uuid4
    from app.kernel.engine import _dicts_to_llm_messages, _tool_execution_evidence
    from app.models.runtime_task import RuntimeTask
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import _persist_tool_call, conversation_from_history_messages
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write

    n = native_workspace
    child = uuid4()
    marker = "R7C_UNKNOWN_CHILD_INSTRUCTION"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(RuntimeTask(id=child, tenant_id=n.tenant, task_type="delegation", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(n.session),
            child_session_id=str(uuid4()), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=n.run, delegation_chain_json=[str(n.agent)], prompt=marker))
        await db.commit()
        args = {"task_id": str(child)} if tool == "check_async_task" else {}
        call, _, turn = await _prepare_write(db, n, tool_name=tool, arguments=args,
            messages=await _admitted_write_messages(db, n))
    trace = {}
    value = str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=call.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace))
    body = json.loads(value)
    if tool == "check_async_task":
        assert body["status"] == "running" and body["result"] is None and body["receipt"] is None
        assert marker not in value
    else:
        assert marker in value  # A list includes the unknown instruction; it must remain withheld.
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": args, "status": "done", "result": value,
        "tool_call_id": call.provider_tool_use_id, "provider_request_id": call.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _tool_execution_evidence(
            tool_name=tool, tool_args=args, trace_metadata=trace, machine_status="settled")})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, turn_id=turn, round_index=2, messages=messages,
            tools=[], provider="local", model="synthetic-no-provider")
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        snapshot = row.model_request_snapshot_json
        entry = next(item for item in snapshot["input_bindings"]["messages"]
            if item.get("source_ref") == f"session-tool-result:{call.id}")
        assert entry["status"] == ("bound" if tool == "check_async_task" else "withheld"), entry
        wire = json.dumps(snapshot["wire_request"])
        assert marker not in wire
        if tool == "check_async_task":
            assert value in [message.get("content") for message in snapshot["wire_request"]["messages"]]


async def _round2_call(n, db_factory, tool, arguments, *, result_projection=None):
    from app.kernel.engine import _tool_execution_evidence
    from app.services.session_tool_runtime import mark_tool_effect_started
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write

    async with db_factory() as db:
        await pin_rls_tenant_context(db, n.tenant)
        call, _, turn = await _prepare_write(db, n, tool_name=tool, arguments=arguments,
            messages=await _admitted_write_messages(db, n))
    trace = {}
    async def start(payload):
        async with db_factory() as db:
            await pin_rls_tenant_context(db, n.tenant)
            await mark_tool_effect_started(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                invocation_id=call.id, effective_arguments=payload["arguments"])
            await db.commit()
    raw = await n.service.execute(tool, arguments, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
        tool_call_id=call.provider_tool_use_id, emit_runtime_hooks=False, trace_metadata_sink=trace,
        pre_effect_callback=start)
    value = str(raw)
    if result_projection is not None:
        from app.services.session_model_inputs import _hash
        value = result_projection(value)
        trace["tool_execution_frame"] = {**trace["tool_execution_frame"], "output_hash": _hash(value)}
    await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data={
        "name": tool, "args": arguments, "status": "done", "result": value,
        "tool_call_id": call.provider_tool_use_id, "provider_request_id": call.provider_request_id,
        "runtime_task_id": str(n.run), "tool_execution_evidence": _tool_execution_evidence(
            tool_name=tool, tool_args=arguments, trace_metadata=trace, machine_status="settled")})
    return call, value, turn


async def _round2_request(db, n, call, turn, *, messages=None, source_index=None):
    from app.kernel.engine import _dicts_to_llm_messages
    from app.services.session_model_round import prepare_model_request
    from app.services.session_semantic_history import load_session_semantic_history
    from app.services.web_chat_runtime import conversation_from_history_messages

    if messages is None:
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session)
        messages = _dicts_to_llm_messages(conversation_from_history_messages(history.messages))
    request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
        run_id=n.run, turn_id=turn, round_index=2, messages=messages, tools=[], provider="local",
        model="synthetic-no-provider")
    row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
    snapshot = row.model_request_snapshot_json
    entry = (snapshot["input_bindings"]["messages"][source_index] if source_index is not None else
        next(item for item in snapshot["input_bindings"]["messages"]
            if item.get("source_ref") == f"session-tool-result:{call.id}"))
    return entry, snapshot["wire_request"]


@pytest.mark.parametrize("tool,arguments", [
    ("browser_manage", {"action": "start", "expected_revision": 0, "expected_epoch": 0}),
    ("browser_observe", {}),
    ("edit_file", {"path": "workspace/r7c-edit.md", "old_text": "MISSING", "new_text": "NEW"}),
    ("read_file", {"path": "workspace/r7c-missing.md"}),
    ("reconcile_memory_write", {"action": "retry"}),
    ("check_async_task", {"task_id": "00000000-0000-0000-0000-000000000001"}),
])
async def test_round2_native_failed_feedback_reaches_next_request(
    native_workspace, app_user_sessionmaker, tool, arguments,  # noqa: F811
):
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionToolInvocation
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_semantic_history import _event_payload
    from tests.integration.test_m1g28_runtime_source_reads_postgres import _create

    n = native_workspace
    await _create(n, "workspace/r7c-edit.md", "AUTHORIZED_EDIT_INPUT")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        for capability in ("workspace.file.read", "agent.memory.write", "computer.browser.control", "computer.browser.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    call, value, turn = await _round2_call(n, app_user_sessionmaker, tool, arguments)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation = await db.get(SessionToolInvocation, call.id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        if tool == "read_file":
            # Production's older settlement retained the unavailable workspace
            # pointer, before the native no-body denial pointer was forwarded.
            metadata = {"workspace_input_receipt": {"status": "unavailable",
                "reason_code": "workspace_input_capture_unavailable"}}
            event.metadata_json = {**event.metadata_json, "v2_payload": {
                **_event_payload(event), "metadata": metadata}}
            await db.commit()
        assert invocation.effect_state == "failed", (value, invocation.effect_state)
        assert _event_payload(event)["outcome"] == "failed", value
        entry, wire = await _round2_request(db, n, call, turn)
        assert entry["status"] == "bound", json.dumps({"entry": entry, "value": value,
            "metadata": _event_payload(event).get("metadata")}, ensure_ascii=False)
        assert value in [message.get("content") for message in wire["messages"]]


@pytest.mark.parametrize("tool", ["list_files", "task_list"])
@pytest.mark.parametrize("extra_body", [False, True])
async def test_round2_unstarted_batch_abort_is_body_free(
    native_workspace, app_user_sessionmaker, tool, extra_body,  # noqa: F811
):
    from app.services.session_tool_runtime import complete_tool_invocation
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages, _prepare_write

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        call, _, turn = await _prepare_write(db, n, tool_name=tool, arguments={},
            messages=await _admitted_write_messages(db, n))
        body = {"schema": "hive.tool_batch_abort.v1", "status": "aborted",
            "reason_code": "session_permission_batch_suspended_before_sibling_start", "retryable": True,
            "provider_tool_use_id": call.provider_tool_use_id}
        if extra_body:
            body["partial_read"] = "R7C_UNVERIFIED_PARTIAL_READ"
        value = json.dumps(body, sort_keys=True, separators=(",", ":"))
        await complete_tool_invocation(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
            invocation_id=call.id, provider_result_content=value, execution_evidence={
                "schema": "hive.tool_execution_evidence.v1", "status": "aborted", "retryable": True,
                "pre_effect_fence_ref": f"session-permission-batch-abort:{call.provider_tool_use_id}",
                "tool_decision": None, "execution_frame": None}, effective_arguments={})
        await db.commit()
        assert call.effect_state == "prepared_not_started" and call.permission_state == "not_required"
        entry, wire = await _round2_request(db, n, call, turn)
        assert entry["status"] == ("withheld" if extra_body else "bound"), entry
        assert "R7C_UNVERIFIED_PARTIAL_READ" not in json.dumps(wire)


@pytest.mark.parametrize("alteration", [None, "changed_bytes", "missing_proof"])
async def test_round2_platform_skill_read_keeps_existing_source_binding(
    native_workspace, app_user_sessionmaker, alteration,  # noqa: F811
):
    from pathlib import Path
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionToolInvocation
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_semantic_history import _event_payload

    n = native_workspace
    path = "skills/mcp-installer/SKILL.md"
    target = n.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((Path(__file__).resolve().parents[2] / "agent_template" / path).read_bytes())
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read", allowed=True, requires_approval=False))
        await db.commit()
    call, value, turn = await _round2_call(n, app_user_sessionmaker, "read_file", {"path": path})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        invocation = await db.get(SessionToolInvocation, call.id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        pointer = _event_payload(event)["metadata"]["skill_input_receipt"]
        assert invocation.effect_state == "effect_committed", value
        assert pointer["status"] == "verified", pointer
        if alteration == "changed_bytes":
            target.write_text("R7C_CHANGED_SKILL_BYTES")
        if alteration == "missing_proof":
            from uuid import UUID
            proof = await db.get(InvocationSpan, UUID(pointer["evidence_ref"].split(":", 1)[1]))
            await db.delete(proof)
            await db.commit()
        entry, wire = await _round2_request(db, n, call, turn)
        assert entry["status"] == ("withheld" if alteration else "bound"), (entry, pointer)
        assert (value in [message.get("content") for message in wire["messages"]]) is (not alteration)


@pytest.mark.parametrize("alteration", [None, "other_member", "other_agent", "unpublished", "unknown_input", "unreadable_input"])
@pytest.mark.parametrize("historical_page", [False, True])
async def test_round2_published_child_continuation_is_bound_only_to_its_parent(
    native_workspace, app_user_sessionmaker, owner_sessionmaker, alteration, historical_page,  # noqa: F811
):
    from types import SimpleNamespace
    from uuid import uuid4
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_result import RuntimeResultObject
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import prepare_model_request, commit_model_response
    from app.services.session_terminal_outcome import prepare_and_seal_run_outcome, commit_terminal_outcome

    n = native_workspace
    child_agent, child_session, delegated_run, continuation = (uuid4() for _ in range(4))
    marker = "R7C_CANONICAL_CHILD_CONTINUATION_ANSWER"
    async with owner_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(Agent(id=child_agent, tenant_id=n.tenant, name="Continuation worker", creator_id=n.user,
            owner_user_id=n.user, sponsor_user_id=n.user))
        await db.flush()
        db.add(RuntimeTask(id=delegated_run, tenant_id=n.tenant, task_type="delegation", status="completed",
            parent_agent_id=n.agent, child_agent_id=child_agent, parent_session_id=str(n.session),
            child_session_id=str(child_session), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=n.run, delegation_chain_json=[str(n.agent)], prompt="Original delegated work"))
        await db.flush()
        db.add(ChatSession(id=child_session, tenant_id=n.tenant, agent_id=child_agent, user_id=n.user,
            source_channel="agent", session_kind="delegation_run", actor_type="agent", runtime_source="delegation",
            peer_agent_id=n.agent, parent_session_id=n.session, root_session_id=n.session,
            runtime_task_id=delegated_run, audience_kind="private"))
        await db.flush()
        db.add(RuntimeTask(id=continuation, tenant_id=n.tenant, task_type="a2a_continuation", status="running",
            parent_agent_id=child_agent, child_agent_id=child_agent, parent_session_id=str(child_session),
            child_session_id=str(child_session), root_user_id=n.user, root_session_id=str(n.session),
            root_runtime_task_id=continuation, delegation_chain_json=[str(n.agent)], prompt="Continue the delegated work",
            metadata_json={"turn_id": f"turn-{continuation.hex}"}))
        await db.flush()
        from app.services.runtime_root_ledger import register_runtime_task_root_item, RuntimeRootIntentSpec
        await register_runtime_task_root_item(db, task=await db.get(RuntimeTask, continuation),
            intent=RuntimeRootIntentSpec(intent_key=f"continuation:{continuation}", work_type="a2a_continuation",
                target_ref=str(child_session)))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        turn = f"turn-{continuation.hex}"
        from app.runtime.context_candidates import ContextMessageOrigin
        guidance = "Complete the requested delegated work using authorized sources."
        context = LLMMessage(role="system", content=guidance, context_origin=ContextMessageOrigin.capture(
            kind="frozen_prompt", role="system", content=guidance, unbound=()))
        request = await prepare_model_request(db, tenant_id=n.tenant, agent_id=child_agent, session_id=child_session,
            run_id=continuation, turn_id=turn, round_index=1, messages=[context], tools=[], provider="local",
            model="synthetic-no-provider")
        await commit_model_response(db, tenant_id=n.tenant, agent_id=child_agent, session_id=child_session,
            run_id=continuation, turn_id=turn, round_index=1, provider_request_id=request,
            response={"content": marker, "finish_reason": "stop"})
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        assert model.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"] is True
        outcome = await prepare_and_seal_run_outcome(db, tenant_id=n.tenant, agent_id=child_agent,
            session_id=child_session, run_id=continuation, turn_id=turn, terminal_result_id=model.id)
        await commit_terminal_outcome(db, tenant_id=n.tenant, agent_id=child_agent, session_id=child_session,
            run_id=continuation, outcome_id=outcome.id)
        from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle
        task = await db.get(RuntimeTask, continuation)
        await _apply_terminal_task_update_and_settle(db, task, status="completed", result_summary=marker,
            metadata_json=dict(task.metadata_json or {}), terminal_source="session_terminal_outcome.commit")
        await db.commit()
        result = await db.scalar(select(RuntimeResultObject).where(RuntimeResultObject.source_run_id == str(continuation)))
        assert result is not None and result.source_kind == "a2a_continuation"
        publication = await db.scalar(select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.result_object_id == result.id))
        assert publication.parent_agent_id == n.agent and publication.parent_session_id == n.session
        assert publication.parent_user_id == n.user
        ref = publication.result_ref
    def old_page(value):
        return json.dumps({**json.loads(value), "dependency_coverage": {"complete": False}})
    call, value, turn = await _round2_call(n, app_user_sessionmaker, "read_runtime_result", {"result_ref": ref},
        result_projection=old_page if historical_page else None)
    assert json.loads(value)["status"] == "ok" and marker in value, value
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        if alteration == "unpublished":
            await db.delete(await db.get(RuntimeNotificationOutbox, publication.id))
        if alteration in {"unknown_input", "unreadable_input"}:
            from app.services.session_model_inputs import _hash
            child_model = await db.get(SessionModelResult, model.id)
            bindings = child_model.model_request_snapshot_json["input_bindings"]
            if alteration == "unreadable_input":
                import hashlib
                from app.models.organization_identity import ContentInputReceipt
                receipt = ContentInputReceipt(id=uuid4(), tenant_id=n.tenant, carrier_kind="workspace_artifact",
                    carrier_ref=f"{n.agent}\n{n.path}", revision=1,
                    content_sha256=hashlib.sha256(n.target.read_bytes()).hexdigest(), dependencies_json=[])
                db.add(receipt)
                bindings = {**bindings, "dependencies": [{"source_kind": "workspace_artifact",
                    "source_id": str(receipt.id), "source_version": receipt.content_sha256}]}
            snapshot = {**child_model.model_request_snapshot_json, "input_bindings": {
                **bindings, "dependency_coverage": {"complete": alteration != "unknown_input"}}}
            child_model.model_request_snapshot_json, child_model.model_request_hash = snapshot, _hash(snapshot)
        await db.commit()
        reader, messages = n, None
        if alteration in {"other_member", "other_agent"}:
            agent = n.agent
            if alteration == "other_agent":
                agent = uuid4()
                async with owner_sessionmaker() as owner:
                    owner.add(Agent(id=agent, tenant_id=n.tenant, name="Other reader", creator_id=n.user, owner_user_id=n.user))
                    await owner.commit()
            session, run = uuid4(), uuid4()
            user = n.foreign if alteration == "other_member" else n.user
            if alteration == "other_member":
                from app.models.security_audit import ResourcePermission
                async with owner_sessionmaker() as owner:
                    owner.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=user,
                        resource_type="agent", resource_id=agent, actions=["execute"], effect="allow"))
                    await owner.commit()
            db.add(ChatSession(id=session, tenant_id=n.tenant, agent_id=agent, user_id=user, audience_kind="private"))
            await db.flush()
            db.add(RuntimeTask(id=run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
                parent_agent_id=agent, child_agent_id=agent, parent_session_id=str(session), child_session_id=str(session),
                root_user_id=user, root_session_id=str(session), root_runtime_task_id=run,
                prompt="Inspect my own work", metadata_json={"turn_id": f"turn-{run.hex}"}))
            await db.commit()
            reader = SimpleNamespace(**{**vars(n), "agent": agent, "user": user, "session": session, "run": run})
            original_page = value
            call, value, turn = await _round2_call(reader, app_user_sessionmaker, "read_runtime_result",
                {"result_ref": ref}, result_projection=lambda _value: original_page)
        entry, wire = await _round2_request(db, reader, call, turn, messages=messages,
            source_index=0 if messages is not None else None)
        assert entry["status"] == ("withheld" if alteration else "bound"), entry
        assert (marker in json.dumps(wire)) is (not alteration)


@pytest.mark.parametrize("tool,arguments", [
    ("edit_file", {"path": "workspace/r7c-missing.md", "old_text": "OLD", "new_text": "NEW"}),
    ("read_file", {"path": "workspace/r7c-missing.md"}),
    ("browser_observe", {}),
])
@pytest.mark.parametrize("shape", ["extra_field", "diagnostic_bytes"])
async def test_round2_failed_feedback_with_possible_read_bytes_stays_withheld(
    native_workspace, app_user_sessionmaker, tool, arguments, shape,  # noqa: F811
):
    from app.models.capability_policy import CapabilityPolicy
    from app.tools.result_envelope import render_tool_error

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        for capability in ("workspace.file.read", "computer.browser.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    marker = "R7C_UNVERIFIED_PARTIAL_READ"
    def partial(value):
        if shape == "extra_field":
            prefix, encoded = value.split("<tool_error>", 1)
            body = json.loads(encoded.removesuffix("</tool_error>"))
            return prefix + "<tool_error>" + json.dumps({**body, "partial_content": marker}) + "</tool_error>"
        return render_tool_error(tool_name=tool, error_class="operation_failed", provider="workspace",
            message=f"Read failed after receiving {marker}")
    call, value, turn = await _round2_call(n, app_user_sessionmaker, tool, arguments, result_projection=partial)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        entry, wire = await _round2_request(db, n, call, turn)
        assert entry["status"] == "withheld", entry
        assert marker not in json.dumps(wire)
