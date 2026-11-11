"""Committed terminal owners must release ordered T0 projection frontiers."""

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, select

from app.memory.t0 import ledger
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services import runtime_control_bus as bus
from app.services.chat_transcript import append_session_event
from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _commit_boundary(db, task, agent, session, terminal):
    return await enqueue_terminal_boundary(
        db, task=task, event_kind="turn_stop" if task.status == "completed" else "turn_abort",
        agent_id=agent, session_id=session, terminal_status=task.status,
        authority_ref="runtime_task", authority_id=task.id,
        binding={"terminal_event_id": terminal.event_id, "terminal_sequence": terminal.sequence},
    )


async def _frontier(factory, monkeypatch, tmp_path, *, owner_type="web_chat_turn",
                    incoming_type="web_chat_turn", owner_status="completed", incoming_status="completed"):
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
        for run, actor, kind, content in (
            (incoming, "system", "model_route", "incoming start"),
            (incoming, "assistant", "assistant_message", "incoming final"),
            (owner, "assistant", "assistant_message", "owner final"),
        ):
            rows.append(await append_session_event(db=db, tenant_id=tenant, agent_id=agent,
                session_id=session, run_id=run, actor_type=actor, event_type=kind, content=content,
                materialize_chat_message=False))
        owner_task, incoming_task = await db.get(RuntimeTask, owner), await db.get(RuntimeTask, incoming)
        owner_task.status, incoming_task.status = owner_status, incoming_status
        owner_task.completed_at = datetime.now(timezone.utc) if owner_status != "running" else None
        incoming_task.completed_at = datetime.now(timezone.utc)
        owner_boundary = None
        if owner_status != "running":
            owner_boundary = await _commit_boundary(db, owner_task, agent, session, rows[-1])
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
    return SimpleNamespace(tenant=tenant, agent=agent, session=session, owner=owner, incoming=incoming,
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


async def test_unchanged_blocked_frontier_only_selects_candidates_until_owner_commits(
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
    assert len(statements) == 2, statements
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
