from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, inspect, select, text

from tests.services.test_runtime_budget_service import _create_run, _seed_tenant


@pytest.fixture
async def expired_budget_conflict(app_user_sessionmaker, owner_sessionmaker, monkeypatch, request):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.services import runtime_terminal_settlement
    from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

    variant = getattr(request, "param", "conflict_first")
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    tenant_ids = [await _seed_tenant(owner_sessionmaker) for _ in range(2)]
    run_ids = []
    task_ids = [uuid.uuid4() for _ in range(3)]
    now = datetime.now(UTC)
    bad_task_id, earlier_task_id, normal_task_id = task_ids
    row_objects = []

    async def snapshot(run_id):
        async with owner_sessionmaker() as db:
            return [
                [dict(row) for row in (await db.execute(statement)).mappings()]
                for statement in (
                    select(RuntimeBudgetRun.__table__).where(RuntimeBudgetRun.id == run_id),
                    select(RuntimeTask.__table__).where(RuntimeTask.budget_run_id == run_id).order_by(RuntimeTask.id),
                    select(RuntimeBudgetEvent.__table__)
                    .where(RuntimeBudgetEvent.budget_run_id == run_id)
                    .order_by(RuntimeBudgetEvent.id),
                )
            ]

    # Keep real PostgreSQL locks, but make the conflict follow a flushed task
    # cancellation so the snapshot detects catching without savepoint rollback.
    lock_tasks = runtime_terminal_settlement.lock_runtime_task_batch_with_session_authority

    async def bad_task_last(*args, **kwargs):
        tasks = await lock_tasks(*args, **kwargs)
        return sorted(tasks, key=lambda task: task.id == bad_task_id)

    monkeypatch.setattr(runtime_terminal_settlement, "lock_runtime_task_batch_with_session_authority", bad_task_last)
    cancel_tasks = service._cancel_pending_unclaimed_tasks

    async def retain_row_objects(db, run, **kwargs):
        try:
            return await cancel_tasks(db, run, **kwargs)
        finally:
            if run.id == run_ids[0]:
                # Hold the read-only reservation objects past rollback, just as
                # another service consumer retaining ORM references would.
                # BF2 round 2: retain only run0's objects, independent of
                # whether another interpreter has collected run1's clean reads.
                row_objects[:] = [
                    obj
                    for obj in db.identity_map.values()
                    if obj is run or inspect(obj).dict.get("budget_run_id") == run_ids[0]
                ]

    monkeypatch.setattr(service, "_cancel_pending_unclaimed_tasks", retain_row_objects)
    try:
        async with app_user_sessionmaker() as db:
            role = (
                await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
        assert role == (False, False)
        for tenant_id in tenant_ids:
            run = await _create_run(service, tenant_id, max_subagents=5)
            run_ids.append(run.id)
        for task_id, run_id in ((earlier_task_id, run_ids[0]), (bad_task_id, run_ids[0]), (normal_task_id, run_ids[1])):
            await service.reserve(
                RuntimeBudgetReservation(
                    budget_run_id=run_id,
                    reservation_key=f"PRIVATE-BF2-{task_id}",
                    runtime_task_id=task_id,
                    tokens=500,
                    subagents=1,
                )
            )
        async with owner_sessionmaker() as db:
            oldest = await db.scalar(select(func.min(RuntimeBudgetRun.expires_at)))
            expiry = min(now, oldest or now) - timedelta(minutes=2)
            for index, run_id in enumerate(run_ids):
                run = await db.get(RuntimeBudgetRun, run_id)
                run.expires_at = expiry + timedelta(minutes=int((index == 1) == (variant != "conflict_last")))
                run.metadata_json = {"private": "BF2-TENANT-CONTENT"}
            for task_id, run_id, tenant_id in (
                (earlier_task_id, run_ids[0], tenant_ids[0]),
                (bad_task_id, run_ids[0], tenant_ids[0]),
                (normal_task_id, run_ids[1], tenant_ids[1]),
            ):
                db.add(
                    RuntimeTask(
                        id=task_id,
                        tenant_id=tenant_id,
                        task_type="subagent",
                        status="pending",
                        budget_run_id=run_id,
                        budget_reservation_key=f"PRIVATE-BF2-{task_id}",
                        budget_admission_status="reserved",
                        metadata_json={"runtime_budget_actuals": {"tokens": "BF2-TENANT-CONTENT"}}
                        if task_id == bad_task_id and variant == "invalid_actuals"
                        else None,
                    )
                )
            if variant != "invalid_actuals":
                reservation = await db.scalar(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run_ids[0],
                        RuntimeBudgetEvent.reservation_key == f"PRIVATE-BF2-{bad_task_id}",
                    )
                )
                reservation.runtime_task_id = uuid.uuid4()
            await db.commit()
        yield SimpleNamespace(
            service=service,
            now=now,
            tenant_ids=tenant_ids,
            run_ids=run_ids,
            task_ids=task_ids,
            bad_task_id=bad_task_id,
            normal_task_id=normal_task_id,
            snapshot=snapshot,
            row_objects=row_objects,
        )
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.budget_run_id.in_(run_ids)))
            await db.execute(delete(RuntimeBudgetRun).where(RuntimeBudgetRun.id.in_(run_ids)))
            await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
            await db.commit()


