"""Atomic business Task ↔ RuntimeTask lifecycle.

The human-facing ``Task`` and execution-facing ``RuntimeTask`` are two views of
one run. This module is the only place that creates their link or applies a
terminal outcome, so they cannot independently invent status transitions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.task import Task, TaskLog


class BusinessTaskInvariantError(RuntimeError):
    """Raised when Task and RuntimeTask no longer describe the same run."""


class BusinessTaskExecutionSuperseded(BusinessTaskInvariantError):
    """Raised when a terminal control decision wins before model invocation."""


class TaskExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs_reconciliation"


_TASK_STATUS_BY_OUTCOME = {
    TaskExecutionStatus.SUCCEEDED: "done",
    TaskExecutionStatus.BLOCKED: "blocked",
    TaskExecutionStatus.FAILED: "failed",
    TaskExecutionStatus.CANCELLED: "cancelled",
    TaskExecutionStatus.NEEDS_RECONCILIATION: "needs_reconciliation",
}
_RUNTIME_STATUS_BY_OUTCOME = {
    TaskExecutionStatus.SUCCEEDED: "completed",
    TaskExecutionStatus.BLOCKED: "skipped",
    TaskExecutionStatus.FAILED: "failed",
    TaskExecutionStatus.CANCELLED: "killed",
    TaskExecutionStatus.NEEDS_RECONCILIATION: "needs_reconciliation",
}
_ACTIVE_RUNTIME_STATUSES = frozenset({"pending", "running", "resumable", "suspended"})
_TASK_TERMINAL_STATUSES = frozenset({"done", "blocked", "failed", "cancelled", "needs_reconciliation"})


@dataclass(frozen=True, slots=True)
class TaskExecutionOutcome:
    status: TaskExecutionStatus
    summary: str
    result: str | None = None
    error_code: str | None = None
    retryable: bool = False
    reflection_session_id: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @property
    def task_status(self) -> str:
        return _TASK_STATUS_BY_OUTCOME[self.status]

    @property
    def runtime_status(self) -> str:
        return _RUNTIME_STATUS_BY_OUTCOME[self.status]

    @property
    def is_success(self) -> bool:
        return self.status is TaskExecutionStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["task_status"] = self.task_status
        payload["runtime_status"] = self.runtime_status
        return payload


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def business_task_request_key(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    action: str,
    payload: dict[str, Any],
) -> str:
    preimage = {
        "schema": "hive.business_task_request.v1",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "requester_user_id": str(requester_user_id),
        "action": str(action),
        "payload": dict(payload),
    }
    return hashlib.sha256(_canonical_json(preimage).encode("utf-8")).hexdigest()


def business_task_runtime_root_key(*, task_id: uuid.UUID, request_id: str) -> str:
    digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:32]
    return f"business_task:{task_id}:request:{digest}"


async def stage_business_task_runtime(
    *,
    db: AsyncSession,
    task: Task,
    requester_user_id: uuid.UUID,
    agent_name: str | None,
    request_id: str,
    request_hash: str | None = None,
    root_session_id: uuid.UUID | None = None,
    delivery_target: dict[str, Any] | None = None,
) -> RuntimeTask:
    """Add a linked RuntimeTask to the caller's uncommitted transaction."""

    if task.id is None or task.tenant_id is None:
        raise BusinessTaskInvariantError("business task requires persisted id and tenant before runtime staging")
    active_runtime_task_id = getattr(task, "active_runtime_task_id", None)
    if active_runtime_task_id is not None:
        active_runtime_task = await db.get(
            RuntimeTask,
            active_runtime_task_id,
            with_for_update=True,
        )
        if active_runtime_task is None:
            raise BusinessTaskInvariantError(
                "business task active runtime pointer is stale; reconciliation is required"
            )
        active_metadata = dict(active_runtime_task.metadata_json or {})
        pointer_matches = (
            active_runtime_task.task_type == "business_task"
            and active_runtime_task.tenant_id == task.tenant_id
            and active_runtime_task.parent_agent_id == task.agent_id
            and active_metadata.get("business_task_id") == str(task.id)
        )
        if not pointer_matches:
            raise BusinessTaskInvariantError("business task active runtime pointer does not belong to this authority")
        if active_runtime_task.status in {"pending", "running", "resumable", "suspended"}:
            raise BusinessTaskInvariantError("business task already has an active run")
    attempt = int(getattr(task, "execution_attempt", 0) or 0) + 1
    runtime_task_id = uuid.uuid4()
    runtime_task = RuntimeTask(
        id=runtime_task_id,
        task_type="business_task",
        status="pending",
        parent_agent_id=task.agent_id,
        child_agent_id=task.agent_id,
        child_agent_name=agent_name,
        tenant_id=task.tenant_id,
        prompt=task.description,
        trace_id=f"business_task:{runtime_task_id.hex}",
        parent_session_id=str(root_session_id) if root_session_id else None,
        root_user_id=requester_user_id,
        root_session_id=str(root_session_id) if root_session_id else None,
        root_runtime_task_id=runtime_task_id,
        delegation_chain_json=[f"agent:{task.agent_id}", f"business-task:{task.id}"],
        depth=1,
        root_idempotency_key=business_task_runtime_root_key(task_id=task.id, request_id=request_id),
        metadata_json={
            "schema": "hive.business_task_run.v1",
            "business_task_id": str(task.id),
            "requester_user_id": str(requester_user_id),
            "request_id": str(request_id),
            "request_hash": str(request_hash or ""),
            "attempt": attempt,
            "phase": "queued",
            "source": "tasks_api",
            "root_session_id": str(root_session_id) if root_session_id else None,
            "delivery_target": dict(delivery_target or {}) or None,
        },
    )
    db.add(runtime_task)
    task.active_runtime_task_id = runtime_task_id
    task.execution_attempt = attempt
    task.status = "pending"
    task.last_execution_status = "queued"
    task.last_error = None
    task.last_result = None
    task.completed_at = None
    await db.flush()
    return runtime_task


