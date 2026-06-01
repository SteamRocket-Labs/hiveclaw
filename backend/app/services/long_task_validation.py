"""Validation reports for RuntimeTask-backed long tasks.

The runtime module records plan and progress artifacts. This module answers a
separate question: can a long task be resumed and audited as a real completed,
failed, skipped, or still-running execution?
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.services.agent_work_ledger import load_agent_work_ledger, validate_agent_work_ledger_completion
from app.services.long_task_runtime import (
    _artifact_dir,
    _load_json,
    _load_progress,
    _now_iso,
    _relative_to_agent,
    build_long_task_resume_context,
)
from app.services.runtime_task_service import update_runtime_task_record


TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped"}
INTERRUPTED_STATUSES = {"failed", "killed", "skipped"}
REASON_KEYS = (
    "blocked_reason",
    "cancel_reason",
    "failure_reason",
    "missed_reason",
    "skip_reason",
    "terminal_reason",
)


def _runtime_status(runtime_task: dict[str, Any] | None) -> str | None:
    status = (runtime_task or {}).get("status")
    return str(status).strip() if status else None


def _runtime_metadata(runtime_task: dict[str, Any] | None) -> dict[str, Any]:
    task = runtime_task or {}
    metadata = task.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata_json = task.get("metadata_json")
    return metadata_json if isinstance(metadata_json, dict) else {}


def _runtime_result(runtime_task: dict[str, Any] | None) -> str | None:
    task = runtime_task or {}
    result = task.get("result") or task.get("result_summary")
    if not result:
        return None
    text = str(result).strip()
    return text or None


def _has_terminal_reason(runtime_task: dict[str, Any] | None, latest_progress: dict[str, Any] | None) -> bool:
    if latest_progress and str(latest_progress.get("blocked_reason") or "").strip():
        return True
    metadata = _runtime_metadata(runtime_task)
    if any(str(metadata.get(key) or "").strip() for key in REASON_KEYS):
        return True
    return _runtime_result(runtime_task) is not None


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    evidence: dict[str, Any] | None = None,
    recommendation: str = "",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _summarize_checks(checks: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"pass": 0, "warning": 0, "fail": 0}
    for check in checks:
        status = check.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def validate_long_task_run(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    runtime_task: dict[str, Any] | None = None,
    data_root: str | Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Build a read-only validation report for a RuntimeTask-backed long task."""

    artifact_dir = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
    plan = _load_json(artifact_dir / "plan.json") or {}
    progress_items = _load_progress(artifact_dir / "progress.jsonl")
    latest_progress = progress_items[-1] if progress_items else None
    resume_context = build_long_task_resume_context(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=data_root,
    )
    runtime_status = _runtime_status(runtime_task)
    latest_status = str(latest_progress.get("status") or "").strip() if latest_progress else None
    acceptance_criteria = [item for item in plan.get("acceptance_criteria", []) if str(item).strip()]
    verification_commands = [item for item in plan.get("verification_commands", []) if str(item).strip()]
    output_paths = [item for item in (latest_progress or {}).get("output_paths", []) if str(item).strip()]
    work_ledger = load_agent_work_ledger(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=data_root,
    )

    checks = [
        _check(
            "plan_artifact_present",
            "pass" if plan.get("schema") == "long_task_plan.v1" else "fail",
            "Long-task plan artifact is present." if plan else "Long-task plan artifact is missing.",
            evidence={"path": "plan.json", "schema": plan.get("schema")},
            recommendation="Record a long_task_plan.v1 artifact before executing long tasks.",
        ),
        _check(
            "progress_artifact_present",
            "pass" if progress_items else "fail",
            "Long-task progress artifact has at least one entry."
            if progress_items
            else "Long-task progress artifact is missing or empty.",
            evidence={"path": "progress.jsonl", "progress_count": len(progress_items)},
            recommendation="Append progress before claiming a long task can be resumed or completed.",
        ),
        _check(
            "acceptance_criteria_present",
            "pass" if acceptance_criteria else "fail",
            "Acceptance criteria are recorded." if acceptance_criteria else "Acceptance criteria are missing.",
            evidence={"count": len(acceptance_criteria)},
            recommendation="Add explicit acceptance criteria to the long-task plan.",
        ),
        _check(
            "verification_commands_present",
            "pass" if verification_commands else "fail",
            "Verification commands are recorded." if verification_commands else "Verification commands are missing.",
            evidence={"count": len(verification_commands)},
            recommendation="Add runnable verification commands to the long-task plan.",
        ),
    ]

    resume_ready = bool(plan.get("spec")) and bool(progress_items) and bool(resume_context.get("resume_prompt"))
    checks.append(
        _check(
            "resume_context_sufficient",
            "pass" if resume_ready else "fail",
            "Resume context contains plan spec and progress."
            if resume_ready
            else "Resume context is not sufficient to continue the task.",
            evidence={
                "has_spec": bool(plan.get("spec")),
                "progress_count": len(progress_items),
            },
            recommendation="Ensure every long task has a plan spec and at least one progress record.",
        )
    )

    if runtime_status in TERMINAL_STATUSES:
        status_matches = bool(latest_status) and latest_status == runtime_status
        checks.append(
            _check(
                "terminal_status_matches_progress",
                "pass" if status_matches else "fail",
                "Terminal RuntimeTask status matches latest progress."
                if status_matches
                else "Terminal RuntimeTask status does not match latest progress.",
                evidence={"runtime_status": runtime_status, "latest_progress_status": latest_status},
                recommendation="Append a terminal progress entry before marking the RuntimeTask terminal.",
            )
        )
    else:
        checks.append(
            _check(
                "terminal_status_matches_progress",
                "pass",
                "RuntimeTask is not terminal, so terminal progress matching is not required.",
                evidence={"runtime_status": runtime_status, "latest_progress_status": latest_status},
                recommendation="",
            )
        )

    is_completed = runtime_status == "completed" or latest_status == "completed"
    completed_has_evidence = bool(output_paths) or bool(verification_commands)
    checks.append(
        _check(
            "completed_has_output_or_verification",
            "pass" if (not is_completed or completed_has_evidence) else "fail",
            "Completed long task has output paths or verification commands."
            if (not is_completed or completed_has_evidence)
            else "Completed long task has no output paths or verification commands.",
            evidence={
                "runtime_status": runtime_status,
                "latest_progress_status": latest_status,
                "output_path_count": len(output_paths),
                "verification_command_count": len(verification_commands),
            },
            recommendation="Record output_paths or verification commands before claiming completion.",
        )
    )

    needs_reason = runtime_status in INTERRUPTED_STATUSES
    has_reason = _has_terminal_reason(runtime_task, latest_progress)
    checks.append(
        _check(
            "terminal_reason_present",
            "pass" if (not needs_reason or has_reason) else "fail",
            "Interrupted terminal status has an explicit reason."
            if (not needs_reason or has_reason)
            else "Interrupted terminal status is missing an explicit reason.",
            evidence={"runtime_status": runtime_status, "reason_keys": list(REASON_KEYS)},
            recommendation="Record skip, cancel, missed, blocked, or failure reason for interrupted long tasks.",
        )
    )
    checks.extend(validate_agent_work_ledger_completion(work_ledger, terminal_status=runtime_status or latest_status))

    summary = _summarize_checks(checks)
    generated_at = _now_iso()
    report: dict[str, Any] = {
        "schema": "long_task_validation_report.v1",
        "agent_id": str(agent_id),
        "runtime_task_id": runtime_task_id.hex,
        "generated_at": generated_at,
        "passed": summary["fail"] == 0,
        "status": "fail" if summary["fail"] else ("warning" if summary["warning"] else "pass"),
        "summary": summary,
        "checks": checks,
        "resume_context": resume_context,
    }

    if write_report:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / "validation_report.json"
        report["report_artifact"] = {
            "schema": "long_task_validation_report.v1",
            "path": _relative_to_agent(agent_id, path, data_root=data_root),
            "created_at": generated_at,
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


async def record_long_task_validation(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    runtime_task: dict[str, Any] | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    report = validate_long_task_run(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        runtime_task=runtime_task,
        data_root=data_root,
        write_report=True,
    )
    await update_runtime_task_record(
        runtime_task_id.hex,
        metadata_json={
            "long_task_validation": report.get("report_artifact"),
            "long_task_validation_passed": report["passed"],
            "long_task_validation_summary": report["summary"],
        },
    )
    return report