@pytest.mark.parametrize(
    "expired_budget_conflict", ["conflict_first", "conflict_last", "invalid_actuals"], indirect=True
)
async def test_reaper_isolates_conflicting_expired_runs(expired_budget_conflict, caplog):
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_service import RuntimeBudgetSettlementConflict

    case = expired_budget_conflict
    conflicting_before = await case.snapshot(case.run_ids[0])
    outcomes = []
    with caplog.at_level(logging.ERROR, logger="app.services.runtime_budget_service"):
        # The second tick selects only the remaining conflict, not old fixtures.
        for batch_limit in (2, 1):
            try:
                outcomes.append(await case.service.reap_expired_runs(now=case.now, limit=batch_limit))
            except RuntimeBudgetSettlementConflict as exc:
                # On the base, record BOTH failures and the untouched normal run.
                outcomes.append(type(exc).__name__)

    assert await case.snapshot(case.run_ids[0]) == conflicting_before
    normal_after = await case.snapshot(case.run_ids[1])
    normal_run, normal_task = normal_after[0][0], normal_after[1][0]
    assert outcomes == [1, 0], {
        "outcomes": outcomes,
        "normal_status": normal_run["status"],
        "normal_reserved_tokens": normal_run["reserved_tokens"],
    }
    assert normal_run["status"] == "expired"
    assert normal_run["terminal_reason"] == "budget_run_expired"
    assert normal_run["completed_at"] == case.now
    assert normal_run["reserved_tokens"] == normal_run["reserved_subagents"] == 0
    assert normal_task["status"] == "killed"
    assert normal_task["budget_admission_status"] == "cancelled"
    assert normal_task["claim_version"] == 1
    assert [event["event_type"] for event in normal_after[2]].count("expired") == 1
    conflict_logs = [record for record in caplog.records if record.name == "app.services.runtime_budget_service"]
    assert [(record.levelno, record.getMessage()) for record in conflict_logs] == [
        (
            logging.ERROR,
            # BF2 round 2: keep an exact, content-free log contract for the wider catch.
            "Expired budget run failed: "
            f"budget_run_id={case.run_ids[0]} error_type=RuntimeBudgetSettlementConflict "
            f"runtime_task_id={case.bad_task_id}",
        )
    ] * 2
    assert all(record.exc_info is None for record in conflict_logs)
    # BF2: only pre-existing rows can be expired. The newly inserted expired
    # event must instead be expunged by rollback, not retained as a durable row.
    events = [obj for obj in case.row_objects if isinstance(obj, RuntimeBudgetEvent)]
    reservations = [obj for obj in events if inspect(obj).has_identity]
    rolled_back_events = [obj for obj in events if not inspect(obj).has_identity]
    assert reservations
    assert all(inspect(obj).expired for obj in reservations)
    assert len(rolled_back_events) == 1
    assert inspect(rolled_back_events[0]).transient
    assert rolled_back_events[0].event_type == "expired"
    runs = [
        obj
        for obj in case.row_objects
        if isinstance(obj, RuntimeBudgetRun) and inspect(obj).identity == (case.run_ids[0],)
    ]
    tasks = [obj for obj in case.row_objects if isinstance(obj, RuntimeTask)]
    assert len(runs) == 1
    assert len(tasks) == 2
    assert all(inspect(obj).expired for obj in runs + tasks)


