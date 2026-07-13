"""Persistence helpers for runtime delegation tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.runtime.tenant_admission import RuntimeTenantPreconditionError, raise_runtime_tenant_precondition
from app.services.runtime_tenant_admission import admit_agent_runtime_tenant
from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_runtime_task


logger = logging.getLogger(__name__)


_RESTART_RESUMABLE_TASK_TYPES = (
    "workflow",
    "web_chat_turn",
    "team_member",
    "goal_continuation",
    "advanced_plan",
    "approval_execution",
    "hr_provisioning",
    "dream",
    "system_plan_run",
)
INLINE_A2A_RECOVERY_TASK_TYPES = ("delegation", "a2a_delegation")
_TERMINAL_STATUSES = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}
RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA = "runtime_restart_replay_contract.v1"
RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA = "runtime_restart_replay_journal.v1"
RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA = "runtime_reconciliation_retry_contract.v1"
StartupResumeCollector = Callable[[str], None]
LockedRuntimeTaskPrecondition = Callable[[RuntimeTask], bool]
_EXPECTED_CLAIM_WORKER_UNSET = object()


def _is_inline_a2a_recovery_task(task: RuntimeTask) -> bool:
    task_type = str(getattr(task, "task_type", None) or "")
    if task_type == "a2a_delegation":
        return True
    metadata = dict(getattr(task, "metadata_json", None) or {})
    return task_type == "delegation" and metadata.get("execution_backend") == "foreground_inline"


def _inline_a2a_recovery_sql_predicate():
    return or_(
        RuntimeTask.task_type == "a2a_delegation",
        and_(
            RuntimeTask.task_type == "delegation",
            RuntimeTask.metadata_json["execution_backend"].astext == "foreground_inline",
        ),
    )


def _coerce_task_id(task_id: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(task_id, uuid.UUID):
        return task_id
    try:
        return uuid.UUID(str(task_id))
    except (ValueError, TypeError, AttributeError):
        return None


def record_startup_resumed_task(
    resumed_task_ids: list[str],
    task_id: str | uuid.UUID,
    on_resumed: StartupResumeCollector | None = None,
) -> str | None:
    """Record one successfully resumed task locally and in the startup collector."""

    normalized = str(task_id or "").strip()
    if not normalized:
        return None
    if normalized in resumed_task_ids:
        return normalized
    resumed_task_ids.append(normalized)
    if on_resumed is not None:
        on_resumed(normalized)
    return normalized


def _task_to_dict(task: RuntimeTask) -> dict[str, Any]:
    return {
        "task_id": task.id.hex,
        "task_type": task.task_type,
        "status": task.status,
        "tenant_id": str(task.tenant_id) if task.tenant_id else None,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "child_agent_id": str(task.child_agent_id) if task.child_agent_id else None,
        "child_agent_name": task.child_agent_name,
        "prompt": task.prompt,
        "result": task.result_summary,
        "trace_id": task.trace_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "root_user_id": str(getattr(task, "root_user_id", None)) if getattr(task, "root_user_id", None) else None,
        "root_session_id": getattr(task, "root_session_id", None),
        "delegation_chain": list(getattr(task, "delegation_chain_json", None) or []),
        "depth": task.depth,
        "budget_run_id": str(task.budget_run_id) if task.budget_run_id else None,
        "root_runtime_task_id": str(getattr(task, "root_runtime_task_id", None))
        if getattr(task, "root_runtime_task_id", None)
        else None,
        "budget_reservation_key": task.budget_reservation_key,
        "budget_admission_status": task.budget_admission_status,
        "budget_terminal_reason": task.budget_terminal_reason,
        "token_usage": dict(getattr(task, "token_usage", None) or {}),
        "claimed_by": str(getattr(task, "claimed_by", None) or "") or None,
        "claim_expires_at": (task.claim_expires_at.isoformat() if getattr(task, "claim_expires_at", None) else None),
        "claim_version": int(getattr(task, "claim_version", 0) or 0),
        "root_idempotency_key": getattr(task, "root_idempotency_key", None),
        "config_snapshot_hash": getattr(task, "config_snapshot_hash", None),
        "policy_snapshot_hash": getattr(task, "policy_snapshot_hash", None),
        "metadata": task.metadata_json or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _synthetic_a2a_frame_id(*parts: Any) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    return f"a2a-recovery:{hashlib.sha256(payload).hexdigest()[:24]}"


_A2A_INSPECTION_UNSET = object()


async def _project_expired_inline_a2a_evidence(
    task: RuntimeTask,
    metadata: dict[str, Any],
    *,
    inspection: Any = _A2A_INSPECTION_UNSET,
) -> dict[str, Any]:
    """Project one already-fenced A2A manifest snapshot into canonical DB evidence."""

    from app.runtime.recovery_manifest import inspect_recovery_manifest_checkpoint

    agent_id = metadata.get("recovery_agent_id") or getattr(task, "child_agent_id", None)
    session_id = str(metadata.get("recovery_session_id") or getattr(task, "child_session_id", None) or "").strip()
    runtime_task_id = getattr(task, "id", None)
    tenant_id = getattr(task, "tenant_id", None)
    incomplete = {
        str(value).strip() for value in metadata.get("recovery_evidence_incomplete_reasons", []) if str(value).strip()
    }
    if not agent_id or not session_id or runtime_task_id is None or tenant_id is None:
        incomplete.add("a2a_recovery_identity_incomplete")
        metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete)
        metadata["recovery_evidence_status"] = "incomplete"
        return metadata

    if inspection is _A2A_INSPECTION_UNSET:
        inspection = await asyncio.to_thread(
            inspect_recovery_manifest_checkpoint,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
        )
    from app.runtime.recovery_manifest import reviewed_recovery_manifest_evidence

    state = str((inspection or {}).get("state") or "missing")
    reviewed_evidence = reviewed_recovery_manifest_evidence(inspection)
    targets = [dict(target) for target in metadata.get("recovery_resolution_targets", []) if isinstance(target, dict)]
    normalized_runtime_task_id = _coerce_task_id(runtime_task_id)
    target = next(
        (
            item
            for item in targets
            if normalized_runtime_task_id is not None
            and _coerce_task_id(item.get("runtime_task_id")) == normalized_runtime_task_id
            and str(item.get("session_id") or "") == session_id
        ),
        None,
    )
    if target is None:
        target = {
            "agent_id": str(agent_id),
            "session_id": session_id,
            "runtime_task_id": str(runtime_task_id),
            "source": "current_run",
        }
        targets.append(target)
    target.update(
        {
            "agent_id": str(agent_id),
            "session_id": session_id,
            "runtime_task_id": str(runtime_task_id),
            "source": "current_run",
        }
    )
    for key in (
        "expected_manifest_state",
        "expected_manifest_ref",
        "expected_sha256",
        "expected_checkpoint_seq",
        "expected_claim_version",
        "expected_claim_worker_id",
    ):
        target.pop(key, None)
    target.update(reviewed_evidence)
    metadata.pop("recovery_manifest_ref", None)
    metadata.pop("recovery_manifest_sha256", None)

    receipt = (inspection or {}).get("receipt") if isinstance(inspection, dict) else None
    if isinstance(receipt, dict):
        if receipt.get("ref"):
            target["expected_manifest_ref"] = receipt["ref"]
            metadata["recovery_manifest_ref"] = receipt["ref"]
        if receipt.get("sha256"):
            target["expected_sha256"] = receipt["sha256"]
            metadata["recovery_manifest_sha256"] = receipt["sha256"]

    if state == "valid":
        cas_fields = {
            "expected_checkpoint_seq": (inspection or {}).get("expected_checkpoint_seq"),
            "expected_claim_version": (inspection or {}).get("expected_claim_version"),
            "expected_claim_worker_id": (inspection or {}).get("expected_claim_worker_id"),
        }
        for key, actual in cas_fields.items():
            if actual is not None:
                target[key] = actual
    elif state in {"identity_mismatch", "nonregular"}:
        incomplete.add(f"a2a_recovery_manifest_{state}")

    frames: list[dict[str, Any]] = []
    for index, raw in enumerate((inspection or {}).get("pending_tool_frames", [])):
        if not isinstance(raw, dict):
            continue
        tool_name = str(raw.get("tool_name") or "unknown_a2a_tool").strip()
        tool_call_id = str(raw.get("tool_call_id") or "").strip() or _synthetic_a2a_frame_id(
            runtime_task_id, "pending", index, tool_name
        )
        frames.append(
            {
                "runtime_task_id": str(runtime_task_id),
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "status": "needs_reconciliation",
                "event_type": "recovered_inline_a2a_tool_frame",
                "reason": "expired_inline_a2a_tool_outcome_unknown",
            }
        )
    for index, raw in enumerate((inspection or {}).get("recent_tool_outcomes", [])):
        if not isinstance(raw, dict):
            continue
        tool_name = str(raw.get("tool") or raw.get("tool_name") or "unknown_a2a_tool").strip()
        summary = str(raw.get("summary") or "").strip()
        frames.append(
            {
                "runtime_task_id": str(runtime_task_id),
                "tool_call_id": _synthetic_a2a_frame_id(runtime_task_id, "outcome", index, tool_name, summary),
                "tool_name": tool_name,
                "status": "needs_reconciliation",
                "event_type": "recovered_inline_a2a_completed_tool",
                "reason": "completed_tool_without_parent_runtime_terminal",
            }
        )
    writes = [
        *list((inspection or {}).get("recent_writes", [])),
        *list((inspection or {}).get("current_turn_writes", [])),
    ]
    for index, path in enumerate(dict.fromkeys(str(value) for value in writes if str(value).strip())):
        frames.append(
            {
                "runtime_task_id": str(runtime_task_id),
                "tool_call_id": _synthetic_a2a_frame_id(runtime_task_id, "write", index, path),
                "tool_name": "workspace_write",
                "status": "needs_reconciliation",
                "event_type": "recovered_inline_a2a_completed_write",
                "reason": "completed_write_without_parent_runtime_terminal",
            }
        )
    if not frames:
        frames.append(
            {
                "runtime_task_id": str(runtime_task_id),
                "tool_call_id": _synthetic_a2a_frame_id(runtime_task_id, state, "invocation"),
                "tool_name": "a2a_agent_message",
                "status": "needs_reconciliation",
                "event_type": "expired_inline_a2a_invocation",
                "reason": f"expired_inline_a2a_manifest_{state}",
            }
        )

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for frame in frames:
        key = (
            str(frame.get("runtime_task_id") or runtime_task_id),
            str(frame.get("tool_call_id") or ""),
            str(frame.get("tool_name") or ""),
        )
        if all(key):
            deduped[key] = {**frame, "runtime_task_id": key[0]}
    metadata.update(
        {
            "recovery_agent_id": str(agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": str(runtime_task_id),
            "recovery_resolution_targets": targets,
            "recovery_tool_frames": list(deduped.values())[-200:],
            "recovery_manifest_state": state,
            "recovery_evidence_status": "ready",
        }
    )
    if incomplete:
        metadata["recovery_evidence_incomplete_reasons"] = sorted(incomplete)
    else:
        metadata.pop("recovery_evidence_incomplete_reasons", None)
    return metadata


def _a2a_projection_snapshot(task: RuntimeTask) -> SimpleNamespace:
    return SimpleNamespace(
        id=getattr(task, "id", None),
        tenant_id=getattr(task, "tenant_id", None),
        child_agent_id=getattr(task, "child_agent_id", None),
        child_session_id=getattr(task, "child_session_id", None),
    )


async def _record_a2a_evidence_projection_failure(
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    claim_version: int,
    claim_worker_id: str,
    error: Exception,
) -> None:
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="inline_a2a_recovery_evidence_projection_failure",
    ) as db:
        result = await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == task_id,
                RuntimeTask.tenant_id == tenant_id,
                _inline_a2a_recovery_sql_predicate(),
                RuntimeTask.status == "needs_reconciliation",
                RuntimeTask.claim_version == claim_version,
                RuntimeTask.claimed_by == claim_worker_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        task = next(
            (
                candidate
                for candidate in result.scalars().all()
                if getattr(candidate, "id", None) == task_id
                and _is_inline_a2a_recovery_task(candidate)
                and str(getattr(candidate, "status", None) or "") == "needs_reconciliation"
                and int(getattr(candidate, "claim_version", 0) or 0) == claim_version
                and str(getattr(candidate, "claimed_by", None) or "") == claim_worker_id
            ),
            None,
        )
        if task is None:
            return
        metadata = dict(task.metadata_json or {})
        reconciliation_operation = metadata.get("reconciliation_operation")
        if isinstance(reconciliation_operation, dict) and str(reconciliation_operation.get("status") or "") in {
            "prepared",
            "failed",
        }:
            return
        metadata.update(
            {
                "recovery_evidence_status": "pending",
                "recovery_evidence_last_error": {
                    "error_class": type(error).__name__,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        task.metadata_json = metadata


async def refresh_inline_a2a_reconciliation_evidence(
    *,
    task_ids: set[uuid.UUID] | None = None,
    limit: int | None = 50,
) -> int:
    """Refresh fenced A2A evidence after DB claim invalidation has committed.

    The filesystem inspection intentionally runs outside the RuntimeTask row
    transaction.  A final claim/status CAS attaches the snapshot, and repeated
    calls detect a later manifest SHA drift without reviving the old worker.
    """

    if task_ids is None:
        normalized_ids: set[uuid.UUID] | None = None
    else:
        normalized_ids = {_coerce_task_id(value) for value in task_ids}
        normalized_ids.discard(None)
        if not normalized_ids:
            return 0
    protected_operation_statuses = {"prepared", "failed"}
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="inline A2A reconciliation evidence locator scan"),
    ):
        stmt = (
            select(RuntimeTask.id, RuntimeTask.tenant_id)
            .where(
                _inline_a2a_recovery_sql_predicate(),
                RuntimeTask.status == "needs_reconciliation",
            )
            .order_by(RuntimeTask.completed_at.asc().nulls_first(), RuntimeTask.created_at.asc())
        )
        operation_status = RuntimeTask.metadata_json["reconciliation_operation"]["status"].astext
        stmt = stmt.where(
            or_(
                operation_status.is_(None),
                operation_status.notin_(protected_operation_statuses),
            )
        )
        if normalized_ids is None:
            stmt = stmt.where(RuntimeTask.metadata_json["recovery_evidence_status"].astext == "pending")
        else:
            stmt = stmt.where(RuntimeTask.id.in_(normalized_ids))
        if limit is not None:
            stmt = stmt.limit(max(1, int(limit)))
        result = await db.execute(stmt)
        locator_rows = result.all()

    _, ids_by_tenant = _group_runtime_task_locators(
        locator_rows,
        operation="inline A2A reconciliation evidence refresh",
    )
    refreshed = 0
    for tenant_id, located_task_ids in ids_by_tenant.items():
        for task_id in located_task_ids:
            async with tenant_scoped_session(
                tenant_id,
                session_factory=async_session,
                require_tenant=True,
                source="inline_a2a_recovery_evidence_snapshot",
            ) as db:
                result = await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.id == task_id,
                        RuntimeTask.tenant_id == tenant_id,
                        _inline_a2a_recovery_sql_predicate(),
                        RuntimeTask.status == "needs_reconciliation",
                    )
                )
                task = next(
                    (
                        candidate
                        for candidate in result.scalars().all()
                        if getattr(candidate, "id", None) == task_id
                        and _is_inline_a2a_recovery_task(candidate)
                        and str(getattr(candidate, "status", None) or "") == "needs_reconciliation"
                    ),
                    None,
                )
                if task is None:
                    continue
                snapshot = _a2a_projection_snapshot(task)
                snapshot_metadata = dict(task.metadata_json or {})
                snapshot_operation = snapshot_metadata.get("reconciliation_operation")
                if (
                    isinstance(snapshot_operation, dict)
                    and str(snapshot_operation.get("status") or "") in protected_operation_statuses
                ):
                    continue
                claim_version = int(task.claim_version or 0)
                claim_worker_id = str(task.claimed_by or "")

            try:
                agent_id = snapshot_metadata.get("recovery_agent_id") or snapshot.child_agent_id
                session_id = str(
                    snapshot_metadata.get("recovery_session_id") or snapshot.child_session_id or ""
                ).strip()
                if not agent_id or not session_id:
                    inspection = None
                else:
                    from app.runtime.recovery_manifest import inspect_recovery_manifest_checkpoint

                    inspection = await asyncio.to_thread(
                        inspect_recovery_manifest_checkpoint,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        runtime_task_id=task_id,
                    )
            except Exception as exc:  # noqa: BLE001 - leave a retryable DB marker after the fence
                logger.warning("Inline A2A evidence projection failed for %s: %s", task_id, type(exc).__name__)
                await _record_a2a_evidence_projection_failure(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    claim_version=claim_version,
                    claim_worker_id=claim_worker_id,
                    error=exc,
                )
                continue

            async with tenant_scoped_session(
                tenant_id,
                session_factory=async_session,
                require_tenant=True,
                source="inline_a2a_recovery_evidence_projection",
            ) as db:
                result = await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == task_id,
                        RuntimeTask.tenant_id == tenant_id,
                        _inline_a2a_recovery_sql_predicate(),
                        RuntimeTask.status == "needs_reconciliation",
                        RuntimeTask.claim_version == claim_version,
                        RuntimeTask.claimed_by == claim_worker_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                current = next(
                    (
                        candidate
                        for candidate in result.scalars().all()
                        if getattr(candidate, "id", None) == task_id
                        and _is_inline_a2a_recovery_task(candidate)
                        and str(getattr(candidate, "status", None) or "") == "needs_reconciliation"
                        and int(getattr(candidate, "claim_version", 0) or 0) == claim_version
                        and str(getattr(candidate, "claimed_by", None) or "") == claim_worker_id
                    ),
                    None,
                )
                if current is None:
                    continue
                before = dict(current.metadata_json or {})
                current_operation = before.get("reconciliation_operation")
                if (
                    isinstance(current_operation, dict)
                    and str(current_operation.get("status") or "") in protected_operation_statuses
                ):
                    continue
                projected = await _project_expired_inline_a2a_evidence(
                    current,
                    dict(before),
                    inspection=inspection,
                )
                projected.pop("recovery_evidence_last_error", None)
                if projected == before:
                    continue
                current.metadata_json = projected
                refreshed += 1
    return refreshed


def _group_runtime_task_locators(
    rows: list[Any],
    *,
    operation: str,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[uuid.UUID]]]:
    """Keep cross-tenant scans metadata-only, then group safe tenant reads."""
    ordered_ids: list[uuid.UUID] = []
    ids_by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in rows:
        task_id, tenant_id = row
        normalized_task_id = _coerce_task_id(task_id)
        normalized_tenant_id = _coerce_task_id(tenant_id) if tenant_id else None
        if normalized_task_id is None:
            logger.error("Skipping malformed RuntimeTask locator during %s: %r", operation, task_id)
            continue
        if normalized_tenant_id is None:
            logger.error(
                "Skipping tenantless RuntimeTask %s during %s; repair its tenant_id before replay.",
                normalized_task_id,
                operation,
            )
            continue
        ordered_ids.append(normalized_task_id)
        ids_by_tenant.setdefault(normalized_tenant_id, []).append(normalized_task_id)
    return ordered_ids, ids_by_tenant


def _is_restart_resumable_runtime_task(task: RuntimeTask) -> bool:
    task_type = getattr(task, "task_type", None)
    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
        return True

    metadata = dict(getattr(task, "metadata_json", None) or {})
    task_id = getattr(task, "id", None)
    task_id_text = task_id.hex if isinstance(task_id, uuid.UUID) else str(task_id or "")
    if task_type in {"trigger", "heartbeat"}:
        resume_flag = "resumable_trigger" if task_type == "trigger" else "resumable_heartbeat"
        return bool(
            metadata.get("resume_after_restart")
            and metadata.get(resume_flag)
            and has_restart_replay_contract(metadata, task_type=str(task_type), task_id=task_id_text)
        )
    return bool(
        metadata.get("resume_after_restart")
        and (metadata.get("resumable_delegation") or metadata.get("resumable_subagent"))
    )


def build_restart_replay_contract(
    *,
    task_type: str,
    task_id: str,
    side_effect_risk: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    normalized_task_type = str(task_type or "runtime_task").strip() or "runtime_task"
    normalized_task_id = str(task_id or "").strip()
    return {
        "schema": RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA,
        "idempotency_key": f"{normalized_task_type}:{normalized_task_id}:restart",
        "task_type": normalized_task_type,
        "task_id": normalized_task_id,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip() or "unknown",
        "mode": "durable_restart_replay",
        "requires_completion_journal": True,
    }


def has_restart_replay_contract(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str | None = None,
) -> bool:
    contract = (metadata or {}).get("restart_replay_contract")
    if not isinstance(contract, dict):
        return False
    if contract.get("schema") != RUNTIME_RESTART_REPLAY_CONTRACT_SCHEMA:
        return False
    if str(contract.get("task_type") or "") != str(task_type or ""):
        return False
    if task_id is not None and str(contract.get("task_id") or "") not in {"", str(task_id)}:
        return False
    return bool(str(contract.get("idempotency_key") or "").strip())


def build_restart_replay_journal_entry(
    *,
    task_type: str,
    task_id: str,
    side_effect_risk: str,
    phase: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    normalized_task_type = str(task_type or "runtime_task").strip() or "runtime_task"
    normalized_task_id = str(task_id or "").strip()
    normalized_phase = str(phase or "").strip() or "unknown"
    return {
        "schema": RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA,
        "idempotency_key": f"{normalized_task_type}:{normalized_task_id}:restart:{normalized_phase}",
        "task_type": normalized_task_type,
        "task_id": normalized_task_id,
        "phase": normalized_phase,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_restart_replay_journal(
    metadata: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    journal = [item for item in merged.get("restart_replay_journal", []) if isinstance(item, dict)]
    key = entry.get("idempotency_key")
    if key:
        journal = [item for item in journal if item.get("idempotency_key") != key]
    journal.append(entry)
    merged["restart_replay_journal"] = journal[-limit:]
    return merged


def has_mutating_restart_replay_journal(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str | None = None,
) -> bool:
    journal = (metadata or {}).get("restart_replay_journal")
    if not isinstance(journal, list):
        return False
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        if entry.get("schema") != RUNTIME_RESTART_REPLAY_JOURNAL_SCHEMA:
            continue
        if str(entry.get("task_type") or "") != str(task_type or ""):
            continue
        if task_id is not None and str(entry.get("task_id") or "") not in {"", str(task_id)}:
            continue
        if str(entry.get("side_effect_risk") or "") != "mutating":
            continue
        if str(entry.get("phase") or "") == "spawn_intent_recorded":
            return True
    return False


def build_completion_journal_entry(
    *,
    task_type: str,
    task_id: str,
    status: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    side_effect_risk: str = "unknown",
    summary: str | None = None,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    return {
        "schema": "runtime_completion_journal.v1",
        "idempotency_key": f"{task_type}:{task_id}:{normalized_status}",
        "task_type": str(task_type or "").strip() or "runtime_task",
        "task_id": str(task_id or "").strip(),
        "status": normalized_status,
        "trace_id": str(trace_id or "").strip() or None,
        "session_id": str(session_id or "").strip() or None,
        "side_effect_risk": str(side_effect_risk or "unknown").strip(),
        "summary": (summary or "").strip()[:1000],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_completion_journal(
    metadata: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    journal = [item for item in merged.get("completion_journal", []) if isinstance(item, dict)]
    key = entry.get("idempotency_key")
    if key:
        journal = [item for item in journal if item.get("idempotency_key") != key]
    journal.append(entry)
    merged["completion_journal"] = journal[-limit:]
    return merged


def _build_restart_reconciliation_retry_contract(
    *,
    task_type: str,
    task_id: str,
    blocker: str,
) -> dict[str, Any] | None:
    normalized_task_type = str(task_type or "").strip()
    normalized_blocker = str(blocker or "").strip()
    if normalized_task_type != "subagent" or normalized_blocker != "non_idempotent_subagent_type":
        return None
    return {
        "schema": RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA,
        "kind": "audited_subagent_restart_retry",
        "task_type": normalized_task_type,
        "task_id": str(task_id or "").strip(),
        "blocker": normalized_blocker,
        "requires_human_approval": True,
        "retry_mode": "restart_from_prompt",
        "side_effect_risk": "mutating",
    }


def has_restart_reconciliation_retry_contract(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str,
    blocker: str,
) -> bool:
    data = dict(metadata or {})
    if data.get("reconciliation_status") != "retry_requested":
        return False
    if not bool(data.get("reconciliation_retry_allowed")):
        return False
    contract = data.get("reconciliation_retry_contract")
    if not isinstance(contract, dict):
        return False
    return (
        contract.get("schema") == RUNTIME_RECONCILIATION_RETRY_CONTRACT_SCHEMA
        and contract.get("kind") == "audited_subagent_restart_retry"
        and str(contract.get("task_type") or "") == str(task_type or "")
        and str(contract.get("task_id") or "") == str(task_id or "")
        and str(contract.get("blocker") or "") == str(blocker or "")
        and contract.get("requires_human_approval") is True
        and str(contract.get("retry_mode") or "") == "restart_from_prompt"
    )


def build_restart_reconciliation_metadata(
    metadata: dict[str, Any] | None,
    *,
    task_type: str,
    task_id: str,
    blocker: str,
    summary: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark a non-idempotent restarted task as requiring side-effect reconciliation."""

    merged = dict(metadata or {})
    merged.update(
        {
            "resume_failed": True,
            "orphaned_by_restart": True,
            "needs_reconciliation": True,
            "reconciliation_reason": blocker,
            "restart_resume_blocker": blocker,
            "side_effect_risk": "mutating",
        }
    )
    retry_contract = _build_restart_reconciliation_retry_contract(
        task_type=task_type,
        task_id=task_id,
        blocker=blocker,
    )
    if retry_contract is not None:
        merged["reconciliation_retry_allowed"] = True
        merged["reconciliation_retry_contract"] = retry_contract
    else:
        merged.pop("reconciliation_retry_allowed", None)
        merged.pop("reconciliation_retry_contract", None)
    entry = build_completion_journal_entry(
        task_type=task_type,
        task_id=task_id,
        status="needs_reconciliation",
        trace_id=trace_id,
        session_id=session_id,
        side_effect_risk="mutating",
        summary=summary,
    )
    return merge_completion_journal(merged, entry)


