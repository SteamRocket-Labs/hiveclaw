from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_long_task_runtime_uses_runtime_task_and_artifact_ledger() -> None:
    source = (APP_ROOT / "services" / "long_task_runtime.py").read_text(encoding="utf-8")
    validation_source = (APP_ROOT / "services" / "long_task_validation.py").read_text(encoding="utf-8")

    assert "long_task_plan.v1" in source
    assert "long_task_progress.v1" in source
    assert "build_long_task_resume_context" in source
    assert "runtime_artifacts" in source
    assert "update_runtime_task_record" in source
    assert "metadata_json" in source

    assert "long_task_validation_report.v1" in validation_source
    assert "validate_long_task_run" in validation_source
    assert "record_long_task_validation" in validation_source
    assert "terminal_status_matches_progress" in validation_source
    assert "completed_has_output_or_verification" in validation_source
