"""The parent's task reader must not bypass a refused colleague source."""

import json
from contextvars import ContextVar
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.invocation_span import InvocationSpan
from app.models.capability_policy import CapabilityPolicy
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration import test_web_chat_worker_restart_kernel_recovery as kernel
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_r22a_owned_delegation_result_postgres import (
    CSV,
    RESULT,
    _deliver_continuation,
    _file_result,
    _parent_turn,
    _revoke_source,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")


async def _member_file_result(owner, app_factory, monkeypatch, tmp_path, **kwargs):
    seed_run = kernel._seed_run

    async def seed_assistant(*args, **kw):
        seed = await seed_run(*args, **kw)
        async with owner() as db:
            db.add(UserAssistantBinding(
                tenant_id=seed["tenant_id"], user_id=seed["user_id"], agent_id=seed["agent_id"],
                source="explicit_selection", status="active", owner_user_id_at_bind=seed["user_id"],
                requester_user_id=seed["user_id"],
            ))
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.async_task.modify",
                allowed=True, requires_approval=False))
            await db.commit()
        return seed

    monkeypatch.setattr(kernel, "_seed_run", seed_assistant)
    return await _file_result(owner, app_factory, monkeypatch, tmp_path, **kwargs)


def _reader_arguments(reader, task_id):
    if reader == "task_output":
        return {"runtime_task_id": str(task_id)}
    return {"task_id": str(task_id)} if reader in {"check_async_task", "cancel_async_task"} else {}


def _task_body(reader, body, task_id):
    if reader == "task_output":
        return body["task"]
    if reader == "list_async_tasks":
        return next(row for row in body if row.get("task_id", "").replace("-", "") == task_id.hex)
    return body


