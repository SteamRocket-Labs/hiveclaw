"""E1: hardened, version-pinned behavior-eval baselines (red tests first)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evals.baseline import (
    BEHAVIOR_EVAL_BASELINE_SCHEMA,
    BaselineModelMismatchError,
    BaselineUnavailableError,
    check_model_match,
    compare_to_baseline,
    load_baseline,
    validate_baseline,
)


def _valid_baseline() -> dict:
    return {
        "schema": BEHAVIOR_EVAL_BASELINE_SCHEMA,
        "suite": "core_behavior_v1",
        "baseline_version": "1.0.0",
        "baseline_model": "claude-opus-4-8",
        "baseline_date": "2026-06-13",
        "commit_sha": "abc1234",
        "scenarios": {
            "coding": {"score_p50": 100.0, "score_p95_variance": 0.0, "transport": "live"},
            "memory_recall": {"score_p50": 100.0, "score_p95_variance": 0.0, "transport": "live"},
        },
    }


def _write(tmp_path: Path, payload: dict, *, suite: str = "core_behavior_v1") -> Path:
    root = tmp_path / "baselines"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{suite}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_validate_baseline_requires_core_fields() -> None:
    payload = _valid_baseline()
    del payload["baseline_model"]
    errors = validate_baseline(payload)
    assert any("baseline_model" in e for e in errors)


def test_validate_baseline_requires_scenario_scores() -> None:
    payload = _valid_baseline()
    payload["scenarios"] = {"coding": {"transport": "live"}}  # no score_p50
    errors = validate_baseline(payload)
    assert any("coding" in e and "score_p50" in e for e in errors)


def test_validate_baseline_accepts_valid() -> None:
    assert validate_baseline(_valid_baseline()) == []


def test_load_baseline_missing_is_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "baselines"
    root.mkdir()
    with pytest.raises(BaselineUnavailableError):
        load_baseline("core_behavior_v1", baselines_root=root)


def test_load_baseline_invalid_is_fail_closed(tmp_path: Path) -> None:
    payload = _valid_baseline()
    del payload["commit_sha"]
    root = _write(tmp_path, payload)
    with pytest.raises(BaselineUnavailableError):
        load_baseline("core_behavior_v1", baselines_root=root)


def test_load_baseline_returns_valid(tmp_path: Path) -> None:
    root = _write(tmp_path, _valid_baseline())
    loaded = load_baseline("core_behavior_v1", baselines_root=root)
    assert loaded["baseline_model"] == "claude-opus-4-8"


def test_compare_detects_regression() -> None:
    baseline = _valid_baseline()
    report = compare_to_baseline({"coding": 70.0, "memory_recall": 100.0}, baseline)
    assert report.passed is False
    assert "coding" in report.regressed_scenarios
    assert "memory_recall" not in report.regressed_scenarios


def test_compare_passes_within_tolerance() -> None:
    baseline = _valid_baseline()
    # 1 point below baseline but within tolerance 2.0 => not a regression
    report = compare_to_baseline({"coding": 99.0, "memory_recall": 100.0}, baseline, tolerance=2.0)
    assert report.passed is True
    assert report.regressed_scenarios == []


def test_compare_missing_scenario_fails() -> None:
    baseline = _valid_baseline()
    report = compare_to_baseline({"coding": 100.0}, baseline)  # memory_recall missing
    assert report.passed is False
    assert "memory_recall" in report.missing_scenarios


def test_check_model_match_raises_on_drift() -> None:
    with pytest.raises(BaselineModelMismatchError):
        check_model_match(_valid_baseline(), running_model="claude-sonnet-4-6")


def test_check_model_match_ok() -> None:
    # must not raise
    check_model_match(_valid_baseline(), running_model="claude-opus-4-8")


def test_seed_baseline_artifact_is_valid() -> None:
    """The committed seed baseline (E1 git artifact) must itself be schema-valid."""
    seed = Path(__file__).resolve().parents[2] / "app" / "evals" / "baselines" / "core_behavior_v1.json"
    assert seed.exists(), f"seed baseline missing: {seed}"
    payload = json.loads(seed.read_text(encoding="utf-8"))
    assert validate_baseline(payload) == []
