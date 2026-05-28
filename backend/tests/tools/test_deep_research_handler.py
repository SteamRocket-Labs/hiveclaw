from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


def _request(tmp_path: Path, *, agent_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    aid = agent_id or uuid.uuid4()
    return ToolExecutionRequest(
        tool_name="deep_research_run",
        arguments={},
        context=ToolExecutionContext(
            agent_id=aid,
            user_id=user_id or uuid.uuid4(),
            tenant_id=str(uuid.uuid4()),
            workspace=tmp_path,
        ),
    )


@pytest.mark.asyncio
async def test_deep_research_run_returns_artifact_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.deep_research.schemas import ResearchRun
    from app.tools.handlers.deep_research import deep_research_run

    async def fake_run_research(*, request, agent_id, user_id, workspace, runtime_task_id=None):
        artifact_dir = workspace / "runtime_artifacts" / "deep_research" / "sync-run"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "report.md").write_text("# Deep Research Report\n", encoding="utf-8")
        (artifact_dir / "sources.jsonl").write_text("{}", encoding="utf-8")
        (artifact_dir / "claims.jsonl").write_text("{}", encoding="utf-8")
        return ResearchRun(
            run_id="sync-run",
            status="completed",
            summary="done",
            artifact_dir=artifact_dir.as_posix(),
            report_path=(artifact_dir / "report.md").as_posix(),
            sources_path=(artifact_dir / "sources.jsonl").as_posix(),
            claims_path=(artifact_dir / "claims.jsonl").as_posix(),
            quality_gates={"attribution": "passed"},
        )

    monkeypatch.setattr("app.tools.handlers.deep_research.run_deep_research", fake_run_research)
    req = _request(tmp_path)
    req.arguments.update(
        {"question": "RWA adoption", "max_rounds": 1, "max_sources": 1, "plan_confirmed": True}
    )

    payload = json.loads(await deep_research_run(req))

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["report_path"] == "workspace/deep_research_reports/sync-run/report.md"
    assert (tmp_path / "workspace" / "deep_research_reports" / "sync-run" / "report.md").is_file()
    assert (tmp_path / "workspace" / "deep_research_reports" / "sync-run" / "sources.jsonl").is_file()
    assert payload["artifact_report_path"].endswith("runtime_artifacts/deep_research/sync-run/report.md")
    assert payload["quality_gates"]["attribution"] == "passed"


