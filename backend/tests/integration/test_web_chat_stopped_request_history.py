"""A member stop reaches the next provider request as historical context."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.integration import test_unverified_t2_recovery_postgres as support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient
from tests.services.test_session_semantic_history import _submit_turn


STOP_MARKER = "[Request interrupted by user; its plan and todos are stopped. Resume only if asked.]"
PARTIAL_WORK = "已开始查找行业资料。"
OLD_TODO = "Finish the previous industry review"
FOLLOWUP = "刚才停了吗？用一句话回答"

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = support.t2_work


@pytest.mark.parametrize("stopped", [True, False], ids=["member_stop", "normal_completion"])
async def test_next_request_records_member_stop_without_current_old_todos(
    t2_work,
    owner_sessionmaker,
    monkeypatch,
    stopped,
    drain_terminal_boundary_for_task,
):
    import app.runtime.invoker as invoker
    from app.api.chat_sessions import cancel_session_run
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput, SessionModelResult
    from app.models.user import User
    from app.services import web_chat_runtime as runtime
    from app.services.agent_work_ledger import read_latest_session_work_ledger_view
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once

    seed = t2_work
    cancel_event = asyncio.Event()
    provider_waiting = asyncio.Event()

    class FirstClient(ScriptedLLMClient):
        async def stream(self, **kwargs):
            if self.requests and stopped:
                provider_waiting.set()
                await cancel_event.wait()
            return await super().stream(**kwargs)

    first = FirstClient(
        [
            {
                "content": PARTIAL_WORK,
                "tool_calls": [
                    {
                        "id": "review-todo",
                        "type": "function",
                        "function": {
                            "name": "track_todo",
                            "arguments": json.dumps(
                                {
                                    "action": "add",
                                    "title": OLD_TODO,
                                    "status": "in_progress" if stopped else "completed",
                                }
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "已完成。", "finish_reason": "stop"},
        ]
    )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: first)
    async with owner_sessionmaker() as db:
        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"], capability="agent.task.track", allowed=True, requires_approval=False
            )
        )
        await db.commit()

    async def execute(run_id, *, cancel=None):
        fence = set_runtime_task_fence(task_id=run_id, worker_id="kernel-worker-1", claim_version=1)
        try:
            await runtime.execute_web_chat_run(run_id, cancel_event=cancel)
        finally:
            reset_runtime_task_fence(fence)

    runtime.register_web_chat_run_for_test(seed["run_id"].hex, cancel_event=cancel_event)
    worker = asyncio.create_task(execute(seed["run_id"], cancel=cancel_event))
    try:
        if stopped:
            waiting = asyncio.create_task(provider_waiting.wait())
            done, _ = await asyncio.wait([worker, waiting], timeout=40, return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                await worker
            assert provider_waiting.is_set(), "Run did not reach its second provider round"
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            async with owner_sessionmaker() as db:
                receipt = await cancel_session_run(
                    agent_id=seed["agent_id"],
                    session_id=seed["session_id"],
                    run_id=seed["run_id"],
                    idempotency_key="member-stop-history",
                    current_user=await db.get(User, seed["user_id"]),
                    db=db,
                )
            assert receipt["accepted"] is True
            assert cancel_event.is_set()
        await asyncio.wait_for(worker, timeout=40)
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        runtime.unregister_web_chat_run_for_test(seed["run_id"].hex)

    async with owner_sessionmaker() as db:
        previous = await db.get(RuntimeTask, seed["run_id"])
        assert previous.status == ("killed" if stopped else "completed"), previous.metadata_json
        if stopped:
            assert previous.metadata_json["cancel_state"] == "cancelled"
            control = await db.scalar(
                select(SessionControlInput).where(SessionControlInput.expected_run_id == previous.id)
            )
            assert control.status == "applied"
        ledger_before = await read_latest_session_work_ledger_view(
            db=db, agent_id=seed["agent_id"], session_id=seed["session_id"], data_root=seed["data_root"]
        )
        assert ledger_before is not None and any(item["title"] == OLD_TODO for item in ledger_before["todo_items"])
        before = [
            (event.id, event.lifecycle, event.content)
            for event in await db.scalars(
                select(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.run_id == previous.id)
                .order_by(ChatTranscriptEvent.sequence)
            )
        ]

    # Finish the real terminal delivery before the next live input is dispatched.
    for event_id, _, _ in before:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    await drain_terminal_boundary_for_task(
        drain_web_terminal_boundary_outbox_once, task_id=seed["run_id"], worker_id="stopped-history-boundary"
    )

    _, next_run, _ = await _submit_turn(
        owner_sessionmaker,
        user_id=seed["user_id"],
        agent_id=seed["agent_id"],
        session_id=seed["session_id"],
        content=FOLLOWUP,
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, next_run)
        task.status, task.claimed_by, task.claim_version, task.attempt_count = "running", "kernel-worker-1", 1, 1
        task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
        ledger = await read_latest_session_work_ledger_view(
            db=db, agent_id=seed["agent_id"], session_id=seed["session_id"], data_root=seed["data_root"]
        )

    second = ScriptedLLMClient([{"content": "已停止。" if stopped else "已经完成。", "finish_reason": "stop"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: second)
    await execute(next_run)
    assert len(second.requests) == 1
    wire = second.requests[0]["messages"]
    contents = [str(message.get("content") or "") for message in wire]
    partial_index = next(i for i, content in enumerate(contents) if PARTIAL_WORK in content)
    todo_indices = [i for i, content in enumerate(contents) if OLD_TODO in content]
    assert todo_indices, "Committed todo work must remain available as history"
    followup_index = next(i for i, content in enumerate(contents) if FOLLOWUP in content)
    markers = [i for i, content in enumerate(contents) if content == STOP_MARKER]
    if stopped:
        assert len(markers) == 1, "Member stop is missing from the next provider request"
        assert partial_index < markers[0] < followup_index
        assert all(i < markers[0] for i in todo_indices), "Stopped todos appeared as current work"
        assert ledger is None, "The successor must not display the stopped request's session todos"
    else:
        assert markers == []
        assert ledger is not None and any(item["title"] == OLD_TODO for item in ledger["todo_items"])

    async with owner_sessionmaker() as db:
        current = await db.get(RuntimeTask, next_run)
        assert current.status == "completed", current.metadata_json
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == next_run))
        assert result.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]
        after = [
            (event.id, event.lifecycle, event.content)
            for event in await db.scalars(
                select(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.run_id == seed["run_id"])
                .order_by(ChatTranscriptEvent.sequence)
            )
        ]
        assert after == before, "Loading a successor must not rewrite the stopped turn"
