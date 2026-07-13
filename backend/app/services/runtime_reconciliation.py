"""Admin-facing RuntimeTask reconciliation helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask

logger = logging.getLogger(__name__)

RECONCILIATION_STATUS = "needs_reconciliation"
RECONCILED_STATUS = "completed"
_ACTIONS = {"mark_resolved", "archive", "retry"}
_SPECIALIZED_TASK_TYPES = {"business_task"}
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FRAME_DECISION_LIMIT = 25_600
_RECOVERY_TARGET_LIMIT = 200
_REVIEWABLE_MANIFEST_STATES = {
    "missing",
    "present",
    "incomplete_authority",
    "corrupt",
    "nonregular",
    "identity_mismatch",
}
_BYTE_BOUND_MANIFEST_STATES = {
    "present",
    "incomplete_authority",
    "corrupt",
    "identity_mismatch",
}
_REVIEWED_EVIDENCE_KEYS = (
    "expected_manifest_state",
    "expected_manifest_ref",
    "expected_sha256",
)


class RuntimeReconciliationNotFound(LookupError):
    pass


class RuntimeReconciliationConflict(RuntimeError):
    pass


def _raise_specialized_reconciliation_conflict(task: RuntimeTask) -> None:
    if task.task_type != "business_task":
        return
    metadata = _metadata(task)
    agent_id = task.parent_agent_id or task.child_agent_id
    business_task_id = str(metadata.get("business_task_id") or "").strip()
    if agent_id is not None and business_task_id:
        raise RuntimeReconciliationConflict(
            "business_task reconciliation must use its specialized endpoint: "
            f"/agents/{agent_id}/tasks/{business_task_id}/reconcile"
        )
    raise RuntimeReconciliationConflict(
        "business_task reconciliation requires the specialized business task endpoint, "
        "but its agent/task binding is incomplete"
    )


def _coerce_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _dt(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _metadata(task: RuntimeTask) -> dict[str, Any]:
    return dict(getattr(task, "metadata_json", None) or {})


def consume_completed_reconciliation_retry(
    metadata: dict[str, Any] | None,
    *,
    next_claim_version: int,
) -> dict[str, Any]:
    """Advance an operator-approved retry into a new worker claim epoch.

    A completed reconciliation operation blocks the stale worker that observed
    the unknown outcome.  Only a later monotonic claim may consume a completed
    ``retry`` decision; other resolved actions remain terminal authority.
    """

    updated = dict(metadata or {})
    operation = updated.get("reconciliation_operation")
    if not isinstance(operation, dict):
        return updated
    valid_retry = (
        operation.get("status") == "completed"
        and operation.get("action") == "retry"
        and updated.get("reconciliation_status") == "retry_requested"
        and updated.get("needs_reconciliation") is False
    )
    current_claim_version = int(updated.get("claim_version") or 0)
    if not valid_retry or int(next_claim_version) <= current_claim_version:
        raise RuntimeReconciliationConflict(
            "RuntimeTask reconciliation operation is not an approved completed retry for a new claim epoch"
        )
    consumed = [dict(item) for item in updated.get("consumed_reconciliation_operations", []) if isinstance(item, dict)]
    consumed.append(
        {
            **operation,
            "consumed_at": datetime.now(timezone.utc).isoformat(),
            "consumed_claim_version": int(next_claim_version),
        }
    )
    updated["consumed_reconciliation_operations"] = consumed[-20:]
    updated.pop("reconciliation_operation", None)
    updated["reconciliation_status"] = "retry_in_progress"
    updated["reconciliation_retry_claim_version"] = int(next_claim_version)
    return updated


def _recovery_resolution_targets(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw_targets = metadata.get("recovery_resolution_targets")
    targets: list[dict[str, Any]] = []
    if raw_targets is not None:
        if not isinstance(raw_targets, list) or not raw_targets:
            raise RuntimeReconciliationConflict("Recovery resolution targets are malformed")
        if len(raw_targets) > _RECOVERY_TARGET_LIMIT:
            raise RuntimeReconciliationConflict(
                f"Recovery resolution target set exceeds the {_RECOVERY_TARGET_LIMIT}-target limit"
            )
        for raw in raw_targets:
            if not isinstance(raw, dict):
                raise RuntimeReconciliationConflict("Recovery resolution target is malformed")
            missing_review_keys = [key for key in _REVIEWED_EVIDENCE_KEYS if key not in raw]
            if missing_review_keys:
                raise RuntimeReconciliationConflict(
                    "Recovery resolution target reviewed evidence keys are missing: " + ", ".join(missing_review_keys)
                )
            session_id = str(raw.get("session_id") or "").strip()
            try:
                agent_id = _coerce_uuid(raw.get("agent_id"))
                runtime_task_id = _coerce_uuid(raw.get("runtime_task_id"))
            except (TypeError, ValueError) as exc:
                raise RuntimeReconciliationConflict("Recovery resolution target identity is invalid") from exc
            if not session_id:
                raise RuntimeReconciliationConflict("Recovery resolution target session is missing")
            targets.append(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "runtime_task_id": runtime_task_id,
                    "source": str(raw.get("source") or "runtime_task"),
                    "expected_manifest_state": raw.get("expected_manifest_state"),
                    "expected_manifest_ref": raw.get("expected_manifest_ref"),
                    "expected_sha256": raw.get("expected_sha256"),
                    "expected_checkpoint_seq": raw.get("expected_checkpoint_seq"),
                    "expected_claim_version": raw.get("expected_claim_version"),
                    "expected_claim_worker_id": raw.get("expected_claim_worker_id"),
                    "workflow_step_id": str(raw.get("workflow_step_id") or "").strip() or None,
                    "workflow_leaf_id": str(raw.get("workflow_leaf_id") or "").strip() or None,
                }
            )
    else:
        legacy = {
            "agent_id": metadata.get("recovery_agent_id"),
            "session_id": metadata.get("recovery_session_id"),
            "runtime_task_id": metadata.get("recovery_runtime_task_id"),
        }
        if any(legacy.values()) and not all(legacy.values()):
            raise RuntimeReconciliationConflict("Recovery reconciliation identity is incomplete")
        if all(legacy.values()):
            try:
                legacy_review_keys = (
                    "recovery_manifest_state",
                    "recovery_manifest_ref",
                    "recovery_manifest_sha256",
                )
                missing_review_keys = [key for key in legacy_review_keys if key not in metadata]
                if missing_review_keys:
                    raise RuntimeReconciliationConflict(
                        "Legacy recovery target reviewed evidence keys are missing: " + ", ".join(missing_review_keys)
                    )
                legacy_manifest_state = metadata.get("recovery_manifest_state")
                if legacy_manifest_state == "valid":
                    legacy_manifest_state = "present"
                targets.append(
                    {
                        "agent_id": _coerce_uuid(legacy["agent_id"]),
                        "session_id": str(legacy["session_id"]),
                        "runtime_task_id": _coerce_uuid(legacy["runtime_task_id"]),
                        "source": "runtime_task",
                        "expected_manifest_state": legacy_manifest_state,
                        "expected_manifest_ref": metadata.get("recovery_manifest_ref"),
                        "expected_sha256": metadata.get("recovery_manifest_sha256"),
                        "expected_checkpoint_seq": metadata.get("recovery_checkpoint_seq"),
                        "expected_claim_version": metadata.get("claim_version"),
                        "expected_claim_worker_id": metadata.get("claim_worker_id"),
                    }
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeReconciliationConflict("Recovery reconciliation identity is invalid") from exc

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[uuid.UUID, str, uuid.UUID]] = set()
    for target in targets:
        key = (target["agent_id"], target["session_id"], target["runtime_task_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _serialized_resolution_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized = []
    for target in targets:
        item = {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in target.items()
            if value is not None
        }
        # Reviewed absence is evidence too.  Preserve these keys as explicit
        # nulls so a missing/nonregular review cannot alias an unreviewed
        # producer payload in the action digest.
        for key in _REVIEWED_EVIDENCE_KEYS:
            item[key] = target.get(key)
        serialized.append(item)
    return sorted(
        serialized,
        key=lambda target: (
            str(target.get("runtime_task_id") or ""),
            str(target.get("agent_id") or ""),
            str(target.get("session_id") or ""),
            str(target.get("source") or ""),
        ),
    )


def _reviewed_target_evidence_incomplete_reasons(targets: list[dict[str, Any]]) -> set[str]:
    reasons: set[str] = set()
    for target in targets:
        state = str(target.get("expected_manifest_state") or "").strip()
        if not state:
            reasons.add("recovery_target_reviewed_state_missing")
            continue
        if state not in _REVIEWABLE_MANIFEST_STATES:
            reasons.add("recovery_target_reviewed_state_invalid")
            continue
        if state in _BYTE_BOUND_MANIFEST_STATES:
            manifest_ref = str(target.get("expected_manifest_ref") or "").strip()
            expected_sha256 = str(target.get("expected_sha256") or "").strip()
            if not manifest_ref or not _DIGEST_RE.fullmatch(expected_sha256):
                reasons.add("recovery_target_byte_evidence_missing")
    return reasons


def _recovery_frame_id(*parts: Any) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    return f"recovery-refresh:{hashlib.sha256(payload).hexdigest()[:24]}"


def _refresh_manifest_evidence_snapshot(
    *,
    targets: list[dict[str, Any]],
    tenant_id: uuid.UUID,
) -> dict[str, Any]:
    """Re-read byte/CAS evidence after a typed manifest drift conflict."""

    from app.runtime.recovery_manifest import inspect_recovery_manifest_checkpoint
    from app.tools.registry import (
        is_destructive_tool,
        is_parallel_safe_tool,
        is_read_only_tool,
        is_workspace_mutating_tool,
    )

    refreshed_targets: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    incomplete: set[str] = set()
    contains_completed_side_effect_evidence = False
    for target_index, target in enumerate(targets):
        agent_id = str(target.get("agent_id") or "").strip()
        session_id = str(target.get("session_id") or "").strip()
        runtime_task_id = str(target.get("runtime_task_id") or "").strip()
        source = str(target.get("source") or "runtime_task").strip() or "runtime_task"
        if not agent_id or not session_id or not runtime_task_id:
            incomplete.add("recovery_refresh_target_identity_incomplete")
            continue
        refreshed_target = {
            "agent_id": agent_id,
            "session_id": session_id,
            "runtime_task_id": runtime_task_id,
            "source": source,
            "expected_manifest_ref": None,
            "expected_sha256": None,
        }
        for authority_key in ("workflow_step_id", "workflow_leaf_id"):
            authority_value = str(target.get(authority_key) or "").strip()
            if authority_value:
                refreshed_target[authority_key] = authority_value
        inspection = inspect_recovery_manifest_checkpoint(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
        )
        state = str((inspection or {}).get("state") or "missing")
        refreshed_target["expected_manifest_state"] = "present" if state == "valid" else state
        receipt = (inspection or {}).get("receipt") if isinstance(inspection, dict) else None
        if isinstance(receipt, dict):
            if receipt.get("ref"):
                refreshed_target["expected_manifest_ref"] = receipt["ref"]
            if receipt.get("sha256"):
                refreshed_target["expected_sha256"] = receipt["sha256"]
        reviewed_state = "present" if state == "valid" else state
        if reviewed_state not in _REVIEWABLE_MANIFEST_STATES:
            incomplete.add("recovery_refresh_manifest_state_invalid")
        if reviewed_state in _BYTE_BOUND_MANIFEST_STATES and (
            not refreshed_target["expected_manifest_ref"] or not refreshed_target["expected_sha256"]
        ):
            incomplete.add("recovery_refresh_manifest_byte_evidence_missing")
        if state == "valid":
            for key in (
                "expected_checkpoint_seq",
                "expected_claim_version",
                "expected_claim_worker_id",
            ):
                value = (inspection or {}).get(key)
                if value is not None:
                    refreshed_target[key] = value
        refreshed_targets.append(refreshed_target)

        target_frames: list[dict[str, Any]] = []
        for frame_index, raw in enumerate((inspection or {}).get("pending_tool_frames", [])):
            if not isinstance(raw, dict):
                continue
            tool_name = str(raw.get("tool_name") or "unknown_tool").strip()
            tool_call_id = str(raw.get("tool_call_id") or "").strip() or _recovery_frame_id(
                runtime_task_id,
                "pending",
                frame_index,
                tool_name,
            )
            target_frames.append(
                {
                    "runtime_task_id": runtime_task_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "needs_reconciliation",
                    "event_type": "refreshed_pending_tool_frame",
                    "reason": "recovery_manifest_pending_outcome_unknown",
                }
            )
        for outcome_index, raw in enumerate((inspection or {}).get("recent_tool_outcomes", [])):
            if not isinstance(raw, dict):
                continue
            tool_name = str(raw.get("tool") or raw.get("tool_name") or "unknown_tool").strip()
            summary = str(raw.get("summary") or "").strip()
            if tool_name.startswith("session_memory"):
                continue
            completed_outcome_is_safe = bool(
                is_read_only_tool(tool_name)
                and is_parallel_safe_tool(tool_name)
                and not is_workspace_mutating_tool(tool_name)
                and not is_destructive_tool(tool_name)
            )
            if completed_outcome_is_safe:
                continue
            contains_completed_side_effect_evidence = True
            target_frames.append(
                {
                    "runtime_task_id": runtime_task_id,
                    "tool_call_id": _recovery_frame_id(
                        runtime_task_id,
                        "outcome",
                        outcome_index,
                        tool_name,
                        summary,
                    ),
                    "tool_name": tool_name,
                    "status": "needs_reconciliation",
                    "event_type": "refreshed_completed_tool",
                    "reason": "completed_tool_without_runtime_terminal",
                }
            )
        writes = [
            *list((inspection or {}).get("recent_writes", [])),
            *list((inspection or {}).get("current_turn_writes", [])),
        ]
        for write_index, path in enumerate(dict.fromkeys(str(value) for value in writes if str(value).strip())):
            contains_completed_side_effect_evidence = True
            target_frames.append(
                {
                    "runtime_task_id": runtime_task_id,
                    "tool_call_id": _recovery_frame_id(runtime_task_id, "write", write_index, path),
                    "tool_name": "workspace_write",
                    "status": "needs_reconciliation",
                    "event_type": "refreshed_completed_write",
                    "reason": "completed_write_without_runtime_terminal",
                }
            )
        if not target_frames:
            target_frames.append(
                {
                    "runtime_task_id": runtime_task_id,
                    "tool_call_id": _recovery_frame_id(runtime_task_id, state, target_index),
                    "tool_name": "runtime_invocation",
                    "status": "needs_reconciliation",
                    "event_type": "refreshed_runtime_manifest",
                    "reason": f"recovery_manifest_{state}",
                }
            )
        frames.extend(target_frames)

    deduped_frames = {
        (
            str(frame.get("runtime_task_id") or ""),
            str(frame.get("tool_call_id") or ""),
            str(frame.get("tool_name") or ""),
        ): frame
        for frame in frames
    }
    all_frames = list(deduped_frames.values())
    if len(all_frames) > _FRAME_DECISION_LIMIT:
        incomplete.add("recovery_refresh_frame_limit_exceeded")
    return {
        "targets": _serialized_resolution_targets(refreshed_targets),
        "frames": all_frames[-_FRAME_DECISION_LIMIT:],
        "incomplete_reasons": sorted(incomplete),
        "contains_completed_side_effect_evidence": contains_completed_side_effect_evidence,
    }


def _canonical_recovery_evidence(task: RuntimeTask | Any) -> dict[str, Any]:
    """Build the action-bound, secret-free operator evidence contract."""

    metadata = _metadata(task)
    incomplete_reasons: set[str] = set()
    persisted_incomplete_reasons = metadata.get("recovery_evidence_incomplete_reasons", [])
    if isinstance(persisted_incomplete_reasons, list):
        incomplete_reasons.update(str(reason).strip() for reason in persisted_incomplete_reasons if str(reason).strip())
    elif persisted_incomplete_reasons:
        incomplete_reasons.add("malformed_recovery_evidence_incomplete_reasons")
    try:
        targets = _serialized_resolution_targets(_recovery_resolution_targets(metadata))
    except RuntimeReconciliationConflict:
        targets = []
        incomplete_reasons.add("malformed_recovery_targets")
    target_ids = {str(target["runtime_task_id"]) for target in targets}
    if not targets:
        incomplete_reasons.add("no_recovery_targets")
    incomplete_reasons.update(_reviewed_target_evidence_incomplete_reasons(targets))

    candidates: list[tuple[int, dict[str, Any]]] = []

    def add_frame(raw: Any, *, runtime_task_id: Any, source: str, priority: int) -> None:
        if not isinstance(raw, dict):
            incomplete_reasons.add("malformed_recovery_frame")
            return
        try:
            normalized_runtime_task_id = str(_coerce_uuid(runtime_task_id))
        except (TypeError, ValueError):
            incomplete_reasons.add("recovery_frame_target_invalid")
            return
        tool_call_id = str(raw.get("tool_call_id") or "").strip()
        tool_name = str(raw.get("tool_name") or "").strip()
        if not tool_call_id or not tool_name:
            incomplete_reasons.add("recovery_frame_identity_missing")
            return
        if normalized_runtime_task_id not in target_ids:
            incomplete_reasons.add("recovery_frame_target_not_reviewed")
            return
        frame: dict[str, Any] = {
            "runtime_task_id": normalized_runtime_task_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        }
        for key in ("status", "event_type", "reason"):
            value = raw.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                frame[key] = str(value)
        frame["source"] = source
        candidates.append((priority, frame))

    raw_prior = metadata.get("prior_run_recovery_reconciliations", [])
    if not isinstance(raw_prior, list):
        incomplete_reasons.add("malformed_prior_recovery_evidence")
        raw_prior = []
    for item in raw_prior:
        if not isinstance(item, dict):
            incomplete_reasons.add("malformed_prior_recovery_evidence")
            continue
        source_runtime_task_id = item.get("source_runtime_task_id")
        raw_frames = item.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            incomplete_reasons.add("prior_recovery_frames_missing")
            continue
        for raw_frame in raw_frames:
            add_frame(
                raw_frame,
                runtime_task_id=source_runtime_task_id,
                source="prior_run",
                priority=0,
            )

    raw_legacy = metadata.get("recovery_tool_frames", [])
    if not isinstance(raw_legacy, list):
        incomplete_reasons.add("malformed_legacy_recovery_frames")
        raw_legacy = []
    legacy_default_runtime_task_id = metadata.get("recovery_runtime_task_id") or getattr(task, "id", None)
    for raw_frame in raw_legacy:
        runtime_task_id = (
            raw_frame.get("runtime_task_id") if isinstance(raw_frame, dict) else None
        ) or legacy_default_runtime_task_id
        add_frame(
            raw_frame,
            runtime_task_id=runtime_task_id,
            source="legacy",
            priority=1,
        )

    frames_by_identity: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    for priority, frame in candidates:
        key = (
            frame["runtime_task_id"],
            frame["tool_call_id"],
            frame["tool_name"],
        )
        candidate_rank = (
            priority,
            json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        existing = frames_by_identity.get(key)
        if existing is None:
            frames_by_identity[key] = (priority, frame)
            continue
        existing_rank = (
            existing[0],
            json.dumps(existing[1], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        if candidate_rank < existing_rank:
            frames_by_identity[key] = (priority, frame)
    frames = [item[1] for item in frames_by_identity.values()]
    frames.sort(
        key=lambda frame: (
            frame["runtime_task_id"],
            frame["tool_call_id"],
            frame["tool_name"],
        )
    )
    if not frames:
        incomplete_reasons.add("no_recovery_frames")
    if len(frames) > _FRAME_DECISION_LIMIT:
        incomplete_reasons.add("recovery_frame_limit_exceeded")

    payload = {
        "schema": "runtime_recovery_evidence.v1",
        "targets": targets,
        "frames": frames[:_FRAME_DECISION_LIMIT],
        "evidence_complete": not incomplete_reasons,
        "incomplete_reasons": sorted(incomplete_reasons),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {**payload, "digest": hashlib.sha256(canonical).hexdigest()}


def _operation_view(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("reconciliation_operation")
    if not isinstance(raw, dict):
        return None
    allowed = {
        "schema",
        "operation_id",
        "status",
        "action",
        "reason",
        "actor_user_id",
        "resumed_by_user_id",
        "evidence_digest",
        "frame_decisions",
        "group_root_task_id",
        "group_member_task_ids",
        "prepared_at",
        "resumed_at",
        "failed_at",
        "completed_at",
        "error",
    }
    return {key: raw[key] for key in allowed if key in raw}


def _group_roots(tasks: list[RuntimeTask]) -> dict[uuid.UUID, uuid.UUID]:
    """Map open group members to the one carrier/root shown to operators."""

    roots: dict[uuid.UUID, uuid.UUID] = {task.id: task.id for task in tasks}
    task_ids = set(roots)
    explicit: list[tuple[uuid.UUID, uuid.UUID]] = []
    inferred: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task in sorted(tasks, key=lambda row: str(row.id)):
        metadata = _metadata(task)
        operation = metadata.get("reconciliation_operation")
        if isinstance(operation, dict):
            try:
                root_id = _coerce_uuid(operation.get("group_root_task_id"))
            except (TypeError, ValueError):
                root_id = None
            if root_id in task_ids:
                explicit.append((task.id, root_id))
        try:
            targets = _recovery_resolution_targets(metadata)
        except RuntimeReconciliationConflict:
            continue
        target_ids = {target["runtime_task_id"] for target in targets}
        is_carrier = any(
            target["runtime_task_id"] == task.id and target.get("source") in {"carrier_run", "current_run"}
            for target in targets
        )
        if is_carrier and len(target_ids) > 1:
            for member_id in sorted(target_ids & task_ids, key=str):
                inferred.setdefault(member_id, []).append(task.id)
    for member_id, candidates in inferred.items():
        roots[member_id] = min(candidates, key=str)
    for member_id, root_id in explicit:
        roots[member_id] = root_id
    return roots


def runtime_reconciliation_view(task: RuntimeTask) -> dict[str, Any]:
    metadata = _metadata(task)
    return {
        "task_id": str(task.id),
        "tenant_id": str(task.tenant_id) if task.tenant_id else None,
        "task_type": task.task_type,
        "status": task.status,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "trace_id": task.trace_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "reason": metadata.get("reconciliation_reason") or metadata.get("restart_resume_blocker"),
        "side_effect_risk": metadata.get("side_effect_risk"),
        "retry_allowed": bool(metadata.get("reconciliation_retry_allowed")),
        "result_summary": task.result_summary,
        "metadata": metadata,
        "recovery_evidence": _canonical_recovery_evidence(task),
        "reconciliation_operation": _operation_view(metadata),
        "created_at": _dt(task.created_at),
        "started_at": _dt(task.started_at),
        "completed_at": _dt(task.completed_at),
    }


async def list_runtime_reconciliation_tasks(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str = RECONCILIATION_STATUS,
    limit: int = 50,
    agent_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(RuntimeTask)
        .where(
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.status == status,
            RuntimeTask.task_type.notin_(_SPECIALIZED_TASK_TYPES),
        )
        .order_by(RuntimeTask.created_at.desc())
    )
    if agent_id is not None:
        stmt = stmt.where(RuntimeTask.parent_agent_id == agent_id)
    result = await db.execute(stmt)
    tasks = list(result.scalars().all())
    roots = _group_roots(tasks)
    visible = [task for task in tasks if roots.get(task.id, task.id) == task.id]
    bounded_limit = max(1, min(int(limit), 200))
    return [runtime_reconciliation_view(task) for task in visible[:bounded_limit]]


async def get_runtime_reconciliation_task(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, Any] | None:
    try:
        runtime_task_id = _coerce_uuid(task_id)
    except ValueError:
        return None
    result = await db.execute(
        select(RuntimeTask).where(RuntimeTask.id == runtime_task_id, RuntimeTask.tenant_id == tenant_id)
    )
    task = result.scalar_one_or_none()
    return runtime_reconciliation_view(task) if task is not None else None


async def mark_runtime_task_recovery_reconciliation(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    session_id: str | None,
    event: dict[str, Any],
    recovery_manifest_receipt: dict[str, Any] | None = None,
    recovery_authority: dict[str, Any] | None = None,
    expected_status: str | tuple[str, ...] | None = None,
    expected_claim_version: int | None = None,
    expected_claim_worker_id: str | None = None,
) -> dict[str, Any] | None:
    """Project a kernel recovery blocker onto the admin RuntimeTask surface."""

    try:
        runtime_task_id = _coerce_uuid(task_id)
    except ValueError:
        return None
    result = await db.execute(
        select(RuntimeTask)
        .where(RuntimeTask.id == runtime_task_id, RuntimeTask.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    metadata = _metadata(task)
    authority = dict(recovery_authority or {})
    is_workflow_leaf = str(authority.get("type") or "") == "workflow_leaf"
    expected_statuses = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status or ())
    status_matches = not expected_statuses or str(task.status or "") in expected_statuses
    continuing_workflow_incident = is_workflow_leaf and str(task.status or "") == RECONCILIATION_STATUS
    if not status_matches and not continuing_workflow_incident:
        raise RuntimeReconciliationConflict("Runtime recovery projection lost its expected running status")
    if expected_claim_version is not None and int(task.claim_version or 0) != int(expected_claim_version):
        raise RuntimeReconciliationConflict("Runtime recovery projection has a stale claim version")
    if expected_claim_worker_id is not None and str(task.claimed_by or "") != str(expected_claim_worker_id):
        raise RuntimeReconciliationConflict("Runtime recovery projection has a stale claim worker")
    if isinstance(metadata.get("reconciliation_operation"), dict):
        raise RuntimeReconciliationConflict("Runtime recovery projection cannot overwrite a reconciliation operation")
    policy = event.get("runtime_failure_policy") if isinstance(event.get("runtime_failure_policy"), dict) else {}
    workflow_step_id = str(authority.get("workflow_step_id") or "").strip()
    workflow_leaf_id = str(authority.get("workflow_leaf_id") or "").strip()
    recovery_session_id = str(session_id or task.parent_session_id or "") or None
    if is_workflow_leaf:
        if task.task_type != "workflow" or not workflow_step_id or not workflow_leaf_id:
            raise RuntimeReconciliationConflict("Workflow leaf recovery event authority is incomplete")
        try:
            workflow_run_id = _coerce_uuid(authority.get("workflow_run_id"))
        except (TypeError, ValueError) as exc:
            raise RuntimeReconciliationConflict("Workflow leaf recovery run authority is invalid") from exc
        if workflow_run_id != task.id:
            raise RuntimeReconciliationConflict("Workflow leaf recovery event targets a different RuntimeTask")
        from app.models.workflow import WorkflowLeafCall, WorkflowStep
        from app.runtime.workflow_engine import workflow_leaf_recovery_identity

        identity = workflow_leaf_recovery_identity(task.id, workflow_step_id, workflow_leaf_id)
        if recovery_session_id != identity.session_id:
            raise RuntimeReconciliationConflict("Workflow leaf recovery session is not deterministic")
        step = (
            await db.execute(
                select(WorkflowStep)
                .where(
                    WorkflowStep.tenant_id == tenant_id,
                    WorkflowStep.run_id == task.id,
                    WorkflowStep.step_id == workflow_step_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if step is None:
            raise RuntimeReconciliationConflict("Workflow leaf recovery event has no authoritative step journal")
        step.status = "unknown_requires_reconciliation"
        if workflow_leaf_id != "singleton":
            leaf = (
                await db.execute(
                    select(WorkflowLeafCall)
                    .where(
                        WorkflowLeafCall.tenant_id == tenant_id,
                        WorkflowLeafCall.run_id == task.id,
                        WorkflowLeafCall.step_id == workflow_step_id,
                        WorkflowLeafCall.leaf_id == workflow_leaf_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if leaf is None:
                raise RuntimeReconciliationConflict("Workflow leaf recovery event has no authoritative leaf journal")
            leaf.status = "needs_reconciliation"
        if task.parent_agent_id is not None:
            from app.services.agent_work_ledger import upsert_agent_work_ledger_todo

            upsert_agent_work_ledger_todo(
                agent_id=task.parent_agent_id,
                title=f"workflow step: {workflow_step_id}",
                status="pending",
                runtime_task_id=task.id,
                source="workflow_runtime_reconciliation",
            )
    frame = {
        "runtime_task_id": str(task.id),
        "tool_name": str(event.get("tool_name") or ""),
        "tool_call_id": str(event.get("tool_call_id") or ""),
        "status": "needs_reconciliation",
        "event_type": str(event.get("event_type") or "recovery_reconciliation_required"),
        "reason": str(event.get("reason") or "tool_execution_outcome_unknown"),
    }
    if is_workflow_leaf:
        frame.update(
            {
                "workflow_step_id": workflow_step_id,
                "workflow_leaf_id": workflow_leaf_id,
            }
        )
    frames = [item for item in metadata.get("recovery_tool_frames", []) if isinstance(item, dict)]
    frames = [
        item
        for item in frames
        if (
            str(item.get("runtime_task_id") or task.id),
            str(item.get("tool_call_id") or ""),
            str(item.get("tool_name") or ""),
            str(item.get("workflow_step_id") or ""),
            str(item.get("workflow_leaf_id") or ""),
        )
        != (
            str(task.id),
            frame["tool_call_id"],
            frame["tool_name"],
            workflow_step_id if is_workflow_leaf else "",
            workflow_leaf_id if is_workflow_leaf else "",
        )
    ]
    frames.append(frame)
    incomplete_reasons = {
        str(reason).strip()
        for reason in metadata.get("recovery_evidence_incomplete_reasons", [])
        if str(reason).strip()
    }
    if len(frames) > _FRAME_DECISION_LIMIT:
        incomplete_reasons.add("recovery_event_frame_limit_exceeded")
        frames = frames[-_FRAME_DECISION_LIMIT:]
    if is_workflow_leaf:
        # A live fanout event can race sibling leaves that have already crossed
        # the execution boundary.  Keep operator actions fail-closed until the
        # WorkflowRuntimeService has awaited the fanout and scanned every
        # deterministic leaf manifest.
        incomplete_reasons.add("workflow_fanout_evidence_aggregation_pending")
        raw_affected_steps = metadata.get("needs_reconciliation", [])
        if not isinstance(raw_affected_steps, (list, tuple, set)):
            raw_affected_steps = ()
        affected_steps = {str(value).strip() for value in raw_affected_steps if str(value).strip()}
        affected_steps.add(workflow_step_id)
        needs_reconciliation: bool | list[str] = sorted(affected_steps)
        retry_allowed = False
    else:
        needs_reconciliation = True
        retry_allowed = bool(
            policy.get("retryable") and str(policy.get("side_effect_risk") or "unknown") in {"none", "read_only"}
        )
    metadata.update(
        {
            "needs_reconciliation": needs_reconciliation,
            "reconciliation_status": "open",
            "reconciliation_reason": frame["reason"],
            "side_effect_risk": str(policy.get("side_effect_risk") or "unknown"),
            "reconciliation_retry_allowed": retry_allowed,
            "recovery_agent_id": str(agent_id) if agent_id else None,
            "recovery_session_id": recovery_session_id,
            "recovery_runtime_task_id": str(task.id),
            "recovery_tool_frames": frames,
        }
    )
    if incomplete_reasons:
        metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete_reasons)
        metadata["recovery_evidence_status"] = "incomplete"
    if agent_id is not None and recovery_session_id is not None:
        from app.runtime.recovery_manifest import (
            inspect_recovery_manifest_checkpoint,
            reviewed_recovery_manifest_evidence,
        )

        if isinstance(recovery_manifest_receipt, dict):
            inspection = {"state": "valid", "receipt": recovery_manifest_receipt}
        else:
            inspection = await asyncio.to_thread(
                inspect_recovery_manifest_checkpoint,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=recovery_session_id,
                runtime_task_id=task.id,
            )
        target = {
            "agent_id": str(agent_id),
            "session_id": recovery_session_id,
            "runtime_task_id": str(task.id),
            "source": "workflow_leaf" if is_workflow_leaf else "current_run",
            **reviewed_recovery_manifest_evidence(inspection),
            "expected_claim_version": int(getattr(task, "claim_version", 0) or 0),
            "expected_claim_worker_id": str(getattr(task, "claimed_by", None) or "unknown"),
        }
        if is_workflow_leaf:
            target.update(
                {
                    "workflow_step_id": workflow_step_id,
                    "workflow_leaf_id": workflow_leaf_id,
                }
            )
        if isinstance(recovery_manifest_receipt, dict):
            if recovery_manifest_receipt.get("ref") is not None:
                target["expected_manifest_ref"] = recovery_manifest_receipt["ref"]
            if recovery_manifest_receipt.get("sha256") is not None:
                target["expected_sha256"] = recovery_manifest_receipt["sha256"]
        if is_workflow_leaf:
            existing_targets = [
                dict(item) for item in metadata.get("recovery_resolution_targets", []) if isinstance(item, dict)
            ]
            existing_targets = [
                item
                for item in existing_targets
                if (
                    str(item.get("source") or ""),
                    str(item.get("workflow_step_id") or ""),
                    str(item.get("workflow_leaf_id") or ""),
                )
                != ("workflow_leaf", workflow_step_id, workflow_leaf_id)
            ]
            existing_targets.append(target)
            if len(existing_targets) > _RECOVERY_TARGET_LIMIT:
                incomplete_reasons.add("recovery_event_target_limit_exceeded")
                existing_targets = existing_targets[-_RECOVERY_TARGET_LIMIT:]
                metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete_reasons)
            metadata["recovery_resolution_targets"] = existing_targets
        else:
            metadata["recovery_resolution_targets"] = [target]
    if isinstance(recovery_manifest_receipt, dict):
        metadata["recovery_manifest_ref"] = recovery_manifest_receipt.get("ref")
        metadata["recovery_manifest_sha256"] = recovery_manifest_receipt.get("sha256")
    task.status = RECONCILIATION_STATUS
    task.completed_at = None
    task.metadata_json = metadata
    task.result_summary = f"Recovery reconciliation required: {frame['tool_name'] or 'unknown tool'}"
    await db.flush()
    return runtime_reconciliation_view(task)


def _append_history(
    metadata: dict[str, Any],
    *,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    resumed_by_user_id: uuid.UUID | None,
    operation_id: str,
    evidence_digest: str,
    frame_decisions: list[dict[str, str]],
    group_root_task_id: uuid.UUID,
) -> dict[str, Any]:
    updated = dict(metadata)
    history = [item for item in updated.get("reconciliation_history", []) if isinstance(item, dict)]
    history.append(
        {
            "schema": "runtime_reconciliation_action.v1",
            "action": action,
            "reason": reason,
            "actor_user_id": str(actor_user_id),
            "operation_id": operation_id,
            "evidence_digest": evidence_digest,
            "frame_decisions": frame_decisions,
            "group_root_task_id": str(group_root_task_id),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **({"resumed_by_user_id": str(resumed_by_user_id)} if resumed_by_user_id is not None else {}),
        }
    )
    updated["reconciliation_history"] = history[-50:]
    return updated


async def _write_reconciliation_audit(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    operation_id: str,
    receipts: list[dict[str, Any]],
    evidence_digest: str,
    frame_decisions: list[dict[str, str]],
    resumed_by_user_id: uuid.UUID | None,
    group_member_task_ids: list[uuid.UUID],
) -> None:
    from app.models.audit import AuditLog

    db.add(
        AuditLog(
            action=f"runtime_reconciliation.{action}",
            details={
                "tenant_id": str(tenant_id),
                "runtime_task_id": str(task.id),
                "task_type": task.task_type,
                "previous_status": task.status,
                "reason": reason,
                "operation_id": operation_id,
                "receipts": receipts,
                "evidence_digest": evidence_digest,
                "frame_decisions": frame_decisions,
                "decision_actor_user_id": str(actor_user_id),
                "group_root_task_id": str(task.id),
                "group_member_task_ids": [str(value) for value in group_member_task_ids],
                **({"resumed_by_user_id": str(resumed_by_user_id)} if resumed_by_user_id is not None else {}),
            },
            agent_id=None,
            user_id=actor_user_id,
            tenant_id=tenant_id,
        )
    )
    await db.flush()


async def _validate_resolution_target_authority(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    tenant_id: uuid.UUID,
    targets: list[dict[str, Any]],
) -> dict[uuid.UUID, RuntimeTask]:
    if not targets:
        return {}
    allowed_agent_ids = {value for value in (task.parent_agent_id, task.child_agent_id) if value is not None}
    allowed_session_ids = {str(value) for value in (task.parent_session_id, task.child_session_id) if value}
    if not allowed_agent_ids or not allowed_session_ids:
        raise RuntimeReconciliationConflict("RuntimeTask has no authoritative agent/session recovery binding")
    workflow_targets = [target for target in targets if target.get("source") == "workflow_leaf"]
    for target in targets:
        if target["agent_id"] not in allowed_agent_ids:
            raise RuntimeReconciliationConflict("Recovery target agent is outside RuntimeTask authority")
        if target.get("source") != "workflow_leaf" and str(target["session_id"]) not in allowed_session_ids:
            raise RuntimeReconciliationConflict("Recovery target session is outside RuntimeTask authority")

    if workflow_targets:
        if task.task_type != "workflow":
            raise RuntimeReconciliationConflict("Workflow leaf recovery target requires a Workflow RuntimeTask")
        from app.models.workflow import WorkflowLeafCall, WorkflowStep
        from app.runtime.workflow_engine import workflow_leaf_recovery_identity

        step_ids: set[str] = set()
        leaf_keys: set[tuple[str, str]] = set()
        for target in workflow_targets:
            step_id = str(target.get("workflow_step_id") or "").strip()
            leaf_id = str(target.get("workflow_leaf_id") or "").strip()
            if not step_id or not leaf_id or target["runtime_task_id"] != task.id:
                raise RuntimeReconciliationConflict("Workflow leaf recovery target identity is incomplete")
            identity = workflow_leaf_recovery_identity(task.id, step_id, leaf_id)
            if str(target["session_id"]) != identity.session_id:
                raise RuntimeReconciliationConflict(
                    "Recovery target session does not match deterministic workflow leaf authority"
                )
            step_ids.add(step_id)
            if leaf_id != "singleton":
                leaf_keys.add((step_id, leaf_id))

        step_result = await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.tenant_id == tenant_id,
                WorkflowStep.run_id == task.id,
                WorkflowStep.step_id.in_(step_ids),
            )
        )
        steps = {row.step_id: row for row in step_result.scalars().all()}
        if set(steps) != step_ids:
            raise RuntimeReconciliationConflict("Workflow leaf recovery target has no authoritative step journal")
        if leaf_keys:
            leaf_result = await db.execute(
                select(WorkflowLeafCall).where(
                    WorkflowLeafCall.tenant_id == tenant_id,
                    WorkflowLeafCall.run_id == task.id,
                    WorkflowLeafCall.step_id.in_({step_id for step_id, _leaf_id in leaf_keys}),
                )
            )
            persisted_leaf_keys = {(row.step_id, row.leaf_id) for row in leaf_result.scalars().all()}
            if not leaf_keys.issubset(persisted_leaf_keys):
                raise RuntimeReconciliationConflict("Workflow leaf recovery target has no authoritative leaf journal")

    target_ids = {target["runtime_task_id"] for target in targets}
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.id.in_(target_ids),
            RuntimeTask.tenant_id == tenant_id,
        )
        .order_by(RuntimeTask.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = {row.id: row for row in result.scalars().all()}
    if set(rows) != target_ids:
        raise RuntimeReconciliationConflict("Recovery target RuntimeTask is missing from tenant authority")
    for target in targets:
        row = rows[target["runtime_task_id"]]
        row_agents = {value for value in (row.parent_agent_id, row.child_agent_id) if value is not None}
        row_sessions = {str(value) for value in (row.parent_session_id, row.child_session_id) if value}
        if target["agent_id"] not in row_agents:
            raise RuntimeReconciliationConflict("Recovery target identity does not match its authoritative RuntimeTask")
        if target.get("source") != "workflow_leaf" and str(target["session_id"]) not in row_sessions:
            raise RuntimeReconciliationConflict("Recovery target identity does not match its authoritative RuntimeTask")
    return rows


def _validated_frame_decisions(
    *,
    evidence: dict[str, Any],
    action: str,
    frame_decisions: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(frame_decisions, list) or len(frame_decisions) > _FRAME_DECISION_LIMIT:
        raise RuntimeReconciliationConflict("Frame decision set is malformed or exceeds its limit")
    expected = {
        (
            frame["runtime_task_id"],
            frame["tool_call_id"],
            frame["tool_name"],
        )
        for frame in evidence["frames"]
    }
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in frame_decisions:
        if not isinstance(raw, dict):
            raise RuntimeReconciliationConflict("Frame decision is malformed")
        try:
            runtime_task_id = str(_coerce_uuid(raw.get("runtime_task_id")))
        except (TypeError, ValueError) as exc:
            raise RuntimeReconciliationConflict("Frame decision target is invalid") from exc
        tool_call_id = str(raw.get("tool_call_id") or "").strip()
        tool_name = str(raw.get("tool_name") or "").strip()
        decision = str(raw.get("decision") or "").strip()
        if not tool_call_id or not tool_name:
            raise RuntimeReconciliationConflict("Frame decision identity is incomplete")
        if decision != action:
            raise RuntimeReconciliationConflict("Frame decision is not bound to the requested action")
        identity = (runtime_task_id, tool_call_id, tool_name)
        if identity in seen:
            raise RuntimeReconciliationConflict("Frame decision set contains duplicate identities")
        seen.add(identity)
        normalized.append(
            {
                "runtime_task_id": runtime_task_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "decision": decision,
            }
        )
    if seen != expected:
        raise RuntimeReconciliationConflict("Frame decision set does not exactly match canonical recovery evidence")
    return sorted(
        normalized,
        key=lambda item: (
            item["runtime_task_id"],
            item["tool_call_id"],
            item["tool_name"],
        ),
    )


async def apply_runtime_reconciliation_action(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    confirmed: bool,
    evidence_digest: str,
    frame_decisions: list[dict[str, Any]],
    operation_id: str | None,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in _ACTIONS:
        raise ValueError(f"Unsupported reconciliation action: {action!r}")
    normalized_reason = str(reason or "").strip()
    if len(normalized_reason) < 8:
        raise ValueError("Reconciliation reason must contain at least 8 characters")
    if confirmed is not True:
        raise RuntimeReconciliationConflict("Explicit reconciliation confirmation is required")
    normalized_digest = str(evidence_digest or "").strip()
    if not _DIGEST_RE.fullmatch(normalized_digest):
        raise ValueError("Recovery evidence digest must be a lowercase 64-character SHA-256 hex value")

    runtime_task_id = _coerce_uuid(task_id)
    result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.status == RECONCILIATION_STATUS,
        )
        .order_by(RuntimeTask.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    open_tasks = list(result.scalars().all())
    task = next((row for row in open_tasks if row.id == runtime_task_id), None)
    if task is None:
        existing_result = await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.id == runtime_task_id,
                RuntimeTask.tenant_id == tenant_id,
            )
        )
        if existing_result.scalar_one_or_none() is None:
            raise RuntimeReconciliationNotFound("Runtime reconciliation task not found")
        raise RuntimeReconciliationConflict("RuntimeTask is not open for reconciliation")
    _raise_specialized_reconciliation_conflict(task)
    root_id = _group_roots(open_tasks).get(runtime_task_id, runtime_task_id)
    if root_id != runtime_task_id:
        raise RuntimeReconciliationConflict(f"Reconciliation must be applied to operation group root {root_id}")

    metadata = _metadata(task)
    evidence = _canonical_recovery_evidence(task)
    if evidence["evidence_complete"] is not True:
        raise RuntimeReconciliationConflict(
            "Canonical recovery evidence is incomplete: " + ", ".join(evidence["incomplete_reasons"])
        )
    if evidence["digest"] != normalized_digest:
        raise RuntimeReconciliationConflict("Recovery evidence digest changed since operator review")
    normalized_decisions = _validated_frame_decisions(
        evidence=evidence,
        action=normalized_action,
        frame_decisions=frame_decisions,
    )
    if normalized_action == "retry" and not bool(metadata.get("reconciliation_retry_allowed")):
        raise RuntimeReconciliationConflict("RuntimeTask is not marked retryable after reconciliation")

    existing_operation = metadata.get("reconciliation_operation")
    resumable_operation = (
        existing_operation
        if isinstance(existing_operation, dict) and existing_operation.get("status") in {"prepared", "failed"}
        else None
    )
    resumed_by_user_id: uuid.UUID | None = None
    if resumable_operation is not None:
        durable_operation_id = str(resumable_operation.get("operation_id") or "").strip()
        if not operation_id or str(operation_id).strip() != durable_operation_id:
            raise RuntimeReconciliationConflict("Resuming a prepared reconciliation requires its exact operation_id")
        if (
            resumable_operation.get("action") != normalized_action
            or resumable_operation.get("reason") != normalized_reason
        ):
            raise RuntimeReconciliationConflict("Prepared reconciliation action and reason are immutable")
        if (
            resumable_operation.get("evidence_digest") != normalized_digest
            or resumable_operation.get("frame_decisions") != normalized_decisions
        ):
            raise RuntimeReconciliationConflict("Prepared reconciliation evidence digest and decisions are immutable")
        try:
            decision_actor_user_id = _coerce_uuid(resumable_operation.get("actor_user_id"))
        except (TypeError, ValueError) as exc:
            raise RuntimeReconciliationConflict("Prepared reconciliation decision actor is invalid") from exc
        if decision_actor_user_id != actor_user_id:
            resumed_by_user_id = actor_user_id
        resolution_targets = _recovery_resolution_targets(
            {"recovery_resolution_targets": resumable_operation.get("targets")}
        )
    else:
        if operation_id:
            raise RuntimeReconciliationConflict("operation_id is only valid when resuming a prepared reconciliation")
        if isinstance(existing_operation, dict):
            raise RuntimeReconciliationConflict("RuntimeTask already carries a non-resumable reconciliation operation")
        durable_operation_id = uuid.uuid4().hex
        decision_actor_user_id = actor_user_id
        resolution_targets = _recovery_resolution_targets(metadata)
    if not durable_operation_id:
        raise RuntimeReconciliationConflict("Reconciliation operation identity is missing")
    target_rows = await _validate_resolution_target_authority(
        db,
        task=task,
        tenant_id=tenant_id,
        targets=resolution_targets,
    )
    for target_row in target_rows.values():
        _raise_specialized_reconciliation_conflict(target_row)
    serialized_targets = _serialized_resolution_targets(resolution_targets)
    if runtime_task_id not in target_rows:
        raise RuntimeReconciliationConflict("Operation group root is missing from recovery targets")
    open_group_rows = sorted(
        (row for row in target_rows.values() if row.status == RECONCILIATION_STATUS),
        key=lambda row: str(row.id),
    )
    group_member_task_ids = [row.id for row in open_group_rows]
    if resumable_operation is None:
        for row in open_group_rows:
            row_operation = _metadata(row).get("reconciliation_operation")
            if isinstance(row_operation, dict) and row_operation.get("status") in {"prepared", "failed"}:
                raise RuntimeReconciliationConflict("An operation group member already has a prepared reconciliation")
        operation = {
            "schema": "runtime_reconciliation_operation.v2",
            "operation_id": durable_operation_id,
            "status": "prepared",
            "action": normalized_action,
            "reason": normalized_reason,
            "actor_user_id": str(decision_actor_user_id),
            "evidence_digest": normalized_digest,
            "frame_decisions": normalized_decisions,
            "targets": serialized_targets,
            "group_root_task_id": str(runtime_task_id),
            "group_member_task_ids": [str(value) for value in group_member_task_ids],
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        expected_members = {str(value) for value in resumable_operation.get("group_member_task_ids", [])}
        if expected_members != {str(value) for value in group_member_task_ids}:
            raise RuntimeReconciliationConflict("Prepared reconciliation operation group changed since review")
        for row in open_group_rows:
            row_operation = _metadata(row).get("reconciliation_operation")
            if not isinstance(row_operation, dict) or row_operation.get("operation_id") != durable_operation_id:
                raise RuntimeReconciliationConflict(
                    "Prepared reconciliation is not durably attached to every operation group member"
                )
        operation = dict(resumable_operation)
        operation.update(
            {
                "status": "prepared",
                "resumed_at": datetime.now(timezone.utc).isoformat(),
                **({"resumed_by_user_id": str(resumed_by_user_id)} if resumed_by_user_id is not None else {}),
            }
        )
    for row in open_group_rows:
        row_metadata = _metadata(row)
        row_metadata["reconciliation_operation"] = operation
        if resumable_operation is None:
            previous_claim_version = int(getattr(row, "claim_version", 0) or 0)
            previous_claim_worker_id = str(getattr(row, "claimed_by", None) or "") or None
            previous_claim_expires_at = (
                row.claim_expires_at.isoformat() if getattr(row, "claim_expires_at", None) else None
            )
            row.claim_version = previous_claim_version + 1
            row.claimed_by = "operator-reconciler"
            row.claim_expires_at = None
            row_metadata.update(
                {
                    "claim_version": row.claim_version,
                    "claimed_by": row.claimed_by,
                    "claim_expires_at": None,
                    "claim_fence": f"{row.id.hex}:{row.claim_version}",
                }
            )
            row_metadata["reconciliation_claim_invalidation"] = {
                "schema": "runtime_reconciliation_claim_invalidation.v1",
                "previous_claim_version": previous_claim_version,
                "previous_claim_worker_id": previous_claim_worker_id,
                "previous_claim_expires_at": previous_claim_expires_at,
                "invalidated_claim_version": row.claim_version,
                "invalidated_by_operation_id": durable_operation_id,
                "invalidated_at": datetime.now(timezone.utc).isoformat(),
            }
        row.metadata_json = row_metadata
    await db.flush()
    # Commit reviewed intent before filesystem mutation so this exact operation
    # can be resumed without changing its decision actor or evidence contract.
    await db.commit()

    operation_id = durable_operation_id

    resolution_receipts: list[dict[str, Any]] = []
    if resolution_targets:
        from app.runtime.recovery_manifest import (
            RecoveryManifestEvidenceDriftError,
            RecoveryManifestReconciliationError,
            resolve_recovery_manifest_reconciliations,
        )

        try:
            resolution_receipts = await asyncio.to_thread(
                resolve_recovery_manifest_reconciliations,
                targets=serialized_targets,
                tenant_id=tenant_id,
                action=normalized_action,
                reason=normalized_reason,
                actor_user_id=decision_actor_user_id,
                operation_id=operation_id,
            )
        except (RecoveryManifestReconciliationError, OSError, ValueError) as exc:
            evidence_drifted = isinstance(exc, RecoveryManifestEvidenceDriftError)
            refreshed_evidence: dict[str, Any] | None = None
            refresh_error: Exception | None = None
            if evidence_drifted:
                try:
                    refreshed_evidence = await asyncio.to_thread(
                        _refresh_manifest_evidence_snapshot,
                        targets=serialized_targets,
                        tenant_id=tenant_id,
                    )
                except Exception as snapshot_exc:  # noqa: BLE001 - preserve retryable drift audit state
                    refresh_error = snapshot_exc
            failed_result = await db.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id.in_(group_member_task_ids),
                    RuntimeTask.tenant_id == tenant_id,
                )
                .order_by(RuntimeTask.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            failed_rows = list(failed_result.scalars().all())
            if {row.id for row in failed_rows} != set(group_member_task_ids):
                await db.rollback()
                raise RuntimeReconciliationConflict(
                    "Reconciliation operation group disappeared while recording failure"
                ) from exc
            failed_operation = dict(operation)
            failed_operation.update(
                {
                    "status": "evidence_drifted" if evidence_drifted else "failed",
                    "error": str(exc),
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            for failed_task in failed_rows:
                failed_metadata = _metadata(failed_task)
                current_operation = failed_metadata.get("reconciliation_operation")
                if (
                    failed_task.status != RECONCILIATION_STATUS
                    or not isinstance(current_operation, dict)
                    or current_operation.get("operation_id") != operation_id
                ):
                    await db.rollback()
                    raise RuntimeReconciliationConflict(
                        "Reconciliation operation changed while recording durable failure"
                    ) from exc
                if evidence_drifted:
                    retired = [
                        dict(item)
                        for item in failed_metadata.get("retired_reconciliation_operations", [])
                        if isinstance(item, dict)
                    ]
                    retired.append(dict(failed_operation))
                    failed_metadata["retired_reconciliation_operations"] = retired[-50:]
                    failed_metadata.pop("reconciliation_operation", None)
                    if refreshed_evidence is not None:
                        failed_metadata["recovery_resolution_targets"] = refreshed_evidence["targets"]
                        failed_metadata["recovery_tool_frames"] = refreshed_evidence["frames"]
                        incomplete_reasons = list(refreshed_evidence["incomplete_reasons"])
                        if incomplete_reasons:
                            failed_metadata["recovery_evidence_incomplete_reasons"] = incomplete_reasons
                            failed_metadata["recovery_evidence_status"] = "incomplete"
                            failed_metadata["reconciliation_retry_allowed"] = False
                        else:
                            failed_metadata.pop("recovery_evidence_incomplete_reasons", None)
                            failed_metadata["recovery_evidence_status"] = "ready"
                            if refreshed_evidence.get("contains_completed_side_effect_evidence") is True:
                                failed_metadata["reconciliation_retry_allowed"] = False
                    else:
                        failed_metadata["recovery_evidence_status"] = "drifted"
                    failed_metadata["recovery_evidence_drift"] = {
                        "operation_id": operation_id,
                        "error_class": type(exc).__name__,
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                        "evidence_refreshed": refreshed_evidence is not None,
                        **({"refresh_error_class": type(refresh_error).__name__} if refresh_error is not None else {}),
                    }
                else:
                    failed_metadata["reconciliation_operation"] = failed_operation
                failed_task.metadata_json = failed_metadata
            await db.flush()
            await db.commit()
            raise RuntimeReconciliationConflict(f"Durable recovery state could not be reconciled: {exc}") from exc

    locked_result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.id.in_(group_member_task_ids),
            RuntimeTask.tenant_id == tenant_id,
        )
        .order_by(RuntimeTask.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_rows = list(locked_result.scalars().all())
    if {row.id for row in locked_rows} != set(group_member_task_ids):
        raise RuntimeReconciliationConflict("RuntimeTask operation group disappeared")
    for row in locked_rows:
        row_operation = _metadata(row).get("reconciliation_operation")
        if (
            row.status != RECONCILIATION_STATUS
            or not isinstance(row_operation, dict)
            or row_operation.get("operation_id") != operation_id
        ):
            raise RuntimeReconciliationConflict("RuntimeTask reconciliation operation changed concurrently")
    task = next(row for row in locked_rows if row.id == runtime_task_id)
    completed_operation = dict(operation)
    completed_operation.update(
        {
            "status": "completed",
            "receipts": resolution_receipts,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    now = datetime.now(timezone.utc)
    quota_resolution_receipts: dict[uuid.UUID, list[dict[str, Any]]] = {}
    from app.models.workflow import WorkflowQuota, WorkflowQuotaReservation

    for row in locked_rows:
        pending_quota = [
            dict(item)
            for item in _metadata(row).get("workflow_quota_reconciliation_pending", [])
            if isinstance(item, dict)
        ]
        if not pending_quota:
            continue
        try:
            reservation_ids = {uuid.UUID(str(item.get("reservation_id"))) for item in pending_quota}
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeReconciliationConflict("Workflow quota reconciliation receipt identity is invalid") from exc
        reservation_rows = list(
            (
                await db.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.id.in_(reservation_ids),
                        WorkflowQuotaReservation.run_id == row.id,
                        WorkflowQuotaReservation.tenant_id == tenant_id,
                    )
                    .order_by(WorkflowQuotaReservation.id.asc())
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if {reservation.id for reservation in reservation_rows} != reservation_ids:
            raise RuntimeReconciliationConflict("Workflow quota reconciliation receipt is missing")
        quota = (
            await db.execute(
                select(WorkflowQuota)
                .where(WorkflowQuota.run_id == row.id, WorkflowQuota.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quota is None:
            raise RuntimeReconciliationConflict("Workflow quota reconciliation envelope is missing")
        receipts: list[dict[str, Any]] = []
        for reservation in reservation_rows:
            if reservation.state == "settled":
                if reservation.reconciliation_operation_id != operation_id:
                    raise RuntimeReconciliationConflict(
                        "Workflow quota receipt was settled by a different reconciliation operation"
                    )
            elif reservation.state == "needs_reconciliation" and reservation.settled_at is None:
                actual = max(0, int(reservation.estimated_tokens))
                quota.consumed_tokens = max(
                    0,
                    int(quota.consumed_tokens or 0) + actual - int(reservation.estimated_tokens),
                )
                reservation.actual_tokens = actual
                reservation.state = "settled"
                reservation.settled_at = now
                reservation.settlement_reason = "operator_resolved_unknown_using_estimate"
                reservation.reconciliation_operation_id = operation_id
            else:
                raise RuntimeReconciliationConflict(
                    f"Workflow quota receipt is not operator-settleable from state {reservation.state}"
                )
            receipts.append(
                {
                    "reservation_id": str(reservation.id),
                    "reservation_key": reservation.logical_key,
                    "attempt": int(reservation.attempt),
                    "actual_tokens": int(reservation.actual_tokens or 0),
                    "settlement_basis": reservation.settlement_reason,
                    "operation_id": operation_id,
                    "action": normalized_action,
                }
            )
        quota_resolution_receipts[row.id] = receipts
    for row in locked_rows:
        row_metadata = _metadata(row)
        if resolution_receipts:
            row_metadata["recovery_resolution_receipts"] = resolution_receipts
            if len(resolution_receipts) == 1:
                row_metadata["recovery_resolution_receipt"] = {
                    key: value for key, value in resolution_receipts[0].items() if key != "source"
                }
        row_metadata["reconciliation_operation"] = completed_operation
        if row.id in quota_resolution_receipts:
            row_metadata.pop("workflow_quota_reconciliation_pending", None)
            prior_quota_receipts = [
                dict(item)
                for item in row_metadata.get("workflow_quota_reconciliation_receipts", [])
                if isinstance(item, dict)
            ]
            prior_quota_receipts.extend(quota_resolution_receipts[row.id])
            row_metadata["workflow_quota_reconciliation_receipts"] = prior_quota_receipts[-500:]
        row_metadata = _append_history(
            row_metadata,
            action=normalized_action,
            reason=normalized_reason,
            actor_user_id=decision_actor_user_id,
            resumed_by_user_id=resumed_by_user_id,
            operation_id=operation_id,
            evidence_digest=normalized_digest,
            frame_decisions=normalized_decisions,
            group_root_task_id=runtime_task_id,
        )
        row_metadata["needs_reconciliation"] = False
        if row.id != runtime_task_id:
            row.status = "killed" if normalized_action == "archive" else RECONCILED_STATUS
            row.completed_at = row.completed_at or now
            row_metadata["reconciliation_status"] = "superseded"
            row_metadata["reconciliation_superseded_by"] = str(runtime_task_id)
            row_metadata["reconciliation_superseded_at"] = now.isoformat()
            row.result_summary = f"Reconciliation superseded by operation group root {runtime_task_id}"
        elif normalized_action == "retry":
            row.status = "pending"
            row.started_at = None
            row.completed_at = None
            row_metadata["reconciliation_status"] = "retry_requested"
            row_metadata["reconciliation_retry_requested_at"] = now.isoformat()
        elif normalized_action == "archive":
            row.status = "killed"
            row.completed_at = row.completed_at or now
            row_metadata["reconciliation_status"] = "archived"
            row_metadata["reconciliation_archived_at"] = now.isoformat()
            row.result_summary = row.result_summary or (f"Archived after reconciliation: {normalized_reason}")
        else:
            row.status = RECONCILED_STATUS
            row.completed_at = row.completed_at or now
            row_metadata["reconciliation_status"] = "resolved"
            row_metadata["reconciliation_resolved_at"] = now.isoformat()
            row.result_summary = f"Reconciliation resolved: {normalized_reason}"
        row.metadata_json = row_metadata
        if (
            row.id == runtime_task_id
            and row.task_type == "workflow"
            and normalized_action != "retry"
            and row.parent_session_id
            and row.parent_agent_id is not None
            and row.root_user_id is not None
        ):
            from app.services.runtime_notification_outbox import (
                CompletionNotification,
                enqueue_completion_notification,
            )

            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=tenant_id,
                    source_kind="workflow",
                    source_run_id=str(row.id),
                    parent_session_id=row.parent_session_id,
                    parent_agent_id=row.parent_agent_id,
                    parent_user_id=row.root_user_id,
                    terminal_status=str(row.status),
                    task_type="workflow",
                    summary=str(row.result_summary or f"Workflow reconciliation {normalized_action}"),
                    child_session_id=row.child_session_id,
                    delivery_mode="parent_continuation",
                    metadata={
                        "reconciliation_operation_id": operation_id,
                        "reconciliation_action": normalized_action,
                        "quota_receipts": quota_resolution_receipts.get(row.id, []),
                    },
                    payload_rank=200,
                ),
            )
    try:
        await _write_reconciliation_audit(
            db,
            task,
            tenant_id=tenant_id,
            action=normalized_action,
            reason=normalized_reason,
            actor_user_id=decision_actor_user_id,
            operation_id=operation_id,
            receipts=resolution_receipts,
            evidence_digest=normalized_digest,
            frame_decisions=normalized_decisions,
            resumed_by_user_id=resumed_by_user_id,
            group_member_task_ids=group_member_task_ids,
        )
        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    view = runtime_reconciliation_view(task)

    from app.services.web_chat_broker import web_chat_broker

    broker_targets: dict[tuple[str, str], set[str]] = {}
    for target in resolution_targets:
        key = (str(target["agent_id"]), str(target["session_id"]))
        broker_targets.setdefault(key, set()).add(str(target["runtime_task_id"]))
    for (agent_id, session_id), target_ids in broker_targets.items():
        await web_chat_broker.resolve_runtime_recovery_state(
            agent_id,
            session_id,
            runtime_task_ids=target_ids,
        )
    return view
