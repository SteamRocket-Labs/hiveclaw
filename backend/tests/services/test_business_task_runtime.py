from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid


class _FakeDb:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.records: dict[uuid.UUID, object] = {}

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, _model: object, record_id: uuid.UUID, **_kwargs: object) -> object | None:
        return self.records.get(record_id)


def test_task_execution_outcome_has_one_total_terminal_mapping() -> None:
    from app.services.business_task_runtime import TaskExecutionOutcome, TaskExecutionStatus

    expected = {
        TaskExecutionStatus.SUCCEEDED: ("done", "completed"),
        TaskExecutionStatus.BLOCKED: ("blocked", "skipped"),
        TaskExecutionStatus.FAILED: ("failed", "failed"),
        TaskExecutionStatus.CANCELLED: ("cancelled", "killed"),
        TaskExecutionStatus.NEEDS_RECONCILIATION: ("needs_reconciliation", "needs_reconciliation"),
    }
    for status, (task_status, runtime_status) in expected.items():
        outcome = TaskExecutionOutcome(status=status, summary=f"{status.value} summary")
        assert outcome.task_status == task_status
        assert outcome.runtime_status == runtime_status
        assert outcome.is_success is (status is TaskExecutionStatus.SUCCEEDED)


async def test_stage_business_task_runtime_uses_caller_transaction_and_links_exact_principals() -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services.business_task_runtime import stage_business_task_runtime

    db = _FakeDb()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    requester_id = uuid.uuid4()
    root_session_id = uuid.uuid4()
    delivery_target = {"channel": "telegram", "chat_id": "chat-1"}
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        description="prepare report",
        execution_attempt=0,
        active_runtime_task_id=None,
        last_execution_status=None,
        status="pending",
    )

    runtime_task = await stage_business_task_runtime(
        db=db,  # type: ignore[arg-type]
        task=task,
        requester_user_id=requester_id,
        agent_name="Research Agent",
        request_id="request-1",
        root_session_id=root_session_id,
        delivery_target=delivery_target,
    )

    assert isinstance(runtime_task, RuntimeTask)
    assert runtime_task in db.added
    assert db.commits == 0
    assert runtime_task.tenant_id == tenant_id
    assert runtime_task.parent_agent_id == agent_id
    assert runtime_task.child_agent_id == agent_id
    assert runtime_task.metadata_json["business_task_id"] == str(task.id)
    assert runtime_task.metadata_json["requester_user_id"] == str(requester_id)
    assert runtime_task.parent_session_id == str(root_session_id)
    assert runtime_task.metadata_json["root_session_id"] == str(root_session_id)
    assert runtime_task.metadata_json["delivery_target"] == delivery_target
    assert runtime_task.metadata_json["phase"] == "queued"
    assert task.active_runtime_task_id == runtime_task.id
    assert task.execution_attempt == 1
    assert task.last_execution_status == "queued"


async def test_stage_business_task_runtime_rejects_a_second_active_run() -> None:
    import pytest

    from app.services.business_task_runtime import (
        BusinessTaskInvariantError,
        stage_business_task_runtime,
    )

    db = _FakeDb()
    active_runtime_id = uuid.uuid4()
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    db.records[active_runtime_id] = SimpleNamespace(
        id=active_runtime_id,
        status="running",
        task_type="business_task",
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        metadata_json={"business_task_id": str(task_id)},
    )
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        description="prepare report",
        execution_attempt=1,
        active_runtime_task_id=active_runtime_id,
        last_execution_status="running",
        status="doing",
    )

    with pytest.raises(BusinessTaskInvariantError, match="already has an active run"):
        await stage_business_task_runtime(
            db=db,  # type: ignore[arg-type]
            task=task,
            requester_user_id=uuid.uuid4(),
            agent_name="Research Agent",
            request_id="request-2",
        )

    assert db.added == []


async def test_stage_business_task_runtime_rejects_a_foreign_terminal_pointer() -> None:
    import pytest

    from app.services.business_task_runtime import (
        BusinessTaskInvariantError,
        stage_business_task_runtime,
    )

    db = _FakeDb()
    active_runtime_id = uuid.uuid4()
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        description="prepare report",
        execution_attempt=1,
        active_runtime_task_id=active_runtime_id,
        last_execution_status="failed",
        status="failed",
    )
    db.records[active_runtime_id] = SimpleNamespace(
        id=active_runtime_id,
        status="failed",
        task_type="business_task",
        tenant_id=task.tenant_id,
        parent_agent_id=task.agent_id,
        metadata_json={"business_task_id": str(uuid.uuid4())},
    )

    with pytest.raises(BusinessTaskInvariantError, match="does not belong"):
        await stage_business_task_runtime(
            db=db,  # type: ignore[arg-type]
            task=task,
            requester_user_id=uuid.uuid4(),
            agent_name="Research Agent",
            request_id="request-2",
        )

    assert db.added == []


