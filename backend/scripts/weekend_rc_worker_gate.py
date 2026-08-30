#!/usr/bin/env python3
"""Mechanical preflight and receipt gate for Weekend RC worker delegations.

This tool owns only target timeout facts and transport terminal facts. It does
not decide whether a worker's code, tests, or explanation are semantically
correct; Codex remains accountable for that independent review.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


INTERRUPTED_STOP_REASONS = {"cancelled", "interrupted", "timeout"}


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _target_timeout_seconds(argv: Sequence[object]) -> int | None:
    values = [str(value) for value in argv]
    try:
        index = values.index("--prompt-timeout-secs")
    except ValueError:
        return None
    if index + 1 >= len(values):
        raise ValueError("--prompt-timeout-secs is missing its value")
    try:
        timeout = int(values[index + 1])
    except ValueError as exc:
        raise ValueError("--prompt-timeout-secs must be an integer") from exc
    if timeout <= 0:
        raise ValueError("--prompt-timeout-secs must be positive")
    return timeout


def validate_preflight(
    targets: Mapping[str, Any],
    *,
    target_name: str,
    outer_timeout_seconds: int,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    target_rows = targets.get("targets")
    if not isinstance(target_rows, list):
        target_rows = []
        errors.append("target registry must contain a targets list")
    target = next(
        (row for row in target_rows if isinstance(row, dict) and row.get("name") == target_name),
        None,
    )
    internal_timeout: int | None = None
    if target is None:
        errors.append(f"target {target_name!r} is not registered")
    else:
        argv = target.get("argv")
        if not isinstance(argv, list):
            errors.append(f"target {target_name!r} argv must be a list")
        else:
            try:
                internal_timeout = _target_timeout_seconds(argv)
            except ValueError as exc:
                errors.append(str(exc))

    if outer_timeout_seconds <= 0:
        errors.append("outer timeout must be positive")
    effective_timeout = outer_timeout_seconds
    if internal_timeout is not None and outer_timeout_seconds > 0:
        effective_timeout = min(outer_timeout_seconds, internal_timeout)
        if internal_timeout < outer_timeout_seconds:
            warnings.append(
                f"target internal timeout {internal_timeout}s is the effective deadline, "
                f"shorter than requested outer timeout {outer_timeout_seconds}s"
            )

    return {
        "schema": "hive.weekend_worker_preflight.v1",
        "ready": not errors,
        "target": target_name,
        "outer_timeout_seconds": outer_timeout_seconds,
        "target_internal_timeout_seconds": internal_timeout,
        "effective_timeout_seconds": effective_timeout,
        "errors": errors,
        "warnings": warnings,
        "semantic_verdict": "not_computed_by_tool",
    }


def validate_receipt(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    expected_target: str | None = None,
    expected_cwd: str | None = None,
    worktree_changed: bool | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    transport_issues: list[str] = []
    warnings: list[str] = []
    if expected_target is not None and request.get("target") != expected_target:
        errors.append(f"receipt target must be {expected_target!r}")
    if expected_cwd is not None:
        observed_cwd = request.get("cwd")
        if not isinstance(observed_cwd, str) or Path(observed_cwd).resolve() != Path(expected_cwd).resolve():
            errors.append(f"receipt cwd must resolve to {expected_cwd!r}")
    if result.get("status") != "success":
        transport_issues.append("transport status is not success")
    if result.get("exit_code") != 0:
        transport_issues.append("transport exit_code is not 0")
    protocol_errors = result.get("protocol_errors")
    if not isinstance(protocol_errors, list):
        transport_issues.append("protocol_errors is not a list")
    elif protocol_errors:
        transport_issues.append("protocol_errors is not empty")
    if worktree_changed is False:
        warnings.append("worktree has no changes; compare this with the task-specific Done contract")

    stop_reason = str(result.get("stop_reason") or "unknown")
    if errors:
        dispatch_state = "invalid_receipt"
    elif transport_issues:
        dispatch_state = "transport_failed"
    elif stop_reason in INTERRUPTED_STOP_REASONS:
        dispatch_state = "interrupted"
        warnings.append("worker run was interrupted; preserve and review any partial evidence before retrying")
    else:
        dispatch_state = "returned"

    return {
        "schema": "hive.weekend_worker_receipt.v1",
        "receipt_valid": not errors,
        "ready_for_independent_review": not errors,
        "dispatch_state": dispatch_state,
        "target": request.get("target"),
        "cwd": request.get("cwd"),
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "stop_reason": stop_reason,
        "protocol_error_count": len(protocol_errors) if isinstance(protocol_errors, list) else None,
        "worktree_changed": worktree_changed,
        "errors": errors,
        "transport_issues": transport_issues,
        "warnings": warnings,
        "semantic_verdict": "not_computed_by_tool",
    }


def _load_targets(path: Path | None) -> dict[str, Any]:
    if path is not None:
        return _json_object(path)
    completed = subprocess.run(
        ["agent-delegate", "list", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeError(f"agent-delegate list failed: {detail}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("agent-delegate list must return a JSON object")
    return value


def _worktree_changed(path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeError(f"git status failed: {detail}")
    return bool(completed.stdout.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--target", required=True)
    preflight.add_argument("--outer-timeout", type=int, required=True)
    preflight.add_argument("--targets-json", type=Path)

    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--receipt-dir", type=Path, required=True)
    receipt.add_argument("--expected-target")
    receipt.add_argument("--expected-cwd")
    receipt.add_argument("--inspect-worktree", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "preflight":
            payload = validate_preflight(
                _load_targets(args.targets_json),
                target_name=args.target,
                outer_timeout_seconds=args.outer_timeout,
            )
            ok = payload["ready"]
        else:
            request = _json_object(args.receipt_dir / "request.json")
            result = _json_object(args.receipt_dir / "result.json")
            changed = _worktree_changed(args.inspect_worktree) if args.inspect_worktree is not None else None
            payload = validate_receipt(
                request,
                result,
                expected_target=args.expected_target,
                expected_cwd=args.expected_cwd,
                worktree_changed=changed,
            )
            ok = payload["receipt_valid"]
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        payload = {
            "schema": "hive.weekend_worker_gate_error.v1",
            "ready": False,
            "error": str(exc),
            "semantic_verdict": "not_computed_by_tool",
        }
        ok = False
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
