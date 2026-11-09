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
