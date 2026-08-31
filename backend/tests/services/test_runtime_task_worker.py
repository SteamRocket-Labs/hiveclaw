from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    assert "a2a_continuation" in worker.SUPPORTED_RUNTIME_TASK_TYPES


@pytest.mark.asyncio
async def test_claimed_delegation_worker_holds_fence_until_inner_execution_finishes(monkeypatch):
    import asyncio

    import app.agents.orchestrator as orchestrator
    import app.services.runtime_task_worker as worker

    task_id = uuid4()
    started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def fake_dispatch(task_id_arg, *, wait_for_completion=False):
        assert task_id_arg == task_id.hex
        assert wait_for_completion is True
        started.set()
        await allow_finish.wait()
        return True

    monkeypatch.setattr(orchestrator, "dispatch_persisted_async_delegation", fake_dispatch)

    execution = asyncio.create_task(worker._execute_claimed_delegation_task(task_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert execution.done() is False

    allow_finish.set()
    await asyncio.wait_for(execution, timeout=1)


@pytest.mark.asyncio
async def test_expired_readonly_delegation_reclaims_new_fence_and_completes_after_stale_local_state(
    monkeypatch,
):
    import asyncio

    import app.agents.orchestrator as orchestrator
    import app.services.runtime_task_worker as worker
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import current_runtime_task_fence

    class ClaimResult:
        def __init__(self, task):
            self.task = task

        def scalars(self):
            return SimpleNamespace(all=lambda: [self.task])

    class ClaimDB:
        async def execute(self, _statement):
            return ClaimResult(runtime_task)

        async def commit(self):
            return None

    runtime_task_id = uuid4()
    runtime_task = RuntimeTask(
        id=runtime_task_id,
        task_type="delegation",
        status="running",
        tenant_id=uuid4(),
        parent_agent_id=uuid4(),
        parent_session_id=str(uuid4()),
        claim_version=4,
        claimed_by="dead-worker",
        claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        metadata_json={
            "tool_profile": "review_readonly",
            "coordination_publish_state": "published",
            "resume_after_restart": True,
            "resumable_delegation": True,
            "restart_replay_contract": {
                "schema": "runtime_restart_replay_contract.v1",
                "idempotency_key": f"delegation:{runtime_task_id.hex}:restart",
                "task_type": "delegation",
                "task_id": runtime_task_id.hex,
            },
        },
    )
    claimed = await RuntimeTaskClaimService(
        db=ClaimDB(),
        worker_id="recovery-worker",
        task_types=("delegation",),
        lease_seconds=60,
    ).claim_available(batch_size=1)

    assert claimed == [runtime_task]
    assert runtime_task.claim_version == 5
    assert runtime_task.metadata_json["reclaimed_expired_claim"] is True

    task_id = runtime_task.id.hex
    stale_task = asyncio.create_task(asyncio.sleep(0))
    await stale_task
    orchestrator._async_tasks[task_id] = SimpleNamespace(task=stale_task)
    record = {
        "task_id": task_id,
        "task_type": "delegation",
        "status": "running",
        "trace_id": "trace-reclaimed-readonly",
        "metadata": dict(runtime_task.metadata_json),
    }
    request = SimpleNamespace(
        trace_id="trace-reclaimed-readonly",
        session_id=str(uuid4()),
        tenant_id=runtime_task.tenant_id,
        coordination_task_key=None,
        coordination_lease_id=None,
    )
    observed_fences = []
    updates = []

    async def fake_get_runtime_task_record(_task_id):
        assert _task_id == task_id
        return record

    async def fake_build_request(_record):
        assert _record is record
        return request

    async def fake_update_runtime_task_record(_task_id, **kwargs):
        fence = current_runtime_task_fence()
        assert fence is not None
        observed_fences.append((fence.task_id, fence.claim_version, fence.worker_id))
        updates.append(kwargs)
        return True

    def fake_spawn(*, task_id, request, trace_id):
        assert task_id == runtime_task.id.hex

        async def complete():
            fence = current_runtime_task_fence()
            assert fence is not None
            observed_fences.append((fence.task_id, fence.claim_version, fence.worker_id))
            assert await orchestrator.update_runtime_task_record(
                task_id,
                status="completed",
                result_summary="reclaimed result",
            )
            return SimpleNamespace(content="reclaimed result")

        orchestrator._async_tasks[task_id] = SimpleNamespace(task=asyncio.create_task(complete()))

    monkeypatch.setattr(orchestrator, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(orchestrator, "_build_delegation_request_from_runtime_record", fake_build_request)
    monkeypatch.setattr(orchestrator, "_delegation_authority_receipt_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(orchestrator, "_spawn_async_delegation_task", fake_spawn)
    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(RUNTIME_TASK_CLAIM_LEASE_SECONDS=60),
    )

    try:
        assert worker._dispatch_claimed_task(runtime_task) is True
        wrapper = worker._DISPATCHED_TASKS[task_id][1]
        await asyncio.wait_for(wrapper, timeout=1)
    finally:
        orchestrator._async_tasks.pop(task_id, None)
        orchestrator._async_task_parent_ids.pop(task_id, None)
        orchestrator._async_task_fallback_records.pop(task_id, None)
        worker._DISPATCHED_TASKS.pop(task_id, None)

    assert [update["status"] for update in updates] == ["running", "completed"]
    assert all(item == (runtime_task.id, 5, "recovery-worker") for item in observed_fences)


def test_terminal_boundary_required_types_exactly_match_live_consumers():
    import app.services.runtime_task_worker as worker
    from app.models.runtime_task import TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES, TERMINAL_BOUNDARY_PENDING_SQL

    consumed = (*worker.EXECUTABLE_CHAT_TASK_TYPES, *worker.DIRECT_INVOCATION_TASK_TYPES)

    assert consumed == TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES
    for task_type in consumed:
        assert f"'{task_type}'" in TERMINAL_BOUNDARY_PENDING_SQL
    for unsupported in ("workflow", "subagent", "approval_execution", "hr_provisioning", "dream"):
        assert f"'{unsupported}'" not in TERMINAL_BOUNDARY_PENDING_SQL


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
    assert limits["a2a_continuation"] >= 8


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


def test_worker_dispatches_a2a_continuation_to_web_chat_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(run_id, *, claim_version, worker_id):
        captured["run_id"] = run_id
        captured["claim_version"] = claim_version
        captured["worker_id"] = worker_id
        return True

    task = SimpleNamespace(id=uuid4(), task_type="a2a_continuation", claim_version=3, claimed_by="worker-9")
    monkeypatch.setattr(worker, "dispatch_web_chat_run", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {"run_id": task.id, "claim_version": 3, "worker_id": "worker-9"}


def test_worker_a2a_continuation_shares_web_chat_capacity(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS="a2a_continuation=4,web_chat_turn=4",
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=8,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 4)

    assert worker._task_type_capacity_remaining("a2a_continuation") == 0
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)
    assert worker._task_type_capacity_remaining("a2a_continuation") == 2


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
        return SimpleNamespace(committed=True, terminal_transitioned=False)

    async def fake_execute_task(
        _business_task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
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

    async def fake_execute_task(
        _task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event: asyncio.Event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
        assert requester_user_id == requester_id
        assert cancel_event is expected_cancel_event
        return TaskExecutionOutcome(status=TaskExecutionStatus.CANCELLED, summary="cancelled")

    async def fake_finalize(*, runtime_task_id, outcome):
        finalized.append(outcome)
        return SimpleNamespace(committed=True, terminal_transitioned=False)

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
async def test_business_task_terminal_sidecar_is_not_run_inline_after_atomic_finalizer(monkeypatch):
    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    tenant_id = uuid4()
    reflection_session_id = uuid4()
    expected_runtime_task_id = runtime_task_id
    order = []
    emitted = []

    async def fake_mark_started(*, runtime_task_id):
        return business_task_id, agent_id, requester_id

    async def fake_execute_task(
        _task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
        return TaskExecutionOutcome(
            status=TaskExecutionStatus.SUCCEEDED,
            summary="completed",
            result="durable result",
            reflection_session_id=str(reflection_session_id),
        )

    async def fake_finalize(*, runtime_task_id, outcome):
        order.append("finalize_committed")
        return SimpleNamespace(
            committed=True,
            terminal_transitioned=True,
            runtime_task_id=runtime_task_id,
            business_task_id=business_task_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            reflection_session_id=outcome.reflection_session_id,
            terminal_status=outcome.runtime_status,
        )

    async def fake_emit_hook(event, **kwargs):
        order.append("turn_stop")
        emitted.append((event, kwargs))

    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    await worker._execute_claimed_business_task(runtime_task_id)

    assert order == ["finalize_committed"]
    assert emitted == []


@pytest.mark.asyncio
async def test_business_task_terminal_replay_does_not_emit_turn_stop_again(monkeypatch):
    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    expected_runtime_task_id = runtime_task_id
    emitted = []

    async def fake_mark_started(*, runtime_task_id):
        return business_task_id, agent_id, requester_id

    async def fake_execute_task(
        _task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
        return TaskExecutionOutcome(
            status=TaskExecutionStatus.SUCCEEDED,
            summary="already committed",
            reflection_session_id=str(uuid4()),
        )

    async def fake_finalize(*, runtime_task_id, outcome):
        return SimpleNamespace(
            committed=True,
            terminal_transitioned=False,
            runtime_task_id=runtime_task_id,
            business_task_id=business_task_id,
            agent_id=agent_id,
            tenant_id=uuid4(),
            reflection_session_id=outcome.reflection_session_id,
        )

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    await worker._execute_claimed_business_task(runtime_task_id)

    assert emitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finalizer_failure", ["missing", "raise"])
async def test_business_task_finalizer_failure_never_emits_turn_stop(monkeypatch, finalizer_failure):
    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    expected_runtime_task_id = runtime_task_id
    emitted = []
    reconciliation_updates = []

    async def fake_mark_started(*, runtime_task_id):
        return business_task_id, agent_id, requester_id

    async def fake_execute_task(
        _task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
        return TaskExecutionOutcome(
            status=TaskExecutionStatus.SUCCEEDED,
            summary="assistant evidence persisted",
            reflection_session_id=str(uuid4()),
        )

    async def fake_finalize(*, runtime_task_id, outcome):
        if finalizer_failure == "raise":
            raise RuntimeError("atomic commit failed")
        return None

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_update_runtime_task_record(*args, **kwargs):
        reconciliation_updates.append((args, kwargs))
        return False

    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr(
        "app.services.runtime_task_service.update_runtime_task_record",
        fake_update_runtime_task_record,
    )

    await worker._execute_claimed_business_task(runtime_task_id)

    assert emitted == []
    assert len(reconciliation_updates) == 1


@pytest.mark.asyncio
async def test_business_task_worker_does_not_run_postcommit_sidecar_inline(monkeypatch):
    import app.services.runtime_task_worker as worker
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    reflection_session_id = uuid4()
    expected_runtime_task_id = runtime_task_id
    reconciliation_updates = []

    async def fake_mark_started(*, runtime_task_id):
        return business_task_id, agent_id, requester_id

    async def fake_execute_task(
        _task_id,
        _agent_id,
        *,
        requester_user_id,
        cancel_event,
        runtime_task_id,
    ):
        assert runtime_task_id == expected_runtime_task_id
        return TaskExecutionOutcome(
            status=TaskExecutionStatus.SUCCEEDED,
            summary="committed",
            reflection_session_id=str(reflection_session_id),
        )

    async def fake_finalize(*, runtime_task_id, outcome):
        return SimpleNamespace(
            committed=True,
            terminal_transitioned=True,
            runtime_task_id=runtime_task_id,
            business_task_id=business_task_id,
            agent_id=agent_id,
            tenant_id=uuid4(),
            reflection_session_id=outcome.reflection_session_id,
            terminal_status=outcome.runtime_status,
        )

    async def failed_emit_hook(event, **kwargs):
        raise OSError("T0 filesystem temporarily unavailable")

    async def fake_update_runtime_task_record(*args, **kwargs):
        reconciliation_updates.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "app.services.business_task_runtime.mark_business_task_execution_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "app.services.business_task_runtime.finalize_business_task_execution",
        fake_finalize,
    )
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", failed_emit_hook)
    monkeypatch.setattr(
        "app.services.runtime_task_service.update_runtime_task_record",
        fake_update_runtime_task_record,
    )

    await worker._execute_claimed_business_task(runtime_task_id)

    assert reconciliation_updates == []


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
async def test_web_terminal_boundary_lane_is_cross_tenant_scoped_and_web_only(
    monkeypatch,
    owner_sessionmaker,
):
    import app.services.runtime_task_worker as worker
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.tenant import Tenant
    from app.models.user import User

    now = datetime.now(UTC)
    fixture_epoch = datetime(2000, 1, 1, tzinfo=UTC)
    tenant_ids = [uuid4(), uuid4()]
    web_task_ids = [uuid4(), uuid4(), uuid4()]
    non_web_task_id = uuid4()
    agent_ids = [uuid4(), uuid4()]
    session_ids = [uuid4(), uuid4(), uuid4(), uuid4()]

    async with owner_sessionmaker() as db:
        for index, tenant_id in enumerate(tenant_ids):
            user_id = uuid4()
            db.add(Tenant(id=tenant_id, name=f"Worker Tenant {index}", slug=f"worker-{tenant_id.hex[:12]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"worker-{user_id.hex[:12]}",
                    email=f"worker-{user_id.hex[:12]}@test.local",
                    password_hash="x",
                    display_name="Worker Test Owner",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_ids[index],
                    tenant_id=tenant_id,
                    name=f"Worker Agent {index}",
                    creator_id=user_id,
                    owner_user_id=user_id,
                )
            )
            await db.flush()

            outbox_task_id = web_task_ids[index]
            outbox_task = RuntimeTask(
                id=outbox_task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_ids[index],
                parent_session_id=str(session_ids[index]),
                root_session_id=str(session_ids[index]),
                root_user_id=user_id,
                status="failed",
                completed_at=fixture_epoch - timedelta(hours=3 - index),
                created_at=fixture_epoch - timedelta(hours=3 - index),
                prompt="terminal boundary worker fixture",
                terminal_boundary_generation=1,
                terminal_boundary_enqueued_at=now - timedelta(hours=2),
            )
            db.add(outbox_task)
            await db.flush()
            terminal_event_id = uuid4()
            db.add(
                RuntimeTerminalBoundaryOutbox(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    runtime_task_id=outbox_task_id,
                    agent_id=agent_ids[index],
                    session_id=str(session_ids[index]),
                    event_kind="turn_abort",
                    terminal_status="failed",
                    authority_ref="runtime_task",
                    authority_id=str(outbox_task_id),
                    binding_json={
                        "tenant_id": str(tenant_id),
                        "runtime_task_id": str(outbox_task_id),
                        "agent_id": str(agent_ids[index]),
                        "session_id": str(session_ids[index]),
                        "authority_ref": "runtime_task",
                        "authority_id": str(outbox_task_id),
                        "terminal_event_id": str(terminal_event_id),
                        "terminal_sequence": 1,
                        "authority_sha256": "a" * 64,
                        "source_refs": [
                            {
                                "event_id": str(terminal_event_id),
                                "sequence": 1,
                                "sha256": "b" * 64,
                            }
                        ],
                    },
                    binding_sha256=("c" if index == 0 else "d") * 64,
                    idempotency_key=("e" if index == 0 else "f") * 64,
                    status="processing" if index == 0 else "pending",
                    attempt_count=1,
                    available_at=fixture_epoch - timedelta(hours=2 - index),
                    claimed_by="expired-worker" if index == 0 else None,
                    claim_token=uuid4() if index == 0 else None,
                    lease_expires_at=fixture_epoch - timedelta(hours=2) if index == 0 else None,
                    created_at=fixture_epoch - timedelta(hours=3 - index),
                )
            )

        missing_web_task = RuntimeTask(
            id=web_task_ids[2],
            tenant_id=tenant_ids[1],
            task_type="goal_continuation",
            parent_agent_id=agent_ids[1],
            parent_session_id=str(session_ids[2]),
            root_session_id=str(session_ids[2]),
            root_user_id=user_id,
            status="failed",
            completed_at=fixture_epoch - timedelta(minutes=20),
            created_at=fixture_epoch - timedelta(minutes=20),
            prompt="missing Web boundary",
            terminal_boundary_generation=1,
        )
        non_web_task = RuntimeTask(
            id=non_web_task_id,
            tenant_id=tenant_ids[1],
            task_type="business_task",
            parent_agent_id=agent_ids[1],
            parent_session_id=str(session_ids[3]),
            root_session_id=str(session_ids[3]),
            root_user_id=user_id,
            status="failed",
            completed_at=now - timedelta(minutes=30),
            created_at=now - timedelta(minutes=30),
            prompt="non-Web boundary must stay untouched",
            terminal_boundary_generation=1,
        )
        db.add_all([missing_web_task, non_web_task])
        await db.commit()

    calls = []
    processors = []
    real_service = worker.RuntimeTerminalBoundaryOutboxService

    class FakeProcessor:
        def __init__(self, *, session_factory):
            assert session_factory is owner_sessionmaker
            processors.append(self)

        async def validate(self, _db, _item):
            raise AssertionError("no real outbox item should be processed in this wiring test")

        async def __call__(self, _item):
            raise AssertionError("no real outbox item should be processed in this wiring test")

    class RecordingService(real_service):
        async def reconcile_terminal_tasks_once(self, **kwargs):
            calls.append(("reconcile", kwargs["tenant_id"]))
            assert tuple(kwargs["task_types"]) == worker.EXECUTABLE_CHAT_TASK_TYPES
            return await super().reconcile_terminal_tasks_once(**kwargs)

        async def drain_once(self, **kwargs):
            calls.append(("drain", kwargs["tenant_id"]))
            assert tuple(kwargs["task_types"]) == worker.EXECUTABLE_CHAT_TASK_TYPES
            assert kwargs["canonical_validator"].__self__ is processors[0]
            assert kwargs["process_callback"] is processors[0]
            return {"claimed": 0, "delivered": 0, "retried": 0, "dead_lettered": 0}

    built = []

    async def fake_builder(_db, task):
        built.append(task.id)
        task.terminal_boundary_enqueued_at = now
        return [SimpleNamespace(tenant_id=task.tenant_id, runtime_task_id=task.id)]

    monkeypatch.setattr(worker, "WebTerminalBoundaryProcessor", FakeProcessor)
    monkeypatch.setattr(worker, "RuntimeTerminalBoundaryOutboxService", RecordingService)

    result = await worker.drain_web_terminal_boundary_outbox_once(
        worker_id="runtime-worker",
        limit=2,
        session_factory=owner_sessionmaker,
        builder=fake_builder,
    )

    assert set(calls) == {
        ("reconcile", tenant_ids[0]),
        ("drain", tenant_ids[0]),
        ("reconcile", tenant_ids[1]),
        ("drain", tenant_ids[1]),
    }
    assert built == [web_task_ids[2]]
    assert result["tenants"] == 2
    assert result["enqueued"] == 1

    async with owner_sessionmaker() as db:
        web_task = await db.get(RuntimeTask, web_task_ids[2])
        non_web_task = await db.get(RuntimeTask, non_web_task_id)
    assert web_task is not None
    assert web_task.terminal_boundary_reconcile_attempted_at is not None
    assert web_task.terminal_boundary_reconcile_attempt_count == 1
    assert non_web_task is not None
    assert non_web_task.terminal_boundary_enqueued_at is None
    assert non_web_task.terminal_boundary_reconcile_attempted_at is None
    assert non_web_task.terminal_boundary_reconcile_attempt_count == 0


@pytest.mark.asyncio
async def test_web_terminal_boundary_builder_delegates_shared_authority_selector(monkeypatch):
    import app.services.runtime_task_worker as worker

    task_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=uuid4(),
        task_type="web_chat_turn",
        parent_agent_id=uuid4(),
        parent_session_id=str(uuid4()),
        status="completed",
    )
    db = object()
    calls = []
    row = SimpleNamespace(tenant_id=task.tenant_id, runtime_task_id=task.id)

    async def fake_select(received_db, received_task):
        calls.append((received_db, received_task))
        return row

    monkeypatch.setattr(
        "app.services.web_terminal_boundary_processor.enqueue_web_terminal_boundary_for_task",
        fake_select,
    )
    rows = await worker._build_web_terminal_boundaries(db, task)

    assert calls == [(db, task)]
    assert rows == (row,)


@pytest.mark.asyncio
async def test_web_terminal_boundary_slow_tenant_does_not_starve_another_tenant(monkeypatch):
    import asyncio

    import app.services.runtime_task_worker as worker

    first_tenant, second_tenant = uuid4(), uuid4()
    first_started = asyncio.Event()
    second_drained = asyncio.Event()
    release_first = asyncio.Event()

    async def discover(**_kwargs):
        return [first_tenant, second_tenant]

    class FakeProcessor:
        def __init__(self, **_kwargs):
            self.validate = object()

    class FakeService:
        def __init__(self, **_kwargs):
            pass

        async def reconcile_terminal_tasks_once(self, **_kwargs):
            return {"enqueued": 0, "held": 0}

        async def drain_once(self, **kwargs):
            if kwargs["tenant_id"] == first_tenant:
                first_started.set()
                await release_first.wait()
            else:
                second_drained.set()
            return {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}

    monkeypatch.setattr(worker, "_discover_terminal_boundary_tenants", discover)
    monkeypatch.setattr(worker, "WebTerminalBoundaryProcessor", FakeProcessor)
    monkeypatch.setattr(worker, "RuntimeTerminalBoundaryOutboxService", FakeService)

    draining = asyncio.create_task(worker.drain_web_terminal_boundary_outbox_once(worker_id="worker"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_drained.wait(), timeout=1)
    release_first.set()

    assert (await draining)["delivered"] == 2


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


@pytest.mark.asyncio
async def test_runtime_worker_claim_path_does_not_wait_for_slow_terminal_boundary(monkeypatch):
    import asyncio

    import app.services.runtime_task_worker as worker

    calls = []
    terminal_started = asyncio.Event()

    async def noop(*_args, **_kwargs):
        return {}

    async def listener():
        await asyncio.Event().wait()

    async def terminal_recovery(*_args, **_kwargs):
        calls.append("terminal_recovery")
        return {}

    async def terminal_boundary(*_args, **_kwargs):
        calls.append("terminal_boundary")
        terminal_started.set()
        await asyncio.Event().wait()

    async def input_dispatch(*_args, **_kwargs):
        await terminal_started.wait()
        calls.append("input_dispatch")
        raise asyncio.CancelledError

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
    for name in (
        "drain_session_event_outbox_once",
        "recover_session_control_inputs_once",
        "expire_session_permission_requests_once",
        "recover_stale_session_input_admissions_once",
        "recover_turn_replacement_sagas_once",
        "recover_session_model_rounds_once",
    ):
        monkeypatch.setattr(worker, name, noop)
    monkeypatch.setattr(worker, "recover_session_terminal_outcomes_once", terminal_recovery)
    monkeypatch.setattr(worker, "start_terminal_boundary_worker_loop", terminal_boundary)
    monkeypatch.setattr(worker, "recover_session_input_dispatches_once", input_dispatch)

    with pytest.raises(asyncio.CancelledError):
        await worker.start_runtime_task_worker_loop()

    assert calls == ["terminal_recovery", "terminal_boundary", "input_dispatch"]