@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "revoked"])
@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks", "cancel_async_task"])
async def test_parent_task_read_respects_the_same_member_source_as_runtime_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoked, reader, hex_task_id=False,
    task_id_format=None,
):
    import app.runtime.invoker as invoker
    from app.tools.runtime import ToolExecutionRegistry

    active_read = ContextVar("r27b_measured_task_read", default=None)
    statements = []
    execute = ToolExecutionRegistry.try_execute

    async def measured_execute(registry, request):
        if request.tool_name not in {reader, "read_runtime_result"}:
            return await execute(registry, request)
        count = []
        token = active_read.set(count)
        try:
            return await execute(registry, request)
        finally:
            active_read.reset(token)
            statements.append({"tool": request.tool_name, "count": len(count)})

    def count_statement(_connection, _cursor, statement, _parameters, _context, _many):
        count = active_read.get()
        if count is not None:
            count.append(statement.split(None, 1)[0])

    monkeypatch.setattr(ToolExecutionRegistry, "try_execute", measured_execute)

    async def revoke_after_colleague_used_the_source(seed):
        await _revoke_source(owner_sessionmaker, seed)

    # The existing real upload/file/delegation fixture grants the member a
    # third person's document, pins that source on the uploaded file, and has
    # the colleague read it before completing. Only then can access be revoked.
    seed = await _member_file_result(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True,
        before_handback=revoke_after_colleague_used_the_source if revoked else None,
    )
    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    arguments = _reader_arguments(reader, seed["child_id"].hex if hex_task_id else seed["child_id"])
    if task_id_format:
        arguments["task_id"] = {
            "upper": str(seed["child_id"]).upper(),
            "braces": "{" + str(seed["child_id"]) + "}",
            "urn": seed["child_id"].urn,
        }[task_id_format]
    if hex_task_id:
        from app.agents import orchestrator

        assert seed["child_id"].hex in orchestrator._async_tasks
        assert orchestrator._async_tasks[seed["child_id"].hex].task.done()
    client = kernel.ScriptedLLMClient([
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r27a-pinned-read")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call(reader, arguments, "r27a-task-read")],
            "finish_reason": "tool_calls"},
        {"content": "The colleague task has ended."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    event.listen(Engine, "before_cursor_execute", count_statement)
    try:
        await _execute(owner_sessionmaker, return_id, "r27a-source-reader")
    finally:
        event.remove(Engine, "before_cursor_execute", count_statement)
    assert len(client.requests) == 3
    assert seed["result_ref"] in json.dumps(client.requests[0], ensure_ascii=False)

    results = await _tool_results(owner_sessionmaker, return_id)
    pinned = json.loads(results["r27a-pinned-read"]["content"])
    checked = _task_body(reader, json.loads(results["r27a-task-read"]["content"]), seed["child_id"])
    if revoked:
        assert (pinned["status"], pinned["reason_code"], pinned["retryable"]) == (
            "source_access_denied", "content_source_access_denied", False,
        )
        assert RESULT not in json.dumps(pinned) and "Gamma,150" not in json.dumps(pinned)
        assert checked["result"] is None
        assert {key: checked["result_publication"][key] for key in pinned} == pinned
        assert UUID(checked["child_session_id"]) == seed["child_session_id"]
        from app.services.session_event_contract import _member_tool_summary

        summary = _member_tool_summary(results["r27a-task-read"], item_kind="tool_result")
        assert summary["state"] == "not_done"
        assert summary["kind"] == "colleague_result"
    else:
        assert pinned["status"] == "ok" and pinned["complete"] is True
        assert RESULT in pinned["content"]
        assert RESULT in json.dumps(checked)
    assert checked["status"] == "completed"

    wires = [json.dumps(request["messages"], ensure_ascii=False) for request in client.requests]
    feedback = [message for message in client.requests[2]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "r27a-task-read"]
    assert len(feedback) == 1
    assert (RESULT in feedback[0]["content"]) is (not revoked)
    # A child_session timeline item may contain the answer, but the automatic
    # continuation must initially expose only the result reference to the model.
    assert RESULT not in wires[0] and CSV not in wires[0]
    if revoked:
        assert RESULT not in wires[1] and "Gamma,150" not in wires[1]
    else:
        assert RESULT in wires[1]

    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == return_id).order_by(SessionModelResult.round_id)))
        assert len(models) == 3
        snapshots = [row.model_request_snapshot_json for row in models]
        for index, snapshot in enumerate(snapshots):
            assert (RESULT in json.dumps(snapshot["wire_request"], ensure_ascii=False)) == (RESULT in wires[index])
        call = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == return_id,
            SessionToolInvocation.provider_tool_use_id == "r27a-task-read"))).one()
        proof_row = await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read"))
        proof = (proof_row.metadata_json or {}).get("runtime_task_read") if proof_row else None

    observation = {
        "revoked": revoked,
        "reader": reader,
        "task_id_format": task_id_format,
        "task_read_sql_statements": statements,
        "automatic_model_has_answer": RESULT in wires[0],
        "pinned_read_status": pinned["status"],
        "model_after_pinned_read_has_answer": RESULT in wires[1],
        "task_read_status": checked["status"],
        "task_result_has_answer": RESULT in json.dumps(checked, ensure_ascii=False),
        "model_after_task_read_has_answer": RESULT in wires[2],
        "task_read_proof_present": proof is not None,
        "task_read_unbound": proof.get("unbound") if proof else None,
        "task_read_source_kinds": sorted({row["source_kind"] for row in proof["sources"]}) if proof else [],
        "final_model_dependency_kinds": sorted({row["source_kind"] for row in
            snapshots[2]["input_bindings"]["dependencies"]}),
    }
    print("R27A_SOURCE_READER_OBSERVATION", json.dumps(observation, sort_keys=True))
    if revoked:
        assert RESULT not in wires[2], f"{reader} exposed a colleague answer whose source the member cannot read"
    else:
        assert RESULT in wires[2]


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks"])
@pytest.mark.parametrize("state", ["running", "unpublished"])
async def test_task_reader_keeps_status_and_unpublished_projection(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader, state,
):
    observations = []

    async def read_before_publication(seed):
        task_id = seed["child_id"]
        if state == "running":
            # Admit a separate running task; never rewind the completed child.
            from app.services.runtime_task_service import create_runtime_task_record

            task_id = uuid4()
            await create_runtime_task_record(task_id=str(task_id), task_type="delegation", status="running",
                parent_agent_id=seed["agent_id"], child_agent_id=seed["child_agent_id"],
                child_agent_name="Running colleague", prompt="Continue the authorized analysis.",
                parent_session_id=str(seed["session_id"]), child_session_id=str(uuid4()),
                root_user_id=seed["user_id"], root_session_id=str(seed["session_id"]),
                root_runtime_task_id=seed["run_id"],
                delegation_chain=[f"agent:{seed['agent_id']}", f"session:{seed['session_id']}"])
        run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
            {"tool_calls": [_call(reader, _reader_arguments(reader, task_id), "r27b-status-read")],
                "finish_reason": "tool_calls"},
            {"content": "The task status is available."},
        ])
        results = await _tool_results(owner_sessionmaker, run_id)
        checked = _task_body(reader, json.loads(results["r27b-status-read"]["content"]), task_id)
        assert checked["status"] == ("running" if state == "running" else "completed")
        legacy_cancel = reader == "cancel_async_task"
        assert checked["result"] == (RESULT if legacy_cancel else None)
        if state == "unpublished" and not legacy_cancel:
            assert checked["result_publication"]["status"] == "pending"
            assert checked["result_publication"]["reason_code"] == "runtime_result_publication_pending"
        else:
            assert "result_publication" not in checked
        wire = json.dumps(client.requests[1]["messages"], ensure_ascii=False)
        assert (RESULT in wire) is legacy_cancel
        assert CSV not in wire
        async with owner_sessionmaker() as db:
            model = (await db.scalars(select(SessionModelResult).where(
                SessionModelResult.run_id == run_id).order_by(SessionModelResult.round_id.desc()))).first()
            assert (RESULT in json.dumps(model.model_request_snapshot_json["wire_request"])) is legacy_cancel
            if legacy_cancel:
                call = (await db.scalars(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == run_id))).one()
                assert await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read")) is None
        observations.append(checked["status"])

    await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        before_handback=read_before_publication)
    assert len(observations) == 1


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks", "check_subagent", "check_subagent_list", "cancel_async_task"])
async def test_result_without_published_sources_keeps_its_body(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader,
):
    from app.services.runtime_result_store import encode_runtime_result_payload, runtime_result_object_id

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    task_id, child_session_id = uuid4(), uuid4()
    payload = encode_runtime_result_payload(summary=RESULT)
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=task_id, tenant_id=seed["tenant_id"], task_type="subagent", status="completed",
            parent_agent_id=seed["agent_id"], child_agent_id=seed["child_agent_id"], child_agent_name="Plain subagent",
            parent_session_id=str(seed["session_id"]), child_session_id=str(child_session_id),
            root_user_id=seed["user_id"], root_session_id=str(seed["session_id"]),
            root_runtime_task_id=seed["run_id"], result_summary=RESULT, metadata_json={},
            delegation_chain_json=[f"agent:{seed['agent_id']}", f"session:{seed['session_id']}"]))
        # Native subagent publications deliberately retain unknown source
        # coverage in read_runtime_result; this change does not widen it.
        db.add(RuntimeResultObject(id=runtime_result_object_id(tenant_id=seed["tenant_id"], source_kind="subagent",
            source_run_id=str(task_id), sha256=payload.sha256), tenant_id=seed["tenant_id"], source_kind="subagent",
            source_run_id=str(task_id), payload_bytes=payload.payload_bytes, sha256=payload.sha256,
            size_bytes=payload.size_bytes))
        await db.commit()
    tool = "check_subagent" if reader == "check_subagent_list" else reader
    arguments = {"run_id": str(task_id)} if reader == "check_subagent" else _reader_arguments(reader, task_id)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call(tool, arguments, "r27b-plain-read")], "finish_reason": "tool_calls"},
        {"content": "The plain result is available."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    checked = json.loads(results["r27b-plain-read"]["content"])
    if reader == "check_subagent_list":
        checked = next(row for row in checked["runs"] if UUID(row["run_id"]) == task_id)
    elif reader != "check_subagent":
        checked = _task_body(reader, checked, task_id)
    assert checked["status"] == "completed"
    assert checked.get("result", checked.get("result_summary")) == RESULT
    assert "result_publication" not in checked
    wire = json.dumps(client.requests[1]["messages"])
    if reader in {"check_subagent", "check_subagent_list", "cancel_async_task"}:
        assert RESULT in wire
    else:
        # The existing task-read input compiler withholds an unknown native
        # producer. Preserve that boundary as well as the raw tool's body.
        assert RESULT not in wire
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == run_id).order_by(SessionModelResult.round_id.desc()))).first()
        assert (RESULT in json.dumps(model.model_request_snapshot_json["wire_request"])) == (RESULT in wire)


