"""Governed behavior baseline updater.

The updater accepts only complete trusted live behavior reports. It never writes
provisional baselines and refuses fallback or partial reports before touching the
output file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.evals.baseline import validate_baseline
from app.services.eval_ci_service import build_behavior_eval_rebaseline_candidate

EXIT_OK = 0
EXIT_REJECTED_REPORT = 2


def _load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"behavior report must be a JSON object: {path}")
    return payload


def build_behavior_baseline_from_report(
    report: dict[str, Any],
    *,
    commit_sha: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    candidate = build_behavior_eval_rebaseline_candidate(
        report,
        generated_at=generated_at,
        commit_sha=commit_sha,
    )
    if candidate.get("status") != "ready" or not isinstance(candidate.get("baseline"), dict):
        reason = candidate.get("reason") or "report rejected by behavior eval baseline gate"
        raise ValueError(str(reason))
    baseline = candidate["baseline"]
    if baseline.get("provisional") is not False:
        raise ValueError("baseline updater refuses provisional baselines")
    errors = validate_baseline(baseline)
    if errors:
        raise ValueError(f"generated baseline is invalid: {'; '.join(errors)}")
    return baseline


def write_behavior_baseline(
    *,
    report_path: Path,
    output_path: Path,
    commit_sha: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    baseline = build_behavior_baseline_from_report(
        _load_report(report_path),
        commit_sha=commit_sha,
        generated_at=generated_at,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update a behavior baseline from a trusted live report.")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)

    try:
        baseline = write_behavior_baseline(
            report_path=args.report,
            output_path=args.output,
            commit_sha=args.commit_sha,
            generated_at=args.generated_at,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must print a concise rejection reason.
        print(f"[update-behavior-baseline] rejected report: {exc}", file=sys.stderr)
        return EXIT_REJECTED_REPORT
    print(json.dumps({"updated": str(args.output), "baseline_version": baseline["baseline_version"]}, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