def apply_business_task_outcome(
    *,
    db: AsyncSession,
    task: Task,
    runtime_task: RuntimeTask,
    outcome: TaskExecutionOutcome,
    completed_at: datetime | None = None,
) -> None:
    """Mutate both projections and their final log inside one transaction."""

    metadata = dict(runtime_task.metadata_json or {})
    if str(metadata.get("business_task_id") or "") != str(task.id):
        raise BusinessTaskInvariantError("runtime task business_task_id mismatch")
    if task.active_runtime_task_id != runtime_task.id:
        raise BusinessTaskInvariantError("task active runtime pointer mismatch")
    finished_at = completed_at or datetime.now(timezone.utc)
    task.status = outcome.task_status
    task.last_execution_status = outcome.status.value
    task.last_result = (outcome.result or outcome.summary)[:20_000] if outcome.is_success else None
    task.last_error = (
        (f"{outcome.error_code}: {outcome.summary}" if outcome.error_code else outcome.summary)[:20_000]
        if not outcome.is_success
        else None
    )
    task.completed_at = finished_at

    runtime_task.status = outcome.runtime_status
    runtime_task.result_summary = outcome.summary[:20_000]
    runtime_task.completed_at = finished_at
    metadata.update(
        {
            "phase": "terminal",
            "outcome": outcome.to_dict(),
            "terminal_at": finished_at.isoformat(),
        }
    )
    runtime_task.metadata_json = metadata

    icon = {
        TaskExecutionStatus.SUCCEEDED: "✅",
        TaskExecutionStatus.BLOCKED: "⏸️",
        TaskExecutionStatus.FAILED: "❌",
        TaskExecutionStatus.CANCELLED: "⛔",
        TaskExecutionStatus.NEEDS_RECONCILIATION: "⚠️",
    }[outcome.status]
    db.add(
        TaskLog(
            tenant_id=task.tenant_id,
            task_id=task.id,
            content=f"{icon} {outcome.summary}" + (f"\n\n{outcome.result}" if outcome.result else ""),
        )
    )