async def test_terminal_cancel_keeps_base_unpublished_feedback(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    await test_task_reader_keeps_status_and_unpublished_projection(owner_sessionmaker,
        app_user_sessionmaker, monkeypatch, tmp_path, reader="cancel_async_task", state="unpublished")


@pytest.mark.parametrize("reader", ["check_async_task", "task_output", "list_async_tasks"])
@pytest.mark.parametrize("has_artifact", [False, True])
async def test_unbound_legacy_feedback_stays_out_of_the_model(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader, has_artifact,
):
    from tests.integration import test_r19a_consumed_result as consumed

    original_read = consumed._read_completed_result
    scripted = consumed.ScriptedLLMClient
    clients = []

    class ObservedClient(scripted):
        def __init__(self, script):
            super().__init__(script)
            clients.append(self)

    async def read_and_observe(owner, patch, seed, tool):
        patch.setattr(consumed, "ScriptedLLMClient", ObservedClient)
        # Keep every landed raw-feedback assertion. Observe the actual next
        # provider request as well: unbound feedback was never admitted.
        await original_read(owner, patch, seed, tool)
        assert len(clients) == 1 and len(clients[0].requests) == 2
        if has_artifact:
            from tests.integration.test_r22a_owned_delegation_result_postgres import _read

            pinned = await _read(seed)
            assert pinned["status"] == "source_unavailable"
            assert pinned["reason_code"] == "runtime_result_artifact_unverified"
            assert pinned["retryable"] is True
            print("R27C_CANONICAL_STOP", json.dumps({"reader": reader,
                "direct_status": pinned["status"], "reason_code": pinned["reason_code"],
                "landed_raw_summary_assertion": "test_r19a_consumed_result.py:63"}))
        wire = json.dumps(clients[0].requests[1]["messages"])
        assert "Three sales points: quality, service, value." not in wire
        async with owner() as db:
            call = (await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == seed["tenant_id"],
                SessionToolInvocation.provider_tool_use_id == "r3-read"))).one()
            model = (await db.scalars(select(SessionModelResult).where(
                SessionModelResult.run_id == call.run_id).order_by(SessionModelResult.round_id.desc()))).first()
            snapshot = json.dumps(model.model_request_snapshot_json["wire_request"])
            assert "Three sales points: quality, service, value." not in snapshot

    monkeypatch.setattr(consumed, "_read_completed_result", read_and_observe)
    await consumed.test_summary_read_does_not_consume_a_corrected_or_artifact_result_page(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models=None,
        reader=reader, has_artifact=has_artifact)


