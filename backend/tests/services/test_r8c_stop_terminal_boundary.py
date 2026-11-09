"""Stop signal bookkeeping must not strand the next Web turn."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionControlInput
from app.models.user import User
from app.services.runtime_terminal_boundary_outbox import (
    RuntimeTerminalBoundaryOutboxService,
    TerminalBoundaryCanonicalMismatch,
)
from app.services.session_control_input import recover_stale_cancel_control_inputs_once
from app.services.session_live_input import submit_live_human_input
from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
from tests.services.test_r2_stop_settlement import _cancel
from tests.services.test_session_input_control_v2 import _seed_session


pytestmark = pytest.mark.asyncio


async def _start(factory, *, user_id, agent_id, session_id):
    input_id = uuid4()
    async with factory() as db:
        receipt = await submit_live_human_input(
            db=db, agent=await db.get(Agent, agent_id), user=await db.get(User, user_id),
            session=await db.get(ChatSession, session_id), content="Continue the conversation",
            source="r8c-test", input_id=input_id, idempotency_key=f"r8c:{input_id}",
        )
    return receipt


async def _stopped_turn(factory, monkeypatch, *, late=True, delivered=True):
    import app.database as database
    from app.database import tenant_scoped_session
    from app.services import tenant_resolver
    from app.services import web_chat_runtime as runtime

    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(tenant_resolver, "async_session", factory)
    monkeypatch.setattr(runtime, "_async_session", factory)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda tenant_id, **kwargs:
        tenant_scoped_session(tenant_id, **{**kwargs, "session_factory": factory}))
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(factory)
    ids = dict(user_id=user_id, agent_id=agent_id, session_id=session_id)
    started = await _start(factory, **ids)
    run_id = UUID(started["run"]["run_id"])
    async with factory() as db:
        stopped = await _cancel(db, agent_id, session_id, run_id, user_id)
    assert stopped["status"] == "applying"

    async def finish():
        assert await runtime._finalize_web_chat_run_without_assistant(
            run_uuid=run_id, agent_id=agent_id, session_id=str(session_id), status="killed",
            result_summary="Generation stopped by user.",
            metadata_json={"terminal_reason": "user_cancel", "cancelled_by_user": True},
        )

    async def signal(**payload):
        assert payload["run_id"] == run_id
        if late:
            # The executor commits while the recovery worker awaits transport.
            # Its real delivery writer resumes only after both cancelled events.
            await finish()
        return SimpleNamespace(local_delivered=True, cross_process_delivered=delivered,
            retryable=not delivered, error_class=None if delivered else "transport_unavailable")

    async with factory() as db:
        counts = await recover_stale_cancel_control_inputs_once(
            db, worker_id="r8c-cancel", tenant_id=tenant_id, stale_after=timedelta(0),
            signal_callback=signal,
        )
    assert counts["claimed"] == 1
    assert counts["signalled" if delivered else "unavailable"] == 1
    if not late:
        await finish()
    async with factory() as db:
        task = await db.get(RuntimeTask, run_id)
        control = await db.get(SessionControlInput, stopped["control_id"])
        assert task.status == "killed"
        assert control.status == "applied"
        assert task.metadata_json["cancel_signal_delivery"]["cross_process_delivered"] is delivered
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.run_id == run_id,
        ).order_by(ChatTranscriptEvent.sequence)))
    print("STOP_TAIL", [(e.sequence, e.item_kind, e.lifecycle) for e in events])
    return tenant_id, run_id, ids, events


@pytest.mark.parametrize("late", [True, False], ids=["late-bookkeeping", "early-bookkeeping"])
@pytest.mark.parametrize("delivered", [True, False], ids=["signal-delivered", "signal-unavailable"])
async def test_stop_bookkeeping_delivers_boundary_and_next_start_turn(
    owner_sessionmaker, monkeypatch, late, delivered,
):
    tenant_id, run_id, ids, events = await _stopped_turn(
        owner_sessionmaker, monkeypatch, late=late, delivered=delivered,
    )
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker)
    item, = await service.claim_batch(tenant_id=tenant_id, worker_id="r8c-boundary", task_types=("web_chat_turn",))
    try:
        result = await service.process_terminal_boundary(
            item=item, worker_id="r8c-boundary", canonical_validator=processor.validate, process_callback=processor,
        )
    except TerminalBoundaryCanonicalMismatch as exc:
        print("CLAIM_MISMATCH", str(exc))
        raise
    async with owner_sessionmaker() as db:
        boundary = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        print("BOUNDARY_RESULT", result, boundary.status, boundary.last_error)
        assert boundary.status == "delivered"
    assert result
    # Both existing cancelled witnesses precede enqueue; retain their contract.
    cancelled = [e for e in events if e.item_kind == "run" and e.lifecycle == "cancelled"]
    assert len(cancelled) == 2
    bookkeeping = [e for e in events if e.item_kind == "recovery_action"]
    # The reader now excludes transport bookkeeping for both existing and new
    # rows, so the writer retains the same transport witness before/after stop.
    assert len(bookkeeping) == 1
    assert bookkeeping[0].command_id is not None
    if late:
        assert bookkeeping[0].sequence > cancelled[-1].sequence
    else:
        assert len(bookkeeping) == 1
        assert bookkeeping[0].sequence < cancelled[0].sequence
    next_turn = await _start(owner_sessionmaker, **ids)
    assert next_turn["admission_state"] == "admitted"
    assert next_turn["run"] is not None
    next_run_id = UUID(next_turn["run"]["run_id"])
    assert next_run_id != run_id
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, next_run_id)).status == "pending"
    print("NEXT_START_TURN", "dispatched")


@pytest.mark.parametrize("divergence", ["content", "terminal-status", "foreign-event"])
async def test_stopped_boundary_still_rejects_real_divergence(owner_sessionmaker, monkeypatch, divergence):
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, run_id, ids, _ = await _stopped_turn(owner_sessionmaker, monkeypatch, late=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker)
    item, = await service.claim_batch(tenant_id=tenant_id, worker_id="r8c-negative", task_types=("web_chat_turn",))
    async with owner_sessionmaker() as db:
        if divergence == "content":
            event = await db.get(ChatTranscriptEvent, UUID(item.binding["terminal_event_id"]))
            event.content = "Changed after the claim"
        elif divergence == "terminal-status":
            (await db.get(RuntimeTask, run_id)).status = "failed"
        else:
            await append_session_events(db, tenant_id=tenant_id, agent_id=ids["agent_id"],
                session_id=ids["session_id"], drafts=[SessionEventDraft(
                    item_id=uuid4(), item_kind="recovery_action", lifecycle="completed",
                    scope={"level": "run", "session_id": str(ids["session_id"]),
                        "thread_id": str(ids["session_id"]), "run_id": str(run_id), "turn_id": f"turn-{run_id.hex}"},
                    actor={"type": "runtime"}, payload={"reason_code": "foreign_bookkeeping"},
                )])
        await db.commit()
    with pytest.raises(TerminalBoundaryCanonicalMismatch) as exc:
        await processor(item)
    print("DIVERGENCE_REJECTED", divergence, str(exc.value))


async def _assert_delivered_and_next_start(factory, service, processor, item, worker_id, ids):
    assert await service.process_terminal_boundary(
        item=item, worker_id=worker_id, canonical_validator=processor.validate, process_callback=processor,
    )
    async with factory() as db:
        boundary = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        print("BOUNDARY_DELIVERED", boundary.status, boundary.last_error)
        assert boundary.status == "delivered"
    next_turn = await _start(factory, **ids)
    assert next_turn["admission_state"] == "admitted"
    assert next_turn["run"] is not None
    next_run_id = UUID(next_turn["run"]["run_id"])
    assert next_run_id != item.runtime_task_id
    async with factory() as db:
        assert (await db.get(RuntimeTask, next_run_id)).status == "pending"
        assert await db.scalar(select(func.count()).select_from(RuntimeTask).where(
            RuntimeTask.parent_session_id == str(ids["session_id"]),
        )) == 2
    print("NEXT_START_TURN", "exactly one successor")


async def test_existing_late_cancel_dead_letter_redrive_delivers_and_starts_once(owner_sessionmaker, monkeypatch):
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    tenant_id, run_id, ids, _ = await _stopped_turn(owner_sessionmaker, monkeypatch, late=False)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker, max_attempts=1)
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker)
    worker_id = "r8c-historical-boundary"
    item, = await service.claim_batch(tenant_id=tenant_id, worker_id=worker_id, task_types=("web_chat_turn",))
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        control = await db.scalar(select(SessionControlInput).where(SessionControlInput.expected_run_id == run_id))
        # Recreate the pre-fix recorder's late transport row, including its
        # command binding. This row predates the repair, not the redrive.
        payload = {**task.metadata_json["cancel_signal_delivery"],
            "attempt_id": f"cancel-signal-delivery:{control.id}:2"}
        late_event, = await append_session_events(db, tenant_id=tenant_id, agent_id=ids["agent_id"],
            session_id=ids["session_id"], drafts=[SessionEventDraft(
                item_id=uuid5(control.id, "cancel-signal-delivery:2"),
                item_kind="recovery_action", lifecycle="completed",
                scope={"level": "run", "session_id": str(ids["session_id"]),
                    "thread_id": str(ids["session_id"]), "run_id": str(run_id), "turn_id": task.metadata_json["turn_id"]},
                actor={"type": "runtime"}, command_id=control.command_id,
                payload={"control_id": str(control.id), "expected_run_id": str(run_id),
                    "worker_id": "r8c-legacy-cancel", **payload},
            )])
        assert late_event.sequence > item.binding["terminal_sequence"]
        assert late_event.command_id == control.command_id
        await db.commit()
    # Seed the already-recorded production failure via the existing service.
    assert await service.fail_terminal_boundary(item=item, worker_id=worker_id,
        error=TerminalBoundaryCanonicalMismatch("canonical authority binding does not match claimed binding")) == "dead_letter"
    async with owner_sessionmaker() as db:
        boundary = await db.get(RuntimeTerminalBoundaryOutbox, item.id)
        assert boundary.status == "dead_letter"
        print("HISTORICAL_DEAD_LETTER", boundary.status, boundary.last_error)
    await service.redrive_dead_letter(tenant_id=tenant_id, outbox_id=item.id,
        actor_user_id=ids["user_id"], reason="Retry the stopped turn after transport-frontier repair")
    redriven, = await service.claim_batch(tenant_id=tenant_id, worker_id=worker_id, task_types=("web_chat_turn",))
    assert redriven.id == item.id
    await _assert_delivered_and_next_start(owner_sessionmaker, service, processor, redriven, worker_id, ids)


@pytest.mark.parametrize("terminal_status", ["killed", "failed"])
async def test_stop_after_terminal_delivers_boundary_and_starts_once(owner_sessionmaker, monkeypatch, terminal_status):
    import app.database as database
    from app.database import tenant_scoped_session
    from app.services import tenant_resolver, web_chat_runtime as runtime

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(tenant_resolver, "async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "_async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda tenant_id, **kwargs:
        tenant_scoped_session(tenant_id, **{**kwargs, "session_factory": owner_sessionmaker}))
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    ids = dict(user_id=user_id, agent_id=agent_id, session_id=session_id)
    started = await _start(owner_sessionmaker, **ids)
    run_id = UUID(started["run"]["run_id"])
    assert await runtime._finalize_web_chat_run_without_assistant(
        run_uuid=run_id, agent_id=agent_id, session_id=str(session_id), status=terminal_status,
        result_summary="The run has already ended.", metadata_json={"terminal_reason": "provider_error"},
    )
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker)
    worker_id = "r8c-late-stop"
    item, = await service.claim_batch(tenant_id=tenant_id, worker_id=worker_id, task_types=("web_chat_turn",))
    async with owner_sessionmaker() as db:
        rejected = await _cancel(db, agent_id, session_id, run_id, user_id)
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "active_run_not_found"
    await _assert_delivered_and_next_start(owner_sessionmaker, service, processor, item, worker_id, ids)
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.command_id == rejected["command_id"], ChatTranscriptEvent.lifecycle == "rejected",
        ))
        assert event.scope_json["level"] == "session"
        assert event.run_id is None
        assert event.metadata_json["v2_payload"]["expected_run_id"] == str(run_id)
    print("LATE_STOP_REJECTED_AT_SESSION_SCOPE", terminal_status)


async def test_operator_recovery_action_without_command_still_rejects_claim(owner_sessionmaker):
    from app.services.session_tool_runtime import acknowledge_unresolved_tool_effects
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.web_terminal_boundary_processor import enqueue_web_terminal_boundary_for_task
    from tests.services.test_runtime_reconciliation import _seed_unresolved_tool_effect

    tenant_id, user_id, agent_id, _, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task, _, session_id = await _seed_unresolved_tool_effect(
            db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
        )
        await append_session_events(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            drafts=[SessionEventDraft(item_id=task.id, item_kind="run", lifecycle="failed",
                scope={"level": "run", "session_id": str(session_id), "thread_id": str(session_id),
                    "run_id": str(task.id), "turn_id": task.metadata_json["turn_id"]},
                actor={"type": "runtime"}, payload={"reason_code": "tool_effect_outcome_unknown"})])
        await enqueue_web_terminal_boundary_for_task(db, task=task)
        await db.commit()
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker)
    item, = await service.claim_batch(tenant_id=tenant_id, worker_id="r8c-operator", task_types=("web_chat_turn",))
    async with owner_sessionmaker() as db:
        events = await acknowledge_unresolved_tool_effects(db, tenant_id=tenant_id, agent_id=agent_id,
            session_id=session_id, run_id=task.id, actor_user_id=user_id,
            reason="Operator inspected the unconfirmed tool effect")
        recovery, = [event for event in events if event.item_kind == "recovery_action"]
        assert recovery.command_id is None
        assert recovery.sequence > item.binding["terminal_sequence"]
        await db.commit()
    async with owner_sessionmaker() as db:
        # Pin the recovery row itself as content. A blanket kind exemption
        # would otherwise still fail on the preceding tool_call row.
        canonical = await processor.validate(db, item)
        assert canonical["terminal_event_id"] == str(recovery.id)
    with pytest.raises(TerminalBoundaryCanonicalMismatch):
        await processor(item)
    print("OPERATOR_RECOVERY_REJECTED", "command_id absent; recovery row remains the frontier")