def _runtime_binding_matches(task: Task, runtime_task: RuntimeTask) -> bool:
    metadata = dict(getattr(runtime_task, "metadata_json", None) or {})
    return (
        getattr(task, "active_runtime_task_id", None) == getattr(runtime_task, "id", None)
        and str(metadata.get("business_task_id") or "") == str(getattr(task, "id", ""))
        and getattr(runtime_task, "task_type", "business_task") == "business_task"
        and getattr(runtime_task, "tenant_id", None) == getattr(task, "tenant_id", None)
        and getattr(runtime_task, "parent_agent_id", getattr(task, "agent_id", None)) == getattr(task, "agent_id", None)
    )


def _assert_business_task_execution_startable(*, task: Task, runtime_task: RuntimeTask) -> None:
    metadata = dict(getattr(runtime_task, "metadata_json", None) or {})
    if (
        str(getattr(runtime_task, "status", "") or "") != "running"
        or str(metadata.get("phase") or "queued") == "terminal"
        or str(getattr(task, "status", "") or "") in _TASK_TERMINAL_STATUSES
    ):
        raise BusinessTaskExecutionSuperseded("business task reached a terminal control state before invocation")


def _business_task_stages(
    *, task_status: str, runtime_status: str | None, phase: str, authorized: bool
) -> list[dict[str, str]]:
    terminal = task_status in _TASK_TERMINAL_STATUSES
    executing_started = phase in {"invoking", "terminal"} or runtime_status == "running"
    terminal_stage_status = {
        "done": "complete",
        "failed": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "needs_reconciliation": "warning",
    }.get(task_status, "pending")
    return [
        {"id": "accepted", "label": "Assignment accepted", "status": "complete"},
        {
            "id": "authorized",
            "label": "Execution authorized",
            "status": "complete" if authorized else "blocked",
        },
        {
            "id": "queued",
            "label": "Durable run queued",
            "status": "complete" if runtime_status else "pending",
        },
        {
            "id": "executing",
            "label": "Agent execution",
            "status": "complete" if terminal and executing_started else "current" if executing_started else "pending",
        },
        {"id": "terminal", "label": "Final outcome", "status": terminal_stage_status},
    ]


def project_business_task(*, task: Task, runtime_task: RuntimeTask | None) -> dict[str, Any]:
    """Build the canonical user-facing state and action projection."""

    task_status = str(getattr(task, "status", "pending") or "pending")
    metadata = dict(getattr(runtime_task, "metadata_json", None) or {}) if runtime_task is not None else {}
    outcome = metadata.get("outcome") if isinstance(metadata.get("outcome"), dict) else {}
    reconciliation = metadata.get("reconciliation") if isinstance(metadata.get("reconciliation"), dict) else {}
    runtime_status = str(getattr(runtime_task, "status", "") or "") or None
    phase = str(metadata.get("phase") or ("terminal" if task_status in _TASK_TERMINAL_STATUSES else "queued"))
    binding_matches = runtime_task is not None and _runtime_binding_matches(task, runtime_task)
    resolved_retry_safe = reconciliation.get("decision") == "retry_safe"
    retryable = bool(outcome.get("retryable")) or resolved_retry_safe
    can_cancel = bool(
        binding_matches and runtime_status in _ACTIVE_RUNTIME_STATUSES and task_status not in _TASK_TERMINAL_STATUSES
    )
    can_retry = bool(
        binding_matches
        and task_status in {"failed", "blocked", "cancelled"}
        and runtime_status not in _ACTIVE_RUNTIME_STATUSES
        and retryable
    )
    can_reconcile = bool(
        binding_matches and task_status == "needs_reconciliation" and not reconciliation.get("resolved_at")
    )
    if can_reconcile:
        recovery_state = "needs_review"
    elif can_retry:
        recovery_state = "retry_available"
    elif task_status == "done":
        recovery_state = "complete"
    elif task_status == "cancelled":
        recovery_state = "cancelled"
    elif runtime_task is None and getattr(task, "active_runtime_task_id", None) is not None:
        recovery_state = "runtime_evidence_missing"
    else:
        recovery_state = "none"
    authorized = isinstance(getattr(task, "plan_authorization", None), dict)
    return {
        "runtime_task_id": str(runtime_task.id) if runtime_task is not None else None,
        "runtime_status": runtime_status,
        "runtime_phase": phase,
        "runtime_summary": getattr(runtime_task, "result_summary", None) if runtime_task is not None else None,
        "runtime_request_id": str(metadata.get("request_id") or "") or None,
        "reflection_session_id": outcome.get("reflection_session_id"),
        "recovery_state": recovery_state,
        "recovery_message": str(outcome.get("summary") or getattr(task, "last_error", None) or "") or None,
        "actions": {
            "can_cancel": can_cancel,
            "can_retry": can_retry,
            "can_reconcile": can_reconcile,
        },
        "dependencies": [
            {
                "id": "confirmed_plan",
                "label": "Confirmed execution plan",
                "status": "satisfied" if authorized else "missing",
            },
            {
                "id": "runtime_intent",
                "label": "Durable runtime intent",
                "status": "satisfied" if binding_matches else "missing",
            },
        ],
        "stages": _business_task_stages(
            task_status=task_status,
            runtime_status=runtime_status,
            phase=phase,
            authorized=authorized,
        ),
    }


