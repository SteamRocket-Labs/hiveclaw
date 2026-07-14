from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4


def _long_text(label: str, size: int = 25_000) -> str:
    return f"{label}\n" + (label[0] * size) + f"\nEND_OF_{label}"


def test_workflow_completion_summary_preserves_full_outputs() -> None:
    from app.services.workflow_runtime_service import WorkflowRuntimeService

    full_value = _long_text("WORKFLOW_OUTPUT", 5000)
    summary = WorkflowRuntimeService._completion_task_summary(
        run_id=uuid4(),
        status="completed",
        reason=None,
        outputs={"final": full_value},
    )

    assert json.loads(summary.split("Outputs: ", 1)[1])["final"] == full_value


def test_business_task_terminal_records_preserve_full_result_and_summary() -> None:
    from app.services.business_task_runtime import (
        TaskExecutionOutcome,
        TaskExecutionStatus,
        apply_business_task_outcome,
    )

    class _DB:
        def __init__(self) -> None:
            self.added = []

        def add(self, value) -> None:
            self.added.append(value)

    runtime_id = uuid4()
    task_id = uuid4()
    full_summary = _long_text("BUSINESS_SUMMARY")
    full_result = _long_text("BUSINESS_RESULT")
    task = SimpleNamespace(
        id=task_id,
        tenant_id=uuid4(),
        active_runtime_task_id=runtime_id,
        status="doing",
        last_execution_status="running",
        last_error=None,
        last_result=None,
        completed_at=None,
    )
    runtime_task = SimpleNamespace(
        id=runtime_id,
        status="running",
        result_summary=None,
        completed_at=None,
        metadata_json={"business_task_id": str(task_id)},
    )

    apply_business_task_outcome(
        db=_DB(),
        task=task,
        runtime_task=runtime_task,
        outcome=TaskExecutionOutcome(
            status=TaskExecutionStatus.SUCCEEDED,
            summary=full_summary,
            result=full_result,
        ),
    )

    assert task.last_result == full_result
    assert runtime_task.result_summary == full_summary


def test_dream_terminal_record_preserves_full_outcome() -> None:
    from app.services.dream_runtime import _complete_task

    full_value = _long_text("DREAM_OUTCOME")
    task = SimpleNamespace(
        status="running",
        completed_at=None,
        result_summary=None,
        claimed_by="worker",
        claim_expires_at=None,
        metadata_json={},
    )

    _complete_task(task, outcome={"result": full_value})

    assert json.loads(task.result_summary)["result"] == full_value


def test_approval_terminal_record_preserves_full_execution_result() -> None:
    from app.services.approval_execution_runtime import _set_task_terminal

    full_result = _long_text("APPROVAL_RESULT")
    task = SimpleNamespace(status="running", completed_at=None, result_summary=None, metadata_json={})

    _set_task_terminal(task, approval_status="succeeded", result=full_result)

    assert task.result_summary == full_result
    assert task.metadata_json["outcome"]["summary"] == full_result


def test_hr_failure_terminal_record_preserves_full_reason() -> None:
    from app.services.hr_provisioning_runtime import _mark_task_failed

    full_reason = _long_text("HR_FAILURE")
    task = SimpleNamespace(
        status="running",
        completed_at=None,
        result_summary=None,
        claimed_by="worker",
        claim_expires_at=None,
        metadata_json={},
    )
    draft = SimpleNamespace(
        failure_message=full_reason,
        status="failed",
        failure_code="provisioning_failed",
    )

    _mark_task_failed(task, draft, reason="fallback")

    assert task.result_summary == full_reason
    assert task.metadata_json["outcome"]["summary"] == full_reason


def test_runtime_failure_policy_preserves_full_model_visible_summary() -> None:
    from app.runtime.failure_policy import build_runtime_failure_policy

    full_message = _long_text("RUNTIME_FAILURE", 1000)

    policy = build_runtime_failure_policy(failure_kind="provider_error", message=full_message)

    assert policy["model_visible_summary"] == full_message


def test_officecli_error_preserves_full_stderr() -> None:
    from app.services.officecli_adapter import OfficeCLIExecutionError

    full_stderr = _long_text("OFFICE_STDERR", 1000)

    error = OfficeCLIExecutionError(command="preview", returncode=1, stderr=full_stderr)

    assert full_stderr in str(error)
