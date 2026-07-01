from __future__ import annotations

from uuid import uuid4

import pytest


def test_long_task_runtime_writes_plan_progress_and_resume_context(tmp_path):
    from app.services.long_task_runtime import (
        append_long_task_progress_artifact,
        build_long_task_resume_context,
        write_long_task_plan_artifact,
    )

    agent_id = uuid4()
    runtime_task_id = uuid4()

    plan = write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Ship the weekly report",
        acceptance_criteria=["report.md exists", "pytest passes"],
        verification_commands=["pytest tests/services/test_report.py"],
        risk_gates=["do not publish externally"],
        data_root=tmp_path,
    )
    progress = append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="running",
        delta="Drafted report outline",
        output_paths=["workspace/report.md"],
        data_root=tmp_path,
    )

    resume = build_long_task_resume_context(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )

    assert plan["path"].endswith("/plan.json")
    assert progress["path"].endswith("/progress.jsonl")
    assert resume["plan"]["spec"] == "Ship the weekly report"
    assert resume["latest_progress"]["status"] == "running"
    assert resume["progress_count"] == 1
    assert resume["resume_prompt"].startswith("Resume long task")
    assert "pytest tests/services/test_report.py" in resume["resume_prompt"]


def test_long_task_completion_does_not_silently_complete_open_work_ledger_items(tmp_path):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.services.long_task_runtime import append_long_task_progress_artifact, write_long_task_plan_artifact

    agent_id = uuid4()
    runtime_task_id = uuid4()
    write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Ship the final report",
        acceptance_criteria=["write report", "attach evidence"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )

    append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status="completed",
        delta="The runtime reached terminal status before every ledger item was closed.",
        data_root=tmp_path,
    )

    ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=runtime_task_id, data_root=tmp_path)
    assert ledger is not None
    assert [item["status"] for item in ledger["todo_items"]] == ["pending", "pending"]
    assert [item["status"] for item in ledger["verification"]] == ["pending"]


@pytest.mark.asyncio
async def test_record_long_task_plan_updates_runtime_task_metadata(monkeypatch, tmp_path):
    from app.services import long_task_runtime

    calls = []

    async def fake_update(task_id, **fields):
        calls.append((task_id, fields))
        return True

    monkeypatch.setattr(long_task_runtime, "update_runtime_task_record", fake_update)

    agent_id = uuid4()
    runtime_task_id = uuid4()
    artifact = await long_task_runtime.record_long_task_plan(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec="Run a long investigation",
        acceptance_criteria=["evidence captured"],
        verification_commands=["pytest"],
        risk_gates=[],
        data_root=tmp_path,
    )

    assert artifact["schema"] == "long_task_plan.v1"
    assert calls[0][0] == runtime_task_id.hex
    assert calls[0][1]["metadata_json"]["long_task_plan"]["path"] == artifact["path"]
    assert calls[0][1]["metadata_json"]["workspace_manifest"]["schema"] == "workspace_manifest.v1"
    assert calls[0][1]["metadata_json"]["artifact_refs"][0]["schema"] == "execution_artifact_ref.v1"