def _invalidate_runtime_claim(runtime_task: RuntimeTask) -> None:
    runtime_task.claim_version = int(getattr(runtime_task, "claim_version", 0) or 0) + 1
    runtime_task.claimed_by = None
    runtime_task.claim_expires_at = None


def apply_business_task_cancellation(
    *,
    db: AsyncSession,
    task: Task,
    runtime_task: RuntimeTask,
    cancelled_by_user_id: uuid.UUID,
    reason: str,
    completed_at: datetime | None = None,
) -> TaskExecutionOutcome:
    """Cancel a queued run, or quarantine an interrupted run with unknown effects."""

    if not _runtime_binding_matches(task, runtime_task):
        raise BusinessTaskInvariantError("business task cancellation runtime binding is invalid")
    if str(getattr(runtime_task, "status", "") or "") not in _ACTIVE_RUNTIME_STATUSES:
        raise BusinessTaskInvariantError("business task has no active run to cancel")
    metadata = dict(runtime_task.metadata_json or {})
    phase = str(metadata.get("phase") or "queued")
    not_started = runtime_task.status in {"pending", "resumable", "suspended"} and phase == "queued"
    clean_reason = str(reason or "").strip() or "Cancelled by the requester."
    outcome = TaskExecutionOutcome(
        status=TaskExecutionStatus.CANCELLED if not_started else TaskExecutionStatus.NEEDS_RECONCILIATION,
        summary=(
            f"Business task cancelled before execution: {clean_reason}"
            if not_started
            else f"Business task interrupted during execution; review possible side effects before retry: {clean_reason}"
        ),
        error_code="cancelled_before_execution" if not_started else "cancelled_during_execution",
        retryable=not_started,
    )
    metadata.update(
        {
            "cancelled_by_user_id": str(cancelled_by_user_id),
            "cancel_reason": clean_reason,
            "cancellation_safety": "not_started" if not_started else "side_effects_unknown",
        }
    )
    runtime_task.metadata_json = metadata
    _invalidate_runtime_claim(runtime_task)
    apply_business_task_outcome(
        db=db,
        task=task,
        runtime_task=runtime_task,
        outcome=outcome,
        completed_at=completed_at,
    )
    return outcome


