from __future__ import annotations

from uuid import uuid4

import pytest


def test_validate_long_task_run_passes_for_complete_artifacts(tmp_path):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact
    from app.services.long_task_validation import validate_long_task_run

    agent_id = uuid4()
    runtime_task_id = uuid4()
    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Ship a long investigation",
        acceptance_criteria=["report exists", "tests pass"],
        verification_commands=["pytest tests/services/test_long_task_validation.py"],
        risk_gates=["do not publish externally"],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Report completed and verified",
        output_paths=["workspace/report.md"],
        completed_todo_ids=["acceptance-1", "acceptance-2"],
        completed_verification_ids=["verify-1"],
        data_root=tmp_path,
    )

    report = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "completed", "metadata": {}},
        data_root=tmp_path,
        write_report=True,
    )

    assert report["schema"] == "long_task_validation_report.v1"
    assert report["passed"] is True
    assert report["summary"]["fail"] == 0
    assert report["resume_context"]["progress_count"] == 1
    assert report["resume_context"]["work_ledger"]["schema"] == "agent_work_ledger_resume.v1"
    assert report["resume_context"]["work_ledger"]["open_required_todos"] == []
    assert report["report_artifact"]["path"].endswith("/validation_report.json")
    ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path)
    assert ledger is not None
    assert ledger["schema"] == "agent_work_ledger.v1"
    assert all(item["status"] == "completed" for item in ledger["todo_items"])


def test_validate_long_task_run_flags_false_completion_without_evidence(tmp_path):
    from app.services.long_task_runtime import append_long_task_progress_artifact
    from app.services.long_task_validation import validate_long_task_run

    agent_id = uuid4()
    runtime_task_id = uuid4()
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="I think this is basically done",
        data_root=tmp_path,
    )

    report = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "completed", "result": "done", "metadata": {}},
        data_root=tmp_path,
    )

    failed_ids = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert report["passed"] is False
    assert "plan_artifact_present" in failed_ids
    assert "terminal_status_matches_progress" in failed_ids
    assert "completed_has_output_or_verification" in failed_ids


def test_validate_long_task_run_fails_terminal_completion_with_pending_work_ledger(tmp_path):
    from app.services.agent_work_ledger import initialize_agent_work_ledger_artifact
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact
    from app.services.long_task_validation import validate_long_task_run

    agent_id = uuid4()
    runtime_task_id = uuid4()
    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Ship a long investigation",
        acceptance_criteria=["report exists"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="long_task_runtime",
        current_phase="execute",
        todo_items=[{"id": "todo-open", "title": "Write report", "status": "pending", "required": True}],
        verification=[{"id": "verify-open", "check": "Run pytest", "status": "pending", "required": True}],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Terminal status was claimed without closing ledger items.",
        output_paths=["workspace/report.md"],
        auto_complete_ledger=False,
        data_root=tmp_path,
    )

    report = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "completed", "metadata": {}},
        data_root=tmp_path,
    )

    failed_ids = {check["id"] for check in report["checks"] if check["status"] == "fail"}
    assert "ledger_required_todos_complete" in failed_ids
    assert "ledger_verification_complete" in failed_ids


def test_validate_long_task_run_requires_reason_for_cancel_or_missed_policy(tmp_path):
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact
    from app.services.long_task_validation import validate_long_task_run

    agent_id = uuid4()
    runtime_task_id = uuid4()
    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Run a cancellable task",
        acceptance_criteria=["reason recorded"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Started",
        data_root=tmp_path,
    )

    missing_reason = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "skipped", "metadata": {}},
        data_root=tmp_path,
    )
    assert "terminal_reason_present" in {check["id"] for check in missing_reason["checks"] if check["status"] == "fail"}

    with_reason = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "skipped", "metadata": {"skip_reason": "missed window expired"}},
        data_root=tmp_path,
    )
    terminal_reason = next(check for check in with_reason["checks"] if check["id"] == "terminal_reason_present")
    assert terminal_reason["status"] == "pass"


@pytest.mark.asyncio
async def test_record_long_task_validation_updates_runtime_task_metadata(monkeypatch, tmp_path):
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact
    from app.services import long_task_validation

    calls = []

    async def fake_update(task_id, **fields):
        calls.append((task_id, fields))
        return True

    monkeypatch.setattr(long_task_validation, "update_runtime_task_record", fake_update)

    agent_id = uuid4()
    runtime_task_id = uuid4()
    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Validate update",
        acceptance_criteria=["metadata updated"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )
    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Done",
        output_paths=["workspace/output.md"],
        completed_todo_ids=["acceptance-1"],
        completed_verification_ids=["verify-1"],
        data_root=tmp_path,
    )

    report = await long_task_validation.record_long_task_validation(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task={"status": "completed", "metadata": {}},
        data_root=tmp_path,
    )

    assert report["passed"] is True
    assert calls[0][0] == runtime_task_id.hex
    metadata = calls[0][1]["metadata_json"]
    assert metadata["long_task_validation"]["path"].endswith("/validation_report.json")
    assert metadata["long_task_validation_passed"] is True