async def create_runtime_task_record(
    *,
    task_id: str,
    task_type: str = "delegation",
    status: str = "pending",
    parent_agent_id: uuid.UUID | None = None,
    child_agent_id: uuid.UUID | None = None,
    child_agent_name: str | None = None,
    prompt: str | None = None,
    trace_id: str | None = None,
    parent_session_id: str | None = None,
    child_session_id: str | None = None,
    depth: int = 1,
    metadata_json: dict[str, Any] | None = None,
    budget_run_id: uuid.UUID | None = None,
    budget_reservation_key: str | None = None,
    budget_admission_status: str | None = None,
    budget_terminal_reason: str | None = None,
    root_user_id: uuid.UUID | None = None,
    root_session_id: str | None = None,
    root_runtime_task_id: uuid.UUID | str | None = None,
    delegation_chain: list[str] | tuple[str, ...] | None = None,
    root_idempotency_key: str | None = None,
    config_snapshot_hash: str | None = None,
    policy_snapshot_hash: str | None = None,
    claimed_by: str | None = None,
    claim_expires_at: datetime | None = None,
    claim_version: int = 0,
    attempt_count: int = 0,
) -> str:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        raise ValueError(f"Invalid runtime task id: {task_id!r}")

    started_at = datetime.now(timezone.utc) if status == "running" else None
    # Stage-2b: runtime_tasks now carries a tenant_id (RLS). Derive it from the
    # parent agent so the INSERT lands inside the agent's tenant. A missing or
    # unresolved parent is a blocked precondition, never a global orphan row.
    if parent_agent_id is not None:
        admission = await admit_agent_runtime_tenant(
            parent_agent_id,
            source=f"runtime_task:{task_type}",
            tenant_resolver=resolve_tenant_for_agent,
        )
        if not admission.ok:
            raise_runtime_tenant_precondition(admission)
        tenant_id = admission.tenant_id
    else:
        raise RuntimeTenantPreconditionError(
            reason_code="tenant_required",
            message=f"runtime_task:{task_type} requires parent_agent_id to resolve tenant authority.",
            source=f"runtime_task:{task_type}",
        )
    async with tenant_scoped_session(
        tenant_id,
        require_tenant=True,
        source=f"runtime_task:{task_type}",
    ) as db:
        try:
            db.add(
                RuntimeTask(
                    id=runtime_task_id,
                    task_type=task_type,
                    status=status,
                    parent_agent_id=parent_agent_id,
                    child_agent_id=child_agent_id,
                    child_agent_name=child_agent_name,
                    prompt=prompt,
                    trace_id=trace_id,
                    parent_session_id=parent_session_id,
                    child_session_id=child_session_id,
                    root_user_id=root_user_id,
                    root_session_id=str(root_session_id or "").strip() or None,
                    root_runtime_task_id=_coerce_task_id(root_runtime_task_id)
                    if root_runtime_task_id is not None
                    else None,
                    delegation_chain_json=[str(item) for item in (delegation_chain or []) if str(item).strip()],
                    depth=depth,
                    metadata_json=metadata_json,
                    started_at=started_at,
                    tenant_id=tenant_id,
                    budget_run_id=budget_run_id,
                    budget_reservation_key=budget_reservation_key,
                    budget_admission_status=budget_admission_status,
                    budget_terminal_reason=budget_terminal_reason,
                    claimed_by=str(claimed_by or "").strip() or None,
                    claim_expires_at=claim_expires_at,
                    claim_version=max(0, int(claim_version)),
                    attempt_count=max(0, int(attempt_count)),
                    root_idempotency_key=root_idempotency_key or f"{task_type}:{runtime_task_id}",
                    config_snapshot_hash=config_snapshot_hash,
                    policy_snapshot_hash=policy_snapshot_hash,
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return runtime_task_id.hex


async def update_runtime_task_record(
    task_id: str,
    *,
    expected_status: str | tuple[str, ...] | None = None,
    expected_claim_version: int | None = None,
    expected_claim_worker_id: str | None | object = _EXPECTED_CLAIM_WORKER_UNSET,
    locked_precondition: LockedRuntimeTaskPrecondition | None = None,
    startup_reconciliation_reason: str | None = None,
    **fields: Any,
) -> bool:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return False

    completion_notification = fields.pop("completion_notification", None)
    if completion_notification is not None and not isinstance(completion_notification, CompletionNotification):
        raise TypeError("completion_notification must be CompletionNotification")

    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=async_session)
    if tenant_id is None:
        return False
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_task_status_update",
    ) as db:
        try:
            result = await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == runtime_task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            task = result.scalar_one_or_none()
            if task is None:
                return False

            expected_statuses = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status or ())
            if expected_statuses and str(task.status or "") not in expected_statuses:
                return False
            if expected_claim_version is not None and int(task.claim_version or 0) != int(expected_claim_version):
                return False
            if expected_claim_worker_id is not _EXPECTED_CLAIM_WORKER_UNSET and str(task.claimed_by or "") != str(
                expected_claim_worker_id or ""
            ):
                return False
            if locked_precondition is not None and not locked_precondition(task):
                return False

            from app.services.runtime_task_fence import assert_runtime_task_fence

            assert_runtime_task_fence(task)

            if startup_reconciliation_reason is not None:
                from app.services.runtime_replay_policy import apply_runtime_replay_reconciliation

                await apply_runtime_replay_reconciliation(
                    db,
                    task,
                    reason=startup_reconciliation_reason,
                    summary=str(fields.get("result_summary") or "") or None,
                    metadata_json=(
                        dict(fields["metadata_json"]) if isinstance(fields.get("metadata_json"), dict) else None
                    ),
                )
                await db.commit()
                return True

            for key, value in fields.items():
                if hasattr(task, key):
                    if key == "metadata_json" and isinstance(value, dict) and isinstance(task.metadata_json, dict):
                        merged = dict(task.metadata_json)
                        merged.update(value)
                        setattr(task, key, merged)
                        continue
                    setattr(task, key, value)

            now = datetime.now(timezone.utc)
            status = fields.get("status")
            if status in _TERMINAL_STATUSES:
                existing_metadata = dict(getattr(task, "metadata_json", None) or {})
                persisted_task_id = getattr(task, "id", None)
                persisted_task_id_text = (
                    persisted_task_id.hex if isinstance(persisted_task_id, uuid.UUID) else str(task_id)
                )
                entry = build_completion_journal_entry(
                    task_type=str(getattr(task, "task_type", None) or "runtime_task"),
                    task_id=persisted_task_id_text,
                    status=str(status),
                    trace_id=fields.get("trace_id") or getattr(task, "trace_id", None),
                    session_id=(
                        fields.get("child_session_id")
                        or fields.get("parent_session_id")
                        or getattr(task, "child_session_id", None)
                    ),
                    side_effect_risk=str(existing_metadata.get("side_effect_risk") or "unknown"),
                    summary=fields.get("result_summary") or getattr(task, "result_summary", None),
                )
                task.metadata_json = merge_completion_journal(task.metadata_json, entry)
            if status == "running" and task.started_at is None:
                task.started_at = now
            if status in _TERMINAL_STATUSES and task.completed_at is None:
                task.completed_at = now
            if completion_notification is not None:
                if status not in _TERMINAL_STATUSES:
                    raise ValueError("completion_notification requires a terminal RuntimeTask status")
                outbox_id = await enqueue_completion_notification(db, completion_notification)
                metadata = dict(getattr(task, "metadata_json", None) or {})
                metadata["completion_outbox_id"] = str(outbox_id)
                task.metadata_json = metadata

            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return True


