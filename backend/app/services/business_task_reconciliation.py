"""Fail-closed recovery for BusinessTask workers lost during execution."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.services.business_task_runtime import quarantine_stale_business_task


def stale_business_task_statement(*, now: datetime, limit: int = 100):
    return (
        select(RuntimeTask, Task)
        .join(Task, Task.active_runtime_task_id == RuntimeTask.id)
        .where(
            RuntimeTask.task_type == "business_task",
            RuntimeTask.status == "running",
            RuntimeTask.claim_expires_at.is_not(None),
            RuntimeTask.claim_expires_at <= now,
            Task.status == "doing",
        )
        .order_by(RuntimeTask.claim_expires_at.asc(), RuntimeTask.created_at.asc())
        .limit(max(1, min(int(limit), 500)))
        .with_for_update(skip_locked=True, of=RuntimeTask)
    )


async def reconcile_stale_business_tasks_in_session(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Quarantine expired running leases without replaying unknown side effects."""

    current = now or datetime.now(timezone.utc)
    rows = (await db.execute(stale_business_task_statement(now=current, limit=limit))).all()
    for runtime_task, task in rows:
        await quarantine_stale_business_task(
            db=db,
            task=task,
            runtime_task=runtime_task,
            detected_at=current,
        )
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="business_task.worker_lease_expired",
            severity="warning",
            actor_type="system",
            actor_id=None,
            tenant_id=task.tenant_id,
            action="quarantine_stale_business_task",
            resource_type="task",
            resource_id=task.id,
            details={
                "runtime_task_id": str(runtime_task.id),
                "claim_version": int(runtime_task.claim_version or 0),
                "automatic_retry_allowed": False,
            },
        )
    await db.commit()
    return {"checked": len(rows), "quarantined": len(rows)}


async def reconcile_stale_business_tasks_once(*, limit: int = 100) -> dict[str, int]:
    """Global worker tick under an explicit, audited RLS bypass."""

    import app.database as database
    from app.database import enter_rls_bypass

    async with (
        database.async_session() as db,
        enter_rls_bypass(db, reason="BusinessTask expired worker lease reconciliation"),
    ):
        return await reconcile_stale_business_tasks_in_session(db, limit=limit)


__all__ = [
    "reconcile_stale_business_tasks_in_session",
    "reconcile_stale_business_tasks_once",
    "stale_business_task_statement",
]
