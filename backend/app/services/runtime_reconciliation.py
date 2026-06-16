"""Admin-facing RuntimeTask reconciliation helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask

logger = logging.getLogger(__name__)

RECONCILIATION_STATUS = "needs_reconciliation"
RECONCILED_STATUS = "reconciled"
_ACTIONS = {"mark_resolved", "archive", "retry"}


class RuntimeReconciliationNotFound(LookupError):
    pass


class RuntimeReconciliationConflict(RuntimeError):
    pass


def _coerce_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _dt(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _metadata(task: RuntimeTask) -> dict[str, Any]:
    return dict(getattr(task, "metadata_json", None) or {})


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
        .where(RuntimeTask.tenant_id == tenant_id, RuntimeTask.status == status)
        .order_by(RuntimeTask.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    )
    if agent_id is not None:
        stmt = stmt.where(RuntimeTask.parent_agent_id == agent_id)
    result = await db.execute(stmt)
    return [runtime_reconciliation_view(task) for task in result.scalars().all()]


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


def _append_history(
    metadata: dict[str, Any],
    *,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    updated = dict(metadata)
    history = [item for item in updated.get("reconciliation_history", []) if isinstance(item, dict)]
    history.append(
        {
            "schema": "runtime_reconciliation_action.v1",
            "action": action,
            "reason": reason,
            "actor_user_id": str(actor_user_id),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    updated["reconciliation_history"] = history[-50:]
    return updated


async def _write_reconciliation_audit(
    task: RuntimeTask,
    *,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
) -> None:
    try:
        from app.services.audit_logger import write_audit_log

        await write_audit_log(
            action=f"runtime_reconciliation.{action}",
            details={
                "tenant_id": str(tenant_id),
                "runtime_task_id": str(task.id),
                "task_type": task.task_type,
                "previous_status": task.status,
                "reason": reason,
            },
            agent_id=task.parent_agent_id,
            user_id=actor_user_id,
        )
    except Exception as exc:
        logger.warning("[RuntimeReconciliation] audit write failed action=%s task=%s: %s", action, task.id, exc)


async def apply_runtime_reconciliation_action(
    db: AsyncSession,
    *,
    task_id: str | uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip()
    if normalized_action not in _ACTIONS:
        raise ValueError(f"Unsupported reconciliation action: {action!r}")
    runtime_task_id = _coerce_uuid(task_id)
    result = await db.execute(
        select(RuntimeTask).where(RuntimeTask.id == runtime_task_id, RuntimeTask.tenant_id == tenant_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise RuntimeReconciliationNotFound("Runtime reconciliation task not found")

    metadata = _metadata(task)
    normalized_reason = (reason or "").strip() or normalized_action
    await _write_reconciliation_audit(
        task,
        tenant_id=tenant_id,
        action=normalized_action,
        reason=normalized_reason,
        actor_user_id=actor_user_id,
    )
    metadata = _append_history(
        metadata,
        action=normalized_action,
        reason=normalized_reason,
        actor_user_id=actor_user_id,
    )

    now = datetime.now(timezone.utc)
    if normalized_action == "retry":
        if not bool(metadata.get("reconciliation_retry_allowed")):
            raise RuntimeReconciliationConflict("RuntimeTask is not marked retryable after reconciliation")
        task.status = "pending"
        task.started_at = None
        task.completed_at = None
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "retry_requested"
        metadata["reconciliation_retry_requested_at"] = now.isoformat()
    elif normalized_action == "archive":
        task.status = "killed"
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "archived"
        metadata["reconciliation_archived_at"] = now.isoformat()
        task.result_summary = task.result_summary or f"Archived after reconciliation: {normalized_reason}"
    else:
        task.status = RECONCILED_STATUS
        task.completed_at = task.completed_at or now
        metadata["needs_reconciliation"] = False
        metadata["reconciliation_status"] = "resolved"
        metadata["reconciliation_resolved_at"] = now.isoformat()
        task.result_summary = f"Reconciliation resolved: {normalized_reason}"

    task.metadata_json = metadata
    await db.flush()
    return runtime_reconciliation_view(task)
