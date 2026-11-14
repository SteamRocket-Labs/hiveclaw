from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Select, delete, func, inspect, select, text

from tests.services.test_runtime_budget_service import _create_run, _seed_tenant


async def test_waiting_breaker_reloads_tasks_after_candidate_rollback(
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    from app.database import tenant_scoped_session
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id, max_background_tasks=1, fail_mode="require_confirmation")
    candidate_ids = [uuid.uuid4(), uuid.uuid4()]
    pending_ids = sorted([uuid.uuid4(), uuid.uuid4()])
    now = datetime.now(UTC)
    waiting_statement = select(RuntimeTask).where(
        RuntimeTask.budget_run_id == run.id,
        RuntimeTask.status.in_(("pending", "resumable", "suspended")),
        RuntimeTask.claimed_by.is_(None),
    )
    retained_tasks = []
    locked_batches = []
    apply_breaker = service._apply_breaker

    async def retain_waiting_tasks(db, *args, **kwargs):
        execute = db.execute

        async def ordered_execute(statement, *args, **kwargs):
            if (
                isinstance(statement, Select)
                and statement.whereclause is not None
                and statement.whereclause.compare(waiting_statement.whereclause)
            ):
                # Only make T1-before-T2 deterministic; still use the real
                # PostgreSQL locking query and the production execution options.
                frozen = (await execute(statement.order_by(RuntimeTask.id), *args, **kwargs)).freeze()
                tasks = frozen().scalars().all()
                retained_tasks.extend(tasks)
                locked_batches.append([task.id for task in tasks])
                return frozen()
            return await execute(statement, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", ordered_execute)
            return await apply_breaker(db, *args, **kwargs)

    monkeypatch.setattr(service, "_apply_breaker", retain_waiting_tasks)
    try:
        for index, task_id in enumerate(candidate_ids):
            await service.reserve(
                RuntimeBudgetReservation(
                    budget_run_id=run.id,
                    reservation_key=f"candidate-{index}",
                    runtime_task_id=task_id,
                    tokens=100,
                )
            )
        async with owner_sessionmaker() as db:
            oldest = await db.scalar(select(func.min(RuntimeBudgetEvent.created_at)))
            for index, task_id in enumerate(candidate_ids):
                reservation = await db.scalar(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.reservation_key == f"candidate-{index}",
                    )
                )
                reservation.created_at = min(now, oldest or now) - timedelta(minutes=2 - index)
                db.add(
                    RuntimeTask(
                        id=task_id,
                        tenant_id=tenant_id,
                        task_type="subagent",
                        status="completed",
                        completed_at=now,
                        budget_run_id=run.id,
                        budget_reservation_key=f"candidate-{index}",
                        budget_admission_status="reserved",
                        metadata_json={
                            "runtime_budget_actuals": {
                                "tokens": 7,
                                "background_tasks": 1,
                            }
                        },
                    )
                )
            db.add_all(
                [
                    RuntimeTask(
                        id=pending_ids[0],
                        tenant_id=tenant_id,
                        task_type="subagent",
                        status="pending",
                        budget_run_id=run.id,
                        budget_reservation_key="candidate-0",
                        budget_admission_status="reserved",
                    ),
                    RuntimeTask(
                        id=pending_ids[1],
                        tenant_id=tenant_id,
                        task_type="subagent",
                        status="pending",
                        budget_run_id=run.id,
                        budget_admission_status="reserved",
                        metadata_json={"initial": True},
                    ),
                ]
            )
            await db.commit()

        lock_run = service._lock_run
        lock_calls = 0

        async def write_between_candidates():
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                first = await db.get(RuntimeTask, pending_ids[0])
                second = await db.get(RuntimeTask, pending_ids[1])
                first.claimed_by = "round2-concurrent"
                first.status = "running"
                second.metadata_json = {
                    "initial": True,
                    "concurrent": "must-survive",
                    "runtime_budget_actuals": {"tokens": 41},
                }
                await db.commit()

        async def lock_after_concurrent_write(db, budget_run_id):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                untouched = next(task for task in retained_tasks if task.id == pending_ids[1])
                assert not inspect(untouched).expired
                await asyncio.wait_for(write_between_candidates(), timeout=5)
            return await lock_run(db, budget_run_id)

        monkeypatch.setattr(service, "_lock_run", lock_after_concurrent_write)
        assert await service.reconcile_orphaned_reservations(limit=2) == 1
        async with owner_sessionmaker() as db:
            stored_run = await db.get(RuntimeBudgetRun, run.id)
            second = await db.get(RuntimeTask, pending_ids[1])
            settlements = list(
                await db.scalars(
                    select(RuntimeBudgetEvent.reservation_key).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
        assert locked_batches == [pending_ids, [pending_ids[1]]]
        assert lock_calls == 2
        assert stored_run.status == "waiting_budget_approval"
        assert stored_run.used_tokens == 7
        assert stored_run.reserved_tokens == 100
        assert settlements == ["candidate-1"]
        assert second.budget_admission_status == "waiting_budget_approval"
        assert second.metadata_json == {
            "initial": True,
            "concurrent": "must-survive",
            "runtime_budget_actuals": {"tokens": 41},
            "budget_admission_status_before_breaker": "reserved",
        }
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.budget_run_id == run.id))
            await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()


