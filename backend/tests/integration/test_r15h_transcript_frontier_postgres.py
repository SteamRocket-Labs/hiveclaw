"""Committed terminal owners must release ordered T0 projection frontiers."""

from datetime import datetime, timezone
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, select, text

from app.memory.t0 import ledger
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services import runtime_control_bus as bus
from app.services.chat_transcript import append_session_event
from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _commit_boundary(db, task, agent, session, terminal, *, status="delivered"):
    boundary = await enqueue_terminal_boundary(
        db, task=task, event_kind="turn_stop" if task.status == "completed" else "turn_abort",
        agent_id=agent, session_id=session, terminal_status=task.status,
        authority_ref="runtime_task", authority_id=task.id,
        binding={"terminal_event_id": terminal.event_id, "terminal_sequence": terminal.sequence},
    )
    boundary.status = status
    return boundary


async def _frontier(factory, monkeypatch, tmp_path, *, owner_type="web_chat_turn",
                    incoming_type="web_chat_turn", owner_status="completed", incoming_status="completed",
                    include_owner_final=True):
    import app.database as database

    tenant, user, agent, session, owner = await _seed_session(factory, active_run=True)
    incoming = uuid.uuid4()
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def no_publish(**_kwargs):
        pass

    monkeypatch.setattr(bus, "publish_transcript_t0_bridge", no_publish)
    async with factory() as db:
        task = await db.get(RuntimeTask, owner)
        task.task_type = owner_type
        task.metadata_json = {}
        db.add(RuntimeTask(id=incoming, tenant_id=tenant, task_type=incoming_type, status=incoming_status,
            parent_agent_id=agent, child_agent_id=agent, parent_session_id=str(session),
            child_session_id=str(session), completed_at=datetime.now(timezone.utc), prompt="incoming transcript run"))
        await db.flush()
        start = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=owner, actor_type="system", event_type="model_route", content="owner start",
            materialize_chat_message=False)
        await db.commit()
    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=start.event_id, attempts=1)

    async with factory() as db:
        rows = []
        # An inline continuation can precede its owner's canonical final. The
        # owner's boundary processor must bridge this same unfinished prefix.
        records = [
            (incoming, "system", "model_route", "incoming start"),
            (incoming, "assistant", "assistant_message", "incoming final"),
        ]
        if include_owner_final:
            records.append((owner, "assistant", "assistant_message", "owner final"))
        for run, actor, kind, content in records:
            rows.append(await append_session_event(db=db, tenant_id=tenant, agent_id=agent,
                session_id=session, run_id=run, actor_type=actor, event_type=kind, content=content,
                materialize_chat_message=False))
        owner_task, incoming_task = await db.get(RuntimeTask, owner), await db.get(RuntimeTask, incoming)
        owner_task.status, incoming_task.status = owner_status, incoming_status
        owner_task.completed_at = datetime.now(timezone.utc) if owner_status != "running" else None
        incoming_task.completed_at = datetime.now(timezone.utc)
        owner_boundary = None
        if owner_status != "running" and owner_type != "subagent":
            owner_boundary = await _commit_boundary(db, owner_task, agent, session,
                rows[-1] if include_owner_final else start)
        if incoming_type != "subagent":
            await _commit_boundary(db, incoming_task, agent, session, rows[1])
        await db.commit()

    # Persist the exact diagnostic produced by the existing ledger, modeling
    # an already-failed production frontier without invoking repaired code.
    with pytest.raises(ledger.T0SegmentBoundaryPending) as caught:
        ledger.append_t0_session_event(agent_id=agent, session_id=session, runtime_task_id=incoming,
            event_type="model_route", content="incoming start", data_root=tmp_path)
    async with factory() as db:
        first = await db.get(ChatTranscriptEvent, rows[0].event_id)
        first.projection_status = "failed"
        first.projection_attempts = 38760
        first.projection_error = f"T0SegmentBoundaryPending: {caught.value}"
        await db.commit()
    return SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, owner=owner, incoming=incoming,
        start=start, rows=rows, owner_boundary=owner_boundary, root=tmp_path)


