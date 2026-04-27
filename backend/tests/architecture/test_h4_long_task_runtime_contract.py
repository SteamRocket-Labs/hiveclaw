from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_long_task_runtime_uses_runtime_task_and_artifact_ledger() -> None:
    source = (APP_ROOT / "services" / "long_task_runtime.py").read_text(encoding="utf-8")

    assert "long_task_plan.v1" in source
    assert "long_task_progress.v1" in source
    assert "build_long_task_resume_context" in source
    assert "runtime_artifacts" in source
    assert "update_runtime_task_record" in source
    assert "metadata_json" in source