@pytest.mark.parametrize("reap_fails", [False, True], ids=["real_conflict", "reap_failure"])
async def test_budget_tick_reconciles_orphans_despite_expired_conflict(
    expired_budget_conflict,
    owner_sessionmaker,
    monkeypatch,
    reap_fails,
):
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_budget_daemon import runtime_budget_maintenance_tick
    from app.services.runtime_budget_service import RuntimeBudgetReservation

    case = expired_budget_conflict
    # A third run in the normal tenant is not expired; only the orphan lane can
    # release this completed task's reservation in the same maintenance tick.
    orphan = await _create_run(case.service, case.tenant_ids[1])
    case.run_ids.append(orphan.id)
    task_id = uuid.uuid4()
    await case.service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=orphan.id,
            reservation_key="orphan",
            runtime_task_id=task_id,
            tokens=73,
        )
    )
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=case.tenant_ids[1],
                task_type="subagent",
                status="completed",
                completed_at=case.now,
                budget_run_id=orphan.id,
                budget_reservation_key="orphan",
                budget_admission_status="reserved",
                metadata_json={"runtime_budget_actuals": {"tokens": 13}},
            )
        )
        await db.commit()
    conflicting_before = await case.snapshot(case.run_ids[0])
    reconcile = AsyncMock(wraps=case.service.reconcile_orphaned_reservations)
    monkeypatch.setattr(case.service, "reconcile_orphaned_reservations", reconcile)
    if reap_fails:
        monkeypatch.setattr(case.service, "reap_expired_runs", AsyncMock(side_effect=RuntimeError("reap unavailable")))
    failure = None
    try:
        result = await runtime_budget_maintenance_tick(case.service)
    except Exception as exc:
        failure = type(exc).__name__
        result = None
    orphan_after = await case.snapshot(orphan.id)
    assert await case.snapshot(case.run_ids[0]) == conflicting_before
    # BF2: successful reaping also leaves the normal killed task's reservation
    # for this same orphan lane, so require TWO repairs, not just the third run.
    assert result == {"expired_runs": 0 if reap_fails else 1, "reconciled_reservations": 1 if reap_fails else 2}, {
        "failure": failure,
        "orphan_reserved_tokens": orphan_after[0][0]["reserved_tokens"],
    }
    # BF2 round 2: the measured callee-safe batch is 32, not the old 500.
    reconcile.assert_awaited_once_with(limit=32)
    assert orphan_after[0][0]["status"] == "active"
    assert orphan_after[0][0]["reserved_tokens"] == 0
    assert orphan_after[0][0]["used_tokens"] == 13
    assert orphan_after[1][0]["budget_admission_status"] == "settled"
    assert [event["event_type"] for event in orphan_after[2]].count("settlement") == 1
    if not reap_fails:
        normal_after = await case.snapshot(case.run_ids[1])
        assert normal_after[0][0]["status"] == "expired"
        assert normal_after[0][0]["reserved_tokens"] == 0
        assert normal_after[0][0]["used_subagents"] == 1
        assert normal_after[1][0]["budget_admission_status"] == "settled"


