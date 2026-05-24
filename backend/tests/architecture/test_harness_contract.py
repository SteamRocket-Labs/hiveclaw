from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_harness_contract_service_exists() -> None:
    source = (APP_ROOT / "services" / "harness_contract.py").read_text(encoding="utf-8")

    assert "workspace_manifest.v1" in source
    assert "execution_artifact_ref.v1" in source
    assert "WorkspaceManifest" in source
    assert "ExecutionArtifactRef" in source
    assert "write_workspace_manifest" in source
    assert "build_manifest_resume_context" in source


def test_long_task_runtime_attaches_workspace_manifest_refs() -> None:
    source = (APP_ROOT / "services" / "long_task_runtime.py").read_text(encoding="utf-8")

    assert "write_workspace_manifest" in source
    assert "workspace_manifest" in source
    assert "artifact_refs" in source