def test_finalize_business_task_execution_mutates_both_records_in_one_unit() -> None:
    from app.models.task import TaskLog
    from app.services.business_task_runtime import (
        TaskExecutionOutcome,
        TaskExecutionStatus,
        apply_business_task_outcome,
    )

    db = _FakeDb()
    runtime_task_id = uuid.uuid4()
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        active_runtime_task_id=runtime_task_id,
        status="doing",
        last_execution_status="running",
        last_error=None,
        last_result=None,
        completed_at=None,
    )
    runtime_task = SimpleNamespace(
        id=runtime_task_id,
        status="running",
        result_summary=None,
        completed_at=None,
        metadata_json={"business_task_id": str(task_id), "phase": "invoking"},
    )
    completed_at = datetime.now(timezone.utc)
    outcome = TaskExecutionOutcome(
        status=TaskExecutionStatus.FAILED,
        summary="provider timed out",
        error_code="provider_timeout",
        retryable=True,
    )

    apply_business_task_outcome(
        db=db,  # type: ignore[arg-type]
        task=task,
        runtime_task=runtime_task,
        outcome=outcome,
        completed_at=completed_at,
    )

    assert task.status == "failed"
    assert task.last_execution_status == "failed"
    assert task.last_error == "provider_timeout: provider timed out"
    assert task.completed_at == completed_at
    assert runtime_task.status == "failed"
    assert runtime_task.completed_at == completed_at
    assert runtime_task.metadata_json["phase"] == "terminal"
    assert runtime_task.metadata_json["outcome"]["status"] == "failed"
    assert any(isinstance(item, TaskLog) and item.task_id == task_id for item in db.added)


def test_business_task_request_key_is_canonical_and_principal_bound() -> None:
    from app.services.business_task_runtime import business_task_request_key

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    payload_a = {"title": "Report", "description": "Q3", "priority": "high"}
    payload_b = {"priority": "high", "description": "Q3", "title": "Report"}

    first = business_task_request_key(
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_user_id=user_id,
        action="create",
        payload=payload_a,
    )
    assert first == business_task_request_key(
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_user_id=user_id,
        action="create",
        payload=payload_b,
    )
    assert first != business_task_request_key(
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_user_id=uuid.uuid4(),
        action="create",
        payload=payload_a,
    )


def _business_task_pair(*, task_status: str, runtime_status: str, phase: str, retryable: bool = False):
    runtime_id = uuid.uuid4()
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        title="Prepare board report",
        description="Use verified figures",
        type="todo",
        priority="high",
        due_date=None,
        status=task_status,
        active_runtime_task_id=runtime_id,
        execution_attempt=1,
        last_execution_status=runtime_status,
        last_error=None,
        last_result=None,
        completed_at=None,
        plan_authorization={"lease_id": "lease-1"},
    )
    runtime = SimpleNamespace(
        id=runtime_id,
        task_type="business_task",
        tenant_id=tenant_id,
        parent_agent_id=task.agent_id,
        status=runtime_status,
        result_summary=None,
        completed_at=None,
        claim_version=1,
        claim_expires_at=None,
        metadata_json={
            "business_task_id": str(task_id),
            "request_id": "runtime-request-1",
            "phase": phase,
            "outcome": {
                "status": runtime_status,
                "summary": "current outcome",
                "retryable": retryable,
            },
        },
    )
    return task, runtime


def test_business_task_projection_is_the_only_ui_state_machine() -> None:
    from app.services.business_task_runtime import project_business_task

    task, runtime = _business_task_pair(task_status="doing", runtime_status="running", phase="invoking")

    projection = project_business_task(task=task, runtime_task=runtime)

    assert projection["runtime_task_id"] == str(runtime.id)
    assert projection["runtime_status"] == "running"
    assert projection["runtime_phase"] == "invoking"
    assert projection["runtime_request_id"] == "runtime-request-1"
    assert projection["actions"] == {"can_cancel": True, "can_retry": False, "can_reconcile": False}
    assert projection["dependencies"] == [
        {"id": "confirmed_plan", "label": "Confirmed execution plan", "status": "satisfied"},
        {"id": "runtime_intent", "label": "Durable runtime intent", "status": "satisfied"},
    ]
    assert [(stage["id"], stage["status"]) for stage in projection["stages"]] == [
        ("accepted", "complete"),
        ("authorized", "complete"),
        ("queued", "complete"),
        ("executing", "current"),
        ("terminal", "pending"),
    ]