@pytest.mark.parametrize("reader", ["check_async_task", "cancel_async_task"])
@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "revoked"])
async def test_hex_task_id_reads_the_completed_native_cache(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader, revoked,
):
    await test_parent_task_read_respects_the_same_member_source_as_runtime_result(owner_sessionmaker,
        app_user_sessionmaker, monkeypatch, tmp_path, revoked=revoked, reader=reader, hex_task_id=True)


@pytest.mark.parametrize("reader", ["check_async_task", "cancel_async_task"])
@pytest.mark.parametrize("task_id_format", ["upper", "braces", "urn"])
async def test_noncanonical_task_id_cannot_bypass_a_source_refusal(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader, task_id_format,
):
    await test_parent_task_read_respects_the_same_member_source_as_runtime_result(owner_sessionmaker,
        app_user_sessionmaker, monkeypatch, tmp_path, revoked=True, reader=reader, task_id_format=task_id_format)


@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "revoked"])
async def test_real_runtime_context_keeps_completed_colleague_content_behind_its_reference(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoked,
):
    import app.runtime.invoker as invoker
    from app.services.agent_context import build_agent_runtime_context

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True)
    if revoked:
        await _revoke_source(owner_sessionmaker, seed)
    # Restore the actual volatile context builder after the fixture's kernel
    # seams. Keep the real team-context queries and native input compiler.
    monkeypatch.setattr(invoker, "build_agent_runtime_context", build_agent_runtime_context)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"content": "The colleague task has ended; its result can be read when needed."},
    ])
    assert len(client.requests) == 1
    wire = json.dumps(client.requests[0], ensure_ascii=False)
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id))).one()
        snapshot = model.model_request_snapshot_json
    persisted = json.dumps(snapshot["wire_request"], ensure_ascii=False)
    print("R27C_TEAM_CONTEXT", json.dumps({"revoked": revoked, "provider_has_answer": RESULT in wire,
        "snapshot_has_answer": RESULT in persisted, "provider_has_reference": seed["result_ref"] in wire,
        "snapshot_has_reference": seed["result_ref"] in persisted}))
    assert RESULT not in wire and CSV not in wire
    assert RESULT not in persisted and CSV not in persisted
    assert seed["result_ref"] in wire and seed["result_ref"] in persisted
    assert any(row["source_kind"] == "runtime_team_context"
        for row in snapshot["input_bindings"]["dependencies"])