async def test_budget_tick_logs_each_failed_lane_independently(monkeypatch):
    from app.services import runtime_budget_daemon as daemon

    service = SimpleNamespace(
        reap_expired_runs=AsyncMock(side_effect=RuntimeError("reap unavailable")),
        reconcile_orphaned_reservations=AsyncMock(side_effect=RuntimeError("orphans unavailable")),
    )
    logs = []
    monkeypatch.setattr(daemon.logger, "warning", lambda *args: logs.append(args))
    assert await daemon.runtime_budget_maintenance_tick(service) == {
        "expired_runs": 0,
        "reconciled_reservations": 0,
    }
    # BF2 round 2: both lanes use the largest measured non-overflowing batch.
    service.reap_expired_runs.assert_awaited_once_with(limit=32)
    service.reconcile_orphaned_reservations.assert_awaited_once_with(limit=32)
    assert [(args[0], str(args[1])) for args in logs] == [
        ("[runtime-budget] expired-run reaping failed: {}", "reap unavailable"),
        ("[runtime-budget] orphan reconciliation failed: {}", "orphans unavailable"),
    ]


@pytest.mark.parametrize("cancelled_lane", ["reap_expired_runs", "reconcile_orphaned_reservations"])
async def test_budget_tick_propagates_cancellation(monkeypatch, cancelled_lane):
    import asyncio

    from app.services import runtime_budget_daemon as daemon

    service = SimpleNamespace(
        reap_expired_runs=AsyncMock(return_value=2), reconcile_orphaned_reservations=AsyncMock(return_value=3)
    )
    getattr(service, cancelled_lane).side_effect = asyncio.CancelledError
    logs = []
    monkeypatch.setattr(daemon.logger, "warning", lambda *args: logs.append(args))
    with pytest.raises(asyncio.CancelledError):
        await daemon.runtime_budget_maintenance_tick(service)
    assert logs == []
    if cancelled_lane == "reap_expired_runs":
        service.reconcile_orphaned_reservations.assert_not_awaited()


