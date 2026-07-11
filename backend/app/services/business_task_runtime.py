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
        await db.commit()
        return True


__all__ = [
    "BusinessTaskInvariantError",
    "TaskExecutionOutcome",
    "TaskExecutionStatus",
    "apply_business_task_outcome",
    "business_task_request_key",
    "business_task_runtime_root_key",
    "finalize_business_task_execution",
    "mark_business_task_execution_started",
    "stage_business_task_runtime",
]