async def _states(factory, fx):
    async with factory() as db:
        rows = list((await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == fx.session).order_by(ChatTranscriptEvent.sequence))).all())
        return [(row.projection_status, row.projection_attempts, row.projection_error) for row in rows]


@pytest.mark.parametrize("owner_type,incoming_type,owner_status,incoming_status", [
    ("delegation", "a2a_continuation", "completed", "completed"),
    ("delegation", "a2a_continuation", "failed", "failed"),
    ("web_chat_turn", "trigger", "completed", "completed"),
    ("web_chat_turn", "web_chat_turn", "completed", "completed"),
    ("trigger", "web_chat_turn", "completed", "completed"),
    ("web_chat_turn", "team_member", "completed", "completed"),
    ("delegation", "web_chat_turn", "completed", "needs_reconciliation"),
])
async def test_committed_terminal_owner_drains_frontier_and_prefix_in_one_sweep(
    owner_sessionmaker, monkeypatch, tmp_path, owner_type, incoming_type, owner_status, incoming_status,
):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, owner_type=owner_type,
        incoming_type=incoming_type, owner_status=owner_status, incoming_status=incoming_status)
    async with owner_sessionmaker() as db:
        for run in (fx.owner, fx.incoming):
            task = await db.get(RuntimeTask, run)
            assert task.terminal_boundary_generation == 1
            assert task.terminal_boundary_enqueued_at is not None
    counts = [await bus.sweep_pending_transcript_t0_bridges(limit=10, session_id=fx.session)]
    if counts[0] == 0:
        counts.append(await bus.sweep_pending_transcript_t0_bridges(limit=10, session_id=fx.session))
    states = await _states(owner_sessionmaker, fx)
    assert counts[0] == 3, f"sweeps={counts}; states={states}"
    assert [(status, attempts) for status, attempts, _error in states] == [
        ("projected", 1), ("projected", 38761), ("projected", 1), ("projected", 1)]
    events = ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)
    transcript_ids = [item.metadata["transcript_event_id"] for item in events
        if item.metadata.get("transcript_event_id")]
    assert transcript_ids == [str(fx.start.event_id), *(str(row.event_id) for row in fx.rows)]
    assert [item.content for item in events if item.event_type != "segment_boundary"] == [
        "owner start", "incoming start", "incoming final", "owner final"]
    assert [item.runtime_task_id for item in events if item.event_type == "segment_boundary"] == [
        fx.owner.hex, fx.incoming.hex]
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert [item.event_id for item in ledger.replay_t0_session_events(
        agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)] == [item.event_id for item in events]
    # The owner can have another open segment after its final is drained.
    # Recovery of that segment must not replay the earlier segment's seal.
    async with owner_sessionmaker() as db:
        followup = await append_session_event(db=db, tenant_id=fx.tenant, agent_id=fx.agent,
            session_id=fx.session, run_id=fx.incoming, actor_type="system", event_type="model_route",
            content="incoming followup", materialize_chat_message=False)
        await db.commit()
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 1
    replay = ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)
    assert replay[-1].metadata["transcript_event_id"] == str(followup.event_id)
    assert [item.runtime_task_id for item in replay if item.event_type == "segment_boundary"] == [
        fx.owner.hex, fx.incoming.hex, fx.owner.hex]


@pytest.mark.parametrize("missing", ["running", "completed_at", "boundary", "unenqueued", "generation_only",
    "tenant", "agent", "session", "boundary_session", "boundary_status"])