@pytest.mark.parametrize(
    "lane,stage,cancelled",
    [
        ("reap_expired_runs", "cancel", False),
        ("reconcile_orphaned_reservations", "breaker", False),
        ("reconcile_orphaned_reservations", "lock", False),
        ("reap_expired_runs", "cancel", True),
        ("reconcile_orphaned_reservations", "breaker", True),
    ],
)
async def test_budget_row_errors_are_isolated_but_cancellation_propagates(
    expired_budget_conflict,
    owner_sessionmaker,
    monkeypatch,
    caplog,
    lane,
    stage,
    cancelled,
):
    import asyncio

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_terminal_settlement

    case = expired_budget_conflict
    async with owner_sessionmaker() as db:
        bad_event = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == case.run_ids[0],
                RuntimeBudgetEvent.reservation_key == f"PRIVATE-BF2-{case.bad_task_id}",
            )
        )
        bad_event.runtime_task_id = case.bad_task_id
        bad_event_id = bad_event.id
        if lane == "reconcile_orphaned_reservations":
            oldest = await db.scalar(select(func.min(RuntimeBudgetEvent.created_at)))
            for index, (run_id, task_id) in enumerate(zip(case.run_ids, (case.bad_task_id, case.normal_task_id))):
                run = await db.get(RuntimeBudgetRun, run_id)
                run.max_background_tasks = 1
                task = await db.get(RuntimeTask, task_id)
                task.status = "completed"
                task.completed_at = case.now
                task.metadata_json = {"runtime_budget_actuals": {"tokens": 13, "background_tasks": 1}}
                reservation = await db.scalar(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run_id,
                        RuntimeBudgetEvent.runtime_task_id == task_id,
                    )
                )
                reservation.created_at = min(case.now, oldest or case.now) - timedelta(minutes=2 - index)
        await db.commit()
    failure = asyncio.CancelledError("PRIVATE-BF2-EXCEPTION") if cancelled else ValueError("PRIVATE-BF2-EXCEPTION")
    if stage == "cancel":
        terminal = runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal

        async def fail_one_task(db, task, **kwargs):
            if task.id == case.bad_task_id:
                raise failure
            return await terminal(db, task, **kwargs)

        monkeypatch.setattr(runtime_terminal_settlement, "settle_and_enqueue_runtime_task_terminal", fail_one_task)
    elif stage == "breaker":
        breaker = case.service._apply_breaker

        async def fail_after_breaker(db, run, *args, **kwargs):
            result = await breaker(db, run, *args, **kwargs)
            if run.id == case.run_ids[0]:
                raise failure
            return result

        monkeypatch.setattr(case.service, "_apply_breaker", fail_after_breaker)
    else:
        lock = case.service._lock_run

        async def fail_before_run_load(db, budget_run_id):
            if budget_run_id == case.run_ids[0]:
                raise failure
            return await lock(db, budget_run_id)

        monkeypatch.setattr(case.service, "_lock_run", fail_before_run_load)
    before = [await case.snapshot(run_id) for run_id in case.run_ids]
    with caplog.at_level(logging.ERROR, logger="app.services.runtime_budget_service"):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await getattr(case.service, lane)(now=case.now, limit=2)
        else:
            result = None
            try:
                result = await getattr(case.service, lane)(now=case.now, limit=2)
            except ValueError:
                pass  # Base-code red: inspect the rolled-back rows as well.
    after = [await case.snapshot(run_id) for run_id in case.run_ids]
    assert after[0] == before[0]
    logs = [record for record in caplog.records if record.name == "app.services.runtime_budget_service"]
    if cancelled:
        assert after[1] == before[1]
        assert logs == []
        return
    assert result == 1
    assert after[1][0][0]["reserved_tokens"] == 0
    if lane == "reap_expired_runs":
        assert after[1][0][0]["status"] == "expired"
        assert after[1][1][0]["status"] == "killed"
        expected = (
            f"Expired budget run failed: budget_run_id={case.run_ids[0]} error_type=ValueError runtime_task_id=None"
        )
    else:
        assert after[1][0][0]["used_tokens"] == 13
        assert after[1][1][0]["budget_admission_status"] == "settled"
        assert [event["event_type"] for event in after[1][2]].count("settlement") == 1
        expected = (
            f"Orphaned reservation failed: budget_run_id={case.run_ids[0]} "
            f"runtime_task_id={case.bad_task_id} source_event_id={bad_event_id} "
            "error_type=ValueError exception_runtime_task_id=None"
        )
    assert [(record.levelno, record.getMessage()) for record in logs] == [(logging.ERROR, expected)]
    assert all(record.exc_info is None for record in logs)