@pytest.mark.asyncio
async def test_deep_research_run_routes_full_depth_to_async_start(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_run

    async def fail_if_called(**_kwargs):
        raise AssertionError("full depth should not run synchronously")

    monkeypatch.setattr("app.tools.handlers.deep_research.run_deep_research", fail_if_called)
    req = _request(tmp_path)
    req.arguments.update({"question": "RWA adoption", "depth": "full"})

    payload = json.loads(await deep_research_run(req))

    assert payload["ok"] is False
    assert payload["status"] == "route_to_async"
    assert payload["recommended_tool"] == "deep_research_start"
    assert "Do not create triggers" in payload["next_action"]


@pytest.mark.asyncio
async def test_deep_research_start_creates_runtime_task(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers.deep_research import deep_research_start

    created: dict = {}

    async def fake_create_runtime_task_record(**kwargs):
        created.update(kwargs)
        return kwargs["task_id"]

    async def fake_record_plan(**_kwargs):
        return {"path": "runtime_artifacts/long_tasks/task/plan.json"}

    async def fake_record_progress(**_kwargs):
        return {"path": "runtime_artifacts/long_tasks/task/progress.jsonl"}

    monkeypatch.setattr("app.tools.handlers.deep_research.create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr("app.tools.handlers.deep_research.record_long_task_plan", fake_record_plan)
    monkeypatch.setattr("app.tools.handlers.deep_research.record_long_task_progress", fake_record_progress)
    monkeypatch.setattr("app.tools.handlers.deep_research._schedule_deep_research_background", lambda *_args, **_kwargs: None)

    req = _request(tmp_path)
    req.arguments.update({"question": "RWA adoption", "max_rounds": 2, "plan_confirmed": True})

    payload = json.loads(await deep_research_start(req))

    assert payload["ok"] is True
    assert payload["status"] == "running"
    assert created["task_type"] == "deep_research"
    assert created["parent_agent_id"] == req.context.agent_id
    assert payload["next_action"].startswith("Use deep_research_check")
    assert "Do not create triggers" in payload["next_action"]


@pytest.mark.asyncio
async def test_deep_research_check_rejects_other_agent_task(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.handlers.deep_research import deep_research_check

    owner = uuid.uuid4()
    other = uuid.uuid4()

    async def fake_get_runtime_task_record(_task_id: str):
        return {"task_id": "abc", "status": "running", "parent_agent_id": str(owner), "metadata": {}}

    monkeypatch.setattr("app.tools.handlers.deep_research.get_runtime_task_record", fake_get_runtime_task_record)

    req = _request(tmp_path, agent_id=other)
    req.arguments.update({"task_id": "abc"})

    payload = json.loads(await deep_research_check(req))

    assert payload["ok"] is False
    assert payload["error"] == "forbidden"


@pytest.mark.asyncio
async def test_deep_research_check_publishes_workspace_visible_report_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_check

    task_id = "task-visible"
    artifact_dir = tmp_path / "runtime_artifacts" / "long_tasks" / "taskvisible" / "deep_research"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("# Visible report\n", encoding="utf-8")
    (artifact_dir / "sources.jsonl").write_text('{"source_id":"src_1"}\n', encoding="utf-8")
    (artifact_dir / "claims.jsonl").write_text('{"claim_id":"claim_1"}\n', encoding="utf-8")
    (artifact_dir / "final.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "summary": "done",
                "source_count": 1,
                "claim_count": 1,
                "quality_gates": {"attribution": "passed"},
            }
        ),
        encoding="utf-8",
    )

    async def fake_get_runtime_task_record(_task_id: str):
        return {"task_id": task_id, "status": "completed", "parent_agent_id": None, "metadata": {}}

    monkeypatch.setattr("app.tools.handlers.deep_research.get_runtime_task_record", fake_get_runtime_task_record)

    req = _request(tmp_path)
    req.arguments.update({"task_id": task_id})

    payload = json.loads(await deep_research_check(req))

    assert payload["ok"] is True
    assert payload["report_path"] == "workspace/deep_research_reports/task-visible/report.md"
    assert payload["workspace_artifact_dir"] == "workspace/deep_research_reports/task-visible"
    assert (tmp_path / "workspace" / "deep_research_reports" / "task-visible" / "report.md").read_text(
        encoding="utf-8"
    ) == "# Visible report\n"
    assert payload["artifact_report_path"] == "runtime_artifacts/long_tasks/taskvisible/deep_research/report.md"


@pytest.mark.asyncio
async def test_deep_research_export_writes_workspace_visible_markdown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_export

    task_id = "task-export"
    artifact_dir = tmp_path / "runtime_artifacts" / "long_tasks" / "taskexport" / "deep_research"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("# Exported report\n", encoding="utf-8")
    (artifact_dir / "sources.jsonl").write_text('{"source_id":"src_1"}\n', encoding="utf-8")
    (artifact_dir / "claims.jsonl").write_text('{"claim_id":"claim_1"}\n', encoding="utf-8")
    (artifact_dir / "final.json").write_text('{"status":"completed"}', encoding="utf-8")

    async def fake_get_runtime_task_record(_task_id: str):
        return {"task_id": task_id, "status": "completed", "parent_agent_id": None, "metadata": {}}

    monkeypatch.setattr("app.tools.handlers.deep_research.get_runtime_task_record", fake_get_runtime_task_record)

    req = _request(tmp_path)
    req.arguments.update({"task_id": task_id, "format": "markdown"})

    payload = json.loads(await deep_research_export(req))

    assert payload["ok"] is True
    assert payload["path"] == "workspace/deep_research_reports/task-export/report.md"
    assert payload["workspace_artifact_dir"] == "workspace/deep_research_reports/task-export"
    assert (tmp_path / "workspace" / "deep_research_reports" / "task-export" / "report.md").read_text(
        encoding="utf-8"
    ) == "# Exported report\n"
    assert (tmp_path / "workspace" / "deep_research_reports" / "task-export" / "sources.jsonl").is_file()