async def test_unproven_owner_stays_open(owner_sessionmaker, monkeypatch, tmp_path, missing):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path,
        owner_status="running" if missing == "running" else "completed")
    async with owner_sessionmaker() as db:
        owner = await db.get(RuntimeTask, fx.owner)
        if missing == "completed_at":
            owner.completed_at = None
        elif missing == "boundary":
            await db.delete(fx.owner_boundary)
        elif missing == "unenqueued":
            owner.terminal_boundary_enqueued_at = None
        elif missing == "generation_only":
            await db.delete(fx.owner_boundary)
            owner.terminal_boundary_enqueued_at = None
        elif missing == "tenant":
            from app.models.tenant import Tenant
            other = Tenant(id=uuid.uuid4(), name="Other tenant", slug=f"r15h-{uuid.uuid4().hex}")
            db.add(other)
            await db.flush()
            owner.tenant_id = other.id
        elif missing == "agent":
            owner.parent_agent_id = owner.child_agent_id = None
        elif missing == "session":
            owner.parent_session_id = owner.child_session_id = str(uuid.uuid4())
        elif missing == "boundary_session":
            (await db.get(type(fx.owner_boundary), fx.owner_boundary.id)).session_id = str(uuid.uuid4())
        elif missing == "boundary_status":
            (await db.get(type(fx.owner_boundary), fx.owner_boundary.id)).terminal_status = "failed"
        await db.commit()
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    states = await _states(owner_sessionmaker, fx)
    assert states[1][0] == "failed" and states[1][2].startswith("T0SegmentBoundaryPending:")
    assert [row[0] for row in states[2:]] == ["pending", "pending"]
    assert [item.content for item in ledger.replay_t0_session_events(
        agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)] == ["owner start"]


async def test_unchanged_blocked_frontier_waits_for_owner_commit_without_new_attempts(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, owner_status="running")
    statements = []
    engine = owner_sessionmaker.kw["bind"].sync_engine

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        for _ in range(2):
            assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 4, statements
    assert sum("FOR UPDATE SKIP LOCKED" in statement for statement in statements) == 2
    assert all("chat_transcript_events" in statement for statement in statements)
    assert (await _states(owner_sessionmaker, fx))[1][1] == 38760
    async with owner_sessionmaker() as db:
        owner = await db.get(RuntimeTask, fx.owner)
        owner.status, owner.completed_at = "completed", datetime.now(timezone.utc)
        await _commit_boundary(db, owner, fx.agent, fx.session, fx.rows[-1])
        await db.commit()
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 3


async def test_empty_projection_sweep_only_issues_candidate_query(owner_sessionmaker, monkeypatch):
    import app.database as database

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    statements = []
    engine = owner_sessionmaker.kw["bind"].sync_engine

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await bus.sweep_pending_transcript_t0_bridges(session_id=uuid.uuid4()) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1, statements
    assert "chat_transcript_events" in statements[0]


