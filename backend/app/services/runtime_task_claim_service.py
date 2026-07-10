from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask


CLAIMABLE_RUNTIME_TASK_STATUSES = ("pending", "resumable")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_runtime_task_claim_statement(
    *,
    task_types: tuple[str, ...] | None = None,
    now: datetime | None = None,
    batch_size: int = 10,
):
    """Build the RuntimeTask claim query with PostgreSQL SKIP LOCKED semantics."""
    claim_now = now or _utcnow()
    stmt = (
        select(RuntimeTask)
        .where(
            RuntimeTask.status.in_(CLAIMABLE_RUNTIME_TASK_STATUSES),
            or_(RuntimeTask.scheduled_at.is_(None), RuntimeTask.scheduled_at <= claim_now),
            or_(
                RuntimeTask.budget_run_id.is_(None),
                exists(
                    select(RuntimeBudgetRun.id).where(
                        RuntimeBudgetRun.id == RuntimeTask.budget_run_id,
                        RuntimeBudgetRun.status == "active",
                    )
                ),
            ),
        )
        .order_by(desc(RuntimeTask.priority), RuntimeTask.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    if task_types:
        stmt = stmt.where(RuntimeTask.task_type.in_(task_types))
    return stmt


class RuntimeTaskClaimService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        worker_id: str,
        task_types: tuple[str, ...] | None = None,
        lease_seconds: int = 120,
    ) -> None:
        self.db = db
        self.worker_id = worker_id
        self.task_types = task_types
        self.lease_seconds = lease_seconds

    async def claim_available(self, *, batch_size: int = 10) -> list[RuntimeTask]:
        now = _utcnow()
        result = await self.db.execute(
            build_runtime_task_claim_statement(
                task_types=self.task_types,
                now=now,
                batch_size=batch_size,
            )
        )
        tasks = list(result.scalars().all())
        if not tasks:
            return []

        claim_expires_at = now + timedelta(seconds=self.lease_seconds)
        for task in tasks:
            task.status = "running"
            task.claimed_by = self.worker_id
            task.claim_expires_at = claim_expires_at
            task.attempt_count = int(getattr(task, "attempt_count", 0) or 0) + 1
            task.claim_version = int(getattr(task, "claim_version", 0) or 0) + 1
            if getattr(task, "started_at", None) is None:
                task.started_at = now
            metadata = dict(getattr(task, "metadata_json", None) or {})
            metadata["claimed_by"] = self.worker_id
            metadata["claimed_at"] = now.isoformat()
            metadata["claim_expires_at"] = claim_expires_at.isoformat()
            metadata["claim_version"] = task.claim_version
            metadata["claim_fence"] = f"{task.id.hex}:{task.claim_version}"
            task.metadata_json = metadata
        await self.db.commit()
        return tasks


def runtime_task_claim_snapshot() -> dict[str, Any]:
    return {
        "claimable_statuses": list(CLAIMABLE_RUNTIME_TASK_STATUSES),
    }
