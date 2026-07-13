from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, desc, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.services.runtime_replay_policy import (
    RuntimeReplaySnapshot,
    apply_runtime_replay_reconciliation,
    runtime_replay_disposition,
)


CLAIMABLE_RUNTIME_TASK_STATUSES = ("pending", "resumable")
LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "approval_execution",
    "hr_provisioning",
    "dream",
    "system_plan_run",
    "subagent",
    "delegation",
    "trigger",
    "heartbeat",
)

RUNTIME_TASK_AGED_LANE_SECONDS = 300
RUNTIME_REPLAY_POLICY_TASK_TYPES = frozenset({"delegation", "trigger", "heartbeat", "subagent"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_runtime_task_claim_statement(
    *,
    task_types: tuple[str, ...] | None = None,
    now: datetime | None = None,
    batch_size: int = 10,
    lane: Literal["normal", "aged"] = "normal",
    exclude_task_ids: tuple[UUID, ...] = (),
):
    """Build the RuntimeTask claim query with PostgreSQL SKIP LOCKED semantics."""
    claim_now = now or _utcnow()
    task_type_set = set(task_types or ())
    includes_reclaimable_type = not task_type_set or bool(
        task_type_set.intersection(LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES)
    )
    status_predicate = RuntimeTask.status.in_(CLAIMABLE_RUNTIME_TASK_STATUSES)
    if includes_reclaimable_type:
        status_predicate = or_(
            status_predicate,
            and_(
                RuntimeTask.status == "running",
                RuntimeTask.task_type.in_(LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES),
                or_(RuntimeTask.claim_expires_at.is_(None), RuntimeTask.claim_expires_at <= claim_now),
            ),
        )
    stmt = select(RuntimeTask).where(
        status_predicate,
        or_(RuntimeTask.scheduled_at.is_(None), RuntimeTask.scheduled_at <= claim_now),
        or_(
            RuntimeTask.budget_run_id.is_(None),
            exists(
                select(RuntimeBudgetRun.id).where(
                    RuntimeBudgetRun.id == RuntimeTask.budget_run_id,
                    RuntimeBudgetRun.tenant_id == RuntimeTask.tenant_id,
                    RuntimeBudgetRun.status == "active",
                )
            ),
        ),
    )
    if task_types:
        stmt = stmt.where(RuntimeTask.task_type.in_(task_types))
    if exclude_task_ids:
        stmt = stmt.where(RuntimeTask.id.not_in(exclude_task_ids))
    if lane == "aged":
        stmt = stmt.where(
            RuntimeTask.created_at <= claim_now - timedelta(seconds=RUNTIME_TASK_AGED_LANE_SECONDS)
        ).order_by(RuntimeTask.created_at.asc(), RuntimeTask.id.asc())
    else:
        stmt = stmt.order_by(desc(RuntimeTask.priority), RuntimeTask.created_at.asc(), RuntimeTask.id.asc())
    return stmt.limit(batch_size).with_for_update(skip_locked=True)


def _quarantine_business_task_claim(
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


async def _quarantine_reconciliation_retry_claim(
    db: AsyncSession,
    runtime_task: RuntimeTask,
    *,
    metadata: dict[str, Any],
    now: datetime,
    reason: str,
) -> None:
    metadata.update(
        {
            "needs_reconciliation": True,
            "reconciliation_status": "open",
            "reconciliation_reason": "runtime_retry_authority_invalid",
            "restart_resume_blocker": reason,
            "reconciliation_detected_at": now.isoformat(),
        }
    )
    runtime_task.scheduled_at = None
    if runtime_task.task_type == "subagent":
        from app.services.subagent_run_service import apply_locked_subagent_terminal_protocol

        await apply_locked_subagent_terminal_protocol(
            db,
            runtime_task,
            status="needs_reconciliation",
            summary=reason,
            blocker="runtime_retry_authority_invalid",
            metadata_json=metadata,
        )
        return
    runtime_task.status = "needs_reconciliation"
    runtime_task.claim_expires_at = None
    runtime_task.completed_at = None
    runtime_task.result_summary = reason
    runtime_task.metadata_json = metadata


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
        _quarantine_business_task_claim(
            runtime_task,
            metadata=metadata,
            now=now,
            reason="business task projection link has no valid business_task_id",
        )
        return False
    if runtime_task.tenant_id is None or runtime_task.parent_agent_id is None:
        _quarantine_business_task_claim(
            runtime_task,
            metadata=metadata,
            now=now,
            reason="business task projection link has no tenant or Agent authority",
        )
        return False
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
        _quarantine_business_task_claim(
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
        aged_result = await self.db.execute(
            build_runtime_task_claim_statement(
                task_types=self.task_types,
                now=now,
                batch_size=1,
                lane="aged",
            )
        )
        tasks = list(aged_result.scalars().all())[:1]
        remaining = max(0, int(batch_size) - len(tasks))
        if remaining:
            normal_result = await self.db.execute(
                build_runtime_task_claim_statement(
                    task_types=self.task_types,
                    now=now,
                    batch_size=remaining,
                    lane="normal",
                    exclude_task_ids=tuple(task.id for task in tasks),
                )
            )
            selected_ids = {task.id for task in tasks}
            tasks.extend(task for task in normal_result.scalars().all() if task.id not in selected_ids)
            tasks = tasks[:batch_size]
        if not tasks:
            return []

        claim_expires_at = now + timedelta(seconds=self.lease_seconds)
        claimed_tasks: list[RuntimeTask] = []
        for task in tasks:
            metadata = dict(getattr(task, "metadata_json", None) or {})
            reclaimed_expired_claim = str(getattr(task, "status", "") or "") == "running"
            previous_claim = {
                "worker_id": str(getattr(task, "claimed_by", None) or ""),
                "claim_version": int(getattr(task, "claim_version", 0) or 0),
                "claim_expires_at": (
                    task.claim_expires_at.isoformat() if getattr(task, "claim_expires_at", None) else None
                ),
            }
            if reclaimed_expired_claim and str(task.task_type) in RUNTIME_REPLAY_POLICY_TASK_TYPES:
                disposition = runtime_replay_disposition(
                    RuntimeReplaySnapshot(
                        task_id=task.id,
                        task_type=str(task.task_type),
                        status=str(task.status),
                        claim_version=previous_claim["claim_version"],
                        claimed_by=str(task.claimed_by) if task.claimed_by else None,
                        claim_expires_at=task.claim_expires_at,
                        child_session_id=task.child_session_id,
                        metadata=metadata,
                    ),
                    now=now,
                )
                if disposition.action == "ignore_live_claim":
                    continue
                if disposition.action == "needs_reconciliation":
                    await apply_runtime_replay_reconciliation(
                        self.db,
                        task,
                        reason=disposition.reason,
                        now=now,
                    )
                    continue
            if task.task_type == "business_task" and not await _bind_business_task_claim(
                self.db,
                runtime_task=task,
                now=now,
            ):
                continue
            if isinstance(metadata.get("reconciliation_operation"), dict):
                from app.services.runtime_reconciliation import (
                    RuntimeReconciliationConflict,
                    consume_completed_reconciliation_retry,
                )

                try:
                    metadata = consume_completed_reconciliation_retry(
                        metadata,
                        next_claim_version=previous_claim["claim_version"] + 1,
                    )
                except RuntimeReconciliationConflict as exc:
                    await _quarantine_reconciliation_retry_claim(
                        self.db,
                        task,
                        metadata=metadata,
                        now=now,
                        reason=f"{task.task_type or 'RuntimeTask'} retry authority is invalid: {exc}",
                    )
                    continue
            task.status = "running"
            task.claimed_by = self.worker_id
            task.claim_expires_at = claim_expires_at
            task.attempt_count = int(getattr(task, "attempt_count", 0) or 0) + 1
            task.claim_version = int(getattr(task, "claim_version", 0) or 0) + 1
            if getattr(task, "started_at", None) is None:
                task.started_at = now
            if reclaimed_expired_claim:
                metadata["reclaimed_expired_claim"] = True
                metadata["reclaimed_at"] = now.isoformat()
                metadata["recovery_state"] = "recovering"
                metadata["previous_claim"] = previous_claim
                if previous_claim["claim_version"] == 0:
                    metadata["legacy_claim_backfilled"] = True
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
