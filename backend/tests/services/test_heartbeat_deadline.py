"""A heartbeat settles before its Redis lease can admit another run."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text

from tests.services.test_session_terminal_outcome_v2 import _seed


@pytest.fixture
async def heartbeat_run(owner_sessionmaker, monkeypatch):
    from app import database
    from app.memory.t2.job_sweep import T2JobSweepReport
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.services import daemon_concurrency, heartbeat, runtime_task_service
    from app.services.heartbeat_t3_core import HeartbeatT3CoreResult

    tenant_id, agent_id, _, _, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        model = LLMModel(
            tenant_id=tenant_id,
            provider="openai",
            model="local-fixture",
            label="Heartbeat fixture",
            api_key_encrypted="unused",
        )
        db.add(model)
        await db.flush()
        agent = await db.get(Agent, agent_id)
        agent.primary_model_id = model.id
        agent.status = "idle"
        await db.commit()

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    redis = Redis.from_url(os.environ["REDIS_URL"])
    state = SimpleNamespace(
        agent_id=agent_id,
        tenant_id=tenant_id,
        task_ids=[],
        tasks=[],
        sessions=0,
        entered=asyncio.Event(),
        core_calls=0,
        phase=None,
    )
    original_session = heartbeat.tenant_scoped_session
    original_acquire = heartbeat._try_acquire_heartbeat_lease_async
    original_create_task = asyncio.create_task
    monkeypatch.setattr(daemon_concurrency, "_semaphores", {})
    state.semaphore = daemon_concurrency.get_daemon_semaphore("heartbeat")
    starting_slots = state.semaphore._value

    @asynccontextmanager
    async def tracked_session(*args, **kwargs):
        async with original_session(*args, **kwargs) as db:
            state.sessions += 1
            try:
                yield db
            finally:
                state.sessions -= 1

    async def get_redis():
        return redis

    async def acquire(agent_id, *, now=None):
        return await original_acquire(agent_id, now=now, ttl_seconds=1)

    async def create_task(agent_id, *, tenant_id=None):
        task_id = uuid4().hex
        await heartbeat.create_runtime_task_record(
            task_id=task_id,
            task_type="heartbeat",
            status="running",
            parent_agent_id=agent_id,
        )
        state.task_ids.append(task_id)
        return task_id

    def track_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        state.tasks.append(task)
        return task

    async def block(phase):
        if state.phase == phase:
            state.entered.set()
            await asyncio.Event().wait()

    async def sweep(*args, **kwargs):
        await block("t2")
        return T2JobSweepReport(agent_id=str(agent_id))

    async def core(**kwargs):
        assert state.semaphore._value == starting_slots - 1, "T3 must hold a heartbeat concurrency slot"
        state.core_calls += 1
        await block("t3")
        return HeartbeatT3CoreResult(
            status="completed",
            outcome_type="action",
            score=5,
            summary="Fixture consolidation completed.",
        )

    async def growth(*args, **kwargs):
        assert state.semaphore._value == starting_slots - 1, "persistence must hold a heartbeat concurrency slot"
        await block("persistence")

    async def noop(*args, **kwargs):
        return SimpleNamespace(stalled=False, archived=[], dirty_files=[])

    async def artifact_gc(*args, **kwargs):
        return {}

    async def records():
        async with owner_sessionmaker() as db:
            tasks = list(
                await db.scalars(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.parent_agent_id == agent_id,
                        RuntimeTask.task_type == "heartbeat",
                    )
                    .order_by(RuntimeTask.created_at)
                )
            )
            agent = await db.get(Agent, agent_id)
            idle_transactions = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction'"
                )
            )
        return tasks, agent, idle_transactions

    state.records = records
    state.redis = redis
    monkeypatch.setattr(heartbeat, "tenant_scoped_session", tracked_session)
    monkeypatch.setattr(heartbeat, "get_redis", get_redis)
    monkeypatch.setattr(heartbeat, "_try_acquire_heartbeat_lease_async", acquire)
    monkeypatch.setattr(heartbeat, "_create_heartbeat_runtime_task", create_task)
    monkeypatch.setattr(heartbeat.asyncio, "create_task", track_create_task)
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_TIMEOUT_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(heartbeat, "_run_memory_lifecycle_maintenance", lambda *args: {})
    monkeypatch.setattr(heartbeat, "_run_t2_job_sweep", sweep)
    monkeypatch.setattr(heartbeat, "run_heartbeat_t3_core", core)
    monkeypatch.setattr(heartbeat, "_run_consolidation_debt_refresh", noop)
    monkeypatch.setattr(heartbeat, "_run_t2_retention", noop)
    monkeypatch.setattr(heartbeat, "_run_chat_artifact_snapshot_retention", artifact_gc)
    monkeypatch.setattr(heartbeat, "_run_convergence_dirtiness_refresh", noop)
    monkeypatch.setattr(heartbeat, "_run_growth_report_refresh", growth)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", noop)
    monkeypatch.setattr("app.services.activity_logger.log_activity", noop)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", noop)
    try:
        yield state
    finally:
        for task in state.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)
        await heartbeat._release_heartbeat_lease_async(agent_id)
        heartbeat._heartbeat_tick_counts.pop(agent_id, None)
        await redis.aclose()


async def test_deadline_prevents_expired_lease_overlap(heartbeat_run):
    from app.services import heartbeat

    state = heartbeat_run
    state.phase = "t2"
    await heartbeat._heartbeat_tick()
    await asyncio.wait_for(state.entered.wait(), timeout=1)
    tasks, _, _ = await state.records()
    assert [task.status for task in tasks] == ["running"]

    await heartbeat._heartbeat_tick()
    assert len(state.task_ids) == 1, "an in-flight lease must reject another tick"
    await asyncio.sleep(1.1)
    await heartbeat._heartbeat_tick()
    # Let a newly admitted run reach the same blocked sweep on the baseline.
    await asyncio.sleep(0.1)
    tasks, agent, idle_transactions = await state.records()
    assert len(tasks) == 1, "an expired lease must never overlap a previous heartbeat"
    assert tasks[0].status == "failed"
    assert "TimeoutError" in tasks[0].result_summary
    assert agent.last_heartbeat_at is not None
    assert state.sessions == idle_transactions == 0
    assert not await state.redis.exists(f"heartbeat_lease:{state.agent_id}")
    assert state.core_calls == 0


@pytest.mark.parametrize("phase", ["t2", "t3", "persistence"])
async def test_deadline_settles_slow_run_as_failed(heartbeat_run, phase):
    from app.services import heartbeat

    state = heartbeat_run
    state.phase = phase
    task = asyncio.create_task(heartbeat._execute_heartbeat(state.agent_id, tenant_id=state.tenant_id))
    await asyncio.wait_for(state.entered.wait(), timeout=1)
    done, _ = await asyncio.wait([task], timeout=0.65)
    assert task in done, f"{phase} must end before the one-second lease expires"
    await task
    tasks, agent, idle_transactions = await state.records()
    assert [task.status for task in tasks] == ["failed"]
    assert "TimeoutError" in tasks[0].metadata_json["error"]
    assert agent.last_heartbeat_at is not None
    assert state.sessions == idle_transactions == 0
    assert not await state.redis.exists(f"heartbeat_lease:{state.agent_id}")


async def test_deadline_preserves_normal_heartbeat(heartbeat_run):
    from app.services import heartbeat

    state = heartbeat_run
    await heartbeat._execute_heartbeat(state.agent_id, tenant_id=state.tenant_id)
    tasks, agent, idle_transactions = await state.records()
    assert [task.status for task in tasks] == ["completed"]
    assert tasks[0].result_summary == "Heartbeat [action]: Fixture consolidation completed."
    assert tasks[0].child_session_id
    assert agent.last_heartbeat_at is not None
    assert state.core_calls == 1
    assert state.sessions == idle_transactions == 0
    assert not await state.redis.exists(f"heartbeat_lease:{state.agent_id}")


async def test_deadline_starts_after_runtime_task_creation(heartbeat_run, monkeypatch):
    from app.services import heartbeat

    state = heartbeat_run
    create_task = heartbeat._create_heartbeat_runtime_task
    creation_committed = asyncio.Event()
    release_creation = asyncio.Event()

    async def delayed_create_task(*args, **kwargs):
        task_id = await create_task(*args, **kwargs)
        creation_committed.set()
        await release_creation.wait()
        return task_id

    monkeypatch.setattr(heartbeat, "_create_heartbeat_runtime_task", delayed_create_task)
    task = asyncio.create_task(heartbeat._execute_heartbeat(state.agent_id, tenant_id=state.tenant_id))
    try:
        await asyncio.wait_for(creation_committed.wait(), timeout=1)
        done, _ = await asyncio.wait([task], timeout=0.4)
        assert task not in done, "the heartbeat deadline must not cancel a committed task before its id returns"
        tasks, _, _ = await state.records()
        assert [record.status for record in tasks] == ["running"]
    finally:
        release_creation.set()

    await asyncio.wait_for(task, timeout=1)
    tasks, _, idle_transactions = await state.records()
    assert [record.status for record in tasks] == ["completed"]
    assert state.sessions == idle_transactions == 0
    assert not await state.redis.exists(f"heartbeat_lease:{state.agent_id}")


async def test_deadline_includes_waiting_for_concurrency_slot(heartbeat_run):
    from app.services import heartbeat

    state = heartbeat_run
    slots = state.semaphore._value
    for _ in range(slots):
        await state.semaphore.acquire()
    try:
        await heartbeat._heartbeat_tick()
        # The baseline queues before creating the durable run; the fix records
        # and bounds the queued run inside the lease.
        await asyncio.sleep(0.65)
        tasks, agent, idle_transactions = await state.records()
        assert [task.status for task in tasks] == ["failed"]
        assert "TimeoutError" in tasks[0].metadata_json["error"]
        assert agent.last_heartbeat_at is not None
        assert state.sessions == idle_transactions == 0
        assert state.core_calls == 0
        assert not await state.redis.exists(f"heartbeat_lease:{state.agent_id}")
    finally:
        for _ in range(slots):
            state.semaphore.release()
