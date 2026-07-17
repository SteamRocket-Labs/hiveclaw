"""Recover legacy peer-A2A Session admission and terminal projections.

Session V2 originally created ``delegation_run`` sessions only after a worker
resolved the target runtime. Tasks that failed or were not admitted before that
point therefore had durable RuntimeTask truth but no readable child Session and
no terminal ``child_session`` event on the parent. This repair is dry-run by
default and is safe to repeat because the live projection path is idempotent by
RuntimeTask id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.database import async_session, enter_rls_bypass
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask


_TERMINAL_DELEGATION_STATUSES = ("completed", "failed", "killed", "skipped", "needs_reconciliation")


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class DelegationProjectionDisposition:
    status: str
    reason: str
    summary: str


def classify_terminal_delegation_projection(task: RuntimeTask | Any) -> DelegationProjectionDisposition:
    status = str(getattr(task, "status", "") or "").strip().lower()
    metadata = dict(getattr(task, "metadata_json", None) or {})
    summary = str(getattr(task, "result_summary", None) or "").strip()
    if status == "completed":
        return DelegationProjectionDisposition(
            status="completed",
            reason="delegation_completed",
            summary=summary or "The delegated digital employee completed the task.",
        )
    if status == "skipped":
        if metadata.get("coordination_publish_state") == "blocked" or metadata.get("blocked_by_lease_id"):
            reason = "blocked_by_coordination_lease"
        elif metadata.get("cycle_detected"):
            reason = str(metadata.get("root_item_reason_code") or "runtime_root_cycle_detected")
        else:
            reason = str(metadata.get("root_item_reason_code") or "delegation_not_admitted")
        return DelegationProjectionDisposition(
            status="blocked",
            reason=reason,
            summary=summary or "The delegation was not admitted for execution.",
        )
    if status == "needs_reconciliation":
        return DelegationProjectionDisposition(
            status="blocked",
            reason=str(metadata.get("root_item_reason_code") or "delegation_needs_reconciliation"),
            summary=summary or "The delegation requires reconciliation before it can continue.",
        )
    if metadata.get("dispatch_failed"):
        reason = str(metadata.get("dispatch_failure_reason") or "target_runtime_unavailable")
    elif status == "killed":
        reason = "delegation_cancelled"
    else:
        reason = str(metadata.get("terminal_reason") or metadata.get("error_code") or "delegation_failed")
    return DelegationProjectionDisposition(
        status="failed",
        reason=reason,
        summary=summary or "The delegated digital employee task failed.",
    )


def _record_from_task(task: RuntimeTask) -> dict[str, Any]:
    return {
        "task_id": task.id.hex,
        "task_type": task.task_type,
        "status": task.status,
        "tenant_id": str(task.tenant_id),
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "root_runtime_task_id": str(task.root_runtime_task_id) if task.root_runtime_task_id else None,
        "root_user_id": str(task.root_user_id) if task.root_user_id else None,
        "trace_id": task.trace_id,
        "depth": task.depth,
        "prompt": task.prompt,
        "result_summary": task.result_summary,
        "budget_run_id": str(task.budget_run_id) if task.budget_run_id else None,
        "metadata": dict(task.metadata_json or {}),
    }


async def _projection_truth(task_ids: list[uuid.UUID]) -> tuple[set[uuid.UUID], set[uuid.UUID], set[uuid.UUID]]:
    if not task_ids:
        return set(), set(), set()
    async with async_session() as db, enter_rls_bypass(db, reason="peer delegation Session repair verification"):
        tasks = list((await db.execute(select(RuntimeTask).where(RuntimeTask.id.in_(task_ids)))).scalars().all())
        child_ids = {parsed for task in tasks if (parsed := _uuid_or_none(task.child_session_id)) is not None}
        parent_ids = {parsed for task in tasks if (parsed := _uuid_or_none(task.parent_session_id)) is not None}
        existing_sessions = set(
            (await db.execute(select(ChatSession.id).where(ChatSession.id.in_(child_ids | parent_ids)))).scalars().all()
        )
        projected_task_ids = set(
            (
                await db.execute(
                    select(ChatTranscriptEvent.run_id).where(
                        ChatTranscriptEvent.run_id.in_(task_ids),
                        ChatTranscriptEvent.event_type == "child_session",
                    )
                )
            )
            .scalars()
            .all()
        )
    return existing_sessions, projected_task_ids, {task.id for task in tasks}


async def repair_peer_delegation_session_projections(
    *,
    apply: bool = False,
    parent_session_id: uuid.UUID | str | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    parent_session_uuid = _uuid_or_none(parent_session_id) if parent_session_id is not None else None
    async with async_session() as db, enter_rls_bypass(db, reason="peer delegation Session repair scan"):
        stmt = (
            select(RuntimeTask)
            .where(
                RuntimeTask.task_type.in_(("delegation", "a2a_delegation")),
                RuntimeTask.status.in_(_TERMINAL_DELEGATION_STATUSES),
                RuntimeTask.parent_agent_id.is_not(None),
                RuntimeTask.child_agent_id.is_not(None),
                RuntimeTask.parent_session_id.is_not(None),
                RuntimeTask.child_session_id.is_not(None),
            )
            .order_by(RuntimeTask.created_at.asc(), RuntimeTask.id.asc())
            .limit(max(1, int(limit)))
        )
        if parent_session_uuid is not None:
            stmt = stmt.where(RuntimeTask.parent_session_id == str(parent_session_uuid))
        tasks = list((await db.execute(stmt)).scalars().all())

    task_ids = [task.id for task in tasks]
    existing_sessions, projected_task_ids, _ = await _projection_truth(task_ids)
    candidates: list[tuple[RuntimeTask, DelegationProjectionDisposition]] = []
    invalid: list[dict[str, str]] = []
    for task in tasks:
        child_session_id = _uuid_or_none(task.child_session_id)
        parent_id = _uuid_or_none(task.parent_session_id)
        if child_session_id is None or parent_id is None:
            invalid.append({"task_id": task.id.hex, "reason": "invalid_session_identifier"})
            continue
        if parent_id not in existing_sessions:
            invalid.append({"task_id": task.id.hex, "reason": "parent_session_missing"})
            continue
        if child_session_id not in existing_sessions or task.id not in projected_task_ids:
            candidates.append((task, classify_terminal_delegation_projection(task)))

    repaired: list[str] = []
    failures: list[dict[str, str]] = []
    if apply:
        from app.agents.orchestrator import _project_delegation_record_terminal_to_parent

        for task, disposition in candidates:
            try:
                await _project_delegation_record_terminal_to_parent(
                    record=_record_from_task(task),
                    status=disposition.status,
                    summary=disposition.summary,
                    reason=disposition.reason,
                )
            except Exception as exc:  # each task remains independently retryable
                failures.append({"task_id": task.id.hex, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            existing_after, projected_after, _ = await _projection_truth([task.id])
            child_session_id = _uuid_or_none(task.child_session_id)
            if child_session_id in existing_after and task.id in projected_after:
                repaired.append(task.id.hex)
            else:
                failures.append({"task_id": task.id.hex, "reason": "projection_verification_failed"})

    return {
        "mode": "apply" if apply else "dry_run",
        "scanned": len(tasks),
        "candidate_count": len(candidates),
        "candidates": [
            {
                "task_id": task.id.hex,
                "parent_session_id": task.parent_session_id,
                "child_session_id": task.child_session_id,
                "runtime_status": task.status,
                "projected_status": disposition.status,
                "reason": disposition.reason,
            }
            for task, disposition in candidates
        ],
        "repaired_count": len(repaired),
        "repaired": repaired,
        "invalid": invalid,
        "failures": failures,
    }
