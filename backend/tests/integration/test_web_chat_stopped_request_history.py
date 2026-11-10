"""A member stop reaches the next provider request as historical context."""

import asyncio
import json
import uuid
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


@pytest.mark.parametrize(
    "case",
    [
        "after_settlement",
        "before_settlement",
        "compact_summary",
        "compact_kept_marker",
        "normal_completion",
        "non_member_kill",
    ],
)
async def test_next_request_records_member_stop_without_current_old_todos(
    t2_work,
    owner_sessionmaker,
    monkeypatch,
    case,
    drain_terminal_boundary_for_task,
):
    import app.runtime.invoker as invoker
    from app.api.chat_sessions import cancel_session_run
    from app.models.capability_policy import CapabilityPolicy
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput, SessionModelResult, SessionTurnInput
    from app.models.user import User
    from app.services import web_chat_runtime as runtime
    from app.services.agent_work_ledger import read_latest_session_work_ledger_view
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0
    from app.services.runtime_task_worker import drain_web_terminal_boundary_outbox_once
    from app.services.session_live_input import submit_live_human_input

    seed = t2_work
    stopped = case != "normal_completion"
    member_stop = stopped and case != "non_member_kill"
    cancel_event = asyncio.Event()
    provider_waiting = asyncio.Event()
    settling = asyncio.Event()
    allow_settlement = asyncio.Event()
    followup_id = None

    # Pause local signal delivery after the real cancelling event commits;
    # neither cancel settlement nor input admission is replaced by a double.
    original_signal = runtime.signal_web_chat_cancel

    async def signal(**kwargs):
        if case == "before_settlement":
            settling.set()
            await allow_settlement.wait()
        return await original_signal(**kwargs)

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)

    async def stop_run():
        async with owner_sessionmaker() as db:
            return await cancel_session_run(
                agent_id=seed["agent_id"],
                session_id=seed["session_id"],
                run_id=seed["run_id"],
                idempotency_key="member-stop-history",
                current_user=await db.get(User, seed["user_id"]),
                db=db,
            )

    stop_task = None

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
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, run_id)
            if task.status == "pending":
                task.status, task.claimed_by, task.claim_version, task.attempt_count = (
                    "running",
                    "kernel-worker-1",
                    1,
                    1,
                )
                task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
                await db.commit()
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
            if member_stop:
                stop_task = asyncio.create_task(stop_run())
                if case != "before_settlement":
                    assert (await stop_task)["accepted"] is True
            else:
                cancel_event.set()
            if case == "before_settlement":
                await asyncio.wait_for(settling.wait(), timeout=40)
                followup_id = uuid.uuid4()
                async with owner_sessionmaker() as db:
                    receipt = await submit_live_human_input(
                        db=db,
                        agent=await db.get(Agent, seed["agent_id"]),
                        user=await db.get(User, seed["user_id"]),
                        session=await db.get(ChatSession, seed["session_id"]),
                        content=FOLLOWUP,
                        source="web",
                        input_id=followup_id,
                        idempotency_key=f"followup:{followup_id}",
                        requested_kind="auto",
                    )
                    assert receipt["intent"] == "steer_current_turn", receipt
                    row = await db.get(SessionTurnInput, followup_id)
                    assert row.target_run_id == seed["run_id"]
                    assert (await db.get(RuntimeTask, seed["run_id"])).metadata_json["cancel_state"] == "cancelling"
                    await db.commit()
                allow_settlement.set()
                assert (await stop_task)["accepted"] is True
            assert cancel_event.is_set()
        await asyncio.wait_for(worker, timeout=40)
    finally:
        allow_settlement.set()
        if stop_task is not None:
            await asyncio.gather(stop_task, return_exceptions=True)
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        runtime.unregister_web_chat_run_for_test(seed["run_id"].hex)

    async with owner_sessionmaker() as db:
        previous = await db.get(RuntimeTask, seed["run_id"])
        assert previous.status == ("killed" if stopped else "completed"), previous.metadata_json
        if member_stop:
            assert previous.metadata_json["cancel_state"] == "cancelled"
            control = await db.scalar(
                select(SessionControlInput).where(SessionControlInput.expected_run_id == previous.id)
            )
            assert control.status == "applied"
        else:
            assert (
                await db.scalar(select(SessionControlInput).where(SessionControlInput.expected_run_id == previous.id))
                is None
            )
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
        if case == "non_member_kill":
            cancelled = list(
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.run_id == previous.id,
                        ChatTranscriptEvent.item_kind == "run",
                        ChatTranscriptEvent.lifecycle == "cancelled",
                    )
                )
            )
            assert cancelled and all(
                not (event.metadata_json or {}).get("v2_payload", {}).get("control_id") for event in cancelled
            )

    # Finish the real terminal delivery before the next live input is dispatched.
    for event_id, _, _ in before:
        await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
    await drain_terminal_boundary_for_task(
        drain_web_terminal_boundary_outbox_once, task_id=seed["run_id"], worker_id="stopped-history-boundary"
    )

    if case.startswith("compact_"):
        from tests.integration.test_session_compact_v2_semantic_history import _run_compact

        compact = await _run_compact(
            owner_sessionmaker,
            (seed["agent_id"], seed["session_id"], seed["tenant_id"], seed["user_id"]),
            monkeypatch,
            [],
            arguments={"keep_recent": 1 if case == "compact_kept_marker" else 100},
        )
        assert compact["action"] == "compacted_context_installed"

    if followup_id is not None:
        from app.services import runtime_task_worker

        await runtime_task_worker.recover_session_input_dispatches_once(
            worker_id="stop-history-dispatch",
            stale_after=timedelta(seconds=0),
            session_factory=owner_sessionmaker,
            tenant_id=seed["tenant_id"],
        )
        await runtime_task_worker.recover_terminal_target_session_inputs_once(
            worker_id="stop-history-rollover",
            stale_after=timedelta(seconds=0),
            session_factory=owner_sessionmaker,
            tenant_id=seed["tenant_id"],
        )
        async with owner_sessionmaker() as db:
            row = await db.get(SessionTurnInput, followup_id)
            assert row.status == "rolled_over"
            next_run = await db.scalar(
                select(RuntimeTask.id).where(
                    RuntimeTask.parent_session_id == str(seed["session_id"]), RuntimeTask.id != seed["run_id"]
                )
            )
            assert next_run is not None
    else:
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
    todo_indices = [i for i, content in enumerate(contents) if OLD_TODO in content]
    followup_index = next(i for i, content in enumerate(contents) if FOLLOWUP in content)
    markers = [i for i, content in enumerate(contents) if content == STOP_MARKER]
    if member_stop:
        assert len(markers) == 1, "Member stop is missing from the next provider request"
        assert markers[0] < followup_index
        if case.startswith("compact_"):
            summary_index = next(
                i for i, content in enumerate(contents) if "[Previous conversation summary]" in content
            )
            assert summary_index < markers[0]
            assert "Pick up the last task" not in contents[summary_index]
            assert "Continue the conversation from where it left off" not in contents[summary_index]
        else:
            partial_index = next(i for i, content in enumerate(contents) if PARTIAL_WORK in content)
            assert partial_index < markers[0]
            assert todo_indices and all(i < markers[0] for i in todo_indices)
    else:
        assert markers == []
    # R11a no longer filters the session file or claims to fix card ownership.
    assert ledger is not None and ledger["todo_items"] == ledger_before["todo_items"]

    async with owner_sessionmaker() as db:
        current = await db.get(RuntimeTask, next_run)
        assert current.status == "completed", current.metadata_json
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == next_run))
        bindings = result.model_request_snapshot_json["input_bindings"]["messages"]
        stop_bindings = [
            entry for entry in bindings if str(entry.get("source_ref", "")).startswith("session-run-cancelled:")
        ]
        assert len(stop_bindings) == int(member_stop)
        assert all(entry["status"] == "bound" and entry["dependency_status"] == "resolved" for entry in stop_bindings)
        after = [
            (event.id, event.lifecycle, event.content)
            for event in await db.scalars(
                select(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.run_id == seed["run_id"])
                .order_by(ChatTranscriptEvent.sequence)
            )
        ]
        assert after == before, "Loading a successor must not rewrite the stopped turn"

    if case == "before_settlement":
        # The rolled-over input is historical on the following request too.
        async with owner_sessionmaker() as db:
            ids = list(await db.scalars(select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.run_id == next_run)))
        for event_id in ids:
            await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1)
        await drain_terminal_boundary_for_task(
            drain_web_terminal_boundary_outbox_once, task_id=next_run, worker_id="stop-history-next-boundary"
        )
        _, third_run, _ = await _submit_turn(
            owner_sessionmaker,
            user_id=seed["user_id"],
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            content="好的，谢谢",
        )
        third = ScriptedLLMClient([{"content": "不客气。", "finish_reason": "stop"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: third)
        await execute(third_run)
        third_contents = [str(message.get("content") or "") for message in third.requests[0]["messages"]]
        assert third_contents.index(PARTIAL_WORK) < third_contents.index(STOP_MARKER) < third_contents.index(FOLLOWUP)
        async with owner_sessionmaker() as db:
            result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == third_run))
            stop_binding = next(
                entry
                for entry in result.model_request_snapshot_json["input_bindings"]["messages"]
                if str(entry.get("source_ref", "")).startswith("session-run-cancelled:")
            )
            assert stop_binding["dependency_status"] == "resolved"
