"""E4: continuous reward + LLM rubric is observational (non-gating). Red first.

Encodes the iron law (round2 §2.3 / decision D3): hard checks decide pass/fail;
an LLM rubric only contributes a continuous quality score and never gates.
"""

from __future__ import annotations

from pathlib import Path

from app.services.evolution_verification import (
    record_verification_eval,
    run_evolution_verification,
    verification_quality_score,
)


def _run(tmp_path: Path, graders: list[dict]) -> dict:
    return run_evolution_verification(workspace=tmp_path, candidate={"candidate_id": "c"}, graders=graders)


def test_llm_rubric_does_not_gate(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("ok", encoding="utf-8")
    report = _run(
        tmp_path,
        [
            {"type": "state_check", "path": "exists.txt"},  # hard check passes
            {"type": "llm_rubric", "rubric": "quality", "score": 40},  # observational
        ],
    )
    assert report["passed"] is True  # rubric never gates a passing hard check


def test_pure_rubric_cannot_pass(tmp_path: Path) -> None:
    # No gating check at all -> fail-closed (a rubric alone can never promote).
    report = _run(tmp_path, [{"type": "llm_rubric", "rubric": "q", "score": 100}])
    assert report["passed"] is False


def test_hard_check_still_gates(tmp_path: Path) -> None:
    report = _run(tmp_path, [{"type": "state_check", "path": "missing.txt"}])
    assert report["passed"] is False


def test_quality_score_is_continuous(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("ok", encoding="utf-8")
    report = _run(
        tmp_path,
        [
            {"type": "state_check", "path": "exists.txt"},  # pass -> 1.0
            {"type": "state_check", "path": "missing.txt"},  # fail -> 0.0
        ],
    )
    score = verification_quality_score(report)
    assert 0.0 < score < 1.0  # continuous, not binary
    assert report.get("quality_score") == score  # exposed on the report


def test_rubric_score_feeds_quality_not_gate(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("ok", encoding="utf-8")
    report = _run(
        tmp_path,
        [
            {"type": "state_check", "path": "exists.txt"},  # hard pass
            {"type": "llm_rubric", "rubric": "q", "score": 0},  # rubric 0 drags quality
        ],
    )
    assert report["passed"] is True  # gate passes regardless of rubric
    assert verification_quality_score(report) < 1.0  # but quality reflects rubric


def test_record_verification_eval_reward_is_continuous(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("ok", encoding="utf-8")
    report = _run(
        tmp_path,
        [
            {"type": "state_check", "path": "exists.txt"},
            {"type": "state_check", "path": "missing.txt"},
        ],
    )
    event = record_verification_eval(tmp_path, candidate={"candidate_id": "c"}, verification_report=report)
    assert event["reward"] not in (0.0, 1.0)  # continuous, not binary
    assert event["passed"] is False  # one hard check failed -> gate fails
