from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _task(*, status: str = "failed") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        task_type="workflow",
        status=status,
        started_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 10, 8, 5, tzinfo=UTC),
        scheduled_at=datetime(2026, 7, 10, 8, 6, tzinfo=UTC),
        claimed_by="old-worker",
        claim_expires_at=datetime(2026, 7, 10, 8, 7, tzinfo=UTC),
        metadata_json={
            "dynamic_workflow": {
                "repair_attempts": 1,
                "repair_plan": {"repairable": True},
            }
        },
    )


def test_queue_repair_reopens_same_workflow_task_and_increments_attempt() -> None:
    from app.services.workflow_user_control import queue_workflow_resume_record

    task = _task()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

    queue_workflow_resume_record(
        task,
        request_kind="repair",
        actor_user_id=uuid4(),
        now=now,
    )

    assert task.status == "pending"
    assert task.completed_at is None
    assert task.scheduled_at is None
    assert task.claimed_by is None
    assert task.claim_expires_at is None
    assert task.metadata_json["dynamic_workflow"]["repair_attempts"] == 2
    assert task.metadata_json["workflow_resume_request"]["kind"] == "repair"
    assert task.metadata_json["workflow_resume_request"]["requested_at"] == now.isoformat()


def test_queue_gate_decision_records_exact_step_without_counting_repair() -> None:
    from app.services.workflow_user_control import queue_workflow_resume_record

    task = _task(status="suspended")

    queue_workflow_resume_record(
        task,
        request_kind="gate_approved",
        actor_user_id=uuid4(),
        details={"step_id": "approve-send", "decision": "approve"},
    )

    assert task.status == "pending"
    assert task.metadata_json["dynamic_workflow"]["repair_attempts"] == 1
    assert task.metadata_json["workflow_resume_request"]["step_id"] == "approve-send"
    assert task.metadata_json["workflow_resume_request"]["decision"] == "approve"


def test_queue_repair_rejects_non_workflow_or_non_terminal_task() -> None:
    from app.services.workflow_user_control import queue_workflow_resume_record

    task = _task(status="running")
    with pytest.raises(ValueError, match="cannot be queued"):
        queue_workflow_resume_record(task, request_kind="repair", actor_user_id=uuid4())

    task = _task()
    task.task_type = "subagent"
    with pytest.raises(ValueError, match="workflow RuntimeTask"):
        queue_workflow_resume_record(task, request_kind="repair", actor_user_id=uuid4())
