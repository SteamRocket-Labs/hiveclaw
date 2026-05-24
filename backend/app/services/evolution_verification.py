"""Verification gates for self-evolution candidates."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.evolution_ledger import record_eval_run


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_result(
    *,
    check_type: str,
    passed: bool,
    message: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": check_type,
        "passed": passed,
        "message": message,
        "evidence": evidence or {},
    }


def _run_deterministic_command(workspace: Path, grader: dict[str, Any]) -> dict[str, Any]:
    command = grader.get("command")
    if not isinstance(command, list) or not command:
        return _check_result(
            check_type="deterministic_command",
            passed=False,
            message="deterministic command grader requires a non-empty command list",
        )
    timeout = int(grader.get("timeout_seconds") or 30)
    try:
        completed = subprocess.run(  # noqa: S603
            [str(part) for part in command],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _check_result(
            check_type="deterministic_command",
            passed=False,
            message=f"command timed out after {timeout}s",
            evidence={"stdout": exc.stdout or "", "stderr": exc.stderr or "", "timeout_seconds": timeout},
        )
    return _check_result(
        check_type="deterministic_command",
        passed=completed.returncode == 0,
        message="command passed" if completed.returncode == 0 else "command failed",
        evidence={
            "command": [str(part) for part in command],
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
        },
    )


def _run_state_check(workspace: Path, grader: dict[str, Any]) -> dict[str, Any]:
    relative_path = str(grader.get("path") or "").strip()
    if not relative_path:
        return _check_result(check_type="state_check", passed=False, message="state_check requires path")
    path = workspace / relative_path
    if not path.exists():
        return _check_result(
            check_type="state_check",
            passed=False,
            message="path does not exist",
            evidence={"path": relative_path},
        )
    expected = grader.get("contains")
    if expected is None:
        return _check_result(
            check_type="state_check",
            passed=True,
            message="path exists",
            evidence={"path": relative_path},
        )
    content = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    passed = str(expected) in content
    return _check_result(
        check_type="state_check",
        passed=passed,
        message="expected content found" if passed else "expected content missing",
        evidence={"path": relative_path, "contains": str(expected)},
    )


def _run_tool_call_check(grader: dict[str, Any]) -> dict[str, Any]:
    tool_calls = [str(item) for item in grader.get("tool_calls") or []]
    required = [str(item) for item in grader.get("required_tools") or []]
    forbidden = [str(item) for item in grader.get("forbidden_tools") or []]
    missing = [tool for tool in required if tool not in tool_calls]
    forbidden_seen = [tool for tool in forbidden if tool in tool_calls]
    passed = not missing and not forbidden_seen
    return _check_result(
        check_type="tool_call_check",
        passed=passed,
        message="tool call contract passed" if passed else "tool call contract failed",
        evidence={"missing": missing, "forbidden_seen": forbidden_seen, "tool_calls": tool_calls},
    )


def _run_llm_rubric_check(grader: dict[str, Any]) -> dict[str, Any]:
    passed = bool(grader.get("passed"))
    return _check_result(
        check_type="llm_rubric",
        passed=passed,
        message="LLM rubric accepted" if passed else "LLM rubric rejected",
        evidence={"rubric": grader.get("rubric", ""), "score": grader.get("score")},
    )


def _run_human_confirmation_check(grader: dict[str, Any]) -> dict[str, Any]:
    confirmed = bool(grader.get("confirmed"))
    return _check_result(
        check_type="human_confirmation",
        passed=confirmed,
        message="human confirmation present" if confirmed else "human confirmation missing",
        evidence={"reviewer": grader.get("reviewer", "")},
    )


def run_evolution_verification(
    *,
    workspace: Path,
    candidate: dict[str, Any],
    graders: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for grader in graders:
        grader_type = str(grader.get("type") or "").strip()
        if grader_type == "deterministic_command":
            checks.append(_run_deterministic_command(workspace, grader))
        elif grader_type == "state_check":
            checks.append(_run_state_check(workspace, grader))
        elif grader_type == "tool_call_check":
            checks.append(_run_tool_call_check(grader))
        elif grader_type == "llm_rubric":
            checks.append(_run_llm_rubric_check(grader))
        elif grader_type == "human_confirmation":
            checks.append(_run_human_confirmation_check(grader))
        else:
            checks.append(
                _check_result(
                    check_type=grader_type or "unknown",
                    passed=False,
                    message="unknown grader type",
                    evidence={"grader": grader},
                )
            )

    passed = bool(checks) and all(bool(check.get("passed")) for check in checks)
    return {
        "schema": "evolution_verification_report.v1",
        "created_at": _now_iso(),
        "candidate_id": candidate.get("candidate_id"),
        "target_type": candidate.get("target_type"),
        "target_id": candidate.get("target_id"),
        "passed": passed,
        "checks": checks,
        "trace_refs": candidate.get("source_attempt_ids") or candidate.get("manifest", {}).get("trace_refs") or [],
    }


def record_verification_eval(
    workspace: Path,
    *,
    candidate: dict[str, Any],
    verification_report: dict[str, Any],
    dataset: str = "evolution_verification",
) -> dict[str, Any]:
    passed = bool(verification_report.get("passed"))
    failed_checks = [check for check in verification_report.get("checks", []) if not bool(check.get("passed"))]
    return record_eval_run(
        workspace,
        candidate_id=str(candidate.get("candidate_id") or ""),
        dataset=dataset,
        reward=1.0 if passed else 0.0,
        baseline_reward=0.0,
        passed=passed,
        traces=[str(ref) for ref in verification_report.get("trace_refs") or [] if str(ref).strip()],
        critical_regressions=len(failed_checks),
        metadata={"verification_report": verification_report},
    )


def decide_verified_promotion(
    candidate: dict[str, Any],
    *,
    verification_report: dict[str, Any] | None,
) -> dict[str, str]:
    del candidate
    if verification_report is None:
        return {"decision": "hold", "reason": "verification evidence is required"}
    if not bool(verification_report.get("passed")):
        return {"decision": "reject", "reason": "verification failed"}
    return {"decision": "promote", "reason": "verification passed"}
