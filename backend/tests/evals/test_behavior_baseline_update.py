from __future__ import annotations

import json
from pathlib import Path


def _trusted_report() -> dict:
    return {
        "kind": "behavior_eval",
        "transport": "hive_live",
        "runtime": {"model": "claude-opus-4-8", "provider": "anthropic"},
        "benchmark_complete": True,
        "fallback_used": False,
        "scenarios": {
            "coding": {"ready": True, "score": 94},
            "review": {"ready": True, "score": 91.5},
        },
    }


def _fallback_report() -> dict:
    report = _trusted_report()
    report["transport"] = "repo_evidence_fallback"
    report["fallback_used"] = True
    return report


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_update_behavior_baseline_writes_non_provisional_baseline(tmp_path: Path) -> None:
    from app.evals.update_behavior_baseline import main

    report_path = _write(tmp_path / "report.json", _trusted_report())
    output_path = tmp_path / "core_behavior_v1.json"

    exit_code = main(
        [
            "--report",
            str(report_path),
            "--output",
            str(output_path),
            "--commit-sha",
            "abc123",
            "--generated-at",
            "2026-06-16T00:00:00+00:00",
        ]
    )

    assert exit_code == 0
    baseline = json.loads(output_path.read_text(encoding="utf-8"))
    assert baseline["schema"] == "behavior_eval_baseline.v1"
    assert baseline["suite"] == "core_behavior_v1"
    assert baseline["baseline_model"] == "claude-opus-4-8"
    assert baseline["baseline_date"] == "2026-06-16"
    assert baseline["commit_sha"] == "abc123"
    assert baseline["provisional"] is False
    assert baseline["scenarios"]["coding"]["score_p50"] == 94.0


def test_update_behavior_baseline_rejects_fallback_report(tmp_path: Path) -> None:
    from app.evals.update_behavior_baseline import main

    report_path = _write(tmp_path / "report.json", _fallback_report())
    output_path = tmp_path / "core_behavior_v1.json"

    exit_code = main(
        [
            "--report",
            str(report_path),
            "--output",
            str(output_path),
            "--commit-sha",
            "abc123",
        ]
    )

    assert exit_code == 2
    assert not output_path.exists()


def test_run_sota_behavior_eval_accepts_trusted_hive_fixture(tmp_path: Path) -> None:
    from app.evals.run_sota_behavior_eval import main

    fixture = _write(tmp_path / "fixture.json", _trusted_report())
    output = tmp_path / "hive-report.json"

    exit_code = main(["--target", "hive", "--fixture-report", str(fixture), "--output", str(output)])

    assert exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["target"] == "hive"
    assert written["transport"] == "hive_live"


def test_run_sota_behavior_eval_rejects_fallback_hive_fixture(tmp_path: Path) -> None:
    from app.evals.run_sota_behavior_eval import main

    fixture = _write(tmp_path / "fixture.json", _fallback_report())
    output = tmp_path / "hive-report.json"

    exit_code = main(["--target", "hive", "--fixture-report", str(fixture), "--output", str(output)])

    assert exit_code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["fallback_used"] is True


def test_run_sota_behavior_eval_accepts_live_hermes_fixture(tmp_path: Path) -> None:
    from app.evals.run_sota_behavior_eval import main

    fixture = _write(
        tmp_path / "hermes.json",
        {
            "kind": "bakeoff",
            "transport": "live_cli",
            "benchmark_complete": True,
            "fallback_used": False,
            "scenarios": {"coding": {"ready": True, "score": 88}},
        },
    )
    output = tmp_path / "hermes-report.json"

    exit_code = main(["--target", "hermes", "--fixture-report", str(fixture), "--output", str(output)])

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["target"] == "hermes"


def test_run_sota_behavior_eval_rejects_unavailable_hermes_fixture(tmp_path: Path) -> None:
    from app.evals.run_sota_behavior_eval import main

    fixture = _write(
        tmp_path / "hermes.json",
        {
            "kind": "bakeoff",
            "transport": "repo_evidence_only",
            "benchmark_complete": True,
            "fallback_used": True,
            "scenarios": {"coding": {"ready": True, "score": 88}},
        },
    )
    output = tmp_path / "hermes-report.json"

    exit_code = main(["--target", "hermes", "--fixture-report", str(fixture), "--output", str(output)])

    assert exit_code == 2
