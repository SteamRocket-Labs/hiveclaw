"""Task-result projection preserves exact publication and read-time authority."""

import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.services.session_event_contract import _member_tool_summary
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_failed_delegation_continuation import _call
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_r22a_owned_delegation_result_postgres import (
    RESULT, _parent_turn, _read, _revoke_source,
)
from tests.integration.test_r27a_delegation_result_source_readers_postgres import _member_file_result
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")


@pytest.mark.parametrize("missing", ["session_id", "runtime_task_id"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_cancel_without_reader_context_withholds_completed_content(monkeypatch, tmp_path, missing, wrapped):
    from app.services import agent_tools
    from app.tools.handlers.communication import cancel_async_task

    task_id = uuid4()
    body = {"task_id": str(task_id), "status": "completed", "result": RESULT, "receipt": {"summary": RESULT}}
    if wrapped:
        body = {"ok": True, "task": body, "summary": RESULT, "receipt": {"summary": RESULT}}

    async def cancel(*_args, **_kwargs):
        return json.dumps(body)

    monkeypatch.setattr(agent_tools, "_cancel_async_task", cancel)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path,
        session_id=str(uuid4()), runtime_task_id=str(uuid4()), tool_call_id=str(uuid4()), turn_id=str(uuid4()))
    setattr(context, missing, None)
    result = json.loads(await cancel_async_task(ToolExecutionRequest("cancel_async_task", {"task_id": str(task_id)}, context)))
    assert result["status"] == "completed" and result["result"] is None
    assert result["result_publication"]["status"] == "source_unavailable"
    assert RESULT not in json.dumps(result)


@pytest.mark.parametrize("missing", ["session_id", "runtime_task_id"])
async def test_cancel_without_model_call_keeps_base_bytes(monkeypatch, tmp_path, missing):
    from app.services import agent_tools
    from app.tools.handlers.communication import cancel_async_task

    content = json.dumps({"task_id": str(uuid4()), "status": "completed", "result": RESULT,
        "receipt": {"summary": RESULT}}, indent=2) + "\n"

    async def cancel(*_args, **_kwargs):
        return content

    monkeypatch.setattr(agent_tools, "_cancel_async_task", cancel)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path,
        session_id=str(uuid4()), runtime_task_id=str(uuid4()))
    setattr(context, missing, None)
    assert await cancel_async_task(ToolExecutionRequest("cancel_async_task", {}, context)) == content


@pytest.mark.parametrize("status", ["killed", "failed"])
async def test_cancel_keeps_base_terminal_feedback(monkeypatch, tmp_path, status):
    from app.services import agent_tools
    from app.tools.handlers.communication import cancel_async_task

    body = {"task_id": str(uuid4()), "status": status, "result": "Task cancelled by parent agent",
        "receipt": {"operation": "cancel", "outcome": status}}

    async def cancel(*_args, **_kwargs):
        return json.dumps(body)

    monkeypatch.setattr(agent_tools, "_cancel_async_task", cancel)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path,
        session_id=str(uuid4()), runtime_task_id=str(uuid4()))
    assert json.loads(await cancel_async_task(ToolExecutionRequest("cancel_async_task", {}, context))) == body


@pytest.mark.parametrize("reader", ["task_output", "cancel_async_task"])
@pytest.mark.parametrize("status", ["pending", "source_access_denied", "source_unavailable"])
def test_only_refused_publications_change_the_base_member_row(reader, status):
    task = {"task_id": str(uuid4()), "task_type": "delegation", "status": "completed", "result": None,
        "result_publication": {"status": status}}
    body = {"ok": True, "task": task} if reader == "task_output" else task
    summary = _member_tool_summary({"tool_name": reader, "outcome": "success", "content": json.dumps(body)},
        item_kind="tool_result")
    if reader == "cancel_async_task" and status == "pending":
        assert summary is None
    else:
        assert summary["kind"] == "colleague_result"
        assert summary["state"] == ("done" if status == "pending" else "not_done")


@pytest.mark.parametrize("record_path", [False, True])
def test_an_unmatched_refusal_cannot_certify_a_result(record_path):
    from app.services.runtime_task_read_sources import _project_refused_results

    body = {"task_id": str(uuid4()), "status": "completed", "result": RESULT, "receipt": {"summary": RESULT}}
    unbound = [] if record_path else None
    projected = _project_refused_results(body, {uuid4().hex: {"status": "source_access_denied"}}, unbound=unbound)
    if record_path:
        assert unbound == ["task_read_sources_unbound"]
    else:
        assert projected["result"] is None and RESULT not in json.dumps(projected)


