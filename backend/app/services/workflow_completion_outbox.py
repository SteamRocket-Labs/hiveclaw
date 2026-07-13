"""Retryable, idempotent Workflow completion-signal delivery."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coordination_wiring import gateway_scope
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.models.workflow_completion_outbox import WorkflowCompletionOutbox

_OUTBOX_NAMESPACE = uuid.UUID("d885ec2d-7ceb-4f27-a1d7-6fd429293c82")


@dataclass(frozen=True, slots=True)
class WorkflowCompletionIntent:
    tenant_id: uuid.UUID | str
    run_id: uuid.UUID | str
    agent_id: uuid.UUID | str
    terminal_status: str


@dataclass(frozen=True, slots=True)
class ClaimedWorkflowCompletion:
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID
    terminal_status: str
    attempt_count: int
    claim_worker_id: str


class WorkflowCompletionDeliveryAuthorityInvalid(RuntimeError):
    """The source or target authority stopped resolving before delivery."""


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def workflow_completion_id(intent: WorkflowCompletionIntent) -> uuid.UUID:
    identity = "|".join(
        (
            str(_uuid(intent.tenant_id)),
            str(_uuid(intent.run_id)),
            str(intent.terminal_status).strip().lower(),
        )
    )
    return uuid.uuid5(_OUTBOX_NAMESPACE, identity)


def _intent_values(intent: WorkflowCompletionIntent) -> dict:
    terminal_status = str(intent.terminal_status or "").strip().lower()
    if terminal_status != "completed":
        raise ValueError("workflow completion signal is emitted only for completed runs")
    return {
        "id": workflow_completion_id(intent),
        "tenant_id": _uuid(intent.tenant_id),
        "run_id": _uuid(intent.run_id),
        "agent_id": _uuid(intent.agent_id),
        "terminal_status": terminal_status,
        "status": "pending",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }


async def enqueue_workflow_completion(db: AsyncSession, intent: WorkflowCompletionIntent) -> uuid.UUID:
    values = _intent_values(intent)
    await db.execute(
        insert(WorkflowCompletionOutbox)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_workflow_completion_outbox_run_status")
    )
    return values["id"]


def _claimed(row: WorkflowCompletionOutbox) -> ClaimedWorkflowCompletion:
    if not row.locked_by:
        raise RuntimeError("claimed Workflow completion is missing its worker identity")
    return ClaimedWorkflowCompletion(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        agent_id=row.agent_id,
        terminal_status=row.terminal_status,
        attempt_count=int(row.attempt_count or 0),
        claim_worker_id=str(row.locked_by),
    )


def serialize_workflow_completion_outbox(
    row: WorkflowCompletionOutbox,
    *,
    authority_valid: bool | None = None,
) -> dict:
    authority_snapshot = {
        "valid": authority_valid,
        "tenant_id": str(row.tenant_id),
        "agent_id": str(row.agent_id),
    }
    return {
        "id": str(row.id),
        "delivery_id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "run_id": str(row.run_id),
        "source_kind": "workflow_completion",
        "source_run_id": str(row.run_id),
        "agent_id": str(row.agent_id),
        "terminal_status": row.terminal_status,
        "execution_terminal_status": row.terminal_status,
        "status": row.status,
        "retryable": row.status == "dead_letter" and authority_valid is not False,
        "attempt_count": int(row.attempt_count or 0),
        "last_error": row.last_error,
        "summary": f"Workflow run {row.run_id} coordination completion signal",
        "available_at": row.available_at.isoformat() if row.available_at else None,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "delivery_only": True,
        "does_not_rerun_execution": True,
        "authority_snapshot": authority_snapshot,
    }


async def _workflow_completion_target_authority_valid(
    db: AsyncSession,
    row: WorkflowCompletionOutbox,
) -> bool:
    target_agent_id = (
        await db.execute(
            select(Agent.id).where(
                Agent.id == row.agent_id,
                Agent.tenant_id == row.tenant_id,
                Agent.deleted_at.is_(None),
                Agent.deactivated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return target_agent_id is not None


class WorkflowCompletionOutboxService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"workflow_completion_outbox.{operation}") as bypass_db:
                yield bypass_db

    async def reconcile_terminal_runs_once(
        self,
        *,
        limit: int = 100,
        tenant_id: uuid.UUID | str | None = None,
    ) -> int:
        async with self._worker_session("reconcile") as db:
            tenant_filter = (RuntimeTask.tenant_id == _uuid(tenant_id),) if tenant_id is not None else ()
            already_enqueued = exists(
                select(WorkflowCompletionOutbox.id).where(
                    WorkflowCompletionOutbox.tenant_id == RuntimeTask.tenant_id,
                    WorkflowCompletionOutbox.run_id == RuntimeTask.id,
                    WorkflowCompletionOutbox.terminal_status == "completed",
                )
            )
            tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask)
                        .join(
                            Agent,
                            and_(
                                Agent.id == RuntimeTask.parent_agent_id,
                                Agent.tenant_id == RuntimeTask.tenant_id,
                            ),
                        )
                        .where(
                            RuntimeTask.task_type == "workflow",
                            RuntimeTask.status == "completed",
                            RuntimeTask.tenant_id.is_not(None),
                            RuntimeTask.parent_agent_id.is_not(None),
                            *tenant_filter,
                            ~already_enqueued,
                        )
                        .order_by(
                            RuntimeTask.completed_at.asc().nullsfirst(),
                            RuntimeTask.created_at.asc(),
                            RuntimeTask.id.asc(),
                        )
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for task in tasks:
                await enqueue_workflow_completion(
                    db,
                    WorkflowCompletionIntent(
                        tenant_id=task.tenant_id,
                        run_id=task.id,
                        agent_id=task.parent_agent_id,
                        terminal_status="completed",
                    ),
                )
            await db.commit()
            return len(tasks)

    async def list_dead_letters(self, *, tenant_id: uuid.UUID | str, limit: int = 100) -> list[dict]:
        tenant_uuid = _uuid(tenant_id)
        async with tenant_scoped_session(
            tenant_uuid,
            session_factory=self._session_factory,
            require_tenant=True,
            source="workflow_completion_outbox_dead_letter_list",
        ) as db:
            rows = list(
                (
                    await db.execute(
                        select(WorkflowCompletionOutbox)
                        .where(
                            WorkflowCompletionOutbox.tenant_id == tenant_uuid,
                            WorkflowCompletionOutbox.status == "dead_letter",
                        )
                        .order_by(WorkflowCompletionOutbox.updated_at.desc())
                        .limit(max(1, int(limit)))
                    )
                )
                .scalars()
                .all()
            )
            valid_agent_ids = set(
                (
                    await db.execute(
                        select(Agent.id).where(
                            Agent.tenant_id == tenant_uuid,
                            Agent.id.in_({row.agent_id for row in rows}),
                            Agent.deleted_at.is_(None),
                            Agent.deactivated_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [
            serialize_workflow_completion_outbox(
                row,
                authority_valid=row.agent_id in valid_agent_ids,
            )
            for row in rows
        ]

    async def retry_dead_letter(
        self,
        *,
        tenant_id: uuid.UUID | str,
        outbox_id: uuid.UUID | str,
        actor_user_id: uuid.UUID | str,
        reason: str,
    ) -> dict:
        """Retry only the stable signal delivery; Workflow execution is untouched."""

        tenant_uuid = _uuid(tenant_id)
        outbox_uuid = _uuid(outbox_id)
        actor_uuid = _uuid(actor_user_id)
        normalized_reason = str(reason or "").strip()
        if len(normalized_reason) < 8:
            raise ValueError("dead-letter retry reason must contain at least 8 characters")
        async with self._worker_session("dead_letter_retry_actor_authority") as authority_db:
            actor_exists = (
                await authority_db.execute(
                    select(User.id).where(
                        User.id == actor_uuid,
                        User.role == "platform_admin",
                        User.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if actor_exists is None:
            raise PermissionError("Workflow completion delivery retry requires an active platform_admin")
        async with tenant_scoped_session(
            tenant_uuid,
            session_factory=self._session_factory,
            require_tenant=True,
            source="workflow_completion_outbox_dead_letter_retry",
        ) as db:
            row = (
                await db.execute(
                    select(WorkflowCompletionOutbox)
                    .where(
                        WorkflowCompletionOutbox.id == outbox_uuid,
                        WorkflowCompletionOutbox.tenant_id == tenant_uuid,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("Workflow completion delivery not found")
            if row.status != "dead_letter":
                raise ValueError("only dead-letter Workflow completion deliveries can be retried")
            task_status = (
                await db.execute(
                    select(RuntimeTask.status).where(
                        RuntimeTask.id == row.run_id,
                        RuntimeTask.tenant_id == tenant_uuid,
                        RuntimeTask.task_type == "workflow",
                    )
                )
            ).scalar_one_or_none()
            if task_status != "completed":
                raise ValueError("delivery retry requires immutable completed Workflow truth")
            if not await _workflow_completion_target_authority_valid(db, row):
                raise PermissionError("Workflow completion delivery target authority does not currently resolve")
            previous_error = row.last_error
            row.status = "pending"
            row.attempt_count = 0
            row.available_at = datetime.now(UTC)
            row.locked_by = None
            row.locked_at = None
            row.last_error = None
            row.delivered_at = None
            db.add(
                AuditLog(
                    tenant_id=tenant_uuid,
                    user_id=actor_uuid,
                    agent_id=row.agent_id,
                    action="workflow_completion_delivery_retry",
                    details={
                        "outbox_id": str(row.id),
                        "run_id": str(row.run_id),
                        "reason": normalized_reason,
                        "previous_error": previous_error,
                        "delivery_only": True,
                        "does_not_rerun_execution": True,
                    },
                )
            )
            await db.flush()
            payload = serialize_workflow_completion_outbox(row, authority_valid=True)
        return payload

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedWorkflowCompletion]:
        current = now or datetime.now(UTC)
        expired = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(WorkflowCompletionOutbox)
                        .where(
                            or_(
                                and_(
                                    WorkflowCompletionOutbox.status == "pending",
                                    WorkflowCompletionOutbox.available_at <= current,
                                ),
                                and_(
                                    WorkflowCompletionOutbox.status == "processing",
                                    WorkflowCompletionOutbox.locked_at <= expired,
                                ),
                            )
                        )
                        .order_by(
                            WorkflowCompletionOutbox.available_at,
                            WorkflowCompletionOutbox.created_at,
                        )
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "processing"
                row.locked_by = str(worker_id)
                row.locked_at = current
                row.attempt_count = int(row.attempt_count or 0) + 1
            await db.commit()
            return [_claimed(row) for row in rows]

    async def _deliver(self, item: ClaimedWorkflowCompletion) -> dict:
        invalid_reason: str | None = None
        signal = None
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="workflow_completion_outbox_delivery_authority",
        ) as db:
            row = (
                await db.execute(
                    select(WorkflowCompletionOutbox)
                    .where(
                        WorkflowCompletionOutbox.id == item.id,
                        WorkflowCompletionOutbox.tenant_id == item.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.status != "processing"
                or row.locked_by != item.claim_worker_id
                or row.run_id != item.run_id
                or row.agent_id != item.agent_id
            ):
                raise RuntimeError("Workflow completion delivery claim is no longer owned")

            source_task = (
                await db.execute(
                    select(RuntimeTask)
                    .where(
                        RuntimeTask.id == row.run_id,
                        RuntimeTask.tenant_id == row.tenant_id,
                        RuntimeTask.task_type == "workflow",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            target_agent = (
                await db.execute(
                    select(Agent)
                    .where(
                        Agent.id == row.agent_id,
                        Agent.tenant_id == row.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if source_task is None or source_task.status != "completed":
                invalid_reason = "Workflow completion source authority is no longer completed"
            elif target_agent is None or target_agent.deleted_at is not None or target_agent.deactivated_at is not None:
                invalid_reason = "Workflow completion target authority is no longer active"

            if invalid_reason is not None:
                row.status = "dead_letter"
                row.last_error = f"WorkflowCompletionDeliveryAuthorityInvalid: {invalid_reason}"
                row.locked_by = None
                row.locked_at = None
                row.delivered_at = None
            else:
                # Keep the source and target rows locked through the external
                # send so deactivation/deletion cannot race this authority
                # decision. The stable signal id preserves crash replay.
                async with gateway_scope(tenant_id=item.tenant_id) as gateway:
                    signal = await gateway.send_signal(
                        signal_id=item.id,
                        from_agent_id=f"workflow:{item.run_id}",
                        to_agent_id=str(item.agent_id),
                        content=f"workflow run {item.run_id} finished: {item.terminal_status}",
                        signal_type="workflow_completed",
                        thread_id=str(item.run_id),
                        metadata={"workflow_completion_outbox_id": str(item.id)},
                    )

        if invalid_reason is not None:
            raise WorkflowCompletionDeliveryAuthorityInvalid(invalid_reason)
        if signal is None:  # pragma: no cover - guarded by the authority branches above.
            raise RuntimeError("Workflow completion delivery produced no signal")
        return {"signal_id": str(signal.id)}

    async def _mark_delivered(self, *, item_id: uuid.UUID, worker_id: str) -> bool:
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "delivered"
            row.delivered_at = datetime.now(UTC)
            row.locked_by = None
            row.locked_at = None
            row.last_error = None
            await db.commit()
            return True

    async def _mark_failed(self, *, item: ClaimedWorkflowCompletion, worker_id: str, error: Exception) -> str:
        async with self._worker_session("fail") as db:
            row = (
                await db.execute(
                    select(WorkflowCompletionOutbox).where(WorkflowCompletionOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return "lost_claim"
            row.last_error = f"{type(error).__name__}: {str(error)[:1000]}"
            row.locked_by = None
            row.locked_at = None
            if int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
            else:
                delay = min(300, self._retry_base_seconds * (2 ** max(0, int(row.attempt_count or 1) - 1)))
                row.status = "pending"
                row.available_at = datetime.now(UTC) + timedelta(seconds=delay)
                outcome = "retry"
            await db.commit()
            return outcome

    async def drain_once(self, *, worker_id: str, limit: int = 20) -> dict[str, int]:
        claimed = await self.claim_batch(worker_id=worker_id, limit=limit)
        result = {"claimed": len(claimed), "delivered": 0, "retried": 0, "dead_lettered": 0}
        for item in claimed:
            try:
                await self._deliver(item)
                if await self._mark_delivered(item_id=item.id, worker_id=worker_id):
                    result["delivered"] += 1
            except WorkflowCompletionDeliveryAuthorityInvalid:
                result["dead_lettered"] += 1
            except Exception as exc:  # noqa: BLE001 - durable retry boundary.
                failed = await self._mark_failed(item=item, worker_id=worker_id, error=exc)
                if failed == "retry":
                    result["retried"] += 1
                elif failed == "dead_letter":
                    result["dead_lettered"] += 1
        return result
