"""E7: Hermes cross-comparison from a REAL live run only (red first).

round2 §1.1: the Hermes side must come from actually running the hermes CLI on
the same tasks — never from grepping markers in the hermes source. If the CLI is
unavailable, the Hermes baseline is 'unavailable' and the cross gate is SKIPPED,
never backfilled with fake scores.
"""

from __future__ import annotations

from pathlib import Path

from app.evals.hermes_baseline import (
    compare_hive_to_hermes,
    extract_hermes_live_scores,
    hermes_baseline_status,
)


def _hermes_runtime_report(*, transport: str = "live_cli", complete: bool = True, fallback: bool = False, scores=None):
    scores = scores or {"coding": 86, "memory_recall": 80}
    return {
        "kind": "bakeoff",
        "transport": transport,
        "benchmark_complete": complete,
        "fallback_used": fallback,
        "scenarios": {name: {"ready": True, "score": value} for name, value in scores.items()},
    }


def test_live_hermes_scores_extracted() -> None:
    scores = extract_hermes_live_scores(_hermes_runtime_report())
    assert scores["coding"] == 86.0


def test_fallback_transport_is_unavailable() -> None:
    assert extract_hermes_live_scores(_hermes_runtime_report(transport="repo_evidence_only")) == {}
    assert extract_hermes_live_scores(_hermes_runtime_report(fallback=True)) == {}
    assert extract_hermes_live_scores(_hermes_runtime_report(complete=False)) == {}
    assert hermes_baseline_status(_hermes_runtime_report(fallback=True)) == "unavailable"
    assert hermes_baseline_status(_hermes_runtime_report()) == "live"


def test_compare_skips_gate_when_unavailable() -> None:
    cmp = compare_hive_to_hermes({"coding": 90}, _hermes_runtime_report(transport="repo_evidence_only"))
    assert cmp["status"] == "unavailable"
    assert cmp["gated"] is False
    assert cmp["all_at_least_match"] is True  # unavailable never fails the cross gate


def test_compare_gates_when_live_and_hive_matches() -> None:
    cmp = compare_hive_to_hermes(
        {"coding": 90, "memory_recall": 80},
        _hermes_runtime_report(scores={"coding": 86, "memory_recall": 80}),
    )
    assert cmp["status"] == "live"
    assert cmp["gated"] is True
    assert cmp["all_at_least_match"] is True


def test_compare_detects_hive_below_hermes() -> None:
    cmp = compare_hive_to_hermes({"coding": 70}, _hermes_runtime_report(scores={"coding": 86}))
    assert cmp["all_at_least_match"] is False
    assert "coding" in cmp["below_hermes"]


# ---- self_evolution_bakeoff: grep-marker fallback removed from the gate ----


def test_self_evolution_bakeoff_marks_hermes_unavailable_without_real_run(tmp_path: Path) -> None:
    from app.evals.self_evolution_bakeoff import run_self_evolution_bakeoff

    report = run_self_evolution_bakeoff(hermes_scores=None, hermes_root=tmp_path / "no-hermes")
    # No grep-marker fake scores: Hermes is unavailable, scores empty.
    assert report["hermes"]["source"] == "unavailable"
    assert report["hermes"]["scores"] == {}
    # The Hive-absolute threshold still gates each scenario (no crash, no fake delta).
    for comparison in report["comparisons"].values():
        assert comparison["hermes_score"] is None
        assert comparison["delta"] is None


def test_self_evolution_bakeoff_no_grep_marker_function() -> None:
    import app.evals.self_evolution_bakeoff as mod

    # The grep-marker score derivation must be gone from the gating module.
    assert not hasattr(mod, "_derive_hermes_scores")
    assert not hasattr(mod, "_score_by_markers")