async def test_existing_ledger_checkpoint_releases_frontier_without_new_task_facts(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path,
        owner_status="running", include_owner_final=False)
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    # PRE_COMPACTION already seals live owners through this ledger operation;
    # it does not change RuntimeTask terminal status or enqueue a boundary.
    assert ledger.seal_t0_session_segment(agent_id=fx.agent, session_id=fx.session,
        reason="pre_compaction_checkpoint", expected_runtime_task_id=fx.owner, data_root=tmp_path)
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 2
    async with owner_sessionmaker() as db:
        owner = await db.get(RuntimeTask, fx.owner)
        assert owner.status == "running" and owner.completed_at is None
        assert owner.terminal_boundary_enqueued_at is None


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_inflight_canonical_owner_keeps_its_seal_and_delivers(
    owner_sessionmaker, monkeypatch, tmp_path, status,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary
    from app.runtime import hooks_setup
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import (
        WebTerminalBoundaryProcessor, build_web_terminal_boundary_binding,
    )

    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, include_owner_final=False)
    async with owner_sessionmaker() as db:
        owner = await db.get(RuntimeTask, fx.owner)
        boundary = await db.get(Boundary, fx.owner_boundary.id)
        # The owner terminal precedes the other run. Its processor can seal
        # normally once the unrelated projection leaves its segment alone.
        from app.services.runtime_terminal_boundary_outbox import terminal_boundary_binding_sha256
        binding = await build_web_terminal_boundary_binding(db, tenant_id=fx.tenant,
            runtime_task_id=fx.owner, agent_id=fx.agent, session_id=fx.session,
            event_kind="turn_stop", terminal_status=owner.status,
            authority_ref="runtime_task", authority_id=fx.owner)
        boundary.binding_json = binding
        boundary.binding_sha256 = terminal_boundary_binding_sha256(binding)
        boundary.status = status
        first = await db.get(ChatTranscriptEvent, fx.rows[0].event_id)
        first.projection_status, first.projection_error = "pending", None
        first.projection_attempts = 0
        await db.commit()
    # A new-event wake and a direct processor drain both reach the bridge.
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session, retry_failed=False) == 0
    assert not await bus.bridge_transcript_event_to_t0(transcript_event_id=fx.rows[0].event_id, attempts=1)
    assert [item.content for item in ledger.replay_t0_session_events(
        agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)] == ["owner start"]

    async def project(ctx):
        await hooks_setup._t0_turn_stop(ctx)

    async def no_hook(*_args, **_kwargs):
        pass

    # Avoid starting an unrelated T2 model job in this runtime-task boundary.
    monkeypatch.setattr(hooks_setup, "_t0_segment_t2_eligible", lambda _ctx: (False, "fixture"))
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker, data_root=tmp_path,
        turn_boundary_projector=project, emit_advisory_hook=no_hook, bridge_attempts=1)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    item, = await service.claim_batch(tenant_id=fx.tenant, worker_id="r15h-inflight", limit=1)
    assert item.id == fx.owner_boundary.id
    assert await service.process_terminal_boundary(item=item, worker_id="r15h-inflight",
        canonical_validator=processor.validate, process_callback=processor)
    async with owner_sessionmaker() as db:
        assert (await db.get(Boundary, item.id)).status == "delivered"
    events = ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)
    assert events[-1].event_type == "segment_boundary"
    assert events[-1].metadata["boundary_id"] == str(item.id)
    assert events[-1].segment_id == events[0].segment_id


async def test_terminal_subagent_guest_with_real_pending_owner_outbox(owner_sessionmaker, monkeypatch, tmp_path):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary
    from app.services.runtime_terminal_boundary_outbox import terminal_boundary_binding_sha256
    from app.services.web_terminal_boundary_processor import build_web_terminal_boundary_binding

    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, incoming_type="subagent",
        incoming_status="needs_reconciliation", include_owner_final=False)
    async with owner_sessionmaker() as db:
        incoming = await db.get(RuntimeTask, fx.incoming)
        incoming.terminal_boundary_enqueued_at = None
        incoming.terminal_boundary_generation = None
        boundary = await db.get(Boundary, fx.owner_boundary.id)
        binding = await build_web_terminal_boundary_binding(db, tenant_id=fx.tenant,
            runtime_task_id=fx.owner, agent_id=fx.agent, session_id=fx.session,
            event_kind="turn_stop", terminal_status="completed",
            authority_ref="runtime_task", authority_id=fx.owner)
        boundary.binding_json = binding
        boundary.binding_sha256 = terminal_boundary_binding_sha256(binding)
        boundary.status = "pending"
        await db.commit()
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 2
    events = ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)
    assert [item.content for item in events] == ["owner start", "incoming start", "incoming final"]
    assert len({item.segment_id for item in events}) == 1
    assert all(item.runtime_task_id is None for item in events[1:])
    assert all(item.metadata["t0_bridge_joined_segment_run_id"] == str(fx.incoming) for item in events[1:])


