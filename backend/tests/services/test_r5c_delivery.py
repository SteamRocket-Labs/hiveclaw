"""Member delivery regressions on the existing database, relay and API paths."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import event, func, select, update

from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_long_session_delivery_has_constant_work_and_batch_transactions(owner_sessionmaker, monkeypatch):
    import app.database as database
    from app.memory.t0 import ledger
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event
    from app.services.runtime_control_bus import bridge_transcript_event_to_t0, sweep_pending_transcript_t0_bridges
    from app.services.session_event_outbox import SessionEventOutboxPublisher

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    # A global sweeper must not make this session's cost depend on other tests.
    foreign_tenant, foreign_user, foreign_agent, foreign_session, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await append_session_event(db=db, tenant_id=foreign_tenant, agent_id=foreign_agent,
            session_id=foreign_session, actor_type="user", event_type="user_message", role="user",
            user_id=foreign_user, content="Unrelated pending projection", materialize_chat_message=False)
        await db.commit()
    samples = []
    for history_length in (2000, 4000):
        tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
        prior = []
        for sequence in range(1, history_length + 1):
            identity = uuid.uuid4()
            ledger.append_t0_session_event(agent_id=agent_id, session_id=session_id, event_type="user_message",
                role="user", content="Prior conversation", tenant_id=tenant_id,
                metadata={"transcript_event_id": str(identity), "transcript_sequence": sequence})
            prior.append(ChatTranscriptEvent(id=identity, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
                sequence=sequence, event_type="user_message", actor_type="user", content="Prior conversation",
                projection_status="projected", metadata_json={"t0_bridge_relayed_at": "fixture"}))
        async with owner_sessionmaker() as db:
            db.add_all(prior)
            await db.commit()
        identities = []
        async with owner_sessionmaker() as db:
            for _ in range(16):
                result = await append_session_event(db=db, tenant_id=tenant_id, agent_id=agent_id,
                    session_id=session_id, actor_type="user", event_type="user_message", role="user",
                    user_id=user_id, content='Add "Next steps".', materialize_chat_message=False)
                identities.append(result.event_id)
            await db.commit()
        counts = {"statements": 0, "commits": 0, "parsed_records": 0}
        def sql(_connection, _cursor, _statement, parameters, _context, executemany):
            counts["statements"] += len(parameters) if executemany else 1
        def commit(*_args):
            counts["commits"] += 1
        original = ledger._event_from_record
        def parse(**kwargs):
            counts["parsed_records"] += 1
            return original(**kwargs)
        monkeypatch.setattr(ledger, "_event_from_record", parse)
        engine = owner_sessionmaker.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", sql)
        event.listen(engine, "commit", commit)
        try:
            start = time.perf_counter()
            assert await bridge_transcript_event_to_t0(transcript_event_id=identities[0], attempts=1)
            bridge = {**counts, "seconds": time.perf_counter() - start}
            counts.update(statements=0, commits=0, parsed_records=0)
            start = time.perf_counter()
            projected = await sweep_pending_transcript_t0_bridges(limit=100, session_id=session_id)
            sweep = {**counts, "seconds": time.perf_counter() - start, "projected": projected}
            counts.update(statements=0, commits=0, parsed_records=0)
            publisher = SessionEventOutboxPublisher(session_factory=owner_sessionmaker)
            delivered = []
            async def publish(payload):
                delivered.append(payload["sequence"])
            start = time.perf_counter()
            outcome = await publisher.drain_once(worker_id="r5c-benchmark", publish_callback=publish, tenant_id=tenant_id)
            outbox = {**counts, "seconds": time.perf_counter() - start, **outcome}
            assert delivered == sorted(delivered)
        finally:
            event.remove(engine, "before_cursor_execute", sql)
            event.remove(engine, "commit", commit)
            monkeypatch.setattr(ledger, "_event_from_record", original)
        samples.append({"history": history_length, "bridge": bridge, "sweep": sweep, "outbox": outbox})
        async with owner_sessionmaker() as db:
            assert await db.scalar(select(ChatTranscriptEvent.projection_status).where(
                ChatTranscriptEvent.session_id == foreign_session)) == "pending"
            await db.execute(update(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
                .values(projection_status="projected"))
            await db.commit()
    print("R5C_BENCHMARK " + json.dumps(samples, sort_keys=True))
    for sample in samples:
        assert sample["bridge"]["parsed_records"] <= 2
        assert sample["bridge"]["statements"] <= 12
        assert sample["sweep"]["projected"] == 15
        assert sample["sweep"]["commits"] <= 3
        assert sample["sweep"]["statements"] <= 2 * 15 + 8
        assert sample["outbox"]["published"] == 16
        assert sample["outbox"]["commits"] <= 2
        assert sample["outbox"]["statements"] <= 8
    assert samples[0]["bridge"]["statements"] == samples[1]["bridge"]["statements"]


async def test_cancel_reaches_inflight_model_while_transcript_projection_is_slow(monkeypatch):
    from app.kernel.engine import _KernelCancelledError, _stream_with_cancel
    from app.services import runtime_control_bus as bus, web_chat_runtime as runtime
    run_id = uuid.uuid4().hex
    cancel = asyncio.Event()
    entered = asyncio.Event()
    closed = asyncio.Event()
    runtime.register_web_chat_run_for_test(run_id, cancel_event=cancel)
    async def slow_bridge(**_kwargs):
        await asyncio.sleep(10)
        return True
    class Model:
        async def stream(self):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()
    class Pubsub:
        async def subscribe(self, _channel):
            pass
        async def listen(self):
            await entered.wait()
            for payload in (
                {"schema": bus.RUNTIME_CONTROL_SCHEMA, "type": "transcript_t0_bridge", "transcript_event_id": str(uuid.uuid4())},
                {"schema": bus.RUNTIME_CONTROL_SCHEMA, "type": "web_chat_cancel", "run_id": run_id},
            ):
                yield {"type": "message", "data": json.dumps(payload)}
    async def redis():
        return SimpleNamespace(pubsub=Pubsub)
    monkeypatch.setattr(bus, "get_redis", redis)
    monkeypatch.setattr(bus, "bridge_transcript_event_to_t0", slow_bridge)
    model = asyncio.create_task(_stream_with_cancel(Model(), cancel_event=cancel))
    listener = asyncio.create_task(bus._listen_runtime_control_once())
    start = time.perf_counter()
    try:
        with pytest.raises(_KernelCancelledError):
            await asyncio.wait_for(model, timeout=4.8)
        assert closed.is_set()
        assert time.perf_counter() - start < 5
    finally:
        listener.cancel()
        model.cancel()
        await asyncio.gather(listener, model, return_exceptions=True)
        runtime._CANCEL_EVENTS.pop(run_id, None)


@pytest.mark.parametrize("resource", ["index", "workbench"])
async def test_peer_child_subresources_share_transcript_read_authority(owner_sessionmaker, resource):
    from app.api import chat_sessions as api
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    tenant_id, user_id, parent_id, _, _ = await _seed_session(owner_sessionmaker)
    child_id, session_id, denied_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=child_id, tenant_id=tenant_id, name="Delegated employee", creator_id=user_id))
        db.add(User(id=denied_id, username=f"denied-{denied_id.hex[:8]}", email=f"{denied_id}@test.invalid",
            tenant_id=tenant_id, password_hash="x", display_name="Other member", role="member"))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=child_id, peer_agent_id=parent_id, tenant_id=tenant_id,
            user_id=user_id, source_channel="agent", session_kind="delegation_run", actor_type="agent"))
        await db.commit()
    handlers = {"index": api.get_session_index, "context-usage": api.get_session_context_usage,
        "decisions": api.list_decisions_for_session, "workbench": api.get_session_workbench,
        "lineage": api.get_session_lineage}
    async with owner_sessionmaker() as db:
        allowed = await db.get(User, user_id)
        denied = await db.get(User, denied_id)
        kwargs = {"agent_id": parent_id, "session_id": session_id, "db": db,
            "operator_view": False, "operator_reason": None}
        if resource == "decisions":
            kwargs["limit"] = 50
        assert await api.get_session_transcript(**kwargs, current_user=allowed, schema_version=2) == []
        result = await handlers[resource](**kwargs, current_user=allowed)
        assert result is not None
        if resource == "workbench":
            assert result["agent_id"] == str(child_id)
        with pytest.raises(HTTPException) as rejected:
            await handlers[resource](**kwargs, current_user=denied)
        assert rejected.value.status_code in {403, 404}


async def test_saved_continuation_projects_exact_quotes_after_reload(owner_sessionmaker):
    from app.api import chat_sessions as api
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.user import User
    from app.services.session_user_checkpoint import user_checkpoint_content
    from app.services.session_v2_persistence import accept_human_input
    from tests.services.test_session_input_control_v2 import _authority
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    text = 'Add "Next steps" and preserve C:\\reports\\draft.md.'
    parts = [{"type": "text", "text": text, "display_content": text},
        {"type": "workspace_file_ref", "agent_id": str(agent_id), "path": "workspace/report.md"}]
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        await accept_human_input(db, authority=authority, intent={"kind": "start_turn", "session_id": str(session_id),
            "input_id": str(input_id), "idempotency_key": f"r5c-quotes:{input_id}", "content_parts": parts})
        await db.commit()
    async with owner_sessionmaker() as db:
        saved = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.item_id == input_id, ChatTranscriptEvent.lifecycle == "accepted"))
        assert json.loads(user_checkpoint_content(saved)) == parts
        user = await db.get(User, user_id)
        index = await api.get_session_index(agent_id=agent_id, session_id=session_id, current_user=user,
            db=db, operator_view=False, operator_reason=None)
        assert index["checkpoints"][0]["content_preview"] == text
        assert user_checkpoint_content(saved, display=True) == text


async def test_legacy_continuation_delivery_uses_display_text_instead_of_runtime_json(owner_sessionmaker):
    from app.api import chat_sessions as api
    from app.models.user import User
    from app.services.chat_transcript import append_session_event
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    text = 'Add "Next steps".'
    async with owner_sessionmaker() as db:
        await append_session_event(db=db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            actor_type="user", event_type="user_message", content=json.dumps([{"type": "text", "text": text}]),
            metadata={"display_content": text}, user_id=user_id, role="user", bridge_to_t0=False)
        await db.commit()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        events = await api.get_session_transcript(agent_id=agent_id, session_id=session_id, current_user=user,
            db=db, operator_view=False, operator_reason=None, schema_version=2)
        assert events[0]["payload"]["content"] == text


async def test_projection_batch_and_live_append_do_not_deadlock(owner_sessionmaker, monkeypatch):
    import app.database as database
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import runtime_control_bus as bus
    from app.services.chat_transcript import append_session_event
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await db.execute(update(ChatTranscriptEvent).where(ChatTranscriptEvent.projection_status.in_(("pending", "failed", "projecting")))
            .values(projection_status="projected"))
        await db.commit()
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async def append(db, content):
        return await append_session_event(db=db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
            actor_type="user", event_type="user_message", user_id=user_id, role="user", content=content,
            materialize_chat_message=False)
    async with owner_sessionmaker() as db:
        first = await append(db, "First input")
        await db.commit()
    writer_locked, projection_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def pause_projection(_db, *, transcript_event):
        if transcript_event.id == first.event_id:
            projection_entered.set()
            await release.wait()
        return False
    monkeypatch.setattr(bus, "_is_settled_question_followup", pause_projection)
    async def write():
        async with owner_sessionmaker() as db:
            key = int.from_bytes(session_id.bytes[:8], byteorder="big", signed=True)
            await db.execute(select(func.pg_advisory_xact_lock(key)))
            writer_locked.set()
            await projection_entered.wait()
            release.set()
            await append(db, "Concurrent live input")
            await db.commit()
    writer = asyncio.create_task(write())
    await writer_locked.wait()
    projection = asyncio.create_task(bus.sweep_pending_transcript_t0_bridges())
    try:
        await asyncio.wait_for(asyncio.gather(writer, projection), timeout=4.8)
    finally:
        release.set()
        writer.cancel()
        projection.cancel()
        await asyncio.gather(writer, projection, return_exceptions=True)
    async with owner_sessionmaker() as db:
        assert (await db.get(ChatTranscriptEvent, first.event_id)).projection_status == "projected"
        assert await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == session_id)) == 2