async def test_cancel_reads_only_the_current_higher_ranked_publication(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

    async def publish_earlier_result(seed):
        async with owner_sessionmaker() as db:
            await enqueue_completion_notification(db, CompletionNotification(
                tenant_id=seed["tenant_id"], source_kind="a2a_delegation", source_run_id=str(seed["child_id"]),
                parent_session_id=seed["session_id"], parent_agent_id=seed["agent_id"], parent_user_id=seed["user_id"],
                child_session_id=seed["child_session_id"], child_agent_name="R22a colleague",
                terminal_status="completed", task_type="delegation", root_runtime_task_id=seed["run_id"],
                summary="An earlier result revision.", payload_rank=10))
            await db.commit()

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        before_handback=publish_earlier_result)
    async with owner_sessionmaker() as db:
        versions = list(await db.scalars(select(RuntimeResultObject).where(
            RuntimeResultObject.source_run_id == str(seed["child_id"]))))
        assert len(versions) == 2
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))).one()
        assert notice.payload_rank > 10 and notice.result_object_id == seed["result_id"]
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r27c-current")], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("cancel_async_task", {"task_id": str(seed["child_id"])}, "r27c-cancel")], "finish_reason": "tool_calls"},
        {"content": "The published result is available."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    direct = json.loads(results["r27c-current"]["content"])
    cancel = json.loads(results["r27c-cancel"]["content"])
    assert direct["status"] == "ok" and direct["complete"] is True
    assert cancel["result"] == RESULT and "result_publication" not in cancel
    assert RESULT in json.dumps(client.requests[2])
    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
        assert len(models) == 3
        assert any(RESULT in json.dumps(model.model_request_snapshot_json["wire_request"]) for model in models)


async def _complete_another_colleague(owner, app_factory, seed, monkeypatch, index):
    import app.runtime.invoker as invoker
    from app.agents import orchestrator
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    parent_id, _client = await _parent_turn(owner, seed, monkeypatch, [
        {"tool_calls": [_call("delegate_to_agent", {"agent_name": "R22a colleague", "message": f"Suggest item {index}.",
            "expected_output": "A plain suggestion", "tool_profile": "worker_safe"}, f"delegate-{index}")], "finish_reason": "tool_calls"},
        {"content": "The colleague is preparing the suggestion."},
    ])
    async with owner() as db:
        child = (await db.scalars(select(RuntimeTask).where(RuntimeTask.root_runtime_task_id == parent_id,
            RuntimeTask.task_type == "delegation"))).one()
        child_id = child.id
        child.status, child.claimed_by, child.claim_version, child.attempt_count = "running", "r27c-colleague", 1, 1
        child.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    summary = f"R27C_READABLE_SUGGESTION_{index}"
    client = ScriptedLLMClient([{"content": summary}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=child_id, worker_id="r27c-colleague", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(child_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(token)
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_factory, data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_factory).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r27c-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1
    return child_id, summary


@pytest.mark.parametrize("count", [1, 5])
@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "one-revoked"])
async def test_list_checks_each_completed_result_in_one_read(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, count, revoked,
):
    from app.tools.runtime import ToolExecutionRegistry

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_grant=True)
    expected = {seed["child_id"]: RESULT}
    for index in range(1, count):
        task_id, summary = await _complete_another_colleague(owner_sessionmaker, app_user_sessionmaker, seed, monkeypatch, index)
        expected[task_id] = summary
    if revoked:
        await _revoke_source(owner_sessionmaker, seed)
    active = ContextVar("r27c_list_statements", default=None)
    statements = []
    execute = ToolExecutionRegistry.try_execute

    async def measured(registry, request):
        if request.tool_name != "list_async_tasks":
            return await execute(registry, request)
        token = active.set(statements)
        try:
            return await execute(registry, request)
        finally:
            active.reset(token)

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if active.get() is not None:
            active.get().append(statement.split(None, 1)[0])

    monkeypatch.setattr(ToolExecutionRegistry, "try_execute", measured)
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
            {"tool_calls": [_call("list_async_tasks", {}, "r27c-list")], "finish_reason": "tool_calls"},
            {"content": "The task list is available."},
        ])
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    results = await _tool_results(owner_sessionmaker, run_id)
    rows = {UUID(row["task_id"]): row for row in json.loads(results["r27c-list"]["content"])}
    wire = json.dumps(client.requests[1])
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        persisted = json.dumps(model.model_request_snapshot_json["wire_request"])
    print("R27C_LIST_STATEMENTS", json.dumps({"count": count, "revoked": revoked, "statements": len(statements)}))
    for task_id, summary in expected.items():
        denied = revoked and task_id == seed["child_id"]
        assert rows[task_id]["status"] == "completed"
        assert rows[task_id]["result"] == (None if denied else summary)
        assert (summary in wire) is (not denied)
        assert (summary in persisted) is (not denied)
        if denied:
            assert rows[task_id]["result_publication"]["reason_code"] == "content_source_access_denied"


