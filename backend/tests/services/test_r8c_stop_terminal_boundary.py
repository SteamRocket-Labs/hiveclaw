"""Stop signal bookkeeping must not strand the next Web turn."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

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
    from app.services import web_chat_runtime as runtime

    monkeypatch.setattr(database, "async_session", factory)
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
    async with owner_sessionmaker() as db:
        try:
            await processor._load(item)
        except TerminalBoundaryCanonicalMismatch as exc:
            print("BOUNDARY_MISMATCH", type(exc).__name__, str(exc))
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
    if late:
        # Delivery facts stay in RuntimeTask metadata after terminality. Appending
        # their transcript witness here would change the already-bound frontier.
        assert bookkeeping == []
    else:
        assert len(bookkeeping) == 1
        assert bookkeeping[0].sequence < cancelled[0].sequence
    next_turn = await _start(owner_sessionmaker, **ids)
    assert next_turn["admission_state"] == "admitted"
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
