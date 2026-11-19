"""Exercise post-commit wakes with the real session-event worker and outbox."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.models.session_v2 import SessionEventOutbox
from app.services import runtime_task_worker as worker
from app.services import session_event_outbox
from app.services.chat_transcript import append_session_event
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


async def _append(db, seed):
    row = await append_session_event(
        db=db,
        tenant_id=seed.tenant_id,
        agent_id=seed.agent_id,
        user_id=seed.user_id,
        session_id=seed.session_id,
        actor_type="user",
        event_type="user_message",
        role="user",
        content="session event wake",
        materialize_chat_message=False,
        bridge_to_t0=False,
    )
    return str(row.event_id)


@asynccontextmanager
async def _run_event_lane(monkeypatch, factory, tenant_id, publish, *, poll=5.0):
    idle = asyncio.Event()
    drains = asyncio.Queue()
    discover = worker._has_due_runtime_work
    drain = worker.drain_session_event_outbox_once
    lanes = worker._terminal_boundary_lanes
    candidates = session_event_outbox.session_event_claim_candidate_statement

    async def observed_discovery(**kwargs):
        result = await discover(**kwargs)
        if not result["session-events"]:
            idle.set()
        return result

    async def observed_drain(**kwargs):
        result = await drain(publish_callback=publish, session_factory=factory, **kwargs)
        drains.put_nowait(result)
        return result

    monkeypatch.setattr(worker, "_STATE", {})
    monkeypatch.setattr(worker._settings(), "RUNTIME_TASK_CLAIM_POLL_SECONDS", poll)
    monkeypatch.setattr(worker, "_has_due_runtime_work", observed_discovery)
    monkeypatch.setattr(worker, "drain_session_event_outbox_once", observed_drain)
    monkeypatch.setattr(worker, "_terminal_boundary_lanes", lambda name: {"session-events": lanes(name)["session-events"]})
    monkeypatch.setattr(
        session_event_outbox,
        "session_event_claim_candidate_statement",
        lambda **kwargs: candidates(**kwargs).where(SessionEventOutbox.tenant_id == tenant_id),
    )
    task = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="a2fp6"))
    try:
        yield idle, drains
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_committed_outbox_row_wakes_idle_lane(monkeypatch, owner_sessionmaker):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Session event wake")
    published = asyncio.Queue()

    async def publish(payload):
        published.put_nowait(payload["event_id"])

    async with _run_event_lane(monkeypatch, owner_sessionmaker, seed.tenant_id, publish) as (idle, drains):
        await asyncio.wait_for(idle.wait(), 1)
        async with owner_sessionmaker() as db:
            async with db.begin_nested():
                event_id = await _append(db, seed)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(drains.get(), 0.05)
            assert published.empty()
            await db.commit()
        assert await asyncio.wait_for(published.get(), 0.5) == event_id
        assert (await asyncio.wait_for(drains.get(), 0.5))["published"] == 1


async def test_commits_during_session_event_drain_share_one_follow_up(monkeypatch, owner_sessionmaker):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Session event follow-up")
    entered, release = asyncio.Event(), asyncio.Event()
    published = []
    async with owner_sessionmaker() as db:
        first = await _append(db, seed)
        await db.commit()

    async def publish(payload):
        published.append(payload["event_id"])
        if payload["event_id"] == first:
            entered.set()
            await release.wait()

    async with _run_event_lane(monkeypatch, owner_sessionmaker, seed.tenant_id, publish) as (_, drains):
        await asyncio.wait_for(entered.wait(), 1)
        following = []
        for _ in range(3):
            async with owner_sessionmaker() as db:
                following.append(await _append(db, seed))
                await db.commit()
        release.set()
        assert (await asyncio.wait_for(drains.get(), 0.5))["published"] == 1
        assert (await asyncio.wait_for(drains.get(), 0.5))["published"] == len(following)
        assert published == [first, *following]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(drains.get(), 0.05)


async def test_rolled_back_outbox_rows_do_not_wake_lane(monkeypatch, owner_sessionmaker):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Session event rollback")
    published = asyncio.Queue()

    async def publish(payload):
        published.put_nowait(payload["event_id"])

    async with _run_event_lane(monkeypatch, owner_sessionmaker, seed.tenant_id, publish) as (idle, drains):
        await asyncio.wait_for(idle.wait(), 1)
        async with owner_sessionmaker() as db:
            savepoint = await db.begin_nested()
            await _append(db, seed)
            await db.flush()
            await savepoint.rollback()
            await db.commit()

            await _append(db, seed)
            await db.flush()
            await db.close()
            await db.commit()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(drains.get(), 0.05)
        assert published.empty()
