"""Long-task artifact helpers built on RuntimeTask.

This module keeps long-running task state recoverable without introducing a
second task ledger. RuntimeTask remains the execution record; files under
runtime_artifacts/long_tasks are resumable evidence.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.agent_work_ledger import (
    append_agent_work_ledger_progress,
    build_agent_work_ledger_resume_summary,
    initialize_agent_work_ledger_artifact,
    load_agent_work_ledger,
)
from app.services.harness_contract import build_manifest_resume_context, write_workspace_manifest
from app.services.runtime_task_service import update_runtime_task_record


def _agent_root(agent_id: uuid.UUID, *, data_root: str | Path | None = None) -> Path:
    root = Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)
    return root / str(agent_id)


def _artifact_dir(agent_id: uuid.UUID, runtime_task_id: uuid.UUID, *, data_root: str | Path | None = None) -> Path:
    return _agent_root(agent_id, data_root=data_root) / "runtime_artifacts" / "long_tasks" / runtime_task_id.hex


def _relative_to_agent(agent_id: uuid.UUID, path: Path, *, data_root: str | Path | None = None) -> str:
    return path.relative_to(_agent_root(agent_id, data_root=data_root)).as_posix()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_long_task_plan_artifact(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    spec: str,
    acceptance_criteria: list[str],
    verification_commands: list[str],
    risk_gates: list[str],
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    artifact_dir = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "plan.json"
    payload = {
        "schema": "long_task_plan.v1",
        "runtime_task_id": runtime_task_id.hex,
        "spec": spec.strip(),
        "acceptance_criteria": [item.strip() for item in acceptance_criteria if item.strip()],
        "verification_commands": [item.strip() for item in verification_commands if item.strip()],
        "risk_gates": [item.strip() for item in risk_gates if item.strip()],
        "created_at": _now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    work_ledger = initialize_agent_work_ledger_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        source="long_task_runtime",
        current_phase="planned",
        status="running",
        todo_items=[
            {
                "id": f"acceptance-{index}",
                "title": item,
                "status": "pending",
                "required": True,
            }
            for index, item in enumerate(payload["acceptance_criteria"], start=1)
        ],
        verification=[
            {
                "id": f"verify-{index}",
                "check": command,
                "status": "pending",
                "required": True,
                "command": command,
            }
            for index, command in enumerate(payload["verification_commands"], start=1)
        ],
        evidence_refs=[_relative_to_agent(agent_id, path, data_root=data_root)],
        data_root=data_root,
    )
    return {
        "schema": payload["schema"],
        "path": _relative_to_agent(agent_id, path, data_root=data_root),
        "created_at": payload["created_at"],
        "work_ledger": work_ledger,
    }


def append_long_task_progress_artifact(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    status: str,
    delta: str,
    output_paths: list[str] | None = None,
    blocked_reason: str | None = None,
    completed_todo_ids: list[str] | None = None,
    completed_verification_ids: list[str] | None = None,
    auto_complete_ledger: bool = False,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    artifact_dir = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "progress.jsonl"
    payload = {
        "schema": "long_task_progress.v1",
        "runtime_task_id": runtime_task_id.hex,
        "status": status,
        "delta": delta.strip(),
        "output_paths": [item.strip() for item in (output_paths or []) if item.strip()],
        "blocked_reason": (blocked_reason or "").strip() or None,
        "created_at": _now_iso(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    work_ledger = append_agent_work_ledger_progress(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status=status,
        delta=delta,
        output_paths=output_paths or [],
        blocked_reason=blocked_reason,
        completed_todo_ids=completed_todo_ids,
        completed_verification_ids=completed_verification_ids,
        auto_complete_terminal=auto_complete_ledger,
        data_root=data_root,
    )
    return {
        "schema": payload["schema"],
        "path": _relative_to_agent(agent_id, path, data_root=data_root),
        "created_at": payload["created_at"],
        "status": status,
        "work_ledger": work_ledger,
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_progress(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def build_long_task_resume_context(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    artifact_dir = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
    plan = _load_json(artifact_dir / "plan.json") or {}
    progress_items = _load_progress(artifact_dir / "progress.jsonl")
    manifest_context = build_manifest_resume_context(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=data_root or get_settings().AGENT_DATA_DIR,
    )
    latest_progress = progress_items[-1] if progress_items else None
    work_ledger = load_agent_work_ledger(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        data_root=data_root,
    )
    work_ledger_summary = build_agent_work_ledger_resume_summary(work_ledger)

    prompt_lines = [
        f"Resume long task {runtime_task_id.hex}.",
        f"Spec: {plan.get('spec') or '(missing plan spec)'}",
        "Acceptance criteria:",
    ]
    prompt_lines.extend(f"- {item}" for item in plan.get("acceptance_criteria", []) or ["(none recorded)"])
    prompt_lines.append("Verification commands:")
    prompt_lines.extend(f"- {item}" for item in plan.get("verification_commands", []) or ["(none recorded)"])
    if latest_progress:
        prompt_lines.extend(
            [
                "Latest progress:",
                f"- status: {latest_progress.get('status')}",
                f"- delta: {latest_progress.get('delta')}",
            ]
        )
        if latest_progress.get("blocked_reason"):
            prompt_lines.append(f"- blocked_reason: {latest_progress['blocked_reason']}")
    if manifest_context.get("artifact_refs"):
        prompt_lines.append("Artifact refs:")
        prompt_lines.extend(f"- {ref.get('path')}" for ref in manifest_context.get("artifact_refs") or [])
    if work_ledger_summary.get("present"):
        prompt_lines.extend(
            [
                "Agent Work Ledger:",
                f"- current_phase: {work_ledger_summary.get('current_phase')}",
                f"- open_required_todos: {', '.join(work_ledger_summary.get('open_required_todos') or []) or '(none)'}",
                f"- verification_pending: {', '.join(work_ledger_summary.get('verification_pending') or []) or '(none)'}",
            ]
        )

    return {
        "schema": "long_task_resume_context.v1",
        "runtime_task_id": runtime_task_id.hex,
        "plan": plan,
        "workspace_manifest": manifest_context.get("manifest") or {},
        "artifact_refs": manifest_context.get("artifact_refs") or [],
        "latest_progress": latest_progress,
        "progress_count": len(progress_items),
        "work_ledger": work_ledger_summary,
        "resume_prompt": "\n".join(prompt_lines),
    }


async def record_long_task_plan(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    spec: str,
    acceptance_criteria: list[str],
    verification_commands: list[str],
    risk_gates: list[str],
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    artifact = write_long_task_plan_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        spec=spec,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        risk_gates=risk_gates,
        data_root=data_root,
    )
    artifact_root = _artifact_dir(agent_id, runtime_task_id, data_root=data_root)
    manifest = write_workspace_manifest(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        workspace_root=_agent_root(agent_id, data_root=data_root),
        artifact_paths=[artifact_root / "plan.json", artifact_root / "work_ledger.json"],
        data_root=data_root or get_settings().AGENT_DATA_DIR,
    )
    await update_runtime_task_record(
        runtime_task_id.hex,
        metadata_json={
            "long_task_plan": artifact,
            "agent_work_ledger": artifact.get("work_ledger"),
            "workspace_manifest": manifest,
            "artifact_refs": manifest["artifact_refs"],
        },
    )
    return artifact


async def record_long_task_progress(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    status: str,
    delta: str,
    output_paths: list[str] | None = None,
    blocked_reason: str | None = None,
    completed_todo_ids: list[str] | None = None,
    completed_verification_ids: list[str] | None = None,
    auto_complete_ledger: bool = True,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    artifact = append_long_task_progress_artifact(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        status=status,
        delta=delta,
        output_paths=output_paths,
        blocked_reason=blocked_reason,
        completed_todo_ids=completed_todo_ids,
        completed_verification_ids=completed_verification_ids,
        auto_complete_ledger=auto_complete_ledger,
        data_root=data_root,
    )
    await update_runtime_task_record(
        runtime_task_id.hex,
        status=status if status in {"pending", "running", "completed", "failed", "killed", "skipped"} else "running",
        metadata_json={"long_task_progress": artifact, "agent_work_ledger": artifact.get("work_ledger")},
    )
    return artifact
