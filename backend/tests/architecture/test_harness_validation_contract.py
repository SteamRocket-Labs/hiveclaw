from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_harness_validation_reuses_h4_h5_validators_read_only() -> None:
    source = (APP_ROOT / "services" / "harness_validation_report.py").read_text(encoding="utf-8")

    assert "harness_validation_report.v1" in source
    assert "validate_long_task_run" in source
    assert "validate_evolution_ledger" in source
    assert "write_report=False" in source
    assert "RuntimeTask" in source
    assert "AgentTrigger" in source
    assert "update_runtime_task_record" not in source
    assert "record_long_task_validation" not in source
    assert "record_evolution_candidate" not in source