async def requeue_runtime_task_for_worker(
    task_id: str,
    *,
    task_type: str,
    expected_status: str,
    expected_claim_version: int,
    expected_claim_worker_id: str | None,
    metadata_json: dict[str, Any] | None = None,
    session_factory: Any | None = None,
) -> bool:
    """CAS an eligible startup record back to the shared worker queue.

    Startup recovery is only a queue repair authority. It must not execute the
    model, steal a live lease, or terminally mutate work already claimed by a
    different worker.
    """

    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return False
    factory = session_factory or async_session
    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=factory)
    if tenant_id is None:
        return False

    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(
        tenant_id,
        session_factory=factory,
        require_tenant=True,
        source="runtime_task_startup_requeue",
    ) as db:
        result = await db.execute(
            select(RuntimeTask)
            .where(RuntimeTask.id == runtime_task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        task = result.scalar_one_or_none()
        if task is None or str(task.task_type or "") != str(task_type or ""):
            return False
        if str(task.status or "") != str(expected_status or ""):
            return False
        if int(task.claim_version or 0) != int(expected_claim_version):
            return False
        if str(task.claimed_by or "") != str(expected_claim_worker_id or ""):
            return False
        if task.status == "running":
            claim_expires_at = task.claim_expires_at
            if claim_expires_at is not None:
                if claim_expires_at.tzinfo is None:
                    claim_expires_at = claim_expires_at.replace(tzinfo=timezone.utc)
                if claim_expires_at > now:
                    return False
        elif task.status != "pending":
            return False

        merged_metadata = dict(task.metadata_json or {})
        merged_metadata.update(metadata_json or {})
        merged_metadata.update(
            {
                "startup_requeued_at": now.isoformat(),
                "recovery_state": "queued_for_shared_worker",
            }
        )
        task.status = "resumable"
        task.claimed_by = None
        task.claim_expires_at = None
        task.scheduled_at = None
        task.metadata_json = merged_metadata
        await db.commit()
    return True


async def reconcile_runtime_task_for_replay_policy(
    task_id: str,
    *,
    task_type: str,
    expected_status: str,
    expected_claim_version: int,
    expected_claim_worker_id: str | None,
    reason: str,
    session_factory: Any | None = None,
) -> bool:
    """CAS an unsafe restart/reclaim outcome into governed reconciliation."""

    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return False
    factory = session_factory or async_session
    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=factory)
    if tenant_id is None:
        return False
    async with tenant_scoped_session(
        tenant_id,
        session_factory=factory,
        require_tenant=True,
        source="runtime_task_startup_reconciliation",
    ) as db:
        task = (
            await db.execute(
                select(RuntimeTask)
                .where(RuntimeTask.id == runtime_task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None or str(task.task_type or "") != str(task_type or ""):
            return False
        if str(task.status or "") != str(expected_status or ""):
            return False
        if int(task.claim_version or 0) != int(expected_claim_version):
            return False
        if str(task.claimed_by or "") != str(expected_claim_worker_id or ""):
            return False

        from app.services.runtime_replay_policy import (
            RuntimeReplaySnapshot,
            apply_runtime_replay_reconciliation,
            runtime_replay_disposition,
        )

        disposition = runtime_replay_disposition(
            RuntimeReplaySnapshot(
                task_id=task.id,
                task_type=str(task.task_type),
                status=str(task.status),
                claim_version=int(task.claim_version or 0),
                claimed_by=str(task.claimed_by or "").strip() or None,
                claim_expires_at=task.claim_expires_at,
                child_session_id=task.child_session_id,
                metadata=dict(task.metadata_json or {}),
            )
        )
        if disposition.action != "needs_reconciliation" or disposition.reason != reason:
            return False
        await apply_runtime_replay_reconciliation(db, task, reason=reason)
        await db.commit()
    return True


async def get_runtime_task_record(task_id: str) -> dict[str, Any] | None:
    runtime_task_id = _coerce_task_id(task_id)
    if runtime_task_id is None:
        return None

    tenant_id = await resolve_tenant_for_runtime_task(runtime_task_id, session_factory=async_session)
    if tenant_id is None:
        return None
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_task_status_read",
    ) as db:
        try:
            result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == runtime_task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return None
            return _task_to_dict(task)
        except Exception:
            await db.rollback()
            raise


async def list_runtime_task_records(
    *,
    parent_agent_id: uuid.UUID | None = None,
    root_user_id: uuid.UUID | None = None,
    root_session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    # Listing a single agent's runtime tasks → scope to that agent's tenant.
    # When no parent_agent is given (cross-agent listing) the tenant is unknown;
    # resolve_tenant_for_agent(None) → None pins an empty GUC (fail-closed),
    # which is the correct safe default for an unscoped list.
    tenant_id = await resolve_tenant_for_agent(parent_agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        try:
            stmt = select(RuntimeTask).order_by(RuntimeTask.created_at.desc()).limit(limit)
            if parent_agent_id is not None:
                stmt = stmt.where(RuntimeTask.parent_agent_id == parent_agent_id)
            if root_user_id is not None:
                stmt = stmt.where(RuntimeTask.root_user_id == root_user_id)
            if root_session_id is not None:
                stmt = stmt.where(RuntimeTask.root_session_id == str(root_session_id))
            result = await db.execute(stmt)
            tasks = result.scalars().all()
        except Exception:
            await db.rollback()
            raise
    return [_task_to_dict(task) for task in tasks]


async def list_active_runtime_task_records(
    *,
    statuses: tuple[str, ...] = ("pending", "running"),
    task_types: tuple[str, ...] | None = None,
    oldest_started_first: bool = False,
    limit: int | None = 50,
    session_factory: Any | None = None,
) -> list[dict[str, Any]]:
    # The global startup pass may only locate task/tenant pairs. Full records
    # are re-read under each tenant's RLS scope before they reach consumers.
    normalized_task_types = (
        tuple(dict.fromkeys(str(task_type).strip() for task_type in task_types if str(task_type).strip()))
        if task_types is not None
        else None
    )
    if normalized_task_types == ():
        return []
    factory = session_factory or async_session
    async with (
        factory() as db,
        enter_rls_bypass(db, reason="restart-safe async-delegation resume scan"),
    ):
        try:
            stmt = select(RuntimeTask.id, RuntimeTask.tenant_id).where(RuntimeTask.status.in_(statuses))
            if normalized_task_types is not None:
                stmt = stmt.where(RuntimeTask.task_type.in_(normalized_task_types))
            if oldest_started_first:
                stmt = stmt.order_by(
                    RuntimeTask.started_at.asc().nulls_last(),
                    RuntimeTask.created_at.asc(),
                )
            else:
                stmt = stmt.order_by(RuntimeTask.created_at.asc())
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await db.execute(stmt)
            locator_rows = result.all()
        except Exception:
            await db.rollback()
            raise

    ordered_ids, ids_by_tenant = _group_runtime_task_locators(
        locator_rows,
        operation="restart-safe active-task scan",
    )
    tasks_by_id: dict[uuid.UUID, RuntimeTask] = {}
    for tenant_id, task_ids in ids_by_tenant.items():
        async with tenant_scoped_session(
            tenant_id,
            session_factory=factory,
            require_tenant=True,
            source="restart_safe_active_task_hydration",
        ) as db:
            hydration_filters = [
                RuntimeTask.id.in_(task_ids),
                RuntimeTask.status.in_(statuses),
            ]
            if normalized_task_types is not None:
                hydration_filters.append(RuntimeTask.task_type.in_(normalized_task_types))
            result = await db.execute(select(RuntimeTask).where(*hydration_filters))
            tasks_by_id.update((task.id, task) for task in result.scalars().all())

    return [_task_to_dict(tasks_by_id[task_id]) for task_id in ordered_ids if task_id in tasks_by_id]


async def reconcile_orphaned_runtime_tasks(
    *,
    exclude_task_ids: set[str] | None = None,
    task_types: set[str] | frozenset[str] | tuple[str, ...] | None = None,
    inline_a2a_only: bool = False,
) -> int:
    """Mark non-resumable persisted running tasks as failed after a worker restart.

    Some task types have restart pumps and must remain active until those pumps
    resume them. Plain in-process tasks have no such recovery path, so surfacing
    them as failed is more honest than leaving them alive forever.
    """
    excluded = {
        runtime_task_id
        for task_id in (exclude_task_ids or set())
        if (runtime_task_id := _coerce_task_id(task_id)) is not None
    }
    scoped_task_types = (
        {str(value).strip() for value in task_types if str(value).strip()} if task_types is not None else None
    )
    if scoped_task_types == set():
        return 0
    # Cross-tenant access is limited to the locator columns. Reconciliation
    # reads and mutations happen only after reopening rows under tenant RLS.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="startup orphaned runtime-task reconcile"),
    ):
        try:
            stmt = select(RuntimeTask.id, RuntimeTask.tenant_id).where(
                RuntimeTask.status == "running",
                or_(
                    RuntimeTask.task_type.is_(None),
                    RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                ),
            )
            if scoped_task_types is not None:
                stmt = stmt.where(RuntimeTask.task_type.in_(scoped_task_types))
            if inline_a2a_only:
                stmt = stmt.where(_inline_a2a_recovery_sql_predicate())
            result = await db.execute(stmt)
            locator_rows = result.all()
        except Exception:
            await db.rollback()
            raise

    _, ids_by_tenant = _group_runtime_task_locators(
        locator_rows,
        operation="startup orphan reconciliation",
    )
    now = datetime.now(timezone.utc)
    updated = 0
    fenced_a2a_task_ids: set[uuid.UUID] = set()
    for tenant_id, located_task_ids in ids_by_tenant.items():
        task_ids = [task_id for task_id in located_task_ids if task_id not in excluded]
        if not task_ids:
            continue
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="startup_orphan_runtime_task_reconciliation",
        ) as db:
            locked_filters = [
                RuntimeTask.id.in_(task_ids),
                RuntimeTask.status == "running",
                or_(
                    RuntimeTask.task_type.is_(None),
                    RuntimeTask.task_type.notin_(_RESTART_RESUMABLE_TASK_TYPES),
                ),
            ]
            if scoped_task_types is not None:
                locked_filters.append(RuntimeTask.task_type.in_(scoped_task_types))
            if inline_a2a_only:
                locked_filters.append(_inline_a2a_recovery_sql_predicate())
            result = await db.execute(select(RuntimeTask).where(*locked_filters).with_for_update(skip_locked=True))
            for task in result.scalars().all():
                if getattr(task, "id", None) in excluded:
                    continue
                if inline_a2a_only and not _is_inline_a2a_recovery_task(task):
                    continue
                claim_expires_at = getattr(task, "claim_expires_at", None)
                if isinstance(claim_expires_at, datetime):
                    if claim_expires_at.tzinfo is None:
                        claim_expires_at = claim_expires_at.replace(tzinfo=timezone.utc)
                    if claim_expires_at > now:
                        continue
                task_type = str(getattr(task, "task_type", None) or "runtime_task")
                if scoped_task_types is not None and task_type not in scoped_task_types:
                    continue
                metadata = dict(getattr(task, "metadata_json", None) or {})
                if _is_restart_resumable_runtime_task(task):
                    if task_type in _RESTART_RESUMABLE_TASK_TYPES:
                        continue
                    metadata.setdefault("restart_resume_blocker", "restart_resume_not_confirmed")
                if task_type == "subagent":
                    from app.services.subagent_run_service import apply_locked_subagent_terminal_protocol

                    blocker = str(metadata.get("restart_resume_blocker") or "non_idempotent_restart_orphan")
                    summary = str(
                        task.result_summary
                        or (
                            "Subagent was interrupted by a worker restart and may have performed external side "
                            "effects; it requires reconciliation before retrying or marking complete."
                        )
                    )
                    reconciliation_metadata = build_restart_reconciliation_metadata(
                        metadata,
                        task_type=task_type,
                        task_id=task.id.hex,
                        blocker=blocker,
                        summary=summary,
                        trace_id=getattr(task, "trace_id", None),
                        session_id=getattr(task, "child_session_id", None) or getattr(task, "parent_session_id", None),
                    )
                    await apply_locked_subagent_terminal_protocol(
                        db,
                        task,
                        status="needs_reconciliation",
                        summary=summary,
                        blocker=blocker,
                        metadata_json=reconciliation_metadata,
                    )
                    updated += 1
                    continue
                if task_type in {
                    "delegation",
                    "a2a_delegation",
                    "trigger",
                    "heartbeat",
                    "business_task",
                }:
                    if _is_inline_a2a_recovery_task(task):
                        previous_claim = {
                            "claim_version": int(getattr(task, "claim_version", 0) or 0),
                            "claim_worker_id": str(getattr(task, "claimed_by", None) or ""),
                            "claim_expires_at": (
                                getattr(task, "claim_expires_at", None).isoformat()
                                if getattr(task, "claim_expires_at", None)
                                else None
                            ),
                        }
                        task.claim_version = previous_claim["claim_version"] + 1
                        task.claimed_by = f"startup-reconciler:{uuid.uuid4().hex}"
                        task.claim_expires_at = None
                        metadata["reconciled_from_claim"] = previous_claim
                        metadata["reconciliation_fence"] = {
                            "claim_version": task.claim_version,
                            "claim_worker_id": task.claimed_by,
                        }
                        metadata["recovery_evidence_status"] = "pending"
                        metadata.pop("recovery_evidence_last_error", None)
                        fenced_a2a_task_ids.add(task.id)
                    task.status = "needs_reconciliation"
                    blocker = str(metadata.get("restart_resume_blocker") or "non_idempotent_restart_orphan")
                    if not task.result_summary:
                        task.result_summary = (
                            "Task was interrupted by a worker restart and may have performed external side effects; "
                            "it requires reconciliation before retrying or marking complete."
                        )
                    task.metadata_json = build_restart_reconciliation_metadata(
                        metadata,
                        task_type=task_type,
                        task_id=getattr(task, "id", None).hex
                        if isinstance(getattr(task, "id", None), uuid.UUID)
                        else str(getattr(task, "id", "")),
                        blocker=blocker,
                        summary=task.result_summary,
                        trace_id=getattr(task, "trace_id", None),
                        session_id=getattr(task, "child_session_id", None) or getattr(task, "parent_session_id", None),
                    )
                    if task_type == "business_task":
                        try:
                            business_task_id = uuid.UUID(str(metadata.get("business_task_id")))
                        except (TypeError, ValueError):
                            business_task_id = None
                        if business_task_id is not None:
                            business_task = (
                                await db.execute(
                                    select(Task)
                                    .where(
                                        Task.id == business_task_id,
                                        Task.tenant_id == tenant_id,
                                        Task.agent_id == task.parent_agent_id,
                                    )
                                    .with_for_update()
                                )
                            ).scalar_one_or_none()
                            if business_task is not None and business_task.active_runtime_task_id == task.id:
                                business_task.status = "needs_reconciliation"
                                business_task.last_execution_status = "needs_reconciliation"
                                business_task.last_error = task.result_summary
                                business_task.completed_at = now
                    task.completed_at = now
                    updated += 1
                    continue
                task.status = "failed"
                task.completed_at = now
                if not task.result_summary:
                    task.result_summary = "Task failed because the worker process restarted before completion."
                metadata["orphaned_by_restart"] = True
                task.metadata_json = metadata
                updated += 1
    if fenced_a2a_task_ids:
        await refresh_inline_a2a_reconciliation_evidence(
            task_ids=fenced_a2a_task_ids,
            limit=None,
        )
    return updated
