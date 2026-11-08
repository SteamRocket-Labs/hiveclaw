"""Review regressions on real PostgreSQL handlers and the durable T0 bridge."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _append(factory, seed, count=1):
    from app.services.chat_transcript import append_session_event
    tenant, user, agent, session, _ = seed
    async with factory() as db:
        ids = [(await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
            actor_type="user", event_type="user_message", role="user", user_id=user,
            content=f"Input {i}", materialize_chat_message=False)).event_id for i in range(count)]
        await db.commit()
    return ids


@pytest.mark.parametrize("resource", ["lineage", "context-usage", "decisions"])
async def test_child_resources_do_not_expand_transcript_authority(owner_sessionmaker, resource):
    from app.api import chat_sessions as api
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    tenant, owner, parent, root, _ = await _seed_session(owner_sessionmaker)
    other, creator, child, own, sibling = [uuid.uuid4() for _ in range(5)]
    async with owner_sessionmaker() as db:
        for uid in (other, creator):
            db.add(User(id=uid, tenant_id=tenant, username=f"r5c-{uid}", email=f"{uid}@test.invalid",
                password_hash="x", display_name="Other member", role="member"))
        await db.flush()
        db.add(Agent(id=child, tenant_id=tenant, creator_id=creator, name="Private child"))
        await db.flush()
        for sid, uid, title in ((own, owner, "My delegation"), (sibling, other, "PRIVATE sibling instruction")):
            db.add(ChatSession(id=sid, agent_id=child, peer_agent_id=parent, tenant_id=tenant,
                user_id=uid, parent_session_id=root, root_session_id=root, title=title,
                source_channel="agent", session_kind="delegation_run", actor_type="agent",
                transcript_metadata_json={"permission_profile": {"requester": str(uid)},
                    "runtime_assembly_state": {"schema": "hive.ccplus.runtime_assembly_state.v1",
                        "prompt_assembly_manifest": {"context_candidates": [{"id": "ctx:messages:conversation",
                            "selected": True, "payload": [{"role": "user", "content": "PRIVATE model input"}]}]}}}))
        await db.commit()
    async with owner_sessionmaker() as db:
        user = await db.get(User, owner)
        kwargs = dict(agent_id=parent, session_id=own, db=db, current_user=user,
            operator_view=False, operator_reason=None)
        assert await api.get_session_transcript(**kwargs, schema_version=2) == []
        assert await api.get_session_index(**kwargs)
        assert (await api.get_session_workbench(**kwargs))["agent_id"] == str(child)
        handlers = {"lineage": api.get_session_lineage, "context-usage": api.get_session_context_usage,
            "decisions": api.list_decisions_for_session}
        with pytest.raises(HTTPException) as error:
            await handlers[resource](**kwargs, **({"limit": 50} if resource == "decisions" else {}))
        assert error.value.status_code == 404
        for sid, aid in ((sibling, parent), (own, child)):
            with pytest.raises(HTTPException) as denied:
                await api.get_session_transcript(**{**kwargs, "agent_id": aid, "session_id": sid}, schema_version=2)
            assert denied.value.status_code in {403, 404}


async def test_retired_history_reader_cannot_read_decisions(owner_sessionmaker):
    from app.api import chat_sessions as api
    from app.models.agent import Agent
    from app.models.user import User
    tenant, owner, agent, session, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        bot = await db.get(Agent, agent)
        bot.deactivated_at = datetime.now(timezone.utc)
        bot.deactivation_reason = "retirement_pending"
        await db.commit()
    async with owner_sessionmaker() as db:
        kwargs = dict(agent_id=agent, session_id=session, db=db, current_user=await db.get(User, owner),
            operator_view=False, operator_reason=None)
        assert await api.get_session_index(**kwargs)
        with pytest.raises(HTTPException) as error:
            await api.list_decisions_for_session(**kwargs, limit=50)
        assert error.value.status_code == 410


async def test_capped_frontier_recovers_on_periodic_sweep(owner_sessionmaker, monkeypatch):
    import app.database as database
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import runtime_control_bus as bus
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    seed = await _seed_session(owner_sessionmaker)
    ids = await _append(owner_sessionmaker, seed, 2)
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, ids[0])
        row.projection_status, row.projection_attempts = "failed", 20
        await db.commit()
    await bus.sweep_pending_transcript_t0_bridges()
    async with owner_sessionmaker() as db:
        rows = list((await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.id.in_(ids))
            .order_by(ChatTranscriptEvent.sequence))).all())
        assert [(row.projection_status, row.projection_attempts) for row in rows] == [("projected", 21), ("projected", 1)]


async def test_live_wakes_do_not_burn_transient_failure_attempts(owner_sessionmaker, monkeypatch):
    import app.database as database
    from app.memory.t0 import ledger
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import runtime_control_bus as bus
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    seed = await _seed_session(owner_sessionmaker)
    ids = await _append(owner_sessionmaker, seed, 30)
    blocked = True
    original = ledger.append_t0_session_event
    def append(**kwargs):
        if blocked:
            raise ledger.T0LedgerIntegrityError("transient_segment_pending")
        return original(**kwargs)
    monkeypatch.setattr(ledger, "append_t0_session_event", append)
    class Pubsub:
        async def subscribe(self, _channel): pass
        async def listen(self):
            for identity in ids:
                await asyncio.sleep(0.01)
                yield {"type": "message", "data": json.dumps({"schema": bus.RUNTIME_CONTROL_SCHEMA,
                    "type": "transcript_t0_bridge", "transcript_event_id": str(identity)})}
    async def redis(): return SimpleNamespace(pubsub=Pubsub)
    monkeypatch.setattr(bus, "get_redis", redis)
    sweeper = asyncio.create_task(bus._run_transcript_projection_sweeper(interval_seconds=1))
    try:
        await bus._listen_runtime_control_once()
        async with owner_sessionmaker() as db:
            frontier = await db.get(ChatTranscriptEvent, ids[0])
            assert frontier.projection_attempts < 5
        blocked = False
        # Three periodic sweeps, as in the reviewer's probe.
        await asyncio.sleep(3.1)
        async with owner_sessionmaker() as db:
            rows = list((await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.id.in_(ids)))).all())
            assert len(rows) == 30
            assert all(row.projection_status == "projected" for row in rows)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)


async def test_poisoned_ledger_does_not_block_healthy_session(owner_sessionmaker, monkeypatch):
    import app.database as database
    from app.memory.t0 import ledger
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import runtime_control_bus as bus
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    broken = await _seed_session(owner_sessionmaker)
    healthy = await _seed_session(owner_sessionmaker)
    first = (await _append(owner_sessionmaker, broken))[0]
    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=first, attempts=1)
    directory = ledger._session_dir(None, broken[2], broken[3])
    segment = ledger._read_index(directory)["segments"][-1]
    with (directory / ledger._segment_events_path(segment)).open("ab") as stream:
        stream.write(b'{"partial":')
    bad = (await _append(owner_sessionmaker, broken))[0]
    for identity in await _append(owner_sessionmaker, healthy, 3):
        assert await bus.bridge_transcript_event_to_t0(transcript_event_id=identity, attempts=1)
    good = (await _append(owner_sessionmaker, healthy))[0]
    await bus.sweep_pending_transcript_t0_bridges()
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, bad)
        assert row.projection_status == "failed"
        assert row.projection_attempts >= 1
        assert "t0_record_truncated" in row.projection_error
        assert (await db.get(ChatTranscriptEvent, good)).projection_status == "projected"


def test_projection_wake_is_owned_by_each_sweeper_loop(monkeypatch):
    from app.services import runtime_control_bus as bus
    async def exercise():
        swept = asyncio.Event()
        calls = 0
        async def sweep(**_kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2: swept.set()
            return 0
        monkeypatch.setattr(bus, "sweep_pending_transcript_t0_bridges", sweep)
        task = asyncio.create_task(bus._run_transcript_projection_sweeper(interval_seconds=0.1))
        try:
            await asyncio.wait_for(swept.wait(), timeout=1)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    asyncio.run(exercise())


@pytest.mark.parametrize("tail_sequence", [1, True])
def test_finder_recovers_an_earlier_written_higher_transcript_sequence(tmp_path, tail_sequence):
    from app.memory.t0 import ledger
    agent, session, target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for identity, sequence in ((target, 2), (uuid.uuid4(), tail_sequence)):
        ledger.append_t0_session_event(agent_id=agent, session_id=session, event_type="user_message",
            role="user", content="Input", data_root=tmp_path,
            metadata={"transcript_event_id": str(identity), "transcript_sequence": sequence})
    found = ledger.find_t0_transcript_event(agent_id=agent, session_id=session, transcript_event_id=target,
        transcript_sequence=2, data_root=tmp_path)
    assert found is not None
    assert found.metadata["transcript_event_id"] == str(target)


@pytest.mark.parametrize("display", [None, 17, "missing"])
async def test_index_and_workbench_do_not_publish_raw_file_prompt(owner_sessionmaker, display):
    from app.api import chat_sessions as api
    from app.models.user import User
    from app.services.session_v2_persistence import accept_human_input
    from tests.services.test_session_input_control_v2 import _authority
    tenant, owner, agent, session, _ = await _seed_session(owner_sessionmaker)
    text = {"type": "text", "text": "INTERNAL PROMPT: private file body"}
    if display != "missing": text["display_content"] = display
    identity = uuid.uuid4()
    async with owner_sessionmaker() as db:
        await accept_human_input(db, authority=await _authority(db, user_id=owner, agent_id=agent, session_id=session),
            intent={"kind": "start_turn", "session_id": str(session), "input_id": str(identity),
                "idempotency_key": f"file:{identity}", "content_parts": [text,
                    {"type": "workspace_file_ref", "agent_id": str(agent), "path": "workspace/report.md"}]})
        await db.commit()
    async with owner_sessionmaker() as db:
        kwargs = dict(agent_id=agent, session_id=session, db=db, current_user=await db.get(User, owner),
            operator_view=False, operator_reason=None)
        index = await api.get_session_index(**kwargs)
        assert index["checkpoints"][0]["content_preview"] == ""
        workbench = await api.get_session_workbench(**kwargs)
        assert workbench is not None


async def test_legacy_operator_and_outbox_keep_exact_input(owner_sessionmaker):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionEventOutbox
    from app.services.chat_transcript import append_session_event
    from app.services.session_event_contract import serialize_session_event
    tenant, owner, agent, session, _ = await _seed_session(owner_sessionmaker)
    raw = json.dumps([{"type": "text", "text": "Model input"}])
    async with owner_sessionmaker() as db:
        result = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
            actor_type="user", event_type="user_message", role="user", user_id=owner,
            content=raw, metadata={"display_content": "Member input"}, bridge_to_t0=False)
        await db.commit()
    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, result.event_id)
        assert serialize_session_event(row, audience="user")["payload"]["content"] == "Member input"
        assert serialize_session_event(row, audience="operator")["payload"]["content"] == raw
        outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == result.event_id))
        assert outbox.envelope_json["payload"]["content"] == raw
