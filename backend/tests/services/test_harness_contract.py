from __future__ import annotations

from uuid import uuid4


def test_workspace_manifest_records_artifact_refs_and_resume_context(tmp_path) -> None:
    from app.services.harness_contract import build_manifest_resume_context, write_workspace_manifest

    agent_id = uuid4()
    runtime_task_id = uuid4()
    workspace = tmp_path / str(agent_id)
    artifact = workspace / "runtime_artifacts" / "long_tasks" / runtime_task_id.hex / "report.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("final report", encoding="utf-8")

    manifest_ref = write_workspace_manifest(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        workspace_root=workspace,
        artifact_paths=[artifact],
        data_root=tmp_path,
    )
    context = build_manifest_resume_context(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=tmp_path,
    )

    assert manifest_ref["schema"] == "workspace_manifest.v1"
    assert manifest_ref["path"].endswith("workspace_manifest.json")
    assert context["schema"] == "workspace_manifest_resume_context.v1"
    assert context["manifest"]["runtime_task_id"] == runtime_task_id.hex
    assert context["manifest"]["artifact_refs"][0]["schema"] == "execution_artifact_ref.v1"
    assert context["manifest"]["artifact_refs"][0]["path"].endswith("report.md")
    assert "report.md" in context["resume_prompt"]