async def test_reopened_nonboundary_owner_keeps_old_receipt_and_seals_new_segment(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, owner_type="subagent",
        include_owner_final=False)
    async with owner_sessionmaker() as db:
        owner = await db.get(RuntimeTask, fx.owner)
        owner.terminal_boundary_generation = None
        owner.terminal_boundary_enqueued_at = None
        await db.commit()
    key = f"t0-terminal-owner-recovery:{fx.owner}"
    original = ledger.seal_t0_session_segment(agent_id=fx.agent, session_id=fx.session,
        reason="terminal_owner_segment_recovery", expected_runtime_task_id=fx.owner,
        idempotency_key=key, data_root=tmp_path)
    reopened = ledger.append_t0_session_event(agent_id=fx.agent, session_id=fx.session,
        runtime_task_id=fx.owner, event_type="tool_result", content="late owner result", data_root=tmp_path)
    assert reopened.segment_id != original.segment_id
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 2
    states = await _states(owner_sessionmaker, fx)
    assert states[1][1] == 38761
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert await _states(owner_sessionmaker, fx) == states
    replay = ledger.seal_t0_session_segment(agent_id=fx.agent, session_id=fx.session,
        reason="terminal_owner_segment_recovery", expected_runtime_task_id=fx.owner,
        idempotency_key=key, data_root=tmp_path)
    assert replay == original
    boundaries = [item for item in ledger.replay_t0_session_events(
        agent_id=fx.agent, session_id=fx.session, data_root=tmp_path) if item.event_type == "segment_boundary"]
    assert [item.segment_id for item in boundaries] == [original.segment_id, reopened.segment_id]


async def test_unchanged_frontier_reads_index_without_reading_segment(owner_sessionmaker, monkeypatch, tmp_path):
    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path, owner_status="running")

    def no_segment(*_args, **_kwargs):
        pytest.fail("unchanged frontier must only read the index pointer")

    monkeypatch.setattr(ledger, "_replay_segment_events", no_segment)
    monkeypatch.setattr(ledger, "_read_jsonl_events_reverse", no_segment)
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert (await _states(owner_sessionmaker, fx))[1][1] == 38760