async def test_member_permission_is_observed_again_on_the_next_read(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, source_grant=True)
    for revoked in (False, True):
        if revoked:
            await _revoke_source(owner_sessionmaker, seed)
        pinned = await _read(seed)
        call_id = f"r27c-fresh-read-{int(revoked)}"
        run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
            {"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])}, call_id)], "finish_reason": "tool_calls"},
            {"content": "The read has finished."},
        ])
        results = await _tool_results(owner_sessionmaker, run_id)
        checked = json.loads(results[call_id]["content"])
        feedback = next(message["content"] for message in client.requests[1]["messages"]
            if message.get("tool_call_id") == call_id and message.get("role") == "tool")
        assert (RESULT in feedback) is (not revoked)
        if revoked:
            assert checked["result"] is None
            assert checked["result_publication"] == pinned
        else:
            assert checked["result"] == RESULT and pinned["status"] == "ok"


@pytest.mark.parametrize("reader", ["check_async_task", "cancel_async_task"])
async def test_publication_route_kind_does_not_skip_the_result_source_check(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, reader,
):
    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True)
    async with owner_sessionmaker() as db:
        notice = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"])))).one()
        # The publication binding accepts this existing route kind too. Source
        # coverage is decided by the exact result object, not a routing label.
        notice.source_kind = "employee_creation"
        await db.commit()
    await _revoke_source(owner_sessionmaker, seed)
    pinned = await _read(seed)
    assert (pinned["status"], pinned["reason_code"]) == ("source_access_denied", "content_source_access_denied")
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call(reader, {"task_id": str(seed["child_id"])}, "r27c-route-read")], "finish_reason": "tool_calls"},
        {"content": "The result source is unavailable to this reader."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    checked = json.loads(results["r27c-route-read"]["content"])
    assert checked["result"] is None and checked["result_publication"] == pinned
    assert RESULT not in json.dumps(client.requests[1])
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        assert RESULT not in json.dumps(model.model_request_snapshot_json["wire_request"])


@pytest.mark.parametrize("binding", ["wrong_run", "moved", "other_tenant", "multiple_pointers"])
async def test_cancel_binds_its_body_to_the_matching_published_object(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, binding,
):
    from app.services import agent_tools
    from app.models.tenant import Tenant
    from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
    from app.services.runtime_result_store import encode_runtime_result_payload

    older = "R27D_PREVIOUS_RESULT: an earlier completed answer."

    async def publish_v1(seed):
        async with owner_sessionmaker() as db:
            await enqueue_completion_notification(db, CompletionNotification(
                tenant_id=seed["tenant_id"], source_kind="a2a_delegation", source_run_id=str(seed["child_id"]),
                parent_session_id=seed["session_id"], parent_agent_id=seed["agent_id"], parent_user_id=seed["user_id"],
                child_session_id=seed["child_session_id"], terminal_status="completed", task_type="delegation",
                root_runtime_task_id=seed["run_id"], summary=older, payload_rank=10))
            await db.commit()

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True, before_handback=publish_v1 if binding in {"moved", "multiple_pointers"} else None)
    if binding == "wrong_run":
        other_id, _summary = await _complete_another_colleague(owner_sessionmaker, app_user_sessionmaker, seed, monkeypatch, 1)
        async with owner_sessionmaker() as db:
            other = (await db.scalars(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(other_id)))).one()
            replacement_id = other.result_object_id
            assert (await _read({**seed, "result_ref": other.result_ref}))["status"] == "ok"
    if binding in {"wrong_run", "other_tenant"}:
        await _revoke_source(owner_sessionmaker, seed)
        assert (await _read(seed))["status"] == "source_access_denied"
    else:
        assert (await _read(seed))["status"] == "ok"

    async with owner_sessionmaker() as db:
        pointer = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(seed["child_id"]),
            RuntimeNotificationOutbox.source_kind == "a2a_delegation"))).one()
        if binding == "other_tenant":
            tenant_id, replacement_id = uuid4(), uuid4()
            db.add(Tenant(id=tenant_id, name="Other result tenant", slug=f"r27d-{tenant_id.hex}"))
            await db.flush()
            payload = encode_runtime_result_payload(summary="OTHER_TENANT_RESULT")
            db.add(RuntimeResultObject(id=replacement_id, tenant_id=tenant_id, source_kind="subagent",
                source_run_id=str(seed["child_id"]), payload_bytes=payload.payload_bytes,
                sha256=payload.sha256, size_bytes=payload.size_bytes))
            await db.flush()
        if binding in {"wrong_run", "other_tenant"}:
            pointer.result_object_id = replacement_id
        if binding == "multiple_pointers":
            versions = list(await db.scalars(select(RuntimeResultObject).where(
                RuntimeResultObject.source_run_id == str(seed["child_id"]))))
            previous = next(obj for obj in versions if json.loads(bytes(obj.payload_bytes))["summary"] == older)
            values = {column.key: getattr(pointer, column.key) for column in RuntimeNotificationOutbox.__table__.columns}
            values.update(id=uuid4(), source_kind="employee_creation", mailbox_sequence=pointer.mailbox_sequence + 100,
                result_object_id=previous.id)
            db.add(RuntimeNotificationOutbox(**values))
        await db.commit()

    if binding == "moved":
        cancel = agent_tools._cancel_async_task

        async def replay_v1(*args, **kwargs):
            body = json.loads(await cancel(*args, **kwargs))
            # Replay the committed v1 summary at the read boundary after the
            # native higher-ranked v2 pointer is already visible. No timing race.
            body["result"] = older
            return json.dumps(body)

        monkeypatch.setattr(agent_tools, "_cancel_async_task", replay_v1)
    run_id, client = await _parent_turn(owner_sessionmaker, seed, monkeypatch, [
        {"tool_calls": [_call("cancel_async_task", {"task_id": str(seed["child_id"])}, "r27d-bound-cancel")],
            "finish_reason": "tool_calls"},
        {"content": "The task status is available."},
    ])
    results = await _tool_results(owner_sessionmaker, run_id)
    body = json.loads(results["r27d-bound-cancel"]["content"])
    allowed = binding == "multiple_pointers"
    target = older if binding == "moved" else RESULT
    wire = json.dumps(client.requests[1])
    async with owner_sessionmaker() as db:
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        persisted = json.dumps(model.model_request_snapshot_json["wire_request"])
    print("R27D_BOUND_CANCEL", json.dumps({"binding": binding, "provider_has_body": target in wire,
        "snapshot_has_body": target in persisted, "allowed": allowed}))
    assert body["status"] == "completed"
    if allowed:
        assert body["result"] == RESULT and "result_publication" not in body
    else:
        assert body["result"] is None
        assert body["result_publication"]["status"] == "source_unavailable"
        assert body["result_publication"]["reason_code"] == "runtime_result_sources_unbound"
    assert (target in wire) is allowed and (target in persisted) is allowed
    assert "OTHER_TENANT_RESULT" not in wire and "OTHER_TENANT_RESULT" not in persisted


async def test_http_task_stop_keeps_readable_completed_feedback(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import httpx
    from fastapi import FastAPI
    from app.api.commands import router
    from app.core.security import get_current_user
    from app.database import get_db, pin_rls_tenant_context
    from app.models.user import User

    seed = await _member_file_result(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture.local") as client:
            response = await client.post(f"/api/agents/{seed['agent_id']}/commands/task_stop/execute",
                json={"origin": "local", "session_id": str(seed["session_id"]),
                    "arguments": {"kind": "delegation", "task_id": str(seed["child_id"])}})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    body = response.json()["result"]
    assert body["status"] == "completed" and body["result"] == RESULT
    assert "result_publication" not in body
