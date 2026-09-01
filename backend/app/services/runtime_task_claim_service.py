from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.task import Task


CLAIMABLE_RUNTIME_TASK_STATUSES = ("pending", "resumable")
LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "a2a_continuation",
    "approval_execution",
    "hr_provisioning",
    "dream",
    # Delegation replay policy is enforced after the worker binds a new fence:
    # read-only profiles may resume, while mutating profiles are quarantined.
    "delegation",
    # A trigger run whose worker died must be reachable again. Without this,
    # 2,107 ``running`` trigger rows with no lease accumulated over 38 days with
    # no runtime path able to touch them (only a process restart could).
    # ``execute_claimed_trigger_runtime_task`` refuses to replay a run that
    # already bound a session or whose intent has gone stale, so reclaim cannot
    # duplicate external side effects or stampede historic fires.
    "trigger",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_task_claim_conditions(*, claim_now: datetime):
    return (
        or_(
            RuntimeTask.status.in_(CLAIMABLE_RUNTIME_TASK_STATUSES),
            and_(
                RuntimeTask.status == "running",
                RuntimeTask.task_type.in_(LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES),
                or_(RuntimeTask.claim_expires_at.is_(None), RuntimeTask.claim_expires_at <= claim_now),
            ),
        ),
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


def build_runtime_task_claim_candidate_statement(
    *,
    task_types: tuple[str, ...] | None = None,
    now: datetime | None = None,
    batch_size: int = 10,
):
    """Discover claim candidates without taking a RuntimeTask lock.

    Budget-bound candidates are linearized later by locking every referenced
    RuntimeBudgetRun before any RuntimeTask row. The final claim query repeats
    all predicates while those run locks are held.
    """

    claim_now = now or _utcnow()
    stmt = (
        select(RuntimeTask.id, RuntimeTask.budget_run_id)
        .where(*_runtime_task_claim_conditions(claim_now=claim_now))
        .order_by(desc(RuntimeTask.priority), RuntimeTask.created_at.asc())
        .limit(batch_size)
    )
    if task_types:
        stmt = stmt.where(RuntimeTask.task_type.in_(task_types))
    return stmt


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
        .where(*_runtime_task_claim_conditions(claim_now=claim_now))
        .order_by(desc(RuntimeTask.priority), RuntimeTask.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    if task_types:
        stmt = stmt.where(RuntimeTask.task_type.in_(task_types))
    return stmt


async def _quarantine_business_task_claim(
    db: AsyncSession,
    runtime_task: RuntimeTask,
    *,
    metadata: dict[str, Any],
    now: datetime,
    reason: str,
) -> None:
    runtime_task.status = "needs_reconciliation"
    runtime_task.result_summary = reason
    runtime_task.completed_at = now
    metadata.update(
        {
            "phase": "terminal",
            "terminal_at": now.isoformat(),
            "outcome": {
                "status": "needs_reconciliation",
                "summary": reason,
            },
        }
    )
    runtime_task.metadata_json = metadata
    from app.services.business_task_runtime import enqueue_business_task_terminal_boundary

    await enqueue_business_task_terminal_boundary(db, runtime_task)


async def _bind_business_task_claim(
    db: AsyncSession,
    *,
    runtime_task: RuntimeTask,
    now: datetime,
) -> bool:
    """Move the user-facing Task to running in the RuntimeTask claim transaction."""

    metadata = dict(runtime_task.metadata_json or {})
    try:
        business_task_id = UUID(str(metadata["business_task_id"]))
    except (KeyError, TypeError, ValueError):
        await _quarantine_business_task_claim(
            db,
            runtime_task,
            metadata=metadata,
            now=now,
            reason="business task projection link has no valid business_task_id",
        )
        return False
    if runtime_task.tenant_id is None or runtime_task.parent_agent_id is None:
        raise RuntimeError("business task projection link has no tenant or Agent authority")
    business_task = (
        await db.execute(
            select(Task)
            .where(
                Task.id == business_task_id,
                Task.tenant_id == runtime_task.tenant_id,
                Task.agent_id == runtime_task.parent_agent_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if business_task is None or business_task.active_runtime_task_id != runtime_task.id:
        await _quarantine_business_task_claim(
            db,
            runtime_task,
            metadata=metadata,
            now=now,
            reason="business task projection link is missing or no longer active",
        )
        return False
    business_task.status = "doing"
    business_task.last_execution_status = "running"
    business_task.completed_at = None
    return True


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
        candidate_result = await self.db.execute(
            build_runtime_task_claim_candidate_statement(
                task_types=self.task_types,
                now=now,
                batch_size=batch_size,
            )
        )
        candidate_rows = list(candidate_result.all())
        if not candidate_rows:
            return []

        budget_run_ids = sorted({row[1] for row in candidate_rows if row[1] is not None}, key=str)
        active_budget_run_ids: set[UUID] = set()
        if budget_run_ids:
            locked_runs = list(
                (
                    await self.db.execute(
                        select(RuntimeBudgetRun)
                        .where(RuntimeBudgetRun.id.in_(budget_run_ids))
                        .order_by(RuntimeBudgetRun.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            active_budget_run_ids = {run.id for run in locked_runs if str(run.status) == "active"}

        eligible_candidate_ids = [
            task_id
            for task_id, budget_run_id in candidate_rows
            if budget_run_id is None or budget_run_id in active_budget_run_ids
        ]
        if not eligible_candidate_ids:
            await self.db.rollback()
            return []

        result = await self.db.execute(
            build_runtime_task_claim_statement(
                task_types=self.task_types,
                now=now,
                batch_size=batch_size,
            ).where(RuntimeTask.id.in_(eligible_candidate_ids))
        )
        tasks = list(result.scalars().all())
        if not tasks:
            await self.db.rollback()
            return []

        claim_expires_at = now + timedelta(seconds=self.lease_seconds)
        claimed_tasks: list[RuntimeTask] = []
        for task in tasks:
            reclaimed_expired_claim = str(getattr(task, "status", "") or "") == "running"
            previous_claim = {
                "worker_id": str(getattr(task, "claimed_by", None) or ""),
                "claim_version": int(getattr(task, "claim_version", 0) or 0),
                "claim_expires_at": (
                    task.claim_expires_at.isoformat() if getattr(task, "claim_expires_at", None) else None
                ),
            }
            if task.task_type == "business_task" and not await _bind_business_task_claim(
                self.db,
                runtime_task=task,
                now=now,
            ):
                continue
            task.status = "running"
            task.claimed_by = self.worker_id
            task.claim_expires_at = claim_expires_at
            task.attempt_count = int(getattr(task, "attempt_count", 0) or 0) + 1
            task.claim_version = int(getattr(task, "claim_version", 0) or 0) + 1
            if getattr(task, "started_at", None) is None:
                task.started_at = now
            metadata = dict(getattr(task, "metadata_json", None) or {})
            if reclaimed_expired_claim:
                metadata["reclaimed_expired_claim"] = True
                metadata["lease_reclaim_count"] = int(metadata.get("lease_reclaim_count") or 0) + 1
                metadata["reclaimed_at"] = now.isoformat()
                metadata["recovery_state"] = "recovering"
                metadata["previous_claim"] = previous_claim
                if previous_claim["claim_version"] == 0:
                    metadata["legacy_claim_backfilled"] = True
            else:
                # These fields describe only the claim that is being recovered.
                # A fresh pending claim must not inherit an older reclaim gate.
                for key in (
                    "reclaimed_expired_claim",
                    "reclaimed_at",
                    "previous_claim",
                    "legacy_claim_backfilled",
                    "recovery_state",
                ):
                    metadata.pop(key, None)
            metadata["claimed_by"] = self.worker_id
            metadata["claimed_at"] = now.isoformat()
            metadata["claim_expires_at"] = claim_expires_at.isoformat()
            metadata["claim_version"] = task.claim_version
            metadata["claim_fence"] = f"{task.id.hex}:{task.claim_version}"
            task.metadata_json = metadata
            claimed_tasks.append(task)
        await self.db.commit()
        return claimed_tasks


def runtime_task_claim_snapshot() -> dict[str, Any]:
    return {
        "claimable_statuses": list(CLAIMABLE_RUNTIME_TASK_STATUSES),
        "lease_reclaimable_task_types": list(LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES),
        "fence_contract": "claim_version+worker_id+lease",
    }
