"""Stable SOTA behavior-eval CLI wrapper.

This module gives ops and CI a single entry point for producing trusted behavior
reports from Hive and Hermes. Fixture mode exists only for deterministic tests;
live mode delegates to the existing Hive live runner or Hermes bakeoff runner and
returns a non-zero code when the result is fallback/partial/unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_UNTRUSTED_REPORT = 2


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"report must be a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _trusted_for_target(target: str, report: dict[str, Any]) -> bool:
    if target == "hive":
        from app.evals.hive_live_runner import behavior_eval_passed

        return behavior_eval_passed(report)
    if target == "hermes":
        from app.evals.hermes_baseline import extract_hermes_live_scores

        return bool(extract_hermes_live_scores(report))
    raise ValueError(f"Unsupported target: {target}")


def _finalize_report(*, target: str, report: dict[str, Any], output: Path) -> int:
    payload = {"target": target, **report}
    _write_json(output, payload)
    if _trusted_for_target(target, payload):
        return EXIT_OK
    print(
        f"[run-sota-behavior-eval] untrusted {target} report: "
        f"transport={payload.get('transport')} fallback_used={payload.get('fallback_used')} "
        f"benchmark_complete={payload.get('benchmark_complete')}",
        file=sys.stderr,
    )
    return EXIT_UNTRUSTED_REPORT


def _run_hive_live(output: Path, passthrough: list[str]) -> int:
    from app.evals.hive_live_runner import main as hive_live_main

    code = hive_live_main(["--output", str(output), *passthrough])
    if code != 0:
        return code
    report = _load_json(output)
    return _finalize_report(target="hive", report=report, output=output)


def _run_hermes_live(output: Path, runtime_output_dir: Path | None) -> int:
    from app.evals.bakeoff_runtime import run_runtime_bakeoff

    output_dir = runtime_output_dir or (output.parent / "hermes-runtime")
    report = run_runtime_bakeoff("hermes_agent", output_dir=output_dir)
    return _finalize_report(target="hermes", report=report, output=output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run trusted SOTA behavior eval for Hive or Hermes.")
    parser.add_argument("--target", choices=("hive", "hermes"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-report", type=Path, help="deterministic test-only report input")
    parser.add_argument("--runtime-output-dir", type=Path, help="Hermes runtime artifact directory")
    args, passthrough = parser.parse_known_args(argv)

    if args.fixture_report is not None:
        return _finalize_report(target=args.target, report=_load_json(args.fixture_report), output=args.output)
    if args.target == "hive":
        return _run_hive_live(args.output, passthrough)
    return _run_hermes_live(args.output, args.runtime_output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
