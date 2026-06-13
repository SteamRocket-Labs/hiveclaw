"""E7: Hermes cross-comparison baseline from a REAL live run only.

round2 §1.1 / E7: the Hermes side of the bakeoff must come from actually running
the hermes CLI on the same behavior tasks (bakeoff_runtime.run_runtime_bakeoff
with target 'hermes_agent') — never from grepping markers in the hermes source.
If the CLI is unavailable (missing / auth / any diagnostic fallback), the Hermes
baseline is 'unavailable' and the cross-comparison gate is SKIPPED, never
backfilled with fake scores. This is what the E8 nightly job feeds in.
"""

from __future__ import annotations

from typing import Any


def extract_hermes_live_scores(runtime_report: dict[str, Any]) -> dict[str, float]:
    """Per-scenario scores from a hermes runtime report — only on a complete,
    trusted live run; otherwise an empty dict (unavailable)."""

    from app.evals.hive_live_runner import TRUSTED_LIVE_TRANSPORTS

    if not isinstance(runtime_report, dict):
        return {}
    if runtime_report.get("fallback_used") or not runtime_report.get("benchmark_complete"):
        return {}
    if runtime_report.get("transport") not in TRUSTED_LIVE_TRANSPORTS:
        return {}
    scores: dict[str, float] = {}
    for name, entry in (runtime_report.get("scenarios") or {}).items():
        if isinstance(entry, dict) and entry.get("score") is not None:
            scores[name] = float(entry["score"])
    return scores


def hermes_baseline_status(runtime_report: dict[str, Any]) -> str:
    return "live" if extract_hermes_live_scores(runtime_report) else "unavailable"


def compare_hive_to_hermes(
    hive_scores: dict[str, float],
    runtime_report: dict[str, Any],
) -> dict[str, Any]:
    """Cross-comparison. Gates only when Hermes ran live; an unavailable Hermes
    baseline is SKIPPED (gated=False, all_at_least_match=True) — never a fake fail."""

    hermes = extract_hermes_live_scores(runtime_report)
    if not hermes:
        return {
            "status": "unavailable",
            "gated": False,
            "comparisons": {},
            "below_hermes": [],
            "all_at_least_match": True,
        }
    comparisons: dict[str, Any] = {}
    below: list[str] = []
    for name, hermes_score in hermes.items():
        hive_score = float(hive_scores.get(name, 0.0))
        at_least_match = hive_score >= hermes_score
        if not at_least_match:
            below.append(name)
        comparisons[name] = {
            "hive": hive_score,
            "hermes": hermes_score,
            "delta": round(hive_score - hermes_score, 4),
            "at_least_match": at_least_match,
        }
    return {
        "status": "live",
        "gated": True,
        "comparisons": comparisons,
        "below_hermes": sorted(below),
        "all_at_least_match": not below,
    }
