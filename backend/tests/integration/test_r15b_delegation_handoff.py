"""Colleague waits end locally and durable results resume the original conversation."""

import asyncio
import json
from uuid import UUID, uuid5

import pytest
from sqlalchemy import select

from app.kernel.loop_guard import LoopGuard
from app.models.runtime_task import RuntimeTask
from tests.integration import test_r11c_colleague_lifecycle_postgres as lifecycle
from tests.integration import test_failed_delegation_continuation as failed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
WAITING = "同事正在处理，结果回来后会在这里继续。"
RETRY = "同事这次没有完成：这次用完了用量预算，没有完成。需要重试的话，请告诉我。"
RETRY_GUIDANCE = (
    "Explain the failure reason to the member in their language and offer to retry. "
    "Do not delegate again unless the member asks."
)


async def test_in_memory_completion_check_records_task_type_and_read_proof(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from datetime import datetime, timezone

    from app.agents import orchestrator
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionToolInvocation

    seed, parent, child_client = await lifecycle._setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent", paused=True)
    parent_task = asyncio.create_task(lifecycle._execute(seed["run_id"], owner_sessionmaker,
        worker="r15b-fallback-parent"))
    try:
        await asyncio.wait_for(parent.started.wait(), 20)
        child = await lifecycle._child(owner_sessionmaker, seed)
        child_client.release.set()
        await lifecycle._execute(child.id, owner_sessionmaker, worker="r15b-fallback-child", delegation=True)
        async with owner_sessionmaker() as db:
            terminal = await db.get(RuntimeTask, child.id)
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(orchestrator.AgentDelegationResult(content=lifecycle.RESULT,
            child_session_id=child.child_session_id, trace_id=child.trace_id, depth=1))
        monkeypatch.setitem(orchestrator._async_tasks, child.id.hex, orchestrator.AsyncDelegationState(
            task=completed, parent_agent_id=seed["agent_id"], child_agent_name=lifecycle.COLLEAGUE,
            child_session_id=child.child_session_id, trace_id=child.trace_id,
            started_at=datetime.now(timezone.utc), receipt=terminal.metadata_json.get("execution_receipt")))

        async def unavailable(_task_id):
            return None

        monkeypatch.setattr(orchestrator, "get_runtime_task_record", unavailable)
        parent.script = [lifecycle._tool("check_async_task", {"task_id": child.id.hex}),
            {"content": "The colleague result was checked."}]
        parent.release.set()
        await asyncio.wait_for(parent_task, 20)
        async with owner_sessionmaker() as db:
            call = await db.scalar(select(SessionToolInvocation).where(
                SessionToolInvocation.run_id == seed["run_id"], SessionToolInvocation.tool_name == "check_async_task"))
            assert call is not None
            event = await db.get(ChatTranscriptEvent, call.result_event_id)
            payload = event.metadata_json["v2_payload"]
            body = json.loads(payload["content"])
            assert body["task_type"] == "delegation"
            assert body["parent_session_id"] == str(seed["session_id"])
            proof = await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read"))
            assert proof is not None, "the completion fallback must carry a recorded read proof"
            assert proof.metadata_json["runtime_task_read"]["task_ids"] == [str(child.id)]
            assert proof.metadata_json["runtime_task_read"]["unbound"] == []
    finally:
        parent.release.set()
        child_client.release.set()
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)