async def test_orphan_conflict_expires_task_before_shared_nested_reservation(
    app_user_sessionmaker,
    owner_sessionmaker,
    monkeypatch,
):
    from app.database import tenant_scoped_session
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await _create_run(service, tenant_id)
    task_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        for key in ("outer-conflict", "nested-normal"):
            await service.reserve(
                RuntimeBudgetReservation(
                    budget_run_id=run.id,
                    reservation_key=key,
                    runtime_task_id=task_id,
                    tokens=100,
                )
            )
        async with owner_sessionmaker() as db:
            oldest = await db.scalar(select(func.min(RuntimeBudgetEvent.created_at)))
            for index, key in enumerate(("outer-conflict", "nested-normal")):
                reservation = await db.scalar(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.reservation_key == key,
                    )
                )
                reservation.created_at = min(now, oldest or now) - timedelta(hours=3 - index)
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="completed",
                    completed_at=now - timedelta(hours=2),
                    budget_run_id=run.id,
                    budget_reservation_key="outer-conflict",
                    budget_admission_status="reserved",
                    metadata_json={"runtime_budget_actuals": {"tokens": 7}},
                )
            )
            db.add(
                RuntimeBudgetEvent(
                    tenant_id=tenant_id,
                    budget_run_id=run.id,
                    event_type="settlement",
                    reservation_key="outer-conflict",
                    runtime_task_id=uuid.uuid4(),
                    amounts_json={},
                )
            )
            await db.commit()
        lock_run = service._lock_run
        lock_calls = 0

        async def resume_task():
            async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, task_id)
                task.status = "running"
                task.completed_at = None
                task.metadata_json = {"concurrent": "running-again"}
                await db.commit()

        async def lock_after_task_resumes(db, budget_run_id):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                await asyncio.wait_for(resume_task(), timeout=5)
            return await lock_run(db, budget_run_id)

        monkeypatch.setattr(service, "_lock_run", lock_after_task_resumes)
        assert await service.reconcile_orphaned_reservations(limit=2) == 0
        async with owner_sessionmaker() as db:
            stored_run = await db.get(RuntimeBudgetRun, run.id)
            task = await db.get(RuntimeTask, task_id)
            settlements = list(
                await db.scalars(
                    select(RuntimeBudgetEvent.reservation_key).where(
                        RuntimeBudgetEvent.budget_run_id == run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            )
        assert lock_calls == 2
        assert stored_run.reserved_tokens == 200
        assert stored_run.used_tokens == 0
        assert settlements == ["outer-conflict"]
        assert task.status == "running"
        assert task.metadata_json == {"concurrent": "running-again"}
        assert task.budget_admission_status == "reserved"
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.budget_run_id == run.id))
            await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.id == run.id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()


@pytest.mark.parametrize("lane", ["reconcile_orphaned_reservations", "reap_expired_runs"])
async def test_budget_savepoint_subxact_measurement(app_user_sessionmaker, owner_sessionmaker, monkeypatch, lane):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.tenant import Tenant
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    count = 65
    tenant_id = await _seed_tenant(owner_sessionmaker)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run_ids = []
    measurements = []
    budget_session = service._budget_session

    @contextlib.asynccontextmanager
    async def measure_before_commit(operation):
        async with budget_session(operation) as db:
            if operation == lane:
                commit = db.commit

                async def measured_commit():
                    await db.flush()
                    measurement = dict(
                        (
                            await db.execute(
                                text(
                                    "SELECT pg_backend_pid() AS pid, current_setting('server_version') AS version, "
                                    "pg_current_xact_id_if_assigned() IS NOT NULL AS assigned_xid, "
                                    "s.subxact_count, s.subxact_overflowed AS subxact_overflow "
                                    "FROM pg_stat_get_backend_idset() AS b(id) "
                                    "CROSS JOIN LATERAL pg_stat_get_backend_subxact(b.id) AS s "
                                    "WHERE pg_stat_get_backend_pid(b.id) = pg_backend_pid()"
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    measurements.append({"lane": lane, "candidates": count, **measurement})
                    await commit()

                monkeypatch.setattr(db, "commit", measured_commit)
            yield db

    monkeypatch.setattr(service, "_budget_session", measure_before_commit)
    try:
        if lane == "reap_expired_runs":
            for _ in range(count):
                run_ids.append((await _create_run(service, tenant_id)).id)
            async with owner_sessionmaker() as db:
                oldest = await db.scalar(select(func.min(RuntimeBudgetRun.expires_at)))
                expiry = min(datetime.now(UTC), oldest or datetime.now(UTC)) - timedelta(minutes=1)
                for run in await db.scalars(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id.in_(run_ids))):
                    run.expires_at = expiry
                await db.commit()
        else:
            run = await _create_run(service, tenant_id)
            run_ids.append(run.id)
            for index in range(count):
                await service.reserve(
                    RuntimeBudgetReservation(
                        budget_run_id=run.id,
                        reservation_key=f"measurement-{index}",
                        tokens=1,
                    )
                )
            async with owner_sessionmaker() as db:
                oldest = await db.scalar(select(func.min(RuntimeBudgetEvent.created_at)))
                created_at = min(datetime.now(UTC), oldest or datetime.now(UTC)) - timedelta(days=2)
                for event in await db.scalars(
                    select(RuntimeBudgetEvent).where(RuntimeBudgetEvent.budget_run_id == run.id)
                ):
                    event.created_at = created_at
                await db.commit()
        assert await getattr(service, lane)(limit=count) == count
        assert len(measurements) == 1
        assert measurements[0]["assigned_xid"] is True
        assert isinstance(measurements[0]["subxact_count"], int)
        assert isinstance(measurements[0]["subxact_overflow"], bool)
        print("BF1_R2_SUBXACT " + json.dumps(measurements[0], sort_keys=True))
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.id.in_(run_ids)))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
