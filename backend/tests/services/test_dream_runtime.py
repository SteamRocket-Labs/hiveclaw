from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_agent(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Dream runtime", slug=f"dream-{tenant_id.hex[:8]}"))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            User(
                id=user_id,
                username=f"dream-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@dream-runtime.test",
                password_hash="x",
                display_name="Dream Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Dreamer",
                creator_id=user_id,
                owner_user_id=user_id,
                sponsor_user_id=user_id,
                status="running",
            )
        )
    return tenant_id, user_id, agent_id


def _isolate_dream_state(monkeypatch, tmp_path) -> None:
    from app.services import auto_dream

    auto_dream._last_dream_time.clear()
    auto_dream._sessions_since_dream.clear()
    auto_dream._heartbeat_ticks_since_dream.clear()
    auto_dream._dream_version.clear()
    auto_dream._dream_history.clear()
    monkeypatch.setattr(
        auto_dream,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_concurrent_dream_boundaries_enqueue_exactly_one_cadence_job(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    from sqlalchemy import select

    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import enqueue_dream_runtime_task

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    wakeups: list[str] = []
    audit_events: list[str] = []

    async def capture_wakeup(*, reason, runtime_task_id):
        assert reason == "dream_queued"
        wakeups.append(str(runtime_task_id))

    async def capture_audit(_db, *, event_type, **_kwargs):
        audit_events.append(event_type)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", capture_wakeup)
    monkeypatch.setattr("app.core.policy.write_audit_event", capture_audit)

    first, second = await asyncio.gather(
        enqueue_dream_runtime_task(agent_id=agent_id, tenant_id=tenant_id, mode="full", source="heartbeat"),
        enqueue_dream_runtime_task(agent_id=agent_id, tenant_id=tenant_id, mode="full", source="trigger"),
    )

    assert first.task_id == second.task_id
    assert sorted([first.created, second.created]) == [False, True]
    assert wakeups == [str(first.task_id)]
    assert audit_events == ["memory.dream_queued"]
    async with owner_sessionmaker() as db:
        tasks = (
            (await db.execute(select(RuntimeTask).where(RuntimeTask.root_idempotency_key == f"dream:{agent_id}:v1")))
            .scalars()
            .all()
        )
        assert len(tasks) == 1
        assert tasks[0].task_type == "dream"
        assert tasks[0].status == "pending"
        assert tasks[0].tenant_id == tenant_id


@pytest.mark.asyncio
async def test_dream_worker_completes_once_and_persists_outcome(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import enqueue_dream_runtime_task, execute_claimed_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode="full",
        source="session_end",
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        task.status = "running"
        task.claimed_by = "dream-worker"
        task.claim_version = 1
        await db.commit()
    calls: list[str] = []

    async def fake_run(*, agent_id, tenant_id, mode):
        calls.append(f"{agent_id}:{tenant_id}:{mode}")
        return {"consolidated": 4, "removed": 1, "added": 1}

    monkeypatch.setattr("app.services.dream_runtime._run_domain_dream", fake_run)

    assert await execute_claimed_dream(queued.task_id) == "completed"
    assert await execute_claimed_dream(queued.task_id) == "completed"
    assert len(calls) == 1
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.metadata_json["outcome"]["consolidated"] == 4
        assert task.metadata_json["outcome"]["status"] == "completed"


@pytest.mark.asyncio
async def test_reclaimed_dream_converges_from_advanced_state_without_replay(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services import auto_dream
    from app.services.dream_runtime import enqueue_dream_runtime_task, execute_claimed_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode="full",
        source="heartbeat",
    )
    auto_dream._dream_version[agent_id.hex] = 1
    auto_dream._last_dream_time[agent_id.hex] = datetime.now(timezone.utc)
    auto_dream._sessions_since_dream[agent_id.hex] = 0
    auto_dream._heartbeat_ticks_since_dream[agent_id.hex] = 0
    auto_dream._persist_dream_state(agent_id)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        task.status = "running"
        task.claimed_by = "reclaimer"
        task.claim_version = 2
        task.metadata_json = {**dict(task.metadata_json or {}), "reclaimed_expired_claim": True}
        await db.commit()

    async def forbidden_run(**_kwargs):
        raise AssertionError("advanced dream state must converge without replay")

    monkeypatch.setattr("app.services.dream_runtime._run_domain_dream", forbidden_run)

    assert await execute_claimed_dream(queued.task_id) == "completed"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.metadata_json["recovered_from_state"] is True


@pytest.mark.asyncio
async def test_soft_dream_pressure_uses_the_same_durable_worker_lane(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services import auto_dream
    from app.services.dream_runtime import enqueue_due_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(auto_dream, "should_dream", lambda _agent_id: False)
    monkeypatch.setattr(auto_dream, "should_soft_dream", lambda _agent_id: True)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_due_dream(agent_id=agent_id, tenant_id=tenant_id, source="conversation_end")

    assert queued is not None
    assert queued.mode == "soft"
    assert queued.idempotency_key.startswith(f"soft-dream:{agent_id}:v0:w")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.task_type == "dream"
        assert task.metadata_json["dream_mode"] == "soft"


@pytest.mark.asyncio
async def test_dream_worker_failure_schedules_bounded_idempotent_retry(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import enqueue_dream_runtime_task, execute_claimed_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode="full",
        source="heartbeat",
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        task.status = "running"
        task.claimed_by = "dream-worker"
        task.claim_version = 1
        task.attempt_count = 1
        await db.commit()

    async def fail_run(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.services.dream_runtime._run_domain_dream", fail_run)

    assert await execute_claimed_dream(queued.task_id) == "retry_scheduled"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.status == "resumable"
        assert task.scheduled_at is not None
        assert task.metadata_json["automatic_retry_allowed"] is True
        assert "provider unavailable" in task.metadata_json["retry_reason"]


@pytest.mark.asyncio
async def test_degraded_semantic_result_is_retried_and_preserves_coverage_evidence(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import enqueue_dream_runtime_task, execute_claimed_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode="full",
        source="heartbeat",
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        task.status = "running"
        task.claimed_by = "dream-worker"
        task.claim_version = 1
        task.attempt_count = 1
        await db.commit()

    async def degraded_run(**_kwargs):
        return {
            "status": "degraded",
            "retryable": True,
            "reason": "semantic_consolidator_unavailable",
            "coverage": {"total": 3, "reviewed": 0, "complete": False},
        }

    monkeypatch.setattr("app.services.dream_runtime._run_domain_dream", degraded_run)

    assert await execute_claimed_dream(queued.task_id) == "retry_scheduled"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.status == "resumable"
        assert task.metadata_json["last_attempt_outcome"]["coverage"]["total"] == 3
        assert task.metadata_json["last_attempt_outcome"]["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_exhausted_dream_retry_enters_operator_retryable_reconciliation(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import enqueue_dream_runtime_task, execute_claimed_dream

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    queued = await enqueue_dream_runtime_task(
        agent_id=agent_id,
        tenant_id=tenant_id,
        mode="full",
        source="heartbeat",
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        task.status = "running"
        task.claimed_by = "dream-worker"
        task.claim_version = 3
        task.attempt_count = 3
        await db.commit()

    async def fail_run(**_kwargs):
        raise RuntimeError("persistent provider failure")

    monkeypatch.setattr("app.services.dream_runtime._run_domain_dream", fail_run)

    assert await execute_claimed_dream(queued.task_id) == "needs_reconciliation"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, queued.task_id)
        assert task is not None
        assert task.status == "needs_reconciliation"
        assert task.metadata_json["reconciliation_retry_allowed"] is True
        assert task.metadata_json["reconciliation_reason"] == "dream_retry_exhausted"


@pytest.mark.asyncio
async def test_due_state_reconciler_backfills_legacy_file_into_runtime_task(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    from sqlalchemy import select

    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.services.dream_runtime import reconcile_due_dream_runtime_tasks

    tenant_id, _, agent_id = await _seed_agent(owner_sessionmaker)
    _isolate_dream_state(monkeypatch, tmp_path)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    state_path = tmp_path / str(agent_id) / "memory" / "control" / "auto_dream_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_dream_time": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
                "sessions_since_dream": 3,
                "heartbeat_ticks_since_dream": 0,
                "version": 2,
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    wakeups: list[str] = []

    async def capture_wakeup(*, reason, runtime_task_id):
        wakeups.append(f"{reason}:{runtime_task_id}")

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", capture_wakeup)

    result = await reconcile_due_dream_runtime_tasks(limit=50)

    assert result == {"scanned": 1, "queued": 1, "existing": 0, "failed": 0}
    assert len(wakeups) == 1
    async with owner_sessionmaker() as db:
        task = (
            await db.execute(select(RuntimeTask).where(RuntimeTask.root_idempotency_key == f"dream:{agent_id}:v3"))
        ).scalar_one()
        assert task.metadata_json["recovery_source"] == "due_state_reconciler"


def test_all_dream_boundaries_use_the_durable_enqueue_contract() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "services"
    evolution = (root / "evolution_daemon.py").read_text(encoding="utf-8")
    memory = (root / "memory_service.py").read_text(encoding="utf-8")
    trigger = (root / "trigger_daemon.py").read_text(encoding="utf-8")

    assert 'asyncio.create_task(run_bounded("dream"' not in evolution
    assert 'asyncio.create_task(run_bounded("dream"' not in trigger
    assert "asyncio.create_task(run_dream" not in memory
    assert "asyncio.create_task(run_soft_dream" not in memory
    assert "enqueue_due_dream" in evolution
    assert "enqueue_due_dream" in memory
    assert "enqueue_due_dream" in trigger
