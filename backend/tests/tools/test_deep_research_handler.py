from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest


def _request(
    tmp_path: Path,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str | None = None,
):
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
            session_id=session_id,
        ),
    )


@pytest.mark.asyncio
async def test_deep_research_run_executes_workflow_synchronously(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DR-6b 单路径：同步工具直接驱动 workflow run 到完成并透传 payload。"""
    from app.tools.handlers.deep_research import deep_research_run

    async def fake_workflow_run(*, request, agent_id, user_id, workspace, plan_id=None, run_id=None, **_kwargs):
        return {
            "workflow_run_id": "sync-run",
            "status": "completed",
            "report_path": str(workspace / "report.md"),
            "workspace_artifact_dir": str(workspace),
        }

    monkeypatch.setattr("app.tools.handlers.deep_research.start_deep_research_workflow_run", fake_workflow_run)
    req = _request(tmp_path, session_id="session-work-ledger")
    req.arguments.update({"question": "RWA adoption", "max_rounds": 1, "max_sources": 1, "plan_confirmed": True})

    payload = json.loads(await deep_research_run(req))

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["workflow_run_id"] == "sync-run"


@pytest.mark.asyncio
async def test_deep_research_run_routes_full_depth_to_async_start(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_run

    async def fail_if_called(**_kwargs):
        raise AssertionError("full depth should not run synchronously")

    monkeypatch.setattr("app.tools.handlers.deep_research.start_deep_research_workflow_run", fail_if_called)
    req = _request(tmp_path, session_id="session-work-ledger")
    req.arguments.update({"question": "RWA adoption", "depth": "full"})

    payload = json.loads(await deep_research_run(req))

    assert payload["ok"] is False
    assert payload["status"] == "route_to_async"
    assert payload["recommended_tool"] == "deep_research_start"
    assert "Do not create triggers" in payload["next_action"]


@pytest.mark.asyncio
async def test_deep_research_start_schedules_background_workflow(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DR-6b 单路径：start 立即返回可查的 workflow run id（异步契约保真），
    防轮询指引原样保留。"""
    import uuid as _uuid

    from app.config import get_settings
    from app.tools.handlers import deep_research as handler

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "agents"))
    scheduled: dict = {}

    def fake_schedule(**kwargs):
        scheduled.update(kwargs)

    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fake_schedule)

    req = _request(tmp_path, session_id="session-work-ledger")
    req.arguments.update({"question": "RWA adoption", "max_rounds": 2, "plan_confirmed": True})

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is True
    assert payload["status"] == "running"
    assert payload["task_id"] == str(scheduled["run_id"])  # checkable id handed out immediately
    assert payload["workflow_run_id"] == payload["task_id"]
    assert _uuid.UUID(payload["task_id"])  # well-formed run id
    assert scheduled["agent_id"] == req.context.agent_id
    next_action = payload["next_action"]
    # The agent must NOT busy-loop deep_research_check on a still-running async task:
    # 5 identical polls trip the kernel loop guard. The guidance must say so explicitly.
    assert "deep_research_check" in next_action
    assert "at most once" in next_action
    assert "loop guard" in next_action.lower()
    assert "Do not create triggers" in next_action
    for key in list(handler._INFLIGHT_DEEP_RESEARCH):
        if key[0] == str(req.context.agent_id):
            handler._INFLIGHT_DEEP_RESEARCH.pop(key, None)


@pytest.mark.asyncio
async def test_deep_research_start_dedups_against_persisted_active_runtime_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.workflow_definition import deep_research_workflow_args
    from app.tools.handlers import deep_research as handler

    req = _request(tmp_path, session_id="session-work-ledger")
    args = {"question": "RWA adoption", "max_rounds": 2, "plan_confirmed": True}
    req.arguments.update(args)
    research_request = ResearchRequest.from_arguments(args)
    signature = deep_research_workflow_args(research_request)["deep_research_signature"]
    existing_task_id = str(uuid.uuid4())
    scheduled: dict = {}

    async def fake_list_active_runtime_task_records(*_args, **_kwargs):
        return [
            {
                "task_id": existing_task_id,
                "task_type": "workflow",
                "status": "running",
                "parent_agent_id": str(req.context.agent_id),
                "metadata": {"deep_research_signature": signature},
            }
        ]

    def fake_schedule(**kwargs):
        scheduled.update(kwargs)

    monkeypatch.setattr(handler, "list_active_runtime_task_records", fake_list_active_runtime_task_records, raising=False)
    monkeypatch.setattr(handler, "_schedule_deep_research_workflow_background", fake_schedule)

    payload = json.loads(await handler.deep_research_start(req))

    assert payload["ok"] is True
    assert payload["deduped"] is True
    assert payload["task_id"] == existing_task_id
    assert scheduled == {}