def reconcile_business_task(
    *,
    db: AsyncSession,
    task: Task,
    runtime_task: RuntimeTask,
    resolved_by_user_id: uuid.UUID,
    decision: str,
    reason: str,
    resolved_at: datetime | None = None,
) -> None:
    """Record the user's explicit decision after an unknown-side-effect stop."""

    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("business task reconciliation reason is required")
    if decision not in {"retry_safe", "close_without_retry"}:
        raise ValueError("business task reconciliation decision is invalid")
    if not _runtime_binding_matches(task, runtime_task):
        raise BusinessTaskInvariantError("business task reconciliation runtime binding is invalid")
    if task.status != "needs_reconciliation" or runtime_task.status != "needs_reconciliation":
        raise BusinessTaskInvariantError("business task is not waiting for reconciliation")
    when = resolved_at or datetime.now(timezone.utc)
    metadata = dict(runtime_task.metadata_json or {})
    metadata["reconciliation"] = {
        "decision": decision,
        "reason": clean_reason,
        "resolved_by_user_id": str(resolved_by_user_id),
        "resolved_at": when.isoformat(),
    }
    metadata["recovery_state"] = "resolved_retry_safe" if decision == "retry_safe" else "resolved_closed"
    runtime_task.metadata_json = metadata
    task.status = "failed" if decision == "retry_safe" else "cancelled"
    task.last_execution_status = "reconciled_retry_safe" if decision == "retry_safe" else "reconciled_closed"
    task.last_error = clean_reason
    task.completed_at = when
    db.add(
        TaskLog(
            tenant_id=task.tenant_id,
            task_id=task.id,
            content=(
                "✅ Reconciliation confirmed; retry is now allowed. "
                if decision == "retry_safe"
                else "⛔ Reconciliation closed without retry. "
            )
            + clean_reason,
        )
    )


def quarantine_stale_business_task(
    *,
    db: AsyncSession,
    task: Task,
    runtime_task: RuntimeTask,
    detected_at: datetime | None = None,
) -> None:
    """Fail closed when a running worker lease expires; never replay side effects."""

    if not _runtime_binding_matches(task, runtime_task):
        raise BusinessTaskInvariantError("stale business task runtime binding is invalid")
    _invalidate_runtime_claim(runtime_task)
    apply_business_task_outcome(
        db=db,
        task=task,
        runtime_task=runtime_task,
        outcome=TaskExecutionOutcome(
            status=TaskExecutionStatus.NEEDS_RECONCILIATION,
            summary="Business task worker lease expired during execution; side effects require review before retry.",
            error_code="worker_lease_expired",
            retryable=False,
        ),
        completed_at=detected_at,
    )


async def _enqueue_business_task_channel_delivery(
    *,
    db: AsyncSession,
    runtime_task: RuntimeTask,
    outcome: TaskExecutionOutcome,
) -> None:
    metadata = dict(runtime_task.metadata_json or {})
    target = metadata.get("delivery_target")
    if not isinstance(target, dict) or not target:
        return
    channel = str(target.get("channel") or "").strip().lower()
    if not channel or channel == "web":
        return
    try:
        session_id = uuid.UUID(str(metadata.get("root_session_id") or runtime_task.parent_session_id))
        requester_user_id = uuid.UUID(str(metadata["requester_user_id"]))
    except (KeyError, ValueError) as exc:
        raise BusinessTaskInvariantError("business task channel delivery authority is invalid") from exc
    if runtime_task.tenant_id is None or runtime_task.parent_agent_id is None:
        raise BusinessTaskInvariantError("business task channel delivery has no tenant or Agent")

    from app.models.channel_config import ChannelConfig
    from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery

    config = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.tenant_id == runtime_task.tenant_id,
                ChannelConfig.agent_id == runtime_task.parent_agent_id,
                ChannelConfig.channel_type == ("microsoft_teams" if channel == "teams" else channel),
            )
        )
    ).scalar_one_or_none()
    text = outcome.summary
    if outcome.result and outcome.result.strip() != outcome.summary.strip():
        text = f"{outcome.summary}\n\n{outcome.result}"
    await enqueue_channel_delivery(
        db,
        ChannelDeliveryIntent(
            tenant_id=runtime_task.tenant_id,
            runtime_task_id=runtime_task.id,
            agent_id=runtime_task.parent_agent_id,
            session_id=session_id,
            user_id=requester_user_id,
            channel_config_id=getattr(config, "id", None),
            delivery_target=target,
            text=text[:20_000],
            terminal_status=outcome.runtime_status,
            metadata={
                "source": "business_task_runtime",
                "business_task_id": metadata.get("business_task_id"),
                "evidence_refs": list(outcome.evidence_refs),
            },
        ),
    )


