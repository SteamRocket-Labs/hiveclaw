from __future__ import annotations

from uuid import uuid4


def test_progress_ledger_detects_stall_and_recommends_replan(tmp_path) -> None:
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_progress,
        build_agent_progress_ledger_review,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
        render_progress_ledger_block,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="workflow_run",
        todo_items=[
            {"id": "research", "title": "Collect source evidence", "status": "in_progress", "owner": "researcher"},
            {"id": "decide", "title": "Decide final answer", "status": "pending", "owner": "lead"},
        ],
        data_root=tmp_path,
    )
    append_agent_work_ledger_progress(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Waiting for source evidence",
        data_root=tmp_path,
    )
    append_agent_work_ledger_progress(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Waiting for source evidence",
        data_root=tmp_path,
    )

    review = build_agent_progress_ledger_review(
        load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path),
        stall_threshold=2,
    )

    assert review["schema"] == "agent_progress_ledger.v2"
    assert review["request_satisfied"] is False
    assert review["stalled"] is True
    assert review["replan_advisory"] is True
    assert "stalled" in review["advisory_reasons"]
    assert "needs_replan" not in review
    assert "hard_transition" not in review
    assert "required_next_write" not in review
    assert review["next_owner"] == "researcher"
    assert "Collect source evidence" in review["next_action"]

    rendered = render_progress_ledger_block(review)
    assert "## Progress Ledger" in rendered
    assert "replan_advisory=true" in rendered
    assert "model decides" in rendered.lower()
    assert "researcher" in rendered


def test_progress_ledger_replan_event_clears_stall_until_new_stall(tmp_path) -> None:
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_finding,
        append_agent_work_ledger_progress,
        build_agent_progress_ledger_review,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="workflow_run",
        todo_items=[{"id": "research", "title": "Collect source evidence", "status": "in_progress"}],
        data_root=tmp_path,
    )
    for _ in range(2):
        append_agent_work_ledger_progress(
            agent_id=agent_id,
            runtime_task_id=runtime_task_id,
            status="running",
            delta="Waiting for source evidence",
            data_root=tmp_path,
        )
    append_agent_work_ledger_finding(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        finding_type="replan",
        summary="Switch to cached source index and ask researcher only for missing citations.",
        next_strategy="Use cached source index before another delegation attempt.",
        data_root=tmp_path,
    )

    review = build_agent_progress_ledger_review(
        load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path),
        stall_threshold=2,
    )

    assert review["stalled"] is False
    assert review["replan_advisory"] is False
    assert review["latest_replan"].startswith("Switch to cached source index")

    for _ in range(2):
        append_agent_work_ledger_progress(
            agent_id=agent_id,
            runtime_task_id=runtime_task_id,
            status="running",
            delta="Still waiting after cached source index",
            data_root=tmp_path,
        )
    next_review = build_agent_progress_ledger_review(
        load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path),
        stall_threshold=2,
    )
    assert next_review["replan_advisory"] is True


def test_progress_ledger_marks_completed_request_satisfied(tmp_path) -> None:
    from app.services.agent_work_ledger import (
        build_agent_progress_ledger_review,
        initialize_agent_work_ledger_artifact,
        load_agent_work_ledger,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="workflow_run",
        status="completed",
        todo_items=[{"id": "ship", "title": "Ship report", "status": "completed"}],
        verification=[{"id": "verify", "title": "Tests passed", "status": "completed"}],
        data_root=tmp_path,
    )

    review = build_agent_progress_ledger_review(
        load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path),
    )

    assert review["request_satisfied"] is True
    assert review["replan_advisory"] is False
