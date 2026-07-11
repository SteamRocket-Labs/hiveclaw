from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_tasks_api_stages_task_and_runtime_task_before_the_only_commit() -> None:
    source = (APP_ROOT / "api/tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    create_task = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_task"
    )
    calls = [node for node in ast.walk(create_task) if isinstance(node, ast.Call)]

    assert "create_runtime_task_record" not in source
    assert "_enqueue_business_task_execution" not in source
    assert any(isinstance(call.func, ast.Name) and call.func.id == "stage_business_task_runtime" for call in calls)
    commit_calls = [call for call in calls if isinstance(call.func, ast.Attribute) and call.func.attr == "commit"]
    assert len(commit_calls) == 1

    trigger_task = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "trigger_task"
    )
    trigger_source = ast.get_source_segment(source, trigger_task) or ""
    assert ".with_for_update()" in trigger_source


def test_business_task_worker_never_unconditionally_marks_completed() -> None:
    source = (APP_ROOT / "services/runtime_task_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    worker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_claimed_business_task"
    )
    worker_source = ast.get_source_segment(source, worker) or ""
    assert 'status="completed"' not in worker_source
    assert "finalize_business_task_execution" in worker_source


def test_task_log_routes_bind_task_id_to_path_agent() -> None:
    source = (APP_ROOT / "api/tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for function_name in ("get_task_logs", "add_task_log"):
        function = next(
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
        )
        segment = ast.get_source_segment(source, function) or ""
        assert "Task.id == task_id" in segment
        assert "Task.agent_id == agent_id" in segment


def test_business_task_status_has_no_rest_or_retired_crud_bypass() -> None:
    from app.schemas.schemas import TaskUpdate

    assert "status" not in TaskUpdate.model_fields
    retired_crud = (APP_ROOT / "services" / "agent_tool_domains" / "tasks.py").read_text(encoding="utf-8")
    assert "active_runtime_task_id" in retired_crud
    assert "linked execution task is read-only" in retired_crud


def test_business_task_api_recovers_unique_request_races() -> None:
    source = (APP_ROOT / "api" / "tasks.py").read_text(encoding="utf-8")

    assert "except IntegrityError" in source
    assert source.count("await db.rollback()") >= 2
    assert "_load_matching_task_request" in source
    assert "_load_matching_runtime_request" in source


def test_business_task_identity_cannot_fall_back_to_random_model_defaults() -> None:
    from app.models.task import Task

    assert Task.__table__.c.request_id.default is None
    assert Task.__table__.c.request_hash.default is None
    active_runtime_fk = next(iter(Task.__table__.c.active_runtime_task_id.foreign_keys))
    assert active_runtime_fk.ondelete == "SET NULL"
