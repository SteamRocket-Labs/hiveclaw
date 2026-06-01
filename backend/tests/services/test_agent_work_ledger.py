from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


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
    assert ledger["todo_items"][0]["status"] == "completed"
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


def test_agent_work_ledger_display_view_is_chat_safe_and_counted(tmp_path):
    from app.services.agent_work_ledger import (
        append_agent_work_ledger_progress,
        initialize_agent_work_ledger_artifact,
        read_agent_work_ledger_view,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()
    initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="deep_research",
        current_phase="collect_sources",
        todo_items=[
            {"id": "todo-1", "title": "Plan research lanes", "status": "complete"},
            {"id": "todo-2", "title": "Collect and grade sources", "status": "running"},
            {"id": "todo-3", "title": "Write final report", "status": "pending"},
        ],
        verification=[
            {"id": "verify-1", "check": "Verify citations", "status": "pending"},
        ],
        findings=[{"id": "finding-1", "summary": "A verified finding", "trust": "verified"}],
        data_root=tmp_path,
    )
    append_agent_work_ledger_progress(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Collected the first source batch.",
        data_root=tmp_path,
    )

    view = read_agent_work_ledger_view(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )

    assert view is not None
    assert view["schema"] == "agent_work_ledger_view.v1"
    assert view["source"] == "deep_research"
    assert view["current_phase"] == "running"
    assert view["todo_items"][0]["status"] == "completed"
    assert view["todo_items"][1]["status"] == "in_progress"
    assert view["todo_items"][1]["title"] == "Collect and grade sources"
    assert view["todo_items"][1]["content"] == "Collect and grade sources"
    assert view["todo_items"][1]["subject"] == "Collect and grade sources"
    assert view["todo_items"][1]["activeForm"] == "Working on Collect and grade sources"
    assert view["todo_items"][1]["blocks"] == []
    assert view["todo_items"][1]["blockedBy"] == []
    assert view["verification"][0]["title"] == "Verify citations"
    assert view["counts"] == {
        "todos_total": 3,
        "todos_complete": 1,
        "todos_open": 2,
        "verification_pending": 1,
        "progress_count": 1,
        "failures_open": 0,
    }
    assert view["path"].endswith(f"runtime_artifacts/long_tasks/{runtime_task_id.hex}/work_ledger.json")


def test_agent_work_ledger_view_rejects_path_traversal_task_ids(tmp_path):
    from app.services.agent_work_ledger import read_agent_work_ledger_view

    agent_id = uuid4()
    escaped_dir = tmp_path / str(agent_id) / "runtime_artifacts" / "plans"
    escaped_dir.mkdir(parents=True)
    (escaped_dir / "secret" / "work_ledger.json").parent.mkdir(parents=True)
    (escaped_dir / "secret" / "work_ledger.json").write_text('{"schema":"agent_work_ledger.v1"}', encoding="utf-8")

    view = read_agent_work_ledger_view(
        agent_id=agent_id,
        runtime_task_id="../../plans/secret",
        data_root=tmp_path,
    )

    assert view is None


def test_agent_work_ledger_view_builds_legacy_long_task_fallback(tmp_path):
    import json

    from app.services.agent_work_ledger import read_agent_work_ledger_view

    agent_id = uuid4()
    runtime_task_id = uuid4()
    artifact_dir = tmp_path / str(agent_id) / "runtime_artifacts" / "long_tasks" / runtime_task_id.hex
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "plan.json").write_text(
        json.dumps(
            {
                "schema": "long_task_plan.v1",
                "runtime_task_id": runtime_task_id.hex,
                "spec": "Research stablecoin market structure",
                "acceptance_criteria": ["Collect sources", "Write report.md"],
                "verification_commands": ["deep_research_check({ task_id })"],
                "created_at": "2026-05-29T07:18:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "progress.jsonl").write_text(
        json.dumps(
            {
                "schema": "long_task_progress.v1",
                "runtime_task_id": runtime_task_id.hex,
                "status": "running",
                "delta": "Collected first source batch.",
                "created_at": "2026-05-29T07:19:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = read_agent_work_ledger_view(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )

    assert view is not None
    assert view["source"] == "legacy_long_task_runtime"
    assert view["runtime_task_id"] == runtime_task_id.hex
    assert view["current_phase"] == "running"
    assert [item["title"] for item in view["todo_items"]] == ["Collect sources", "Write report.md"]
    assert [item["status"] for item in view["todo_items"]] == ["in_progress", "pending"]
    assert view["verification"][0]["title"] == "deep_research_check({ task_id })"
    assert view["progress"][0]["delta"] == "Collected first source batch."
    assert view["counts"]["todos_total"] == 2
    assert view["path"].endswith(f"runtime_artifacts/long_tasks/{runtime_task_id.hex}/plan.json")


@pytest.mark.asyncio
async def test_latest_session_work_ledger_prefers_active_session_task(monkeypatch):
    from app.services import agent_work_ledger as module

    agent_id = uuid4()
    session_id = uuid4()
    older_completed = SimpleNamespace(
        id=uuid4(),
        task_type="web_chat_turn",
        status="completed",
        created_at=None,
    )
    active_task = SimpleNamespace(
        id=uuid4(),
        task_type="delegation",
        status="running",
        created_at=None,
    )

    class _Scalars:
        def all(self):
            return [older_completed, active_task]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        async def execute(self, _stmt):
            return _Result()

    def fake_read_agent_work_ledger_view(*, agent_id, runtime_task_id, data_root=None):
        if runtime_task_id == active_task.id:
            return {
                "schema": "agent_work_ledger_view.v1",
                "runtime_task_id": active_task.id.hex,
                "status": "running",
                "todo_items": [{"id": "todo-1", "title": "Run active todo", "status": "running"}],
            }
        return None

    monkeypatch.setattr(module, "read_agent_work_ledger_view", fake_read_agent_work_ledger_view)

    view = await module.read_latest_session_work_ledger_view(
        db=_DB(),
        agent_id=agent_id,
        session_id=session_id,
    )

    assert view is not None
    assert view["runtime_task_id"] == active_task.id.hex
    assert view["session_id"] == str(session_id)
    assert view["task_type"] == "delegation"


@pytest.mark.asyncio
async def test_latest_session_work_ledger_does_not_fallback_to_old_ledger_when_active_task_has_none(monkeypatch):
    from app.services import agent_work_ledger as module

    agent_id = uuid4()
    session_id = uuid4()
    older_completed = SimpleNamespace(
        id=uuid4(),
        task_type="deep_research",
        status="completed",
        created_at=None,
    )
    active_task = SimpleNamespace(
        id=uuid4(),
        task_type="web_chat_turn",
        status="running",
        created_at=None,
    )

    class _Scalars:
        def all(self):
            return [older_completed, active_task]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        async def execute(self, _stmt):
            return _Result()

    def fake_read_agent_work_ledger_view(*, agent_id, runtime_task_id, data_root=None):
        if runtime_task_id == older_completed.id:
            return {
                "schema": "agent_work_ledger_view.v1",
                "runtime_task_id": older_completed.id.hex,
                "status": "completed",
                "todo_items": [{"id": "todo-old", "title": "Old completed todo", "status": "complete"}],
            }
        return None

    monkeypatch.setattr(module, "read_agent_work_ledger_view", fake_read_agent_work_ledger_view)

    view = await module.read_latest_session_work_ledger_view(
        db=_DB(),
        agent_id=agent_id,
        session_id=session_id,
    )

    assert view is None