def test_business_task_projection_opens_retry_only_for_retryable_terminal_outcome() -> None:
    from app.services.business_task_runtime import project_business_task

    task, runtime = _business_task_pair(
        task_status="failed",
        runtime_status="failed",
        phase="terminal",
        retryable=True,
    )

    projection = project_business_task(task=task, runtime_task=runtime)

    assert projection["recovery_state"] == "retry_available"
    assert projection["actions"] == {"can_cancel": False, "can_retry": True, "can_reconcile": False}
    assert projection["stages"][-1]["status"] == "failed"


def test_business_task_cancel_is_safe_before_execution_and_retryable() -> None:
    from app.services.business_task_runtime import apply_business_task_cancellation

    db = _FakeDb()
    task, runtime = _business_task_pair(task_status="pending", runtime_status="pending", phase="queued")
    user_id = uuid.uuid4()

    outcome = apply_business_task_cancellation(
        db=db,  # type: ignore[arg-type]
        task=task,
        runtime_task=runtime,
        cancelled_by_user_id=user_id,
        reason="Priority changed",
        completed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    assert outcome.status.value == "cancelled"
    assert outcome.retryable is True
    assert task.status == "cancelled"
    assert runtime.status == "killed"
    assert runtime.claim_version == 2
    assert runtime.metadata_json["cancelled_by_user_id"] == str(user_id)
    assert runtime.metadata_json["cancellation_safety"] == "not_started"
    assert any(getattr(item, "task_id", None) == task.id for item in db.added)


def test_business_task_cancel_while_running_requires_side_effect_reconciliation() -> None:
    from app.services.business_task_runtime import apply_business_task_cancellation, project_business_task

    db = _FakeDb()
    task, runtime = _business_task_pair(task_status="doing", runtime_status="running", phase="invoking")

    outcome = apply_business_task_cancellation(
        db=db,  # type: ignore[arg-type]
        task=task,
        runtime_task=runtime,
        cancelled_by_user_id=uuid.uuid4(),
        reason="Stop now",
        completed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    assert outcome.status.value == "needs_reconciliation"
    assert outcome.retryable is False
    assert task.status == "needs_reconciliation"
    assert runtime.status == "needs_reconciliation"
    projection = project_business_task(task=task, runtime_task=runtime)
    assert projection["recovery_state"] == "needs_review"
    assert projection["actions"] == {"can_cancel": False, "can_retry": False, "can_reconcile": True}


def test_terminal_business_task_cannot_reenter_model_execution() -> None:
    import pytest

    from app.services.business_task_runtime import (
        BusinessTaskExecutionSuperseded,
        _assert_business_task_execution_startable,
    )

    task, runtime = _business_task_pair(
        task_status="needs_reconciliation",
        runtime_status="needs_reconciliation",
        phase="terminal",
    )

    with pytest.raises(BusinessTaskExecutionSuperseded):
        _assert_business_task_execution_startable(task=task, runtime_task=runtime)


def test_business_task_reconciliation_requires_reason_before_retry() -> None:
    import pytest

    from app.services.business_task_runtime import reconcile_business_task, project_business_task

    db = _FakeDb()
    task, runtime = _business_task_pair(
        task_status="needs_reconciliation",
        runtime_status="needs_reconciliation",
        phase="terminal",
    )

    with pytest.raises(ValueError, match="reason"):
        reconcile_business_task(
            db=db,  # type: ignore[arg-type]
            task=task,
            runtime_task=runtime,
            resolved_by_user_id=uuid.uuid4(),
            decision="retry_safe",
            reason="",
        )

    user_id = uuid.uuid4()
    reconcile_business_task(
        db=db,  # type: ignore[arg-type]
        task=task,
        runtime_task=runtime,
        resolved_by_user_id=user_id,
        decision="retry_safe",
        reason="Reviewed the external system; no action was committed.",
        resolved_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    assert task.status == "failed"
    assert task.last_execution_status == "reconciled_retry_safe"
    assert runtime.metadata_json["reconciliation"]["resolved_by_user_id"] == str(user_id)
    assert project_business_task(task=task, runtime_task=runtime)["actions"]["can_retry"] is True


def test_expired_running_business_task_is_never_automatically_replayed() -> None:
    from app.services.business_task_runtime import quarantine_stale_business_task

    db = _FakeDb()
    task, runtime = _business_task_pair(task_status="doing", runtime_status="running", phase="invoking")

    quarantine_stale_business_task(
        db=db,  # type: ignore[arg-type]
        task=task,
        runtime_task=runtime,
        detected_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    assert task.status == "needs_reconciliation"
    assert runtime.status == "needs_reconciliation"
    assert runtime.metadata_json["outcome"]["error_code"] == "worker_lease_expired"
    assert runtime.metadata_json["outcome"]["retryable"] is False
