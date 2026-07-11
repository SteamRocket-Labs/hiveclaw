from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_self_evolution_ledger_contract_exists() -> None:
    source = (APP_ROOT / "services" / "evolution_ledger.py").read_text(encoding="utf-8")

    assert "evolution_candidate.v1" in source
    assert "hive_evolution_manifest.v1" in source
    assert "evolution_eval_run.v1" in source
    assert "evolution_promotion_decision.v1" in source
    assert "evolution_rollback_event.v1" in source
    assert "build_evolution_manifest" in source
    assert "manifest" in source
    assert "rollback_ref" in source
    assert "critical_regressions" in source


def test_self_evolution_validation_contract_exists() -> None:
    source = (APP_ROOT / "services" / "evolution_validation.py").read_text(encoding="utf-8")

    assert "evolution_validation_report.v1" in source
    assert "validate_evolution_ledger" in source
    assert "candidate_has_valid_manifest" in source
    assert "candidate_has_eval_run" in source
    assert "promotion_has_rollback_ref" in source
    assert "promotion_blocks_critical_regression" in source


def test_skill_distiller_promotion_records_evolution_ledger() -> None:
    source = (APP_ROOT / "services" / "skill_distillation_runner.py").read_text(encoding="utf-8")

    assert "record_evolution_candidate" in source
    assert "record_verification_eval" in source
    assert "record_promotion_decision" in source