async def _deadletter_frontier(factory, monkeypatch, tmp_path):
    import app.database as database
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.web_terminal_boundary_processor import _sha256, build_web_terminal_boundary_binding

    tenant, user, agent, session, owner = await _seed_session(factory, active_run=True)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def no_publish(**_kwargs):
        pass

    monkeypatch.setattr(bus, "publish_transcript_t0_bridge", no_publish)
    result_id, outcome_id, incoming = (uuid.uuid4() for _ in range(3))
    snapshot, response = {"wire_request": {"messages": []}}, "Committed owner answer."
    scope = {"level": "run", "session_id": str(session), "thread_id": str(session),
        "turn_id": f"turn-{owner.hex}", "run_id": str(owner)}
    async with factory() as db:
        task = await db.get(RuntimeTask, owner)
        task.status, task.completed_at = "completed", datetime.now(timezone.utc)
        db.add(SessionModelResult(id=result_id, tenant_id=tenant, session_id=session,
            turn_id=scope["turn_id"], run_id=owner, round_id=f"{owner}:round:1",
            provider_request_id=f"hive:{owner}:round:1:attempt:1", state="round_committed",
            model_request_hash=_sha256(snapshot), model_request_snapshot_json=snapshot,
            bound_input_ids_json=[], seal_json={"semantic_content": response, "content_hash": _sha256(response),
                "continuation": {"verdict": "terminal_candidate"}, "logical_round_complete": True}))
        await db.flush()
        assistant, = await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session,
            drafts=[SessionEventDraft(item_id=uuid.uuid4(), item_kind="assistant_final", lifecycle="completed",
                scope={**scope, "level": "round", "round_id": f"{owner}:round:1"},
                actor={"type": "assistant", "agent_id": str(agent)}, result_id=result_id,
                content_hash=_sha256([]), payload={"zero_copy": True, "outcome_id": str(outcome_id),
                    "terminal_result_id": str(result_id), "render_owner_id": str(uuid.uuid4()),
                    "source_blocks": [], "artifact_refs": [], "result_content_hash": _sha256([]), "parts": []})])
        db.add(SessionRunOutcome(id=outcome_id, tenant_id=tenant, session_id=session,
            turn_id=scope["turn_id"], run_id=owner, terminal_result_id=result_id,
            state="terminal_committed", eligibility_snapshot_hash="4" * 64, terminal_event_id=assistant.id,
            seal_json={"result_content_hash": _sha256([]), "semantic_content_hash": _sha256(response)}))
        await db.flush()
        terminal, = await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session,
            drafts=[SessionEventDraft(item_id=outcome_id, item_kind="run_outcome", lifecycle="terminal_committed",
                scope=scope, actor={"type": "runtime"}, content_hash="6" * 64,
                payload={"outcome_id": str(outcome_id), "terminal_result_id": str(result_id)})])
        binding = await build_web_terminal_boundary_binding(db, tenant_id=tenant, runtime_task_id=owner,
            agent_id=agent, session_id=session, event_kind="turn_stop", terminal_status="completed",
            authority_ref="session_run_outcome", authority_id=outcome_id)
        boundary = await enqueue_terminal_boundary(db, task=task, event_kind="turn_stop", agent_id=agent,
            session_id=session, terminal_status="completed", authority_ref="session_run_outcome",
            authority_id=outcome_id, binding=binding)
        boundary.status, boundary.attempt_count = "dead_letter", 8
        await db.commit()
    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=assistant.id, attempts=1)
    projected = await bus.bridge_transcript_event_to_t0(transcript_event_id=terminal.id, attempts=1)
    async with factory() as db:
        states = list((await db.execute(select(ChatTranscriptEvent.projection_status,
            ChatTranscriptEvent.projection_error).where(ChatTranscriptEvent.session_id == session))).all())
    assert projected, states
    original_segment = ledger._read_index(ledger._session_dir(tmp_path, agent, session))["active_segment_id"]
    async with factory() as db:
        db.add(RuntimeTask(id=incoming, tenant_id=tenant, task_type="trigger", status="running",
            parent_agent_id=agent, parent_session_id=str(session), prompt="next run"))
        await db.flush()
        frontier = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=incoming, actor_type="system", event_type="model_route", content="next run",
            materialize_chat_message=False)
        await db.commit()
    with pytest.raises(ledger.T0SegmentBoundaryPending) as caught:
        ledger.append_t0_session_event(agent_id=agent, session_id=session, runtime_task_id=incoming,
            event_type="model_route", content="next run", data_root=tmp_path)
    async with factory() as db:
        row = await db.get(ChatTranscriptEvent, frontier.event_id)
        row.projection_status, row.projection_attempts = "failed", 8
        row.projection_error = f"T0SegmentBoundaryPending: {caught.value}"
        await db.commit()
    return SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, owner=owner,
        boundary=boundary, terminal=terminal, segment=original_segment, frontier=frontier,
        error=caught.value)