async def mark_business_task_execution_started(*, runtime_task_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Bind the claimed RuntimeTask to its Task and requester before invocation."""

    from app.database import async_session, enter_rls_bypass, tenant_scoped_session

    async with async_session() as locator_db:
        async with enter_rls_bypass(locator_db, reason="business task runtime locator") as bypass_db:
            row = (
                await bypass_db.execute(select(RuntimeTask.tenant_id).where(RuntimeTask.id == runtime_task_id))
            ).one_or_none()
        await locator_db.rollback()
    if row is None or row[0] is None:
        raise BusinessTaskInvariantError("business RuntimeTask tenant not found")
    tenant_id = row[0]
    async with tenant_scoped_session(tenant_id) as db:
        runtime_task = await db.get(RuntimeTask, runtime_task_id, with_for_update=True)
        if runtime_task is None or runtime_task.task_type != "business_task":
            raise BusinessTaskInvariantError("business RuntimeTask not found")
        metadata = dict(runtime_task.metadata_json or {})
        try:
            task_id = uuid.UUID(str(metadata["business_task_id"]))
            requester_id = uuid.UUID(str(metadata["requester_user_id"]))
        except (KeyError, ValueError) as exc:
            raise BusinessTaskInvariantError("business RuntimeTask authority metadata is invalid") from exc
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id, Task.agent_id == runtime_task.parent_agent_id, Task.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None or task.active_runtime_task_id != runtime_task_id:
            raise BusinessTaskInvariantError("business Task runtime link is invalid")
        if runtime_task.parent_agent_id is None:
            raise BusinessTaskInvariantError("business RuntimeTask has no Agent authority")
        _assert_business_task_execution_startable(task=task, runtime_task=runtime_task)
        task.status = "doing"
        task.last_execution_status = "running"
        metadata["phase"] = "invoking"
        metadata["started_at"] = datetime.now(timezone.utc).isoformat()
        runtime_task.metadata_json = metadata
        await db.commit()
        return task_id, runtime_task.parent_agent_id, requester_id


async def finalize_business_task_execution(
    *,
    runtime_task_id: uuid.UUID,
    outcome: TaskExecutionOutcome,
) -> bool:
    """Atomically persist Task and RuntimeTask terminal state; idempotent on retry."""

    from app.database import async_session, enter_rls_bypass, tenant_scoped_session

    async with async_session() as locator_db:
        async with enter_rls_bypass(locator_db, reason="business task finalization locator") as bypass_db:
            row = (
                await bypass_db.execute(select(RuntimeTask.tenant_id).where(RuntimeTask.id == runtime_task_id))
            ).one_or_none()
        await locator_db.rollback()
    if row is None or row[0] is None:
        return False
    tenant_id = row[0]
    async with tenant_scoped_session(tenant_id) as db:
        runtime_task = await db.get(RuntimeTask, runtime_task_id, with_for_update=True)
        if runtime_task is None:
            return False
        metadata = dict(runtime_task.metadata_json or {})
        if metadata.get("phase") == "terminal":
            return True
        try:
            task_id = uuid.UUID(str(metadata["business_task_id"]))
        except (KeyError, ValueError) as exc:
            raise BusinessTaskInvariantError("business RuntimeTask has no valid Task binding") from exc
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id, Task.agent_id == runtime_task.parent_agent_id, Task.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            raise BusinessTaskInvariantError("business Task not found during finalization")
        apply_business_task_outcome(db=db, task=task, runtime_task=runtime_task, outcome=outcome)
        await _enqueue_business_task_channel_delivery(db=db, runtime_task=runtime_task, outcome=outcome)
        await db.commit()
        return True


__all__ = [
    "BusinessTaskExecutionSuperseded",
    "BusinessTaskInvariantError",
    "TaskExecutionOutcome",
    "TaskExecutionStatus",
    "apply_business_task_cancellation",
    "apply_business_task_outcome",
    "business_task_request_key",
    "business_task_runtime_root_key",
    "finalize_business_task_execution",
    "mark_business_task_execution_started",
    "project_business_task",
    "quarantine_stale_business_task",
    "reconcile_business_task",
    "stage_business_task_runtime",
]