async def test_reaper_conflict_rolls_back_chat_outbox_transcript_and_callbacks(
    expired_budget_conflict,
    owner_sessionmaker,
    monkeypatch,
):
    from app.database import _AFTER_COMMIT_CALLBACKS_KEY, schedule_after_commit
    from app.models.agent import Agent
    from app.models.budget_transition_outbox import BudgetTransitionOutbox
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask, TERMINAL_BOUNDARY_GENERATION
    from app.models.user import User
    from app.services.runtime_budget_service import RuntimeBudgetSettlementConflict

    case = expired_budget_conflict
    user_id, agent_id, session_id = (uuid.uuid4() for _ in range(3))
    try:
        async with owner_sessionmaker() as db:
            db.add(
                User(
                    id=user_id,
                    tenant_id=case.tenant_ids[0],
                    username=f"bf2-{user_id.hex}",
                    email=f"{user_id.hex}@budget.test",
                    password_hash="x",
                    display_name="BF2 owner",
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=case.tenant_ids[0],
                    name="BF2 agent",
                    role_description="rollback test",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                )
            )
            await db.flush()
            db.add(
                ChatSession(
                    id=session_id,
                    tenant_id=case.tenant_ids[0],
                    agent_id=agent_id,
                    user_id=user_id,
                    title="BF2 session",
                    source_channel="web",
                    delivery_target_json={"channel": "web"},
                )
            )
            run = await db.get(RuntimeBudgetRun, case.run_ids[0])
            run.root_session_id, run.root_agent_id, run.root_user_id = str(session_id), agent_id, user_id
            task = await db.get(RuntimeTask, case.task_ids[1])
            task.task_type = "web_chat_turn"
            task.terminal_boundary_generation = TERMINAL_BOUNDARY_GENERATION
            task.parent_session_id, task.parent_agent_id = str(session_id), agent_id
            await db.commit()
        before = await case.snapshot(case.run_ids[0])
        cancel = case.service._cancel_pending_unclaimed_tasks
        observed = []
        callback = AsyncMock()

        async def inspect_partial_effects(db, run, **kwargs):
            try:
                return await cancel(db, run, **kwargs)
            except RuntimeBudgetSettlementConflict:
                # V2 appends need not request a T0 callback. Register a test-only
                # row-local callback to pin its rollback without inventing a send.
                assert schedule_after_commit(db, callback, description="BF2 row rollback test")
                observed.append(
                    (
                        await db.scalar(
                            select(func.count())
                            .select_from(BudgetTransitionOutbox)
                            .where(BudgetTransitionOutbox.budget_run_id == case.run_ids[0])
                        ),
                        await db.scalar(
                            select(func.count())
                            .select_from(ChatTranscriptEvent)
                            .where(ChatTranscriptEvent.session_id == session_id)
                        ),
                        len(db.sync_session.info.get(_AFTER_COMMIT_CALLBACKS_KEY, [])),
                    )
                )
                raise

        monkeypatch.setattr(case.service, "_cancel_pending_unclaimed_tasks", inspect_partial_effects)
        assert await case.service.reap_expired_runs(now=case.now, limit=2) == 1
        assert observed == [(1, 1, 1)]
        assert await case.snapshot(case.run_ids[0]) == before
        callback.assert_not_awaited()
        async with owner_sessionmaker() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(BudgetTransitionOutbox)
                    .where(BudgetTransitionOutbox.budget_run_id == case.run_ids[0])
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.session_id == session_id)
                )
                == 0
            )
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(RuntimeTask).where(RuntimeTask.budget_run_id == case.run_ids[0]))
            await db.execute(
                delete(BudgetTransitionOutbox).where(BudgetTransitionOutbox.budget_run_id == case.run_ids[0])
            )
            await db.execute(delete(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            await db.execute(delete(Agent).where(Agent.id == agent_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.parametrize("lane", ["reap_expired_runs", "reconcile_orphaned_reservations"])
@pytest.mark.parametrize("failure_kind", ["rollback", "statement_timeout"])
async def test_budget_database_errors_fail_tick_and_rollback_all_rows(
    expired_budget_conflict,
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    caplog,
    lane,
    failure_kind,
):
    from sqlalchemy.exc import DBAPIError, InterfaceError, SQLAlchemyError

    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_terminal_settlement

    case = expired_budget_conflict
    async with owner_sessionmaker() as db:
        reservation = await db.scalar(
            select(RuntimeBudgetEvent).where(
                RuntimeBudgetEvent.budget_run_id == case.run_ids[0],
                RuntimeBudgetEvent.reservation_key == f"PRIVATE-BF2-{case.bad_task_id}",
            )
        )
        reservation.runtime_task_id = case.bad_task_id
        if lane == "reconcile_orphaned_reservations":
            oldest = await db.scalar(select(func.min(RuntimeBudgetEvent.created_at)))
            for index, (run_id, task_id) in enumerate(zip(case.run_ids, (case.bad_task_id, case.normal_task_id))):
                run = await db.get(RuntimeBudgetRun, run_id)
                run.max_background_tasks = 1
                task = await db.get(RuntimeTask, task_id)
                task.status = "completed"
                task.completed_at = case.now
                task.metadata_json = {"runtime_budget_actuals": {"tokens": 13, "background_tasks": 1}}
                event = await db.scalar(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == run_id,
                        RuntimeBudgetEvent.runtime_task_id == task_id,
                    )
                )
                event.created_at = min(case.now, oldest or case.now) - timedelta(minutes=2 - index)
        await db.commit()

    injections = 0
    rollback_calls = 0
    if failure_kind == "rollback":
        dialect = app_user_sessionmaker.kw["bind"].sync_engine.dialect
        rollback_to = dialect.do_rollback_to_savepoint

        def fail_first_rollback(connection, name):
            nonlocal rollback_calls
            rollback_calls += 1
            if rollback_calls == 1:
                # Raise client-side WITHOUT sending ROLLBACK TO. PostgreSQL is
                # still connected and still contains the row's partial writes.
                raise InterfaceError("ROLLBACK TO SAVEPOINT", {}, RuntimeError("another operation is in progress"))
            return rollback_to(connection, name)

        monkeypatch.setattr(dialect, "do_rollback_to_savepoint", fail_first_rollback)

    async def fail_row(db):
        nonlocal injections
        injections += 1
        if failure_kind == "rollback":
            raise ValueError("PRIVATE-BF3-ROW-DATA")
        # This must be an actual asyncpg/server failure, not a constructed
        # DBAPIError. The SET LOCAL is scoped to this test's row transaction.
        await db.execute(text("SET LOCAL statement_timeout = '10ms'"))
        await db.execute(text("SELECT pg_sleep(1)"))
        pytest.fail("PostgreSQL did not enforce statement_timeout")

    if lane == "reap_expired_runs":
        terminal = runtime_terminal_settlement.settle_and_enqueue_runtime_task_terminal

        async def fail_one_task(db, task, **kwargs):
            if task.id == case.bad_task_id:
                await fail_row(db)
            return await terminal(db, task, **kwargs)

        monkeypatch.setattr(runtime_terminal_settlement, "settle_and_enqueue_runtime_task_terminal", fail_one_task)
    else:
        breaker = case.service._apply_breaker

        async def fail_after_breaker(db, run, *args, **kwargs):
            result = await breaker(db, run, *args, **kwargs)
            if run.id == case.run_ids[0]:
                await fail_row(db)
            return result

        monkeypatch.setattr(case.service, "_apply_breaker", fail_after_breaker)

    before = [await case.snapshot(run_id) for run_id in case.run_ids]
    caught = None
    returned = None
    with caplog.at_level(logging.ERROR, logger="app.services.runtime_budget_service"):
        try:
            returned = await getattr(case.service, lane)(now=case.now, limit=2)
        except SQLAlchemyError as exc:
            caught = exc
    # Each snapshot opens a fresh owner session: inspect the database that
    # actually committed/rolled back, not the invalidated ORM identity map.
    after = [await case.snapshot(run_id) for run_id in case.run_ids]
    assert isinstance(caught, InterfaceError if failure_kind == "rollback" else DBAPIError), {
        "returned": returned,
        "snapshots_unchanged": after == before,
        "changed_runs": [run_id for run_id, old, new in zip(case.run_ids, before, after) if old != new],
    }
    assert returned is None
    assert after == before
    assert injections == 1
    if failure_kind == "rollback":
        assert rollback_calls == 1
    else:
        assert caught.orig.sqlstate == "57014"
    # Database/session errors are tick failures, not per-row data errors:
    # neither the failing nor any innocent run may be blamed by a row ERROR.
    assert [
        record
        for record in caplog.records
        if record.name == "app.services.runtime_budget_service" and record.levelno >= logging.ERROR
    ] == []