async def test_running_colleague_reads_end_parent_without_waiting_for_child(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker

    seed, parent, _ = await lifecycle._setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent")
    delegate = parent.script[0]

    class PollingClient(lifecycle.ScriptedLLMClient):
        async def stream(self, **kwargs):
            if self.requests:
                child = await lifecycle._child(owner_sessionmaker, seed)
                self.script = [lifecycle._tool("check_async_task", {"task_id": child.id.hex})
                    if len(self.requests) <= LoopGuard().identical_tool_threshold
                    else {"content": "The running read guard did not stop this turn."}]
            return await super().stream(**kwargs)

    client = PollingClient([delegate])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await lifecycle._execute(seed["run_id"], owner_sessionmaker, worker="r15b-polling-parent")
    async with owner_sessionmaker() as db:
        parent_run = await db.get(RuntimeTask, seed["run_id"])
        # R19a: this runtime-authored stop has no sealed model final. It uses
        # the same typed failure as a delegated child, while retaining the
        # fixed waiting sentence and the original read/dispatch counts.
        assert parent_run.status == "failed", parent_run.result_summary
        assert parent_run.result_summary == "loop_guard"
        assert parent_run.metadata_json["terminal_reason"] == "loop_guard"
        assert parent_run.metadata_json["error_code"] == "identical_running_task_read"
        assert len(client.requests) == 1 + LoopGuard().identical_tool_threshold
        child = await lifecycle._child(owner_sessionmaker, seed)
        assert child.status == "pending"
    # No extra model round is needed to write the runtime's fixed waiting sentence.
    events = await lifecycle._session_api(app_user_sessionmaker, seed)
    failures = [event for event in events if event.get("item_kind") == "runtime_failure"]
    assert len(failures) == 1
    assert failures[0]["payload"]["failure_code"] == "identical_running_task_read"
    assert sum(event.get("payload", {}).get("content") == WAITING for event in events) == 1


async def test_failure_notification_instructs_member_requested_retry_before_reading_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker

    seed, _ = await failed._failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    client = lifecycle.ScriptedLLMClient([
        lifecycle._tool("read_runtime_result", {"result_ref": seed["result_ref"]}),
        {"content": RETRY},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await failed._execute(owner_sessionmaker, seed["return_id"], "r15b-unread-failure")
    async with owner_sessionmaker() as db:
        children = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.parent_session_id == str(seed["session_id"]), RuntimeTask.task_type == "delegation")))
        assert len(children) == 1
        assert (await db.get(RuntimeTask, seed["return_id"])).result_summary == RETRY
    assert RETRY_GUIDANCE in json.dumps(client.requests[0]["messages"], ensure_ascii=False)
    assert len(client.requests) == 2
    await failed._assert_member_final(app_user_sessionmaker, seed, RETRY)


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_normal_parent_end_result_delivery_resumes_once_and_stop_never_resumes(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, status,
):
    # Exercise the existing delivery path rather than the unused notification helper.
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    import app.runtime.invoker as invoker

    seed, _, child_client = await lifecycle._setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent")
    await lifecycle._execute(seed["run_id"], owner_sessionmaker, worker="r15b-normal-parent")
    child = await lifecycle._child(owner_sessionmaker, seed)
    if status == "failed":
        child_client.script = [{"tool_calls": [failed._call("get_current_time", {}, "exhaust")],
            "usage": {"total_tokens": 1_000_001}, "finish_reason": "tool_calls"}]
    child_task = asyncio.create_task(lifecycle._execute(child.id, owner_sessionmaker,
        worker="r15b-result-child", delegation=True))
    try:
        await asyncio.wait_for(child_client.started.wait(), 20)
        live = await lifecycle._session_api(app_user_sessionmaker,
            {**seed, "agent_id": child.child_agent_id, "session_id": UUID(child.child_session_id)})
        assert len([event for event in live if event.get("item_kind") == "run"
            and event.get("lifecycle") == "running"]) == 1
    finally:
        child_client.release.set()
        await asyncio.wait_for(child_task, 20)
    wire = "\n".join(str(message["content"]) for message in child_client.requests[0]["messages"] if message["role"] == "system")
    assert "Write progress updates in the member's language." in wire
    assert "The delegation brief carries the member's language; English tool output does not change it." in wire
    child_events = await lifecycle._session_api(app_user_sessionmaker,
        {**seed, "agent_id": child.child_agent_id, "session_id": UUID(child.child_session_id)})
    terminals = [event for event in child_events if event.get("item_kind") == "run" and event.get("lifecycle") == status]
    assert len(terminals) == 1, child_events
    async with owner_sessionmaker() as db:
        actual = await db.get(RuntimeTask, child.id)
        assert terminals[0]["occurred_at"] == actual.completed_at.isoformat()
        assert terminals[0]["payload"]["started_at"] == actual.started_at.isoformat()

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    assert (await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r15b-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",)))["delivered"] == 1
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="r15b-delivery", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r15b-delivery",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    assert (await service._deliver_page(pages[0]))["ok"]
    assert (await service._deliver_page(pages[0]))["deduplicated"]
    async with owner_sessionmaker() as db:
        continuations = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id))))
        assert len(continuations) == 1
    final = f"根据同事核实的结果整理：{lifecycle.RESULT}" if status == "completed" else RETRY
    resumed = lifecycle.ScriptedLLMClient([
        lifecycle._tool("read_runtime_result", {"result_ref": pages[0].items[0].result_ref}),
        {"content": final},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: resumed)
    await lifecycle._execute(continuations[0].id, owner_sessionmaker, worker="r15b-use-returned-result")
    if status == "failed":
        assert RETRY_GUIDANCE in json.dumps(resumed.requests[0]["messages"], ensure_ascii=False)
    expected = lifecycle.RESULT if status == "completed" else "这次用完了用量预算，没有完成。"
    assert expected in json.dumps(resumed.requests[1]["messages"], ensure_ascii=False)
    async with owner_sessionmaker() as db:
        returned = await db.get(RuntimeTask, continuations[0].id)
        assert returned.status == "completed" and returned.result_summary == final
    await failed._assert_member_final(app_user_sessionmaker, {**seed, "return_id": continuations[0].id}, final)


async def test_member_stop_projects_late_result_without_resuming_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed, parent, child_client = await lifecycle._setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent", paused=True)
    parent_task = asyncio.create_task(lifecycle._execute(seed["run_id"], owner_sessionmaker, worker="r15b-stop-parent"))
    try:
        await asyncio.wait_for(parent.started.wait(), 20)
        child = await lifecycle._child(owner_sessionmaker, seed)
        child_client.release.set()
        await lifecycle._execute(child.id, owner_sessionmaker, worker="r15b-stop-child", delegation=True)
        # The member's real cancellation command wins after child commit and before delivery.
        async with owner_sessionmaker() as db:
            receipt = await lifecycle._cancel(db, seed["agent_id"], seed["session_id"], seed["run_id"], seed["user_id"])
            assert receipt["status"] == "applying"
        parent.release.set()
        await asyncio.wait_for(parent_task, 20)
    finally:
        parent.release.set()
        child_client.release.set()
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r15b-stopped-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="r15b-stopped-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r15b-stopped-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    receipt = await service._deliver_page(pages[0])
    assert receipt["consumer"] == "session_projection"
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id))))
