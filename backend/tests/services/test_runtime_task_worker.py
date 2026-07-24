from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_worker_claimable_task_types_cover_v3_runtime_planes():
    import app.services.runtime_task_worker as worker

    assert "web_chat_turn" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "workflow" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "delegation" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "business_task" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "subagent" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "trigger" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "approval_execution" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "hr_provisioning" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "dream" in worker.SUPPORTED_RUNTIME_TASK_TYPES


def test_worker_claim_batch_is_capped_by_active_web_chat_runs(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 7)

    assert worker._claim_batch_size_for_available_slots() == 1

    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 8)

    assert worker._claim_batch_size_for_available_slots() == 0


def test_worker_claim_batch_uses_configured_batch_when_capacity_allows(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)

    assert worker._claim_batch_size_for_available_slots() == 4


def test_worker_task_type_limit_parser_caps_claimable_types(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS="web_chat_turn=2,workflow=1,delegation=3,subagent=2",
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=8,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)
    monkeypatch.setattr(
        worker,
        "_active_dispatched_task_type_counts",
        lambda: {"web_chat_turn": 2, "workflow": 0, "subagent": 1},
    )

    assert worker._task_type_capacity_remaining("web_chat_turn") == 0
    assert worker._task_type_capacity_remaining("workflow") == 1
    assert worker._task_type_capacity_remaining("subagent") == 1
    assert worker._claimable_task_types_for_available_capacity() == ("workflow", "delegation", "subagent")


def test_default_worker_capacity_is_not_cc_hostile():
    import app.services.runtime_task_worker as worker
    from app.config import get_settings

    settings = get_settings()
    limits = worker._parse_task_type_limits(settings.RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS)

    assert settings.RUNTIME_TASK_WORKER_MAX_CONCURRENT >= 16
    assert settings.RUNTIME_TASK_WORKER_BATCH_SIZE >= 8
    assert limits["web_chat_turn"] >= 16
    assert limits["workflow"] >= 16
    assert limits["delegation"] >= 16
    assert limits["subagent"] >= 16
    assert limits["trigger"] >= 8
    assert limits["approval_execution"] >= 8
    assert limits["hr_provisioning"] >= 4
    assert limits["dream"] >= 2


def test_worker_dispatches_claimed_subagent_to_runtime_task_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="subagent")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "subagent",
        "coro_name": "_execute_claimed_subagent_task",
    }


def test_worker_dispatches_claimed_trigger_to_runtime_task_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="trigger")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "trigger",
        "coro_name": "_execute_claimed_trigger_task",
    }


def test_worker_dispatches_claimed_approval_execution_to_durable_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="approval_execution")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "approval_execution",
        "coro_name": "_execute_claimed_approval_execution_task",
    }


def test_worker_dispatches_claimed_hr_provisioning_to_durable_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="hr_provisioning")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "hr_provisioning",
        "coro_name": "_execute_claimed_hr_provisioning_task",
    }


def test_worker_dispatches_claimed_dream_to_durable_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="dream")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "dream",
        "coro_name": "_execute_claimed_dream_task",
    }


@pytest.mark.asyncio
async def test_execute_claimed_business_task_marks_failed_on_executor_error(monkeypatch):
    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    finalized = []

    async def fake_mark_started(*, runtime_task_id):
        assert runtime_task_id == expected_runtime_task_id
        return business_task_id, agent_id, requester_id

    async def fake_finalize(*, runtime_task_id, outcome):
        finalized.append((runtime_task_id, outcome))
        return True

    async def fake_execute_task(_business_task_id, _agent_id, *, requester_user_id, cancel_event):
        assert requester_user_id == requester_id
        assert cancel_event is not None
        raise RuntimeError("executor exploded")

    expected_runtime_task_id = runtime_task_id
    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)

    await worker._execute_claimed_business_task(runtime_task_id)

    assert len(finalized) == 1
    finalized_task_id, outcome = finalized[0]
    assert finalized_task_id == runtime_task_id
    assert outcome.status is TaskExecutionStatus.FAILED
    assert outcome.retryable is True
    assert "executor exploded" in outcome.summary


@pytest.mark.asyncio
async def test_execute_claimed_business_task_passes_cross_process_cancel_to_kernel(monkeypatch):
    import asyncio

    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    cancel_event = asyncio.Event()
    released = []
    finalized = []
    registered = False

    async def fake_mark_started(*, runtime_task_id):
        assert registered is True
        cancel_event.set()
        return business_task_id, agent_id, requester_id

    async def fake_execute_task(_task_id, _agent_id, *, requester_user_id, cancel_event: asyncio.Event):
        assert requester_user_id == requester_id
        assert cancel_event is expected_cancel_event
        return TaskExecutionOutcome(status=TaskExecutionStatus.CANCELLED, summary="cancelled")

    async def fake_finalize(*, runtime_task_id, outcome):
        finalized.append(outcome)
        return True

    def fake_release(task_id, event):
        released.append((task_id, event))

    def fake_cancel_event(task_id):
        nonlocal registered
        assert task_id == expected_runtime_task_id
        registered = True
        return cancel_event

    expected_cancel_event = cancel_event
    expected_runtime_task_id = runtime_task_id
    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.business_task_cancel_event", fake_cancel_event)
    monkeypatch.setattr("app.services.task_executor.release_business_task_cancel_event", fake_release)
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)

    await worker._execute_claimed_business_task(runtime_task_id)

    assert finalized[0].status is TaskExecutionStatus.CANCELLED
    assert released == [(runtime_task_id, cancel_event)]


