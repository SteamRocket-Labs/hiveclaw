from __future__ import annotations

from uuid import uuid4


def test_agent_work_ledger_artifact_round_trip_and_resume_summary(tmp_path):
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_progress,
        build_agent_work_ledger_resume_summary,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
    )

    agent_id = uuid4()
    plan_id = uuid4()
    runtime_task_id = uuid4()

    artifact = initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        source="plan_mode_planner",
        current_phase="inspect_current_state",
        todo_items=[
            {"id": "todo-1", "title": "Inspect current Plan Mode implementation", "status": "pending"},
            {"id": "todo-2", "title": "Draft user-confirmable plan", "status": "pending"},
        ],
        findings=[{"id": "finding-1", "summary": "Plan and execution ledger are separate.", "trust": "verified"}],
        verification=[{"id": "verify-1", "check": "planner output validates", "status": "pending"}],
        data_root=tmp_path,
    )

    assert artifact["schema"] == "agent_work_ledger.v1"
    assert artifact["path"].endswith(f"runtime_artifacts/long_tasks/{runtime_task_id.hex}/work_ledger.json")

    append_agent_work_ledger_progress(
        agent_id=agent_id,
        plan_id=plan_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Inspected PlanModeService and planner prompt.",
        completed_todo_ids=["todo-1"],
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path)
    assert ledger is not None
    assert ledger["schema"] == "agent_work_ledger.v1"
    assert ledger["current_phase"] == "running"
    assert ledger["todo_items"][0]["status"] == "complete"
    assert ledger["todo_items"][1]["status"] == "pending"
    assert ledger["progress"][-1]["delta"] == "Inspected PlanModeService and planner prompt."

    summary = build_agent_work_ledger_resume_summary(ledger)
    assert summary["current_phase"] == "running"
    assert summary["open_required_todos"] == ["Draft user-confirmable plan"]
    assert summary["verified_findings"] == ["Plan and execution ledger are separate."]
    assert summary["progress_count"] == 1


def test_agent_work_ledger_completion_checks_pending_todos_and_failures(tmp_path):
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_progress,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
        validate_agent_work_ledger_completion,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="long_task_runtime",
        current_phase="execute",
        todo_items=[{"id": "todo-1", "title": "Write final report", "status": "pending", "required": True}],
        verification=[{"id": "verify-1", "check": "pytest passes", "status": "pending", "required": True}],
        data_root=tmp_path,
    )

    pending_ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path)
    pending_checks = validate_agent_work_ledger_completion(pending_ledger, terminal_status="completed")
    pending_failed = {check["id"] for check in pending_checks if check["status"] == "fail"}
    assert "ledger_required_todos_complete" in pending_failed
    assert "ledger_verification_complete" in pending_failed

    append_agent_work_ledger_progress(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="Report written and tests passed.",
        completed_todo_ids=["todo-1"],
        completed_verification_ids=["verify-1"],
        data_root=tmp_path,
    )

    complete_ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path)
    complete_checks = validate_agent_work_ledger_completion(complete_ledger, terminal_status="completed")
    assert {check["status"] for check in complete_checks} == {"pass"}