async def test_released_deadletter_redrives_canonical_web_summary_and_t2_once(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.chat_session import ChatSession
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary
    from app.runtime import hooks_setup
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    fx = await _deadletter_frontier(owner_sessionmaker, monkeypatch, tmp_path)
    monkeypatch.setattr(hooks_setup, "_agent_data_root", lambda: tmp_path)
    # Keep the real durable T2 enqueue; leave model execution to its existing
    # worker instead of spending provider quota in this PostgreSQL test.
    monkeypatch.setattr(hooks_setup, "_schedule_t2_segment_package_job", lambda **_kwargs: None)
    summary_calls = []

    async def summary(messages, **kwargs):
        summary_calls.append((messages, kwargs))
        return "Canonical owner summary."

    async def no_hook(*_args, **_kwargs):
        pass

    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 1
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    await service.redrive_dead_letter(tenant_id=fx.tenant, outbox_id=fx.boundary.id,
        actor_user_id=fx.user, reason="Verify canonical recovery replay")
    processor = WebTerminalBoundaryProcessor(session_factory=owner_sessionmaker, data_root=tmp_path,
        summary_generator=summary, emit_advisory_hook=no_hook, bridge_attempts=1)
    item, = await service.claim_batch(tenant_id=fx.tenant, worker_id="r15h-redrive")
    assert await service.process_terminal_boundary(item=item, worker_id="r15h-redrive",
        canonical_validator=processor.validate, process_callback=processor)
    async with owner_sessionmaker() as db:
        row = await db.get(Boundary, item.id)
        assert row.status == "delivered"
        receipt = dict(row.delivery_receipt_json)
        session = await db.get(ChatSession, fx.session)
        assert session.summary == "Canonical owner summary."
        assert session.summary_through_sequence == fx.terminal.sequence
        terminal_t0_id = (await db.get(ChatTranscriptEvent, fx.terminal.id)).metadata_json["t0_bridge_event_id"]
    assert len(summary_calls) == 1
    assert receipt["t0_boundary_id"] == str(item.id)
    assert receipt["response_projection_sha256"]
    events = ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)
    boundary, = [row for row in events if row.event_type == "segment_boundary"]
    assert boundary.segment_id == fx.segment
    assert next(row for row in events if row.event_id == terminal_t0_id).segment_id == fx.segment
    manifests = list(tmp_path.rglob("job_manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["t0_segment_id"] == fx.segment
    # An ack-gap replay reuses the boundary, summary CAS and durable T2 job.
    replay = await processor(item)
    assert replay == receipt
    assert len(summary_calls) == 1
    assert list(tmp_path.rglob("job_manifest.json")) == manifests


async def test_recovery_outbox_lock_orders_against_deadletter_redrive(owner_sessionmaker, monkeypatch, tmp_path):
    from sqlalchemy.exc import DBAPIError
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    fx = await _deadletter_frontier(owner_sessionmaker, monkeypatch, tmp_path)

    @asynccontextmanager
    async def limited_factory():
        async with owner_sessionmaker() as db:
            await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
            yield db

    service = RuntimeTerminalBoundaryOutboxService(session_factory=limited_factory)
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, fx.frontier.event_id)
        assert await bus._seal_terminal_boundary_pending_owner(db, transcript_event=row, boundary_error=fx.error)
        # A task-only lock also blocks redrive, but after redrive has already
        # locked the outbox. Prove the outbox itself is held first.
        async with owner_sessionmaker() as contender:
            with pytest.raises(DBAPIError, match="could not obtain lock"):
                await contender.execute(select(Boundary.id).where(Boundary.id == fx.boundary.id)
                    .with_for_update(nowait=True))
        with pytest.raises(DBAPIError, match="lock timeout"):
            await service.redrive_dead_letter(tenant_id=fx.tenant, outbox_id=fx.boundary.id,
                actor_user_id=fx.user, reason="Concurrent recovery ordering")
        await db.commit()
    await service.redrive_dead_letter(tenant_id=fx.tenant, outbox_id=fx.boundary.id,
        actor_user_id=fx.user, reason="Recovery transaction committed")


async def test_redrive_wins_before_recovery_so_pending_owner_is_not_sealed(owner_sessionmaker, monkeypatch, tmp_path):
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    fx = await _deadletter_frontier(owner_sessionmaker, monkeypatch, tmp_path)
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    await service.redrive_dead_letter(tenant_id=fx.tenant, outbox_id=fx.boundary.id,
        actor_user_id=fx.user, reason="Redrive wins first")
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert ledger._read_index(ledger._session_dir(tmp_path, fx.agent, fx.session))["active_segment_id"] == fx.segment
    async with owner_sessionmaker() as db:
        assert (await db.get(ChatTranscriptEvent, fx.frontier.event_id)).projection_attempts == 8


async def test_deadletter_terminal_behind_frontier_cannot_take_prefix_identity(owner_sessionmaker, monkeypatch, tmp_path):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary

    fx = await _frontier(owner_sessionmaker, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        (await db.get(Boundary, fx.owner_boundary.id)).status = "dead_letter"
        await db.commit()
    states = await _states(owner_sessionmaker, fx)
    for _ in range(2):
        assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert await _states(owner_sessionmaker, fx) == states
    assert [row.content for row in ledger.replay_t0_session_events(
        agent_id=fx.agent, session_id=fx.session, data_root=tmp_path)] == ["owner start"]


async def test_recovery_rechecks_active_segment_after_locked_terminal_read(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    fx = await _deadletter_frontier(owner_sessionmaker, monkeypatch, tmp_path)
    original_get, reopened = AsyncSession.get, []

    async def get_with_checkpoint(db, entity, ident, **kwargs):
        result = await original_get(db, entity, ident, **kwargs)
        if entity is ChatTranscriptEvent and ident == fx.terminal.id and not reopened:
            # Recovery has proved the terminal's segment and locked both rows.
            # Its last awaited read lets an existing checkpoint reopen the tail.
            sealed = ledger.seal_t0_session_segment(agent_id=fx.agent, session_id=fx.session,
                reason="session_idle", data_root=tmp_path)
            assert sealed.segment_id == fx.segment
            reopened.append(ledger.append_t0_session_event(agent_id=fx.agent, session_id=fx.session,
                runtime_task_id=fx.owner, event_type="tool_result", content="late owner result",
                metadata={"turn_id": result.turn_id}, data_root=tmp_path).segment_id)
        return result

    monkeypatch.setattr(AsyncSession, "get", get_with_checkpoint)
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert len(reopened) == 1 and reopened[0] != fx.segment
    index = ledger._read_index(ledger._session_dir(tmp_path, fx.agent, fx.session))
    assert all(segment.get("boundary_id") != str(fx.boundary.id) for segment in index["segments"])
    assert index["active_segment_id"] == reopened[0]
    assert (await _states(owner_sessionmaker, fx))[-1][1] == 9
    # One more append refreshes the old diagnostic to the newly opened segment;
    # once caught up, unchanged polls take the existing attempt-free wait.
    assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    states = await _states(owner_sessionmaker, fx)
    assert states[-1][0] == "failed" and states[-1][1] == 10
    for _ in range(2):
        assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert await _states(owner_sessionmaker, fx) == states
    assert ledger.get_t0_active_segment_id(agent_id=fx.agent, session_id=fx.session) == reopened[0]
    assert all(row.metadata.get("boundary_id") != str(fx.boundary.id)
        for row in ledger.replay_t0_session_events(agent_id=fx.agent, session_id=fx.session, data_root=tmp_path))


async def test_invalid_deadletter_terminal_uuid_stays_unselected_without_sweep_error(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary

    fx = await _deadletter_frontier(owner_sessionmaker, monkeypatch, tmp_path)
    async with owner_sessionmaker() as db:
        boundary = await db.get(Boundary, fx.boundary.id)
        boundary.binding_json = {**boundary.binding_json, "terminal_event_id": "not-a-uuid"}
        await db.commit()
    states = await _states(owner_sessionmaker, fx)
    monkeypatch.setitem(bus._STATE, "last_error", None)
    for _ in range(2):
        assert await bus.sweep_pending_transcript_t0_bridges(session_id=fx.session) == 0
    assert bus._STATE["last_error"] is None
    assert await _states(owner_sessionmaker, fx) == states
    assert ledger.get_t0_active_segment_id(agent_id=fx.agent, session_id=fx.session) == fx.segment