@pytest.mark.asyncio
async def test_execute_claimed_business_task_treats_terminal_cancel_race_as_benign(monkeypatch):
    import asyncio

    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import BusinessTaskExecutionSuperseded

    runtime_task_id = uuid4()
    cancel_event = asyncio.Event()
    released = []
    reconciliation_updates = []

    async def fake_mark_started(*, runtime_task_id):
        raise BusinessTaskExecutionSuperseded("run was cancelled before invocation")

    async def fake_update_runtime_task_record(*args, **kwargs):
        reconciliation_updates.append((args, kwargs))

    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr("app.services.task_executor.business_task_cancel_event", lambda _run_id: cancel_event)
    monkeypatch.setattr(
        "app.services.task_executor.release_business_task_cancel_event",
        lambda run_id, event: released.append((run_id, event)),
    )
    monkeypatch.setattr(
        "app.services.runtime_task_service.update_runtime_task_record",
        fake_update_runtime_task_record,
    )

    await worker._execute_claimed_business_task(runtime_task_id)

    assert released == [(runtime_task_id, cancel_event)]
    assert reconciliation_updates == []


@pytest.mark.asyncio
async def test_execute_claimed_subagent_task_dispatches_persisted_run(monkeypatch):
    import app.services.runtime_task_worker as worker

    runtime_task_id = uuid4()
    calls = []

    async def fake_dispatch_persisted_subagent_run(task_id):
        calls.append(task_id)
        return True

    monkeypatch.setattr(
        "app.services.subagent_run_service.dispatch_persisted_subagent_run",
        fake_dispatch_persisted_subagent_run,
        raising=False,
    )

    await worker._execute_claimed_subagent_task(runtime_task_id)

    assert calls == [runtime_task_id.hex]


@pytest.mark.asyncio
async def test_runtime_worker_drains_completion_outbox_and_records_counts(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    class FakeOutboxService:
        async def reconcile_terminal_tasks_once(self, *, limit):
            captured["reconcile_limit"] = limit
            return 4

        async def drain_once(self, *, worker_id, limit):
            captured.update({"worker_id": worker_id, "limit": limit})
            return {"claimed": 3, "delivered": 2, "retried": 1, "dead_lettered": 0}

    monkeypatch.setattr(worker, "RuntimeNotificationOutboxService", FakeOutboxService, raising=False)
    before_delivered = int(worker._STATE.get("outbox_delivered") or 0)
    before_retried = int(worker._STATE.get("outbox_retried") or 0)
    previous_error = worker._STATE.get("last_error")
    worker._STATE["last_error"] = "outbox:ConfigurationLimitExceededError: temporary file limit exceeded"

    try:
        result = await worker.drain_runtime_notification_outbox_once(worker_id="runtime-worker")

        assert captured == {"reconcile_limit": 100, "worker_id": "runtime-worker", "limit": 100}
        assert result["claimed"] == 3
        assert result["reconciled"] == 4
        assert worker._STATE["outbox_delivered"] == before_delivered + 2
        assert worker._STATE["outbox_retried"] == before_retried + 1
        assert worker._STATE["last_error"] is None
    finally:
        worker._STATE["last_error"] = previous_error


@pytest.mark.asyncio
async def test_runtime_worker_consumes_hr_draft_reconciler(monkeypatch):
    import app.services.runtime_task_worker as worker

    calls = []

    async def fake_reconcile():
        calls.append(None)
        return {"checked": 3, "expired": 1, "jobs_created": 1, "failed_converged": 1}

    monkeypatch.setattr("app.services.hr_creation_reconciliation.reconcile_hr_creation_drafts_once", fake_reconcile)
    before = int(worker._STATE.get("hr_drafts_reconciled") or 0)

    result = await worker.reconcile_hr_creation_drafts_once()

    assert calls == [None]
    assert result["checked"] == 3
    assert worker._STATE["hr_drafts_reconciled"] == before + 3


@pytest.mark.asyncio
async def test_runtime_worker_consumes_team_fanout_recovery_and_records_counts(monkeypatch):
    import app.services.runtime_task_worker as worker

    calls = []

    class FakeRecoveryService:
        async def drain_once(self, *, worker_id, limit):
            calls.append((worker_id, limit))
            return {
                "claimed": 3,
                "recovered": 2,
                "retried": 1,
                "needs_reconciliation": 0,
            }

    monkeypatch.setattr(
        "app.services.team_fanout_recovery.TeamFanoutRecoveryService",
        FakeRecoveryService,
        raising=False,
    )
    before = int(worker._STATE.get("team_fanout_recovered") or 0)

    result = await worker.recover_team_fanout_admissions_once(worker_id="runtime-worker")

    assert calls == [("runtime-worker", 100)]
    assert result["claimed"] == 3
    assert worker._STATE["team_fanout_recovered"] == before + 2


@pytest.mark.asyncio
async def test_runtime_worker_loop_calls_hr_reconciler_before_claiming(monkeypatch):
    import asyncio

    import app.services.runtime_task_worker as worker

    calls = []

    async def stop_after_reconcile():
        calls.append("reconcile")
        raise asyncio.CancelledError

    async def listener():
        await asyncio.Event().wait()

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            HIVE_PROCESS_ROLE="runtime",
            RUNTIME_TASK_WORKER_ENABLED=True,
            RUNTIME_TASK_WORKER_ID="test-worker",
        ),
    )
    monkeypatch.setattr(worker, "_redis_wakeup_listener", listener)
    monkeypatch.setattr(worker, "reconcile_hr_creation_drafts_once", stop_after_reconcile)

    with pytest.raises(asyncio.CancelledError):
        await worker.start_runtime_task_worker_loop()

    assert calls == ["reconcile"]
