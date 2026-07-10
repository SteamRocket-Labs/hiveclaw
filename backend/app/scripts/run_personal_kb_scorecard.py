"""Run the Personal KB SAG scorecard from a JSON fixture/report input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.evals.personal_kb_scorecard import score_personal_kb_benchmark


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score Personal KB retrieval results against SAG trace fixtures.")
    parser.add_argument("--input", required=True, help="Path to scorecard input JSON.")
    parser.add_argument("--output", required=True, help="Path to write scorecard report JSON.")
    parser.add_argument(
        "--fail-on-acl-leakage", action="store_true", help="Return exit code 2 if any forbidden ref leaks."
    )
    parser.add_argument(
        "--fail-under-recall", type=float, default=None, help="Return exit code 2 if Hive recall@k is lower."
    )
    parser.add_argument(
        "--fail-under-citation",
        type=float,
        default=None,
        help="Return exit code 2 if Hive citation accuracy is lower.",
    )
    return parser


def _read_payload(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Personal KB scorecard input must be a JSON object")
    return payload


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _failed_thresholds(
    report: dict, *, fail_under_recall: float | None, fail_under_citation: float | None
) -> list[str]:
    failures: list[str] = []
    hive = (report.get("providers") or {}).get("hive") or {}
    if fail_under_recall is not None and float(hive.get("recall_at_k") or 0.0) < fail_under_recall:
        failures.append("hive_recall_at_k")
    if fail_under_citation is not None and float(hive.get("citation_accuracy") or 0.0) < fail_under_citation:
        failures.append("hive_citation_accuracy")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = score_personal_kb_benchmark(_read_payload(Path(args.input)))
    _write_report(Path(args.output), report)

    failures = _failed_thresholds(
        report,
        fail_under_recall=args.fail_under_recall,
        fail_under_citation=args.fail_under_citation,
    )
    if args.fail_on_acl_leakage and not bool((report.get("hard_gates") or {}).get("acl_leakage_zero", False)):
        failures.append("acl_leakage")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