@pytest.mark.asyncio
async def test_deep_research_background_metadata_records_signature(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.workflow_definition import deep_research_workflow_args
    from app.tools.handlers import deep_research as handler

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4()
    research_request = ResearchRequest.from_arguments(
        {"question": "RWA adoption", "max_rounds": 2, "plan_confirmed": True}
    )
    signature = deep_research_workflow_args(research_request)["deep_research_signature"]
    updates: list[tuple[str, dict]] = []

    async def fake_workflow_run(**_kwargs):
        return {"status": "completed", "workflow_run_id": str(run_id)}

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    monkeypatch.setattr(handler, "start_deep_research_workflow_run", fake_workflow_run)
    monkeypatch.setattr(handler, "update_runtime_task_record", fake_update_runtime_task_record)

    handler._schedule_deep_research_workflow_background(
        research_request=research_request,
        run_id=run_id,
        agent_id=agent_id,
        user_id=user_id,
        workspace=tmp_path,
    )
    await asyncio.sleep(0)

    assert updates
    assert updates[0][0] == run_id.hex
    assert updates[0][1]["metadata_json"]["deep_research_signature"] == signature
    assert updates[0][1]["metadata_json"]["deep_research_question"] == "RWA adoption"


def test_deep_research_signature_matches_workflow_args_for_empty_worker_topics() -> None:
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.workflow_definition import deep_research_workflow_args
    from app.tools.handlers import deep_research as handler

    research_request = ResearchRequest.from_arguments(
        {"question": "RWA adoption", "max_rounds": 2, "plan_confirmed": True}
    )

    assert handler._deep_research_signature(research_request) == deep_research_workflow_args(research_request)[
        "deep_research_signature"
    ]


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


@pytest.mark.asyncio
async def test_deep_research_export_reads_workflow_run_artifacts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_export

    run_id = str(uuid.uuid4())
    artifact_dir = tmp_path / "runtime_artifacts" / "workflow_runs" / run_id / "deep_research"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("# Workflow report\n", encoding="utf-8")
    (artifact_dir / "sources.jsonl").write_text('{"source_id":"src_1"}\n', encoding="utf-8")
    (artifact_dir / "final.json").write_text('{"status":"completed"}', encoding="utf-8")

    async def fake_get_runtime_task_record(_task_id: str):
        return {"task_id": run_id, "task_type": "workflow", "status": "completed", "parent_agent_id": None}

    monkeypatch.setattr("app.tools.handlers.deep_research.get_runtime_task_record", fake_get_runtime_task_record)

    req = _request(tmp_path)
    req.arguments.update({"task_id": run_id, "format": "markdown"})

    payload = json.loads(await deep_research_export(req))

    assert payload["ok"] is True
    assert payload["artifact_path"] == f"runtime_artifacts/workflow_runs/{run_id}/deep_research/report.md"
    assert payload["path"] == f"workspace/deep_research_reports/{run_id}/report.md"
    assert (tmp_path / "workspace" / "deep_research_reports" / run_id / "report.md").read_text(
        encoding="utf-8"
    ) == "# Workflow report\n"


@pytest.mark.asyncio
async def test_deep_research_export_writes_office_docx_without_replacing_markdown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.deep_research import deep_research_export

    task_id = "task-docx"
    artifact_dir = tmp_path / "runtime_artifacts" / "long_tasks" / "taskdocx" / "deep_research"
    artifact_dir.mkdir(parents=True)
    markdown = "# Exported report\n\n## Findings\n\n- Source-bound finding.\n"
    (artifact_dir / "report.md").write_text(markdown, encoding="utf-8")
    (artifact_dir / "sources.jsonl").write_text('{"source_id":"src_1"}\n', encoding="utf-8")
    (artifact_dir / "claims.jsonl").write_text('{"claim_id":"claim_1"}\n', encoding="utf-8")
    (artifact_dir / "final.json").write_text('{"status":"completed"}', encoding="utf-8")

    async def fake_get_runtime_task_record(_task_id: str):
        return {"task_id": task_id, "status": "completed", "parent_agent_id": None, "metadata": {}}

    monkeypatch.setattr("app.tools.handlers.deep_research.get_runtime_task_record", fake_get_runtime_task_record)

    req = _request(tmp_path)
    req.arguments.update({"task_id": task_id, "format": "docx"})

    payload = json.loads(await deep_research_export(req))

    assert payload["ok"] is True
    assert payload["format"] == "docx"
    assert payload["path"] == "workspace/deep_research_reports/task-docx/report.docx"
    assert (tmp_path / "workspace" / "deep_research_reports" / "task-docx" / "report.md").read_text(
        encoding="utf-8"
    ) == markdown
    docx_path = tmp_path / "workspace" / "deep_research_reports" / "task-docx" / "report.docx"
    assert docx_path.is_file()
    assert docx_path.read_bytes().startswith(b"PK")


@pytest.mark.asyncio
async def test_deep_research_check_reads_workflow_run_artifacts(tmp_path, monkeypatch):
    """DR-4 parity: a workflow-shaped Deep Research run is checkable through
    the SAME tool — progress comes from the workflow run's artifact root, not
    the legacy long_tasks directory."""
    import json as _json
    import uuid as _uuid

    from app.config import get_settings
    from app.tools.handlers import deep_research as handler

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "agents"))
    req = _request(tmp_path, session_id="session-check-wf")
    agent_id = req.context.agent_id
    run_id = str(_uuid.uuid4())

    from app.services.deep_research.leaf_presets import run_artifact_dir

    root = run_artifact_dir(agent_id, run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.md").write_text("# RWA Report\n\nGrounded findings.", encoding="utf-8")
    (root / "final.json").write_text(
        _json.dumps({"status": "completed", "source_count": 3, "summary": {"claim_count": 5}}), encoding="utf-8"
    )
    (root / "sources.jsonl").write_text('{"source_id": "src_x"}\n', encoding="utf-8")

    async def fake_record(task_id):
        return {"id": task_id, "task_type": "workflow", "status": "completed", "parent_agent_id": str(agent_id)}

    monkeypatch.setattr(handler, "get_runtime_task_record", fake_record)

    req.arguments = {"task_id": run_id}
    payload = _json.loads(await handler.deep_research_check(req))

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["source_count"] == 3
    assert "RWA Report" in payload["partial_report"]
