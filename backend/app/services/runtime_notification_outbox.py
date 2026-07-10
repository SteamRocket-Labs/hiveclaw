"""Durable completion delivery for child and background runtime work."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid
from typing import Any, Literal

from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

DeliveryMode = Literal["parent_continuation", "session_projection"]
_OUTBOX_ID_NAMESPACE = uuid.UUID("0df71dc3-a9b3-4bb9-9886-702a16fbe953")


class CompletionDeliveryDeferred(RuntimeError):
    """A governed continuation is waiting for an approval, not failing."""


def _uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


@dataclass(frozen=True, slots=True)
class CompletionNotification:
    tenant_id: uuid.UUID | str
    source_kind: str
    source_run_id: str
    parent_session_id: uuid.UUID | str
    parent_agent_id: uuid.UUID | str
    parent_user_id: uuid.UUID | str
    terminal_status: str
    task_type: str
    summary: str
    child_session_id: uuid.UUID | str | None = None
    child_agent_name: str | None = None
    delivery_mode: DeliveryMode = "parent_continuation"
    artifacts: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    payload_rank: int = 100


@dataclass(frozen=True, slots=True)
class ClaimedCompletionNotification:
    id: uuid.UUID
    tenant_id: uuid.UUID
    source_kind: str
    source_run_id: str
    parent_session_id: uuid.UUID
    parent_agent_id: uuid.UUID
    parent_user_id: uuid.UUID
    terminal_status: str
    task_type: str
    summary: str
    child_session_id: uuid.UUID | None
    child_agent_name: str | None
    delivery_mode: DeliveryMode
    artifacts: list[dict[str, Any]]
    metadata: dict[str, Any]
    attempt_count: int


def completion_notification_id(notification: CompletionNotification) -> uuid.UUID:
    identity = "|".join(
        (
            str(_uuid(notification.tenant_id, field="tenant_id")),
            str(notification.source_kind).strip().lower(),
            str(notification.source_run_id).strip(),
            str(_uuid(notification.parent_session_id, field="parent_session_id")),
            str(notification.terminal_status).strip().lower(),
        )
    )
    return uuid.uuid5(_OUTBOX_ID_NAMESPACE, identity)


def _normalized_values(notification: CompletionNotification) -> dict[str, Any]:
    source_kind = str(notification.source_kind or "").strip().lower()
    source_run_id = str(notification.source_run_id or "").strip()
    terminal_status = str(notification.terminal_status or "").strip().lower()
    task_type = str(notification.task_type or "").strip()
    summary = str(notification.summary or "").strip()
    if not all((source_kind, source_run_id, terminal_status, task_type, summary)):
        raise ValueError("completion notification requires source, status, task type, and summary")
    if notification.delivery_mode not in {"parent_continuation", "session_projection"}:
        raise ValueError(f"unsupported delivery_mode: {notification.delivery_mode}")
    return {
        "id": completion_notification_id(notification),
        "tenant_id": _uuid(notification.tenant_id, field="tenant_id"),
        "source_kind": source_kind,
        "source_run_id": source_run_id,
        "parent_session_id": _uuid(notification.parent_session_id, field="parent_session_id"),
        "parent_agent_id": _uuid(notification.parent_agent_id, field="parent_agent_id"),
        "parent_user_id": _uuid(notification.parent_user_id, field="parent_user_id"),
        "child_session_id": (
            _uuid(notification.child_session_id, field="child_session_id")
            if notification.child_session_id is not None
            else None
        ),
        "child_agent_name": str(notification.child_agent_name or "").strip() or None,
        "terminal_status": terminal_status,
        "task_type": task_type,
        "summary": summary,
        "delivery_mode": notification.delivery_mode,
        "artifacts_json": list(notification.artifacts or []),
        "metadata_json": dict(notification.metadata or {}),
        "payload_rank": max(0, min(1000, int(notification.payload_rank))),
        "status": "pending",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }


async def enqueue_completion_notification(
    db: AsyncSession,
    notification: CompletionNotification,
) -> uuid.UUID:
    """Insert one terminal delivery intent in the caller's transaction."""

    values = _normalized_values(notification)
    stmt = insert(RuntimeNotificationOutbox).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_runtime_notification_outbox_delivery",
        set_={
            "parent_agent_id": stmt.excluded.parent_agent_id,
            "parent_user_id": stmt.excluded.parent_user_id,
            "child_session_id": stmt.excluded.child_session_id,
            "child_agent_name": stmt.excluded.child_agent_name,
            "task_type": stmt.excluded.task_type,
            "summary": stmt.excluded.summary,
            "delivery_mode": stmt.excluded.delivery_mode,
            "artifacts_json": stmt.excluded.artifacts_json,
            "metadata_json": stmt.excluded.metadata_json,
            "payload_rank": stmt.excluded.payload_rank,
            "updated_at": datetime.now(UTC),
        },
        where=stmt.excluded.payload_rank > RuntimeNotificationOutbox.payload_rank,
    )
    await db.execute(stmt)
    return values["id"]


def _claimed(row: RuntimeNotificationOutbox) -> ClaimedCompletionNotification:
    return ClaimedCompletionNotification(
        id=row.id,
        tenant_id=row.tenant_id,
        source_kind=row.source_kind,
        source_run_id=row.source_run_id,
        parent_session_id=row.parent_session_id,
        parent_agent_id=row.parent_agent_id,
        parent_user_id=row.parent_user_id,
        terminal_status=row.terminal_status,
        task_type=row.task_type,
        summary=row.summary,
        child_session_id=row.child_session_id,
        child_agent_name=row.child_agent_name,
        delivery_mode=row.delivery_mode,  # type: ignore[arg-type]
        artifacts=list(row.artifacts_json or []),
        metadata=dict(row.metadata_json or {}),
        attempt_count=int(row.attempt_count or 0),
    )


class RuntimeNotificationOutboxService:
    """Claim, deliver, retry, and acknowledge completion notifications."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        deferred_retry_seconds: int = 15,
        max_attempts: int = 8,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._deferred_retry_seconds = max(0, int(deferred_retry_seconds))
        self._max_attempts = max(1, int(max_attempts))

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"runtime_notification_outbox.{operation}") as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedCompletionNotification]:
        current = now or datetime.now(UTC)
        expired_lock = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(
                            or_(
                                and_(
                                    RuntimeNotificationOutbox.status == "pending",
                                    RuntimeNotificationOutbox.available_at <= current,
                                ),
                                and_(
                                    RuntimeNotificationOutbox.status == "processing",
                                    RuntimeNotificationOutbox.locked_at <= expired_lock,
                                ),
                            )
                        )
                        .order_by(RuntimeNotificationOutbox.available_at, RuntimeNotificationOutbox.created_at)
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

    async def reconcile_terminal_tasks_once(self, *, limit: int = 100) -> int:
        """Backfill a missing intent after a crash between terminal write and enqueue.

        Normal producers enqueue in their terminal/projection transaction. This
        sweep is the recovery atom for legacy rows and the two paths whose
        terminal RuntimeTask precedes their richer parent projection.
        """

        terminal_statuses = ("completed", "failed", "killed", "skipped", "needs_reconciliation")
        supported_types = ("subagent", "team_member", "workflow", "delegation", "a2a_delegation", "trigger")
        async with self._worker_session("reconcile_terminal_tasks") as db:
            already_enqueued = exists(
                select(RuntimeNotificationOutbox.id).where(
                    RuntimeNotificationOutbox.tenant_id == RuntimeTask.tenant_id,
                    RuntimeNotificationOutbox.source_run_id == cast(RuntimeTask.id, String),
                )
            )
            tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.task_type.in_(supported_types),
                            RuntimeTask.status.in_(terminal_statuses),
                            RuntimeTask.tenant_id.is_not(None),
                            RuntimeTask.parent_agent_id.is_not(None),
                            ~already_enqueued,
                        )
                        .order_by(RuntimeTask.completed_at.desc().nullslast(), RuntimeTask.created_at.desc())
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            repaired = 0
            for task in tasks:
                metadata = dict(task.metadata_json or {})
                source_kind = {
                    "team_member": "agent_team",
                    "delegation": "a2a_delegation",
                    "a2a_delegation": "a2a_delegation",
                }.get(task.task_type, task.task_type)
                target_value = (
                    task.child_session_id
                    if task.task_type == "trigger"
                    else metadata.get("parent_session_id") or task.parent_session_id
                )
                try:
                    target_session_id = _uuid(target_value, field="parent_session_id")
                except ValueError:
                    continue
                parent_session = (
                    await db.execute(
                        select(ChatSession).where(
                            ChatSession.id == target_session_id,
                            ChatSession.agent_id == task.parent_agent_id,
                            ChatSession.tenant_id == task.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if parent_session is None:
                    continue
                owner_value = metadata.get("user_id") or metadata.get("owner_id") or parent_session.user_id
                try:
                    owner_id = _uuid(owner_value, field="parent_user_id")
                except ValueError:
                    continue
                child_session_id = None
                if task.task_type != "trigger" and task.child_session_id:
                    try:
                        child_session_id = _uuid(task.child_session_id, field="child_session_id")
                    except ValueError:
                        child_session_id = None
                await enqueue_completion_notification(
                    db,
                    CompletionNotification(
                        tenant_id=task.tenant_id,
                        source_kind=source_kind,
                        source_run_id=str(task.id),
                        parent_session_id=target_session_id,
                        parent_agent_id=task.parent_agent_id,
                        parent_user_id=owner_id,
                        child_session_id=child_session_id,
                        child_agent_name=task.child_agent_name,
                        terminal_status=task.status,
                        task_type=task.task_type,
                        summary=str(task.result_summary or f"{task.task_type} finished with status {task.status}."),
                        delivery_mode=("session_projection" if task.task_type == "trigger" else "parent_continuation"),
                        artifacts=list(metadata.get("artifacts") or []),
                        metadata={
                            **metadata,
                            "reconciled_from_terminal_runtime_task": True,
                        },
                        payload_rank=10,
                    ),
                )
                repaired += 1
            await db.commit()
            return repaired

    async def _mark_delivered(
        self,
        *,
        item_id: uuid.UUID,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> bool:
        now = datetime.now(UTC)
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "delivered"
            row.delivered_at = now
            row.delivery_receipt_json = receipt
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            await db.commit()
            return True

    async def _mark_failed(self, *, item: ClaimedCompletionNotification, worker_id: str, error: Exception) -> str:
        now = datetime.now(UTC)
        async with self._worker_session("retry") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return "stale"
            row.last_error = f"{type(error).__name__}: {str(error)[:1000]}"
            row.locked_by = None
            row.locked_at = None
            if int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
                if row.source_kind == "agent_team" and row.task_type == "agent_team_close":
                    from app.services.agent_team_runtime_service import (
                        reopen_agent_team_close_after_delivery_failure,
                    )

                    raw_team_id = (row.metadata_json or {}).get("agent_team_close_id")
                    try:
                        team_id = _uuid(raw_team_id, field="agent_team_close_id")
                    except ValueError:
                        team_id = None
                    if team_id is not None:
                        await reopen_agent_team_close_after_delivery_failure(
                            db=db,
                            team_id=team_id,
                            notification_id=row.id,
                            error=row.last_error,
                        )
            else:
                delay = min(300, self._retry_base_seconds * (2 ** max(0, int(row.attempt_count or 1) - 1)))
                row.status = "pending"
                row.available_at = now + timedelta(seconds=delay)
                outcome = "retry"
            await db.commit()
            return outcome

    async def _mark_deferred(
        self,
        *,
        item: ClaimedCompletionNotification,
        worker_id: str,
        reason: str,
    ) -> bool:
        async with self._worker_session("defer") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "pending"
            row.available_at = datetime.now(UTC) + timedelta(seconds=self._deferred_retry_seconds)
            row.last_error = reason
            row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
            row.locked_by = None
            row.locked_at = None
            await db.commit()
            return True

    async def _deliver(self, item: ClaimedCompletionNotification) -> dict[str, Any]:
        from app.services.agent_session_continuation import continue_parent_session_with_task_notification

        if item.task_type == "agent_team_close" and item.delivery_mode == "parent_continuation":
            from app.services.web_chat_runtime import get_active_web_chat_run

            async with tenant_scoped_session(
                item.tenant_id,
                session_factory=self._session_factory,
                require_tenant=True,
                source="runtime_notification_outbox_team_close_preflight",
            ) as preflight_db:
                active_run = await get_active_web_chat_run(
                    db=preflight_db,
                    agent_id=item.parent_agent_id,
                    session_id=item.parent_session_id,
                )
            if active_run is not None:
                raise CompletionDeliveryDeferred("parent_session_active")

        admission: ExecutionAdmission | None = None
        admission_decision: ExecutionAdmissionDecision | None = None
        raw_budget_run_id = item.metadata.get("budget_run_id")
        if item.delivery_mode == "parent_continuation" and raw_budget_run_id:
            try:
                budget_run_id = _uuid(raw_budget_run_id, field="budget_run_id")
            except ValueError:
                budget_run_id = None
            if budget_run_id is not None:
                admission = ExecutionAdmission(RuntimeBudgetService(session_factory=self._session_factory))
                admission_decision = await admission.admit(
                    RuntimeBudgetReservation(
                        budget_run_id=budget_run_id,
                        reservation_key=f"completion_outbox:{item.id}:continuation",
                        continuation_wakes=1,
                        reason="completion_outbox_parent_continuation",
                        metadata={
                            "completion_outbox_id": str(item.id),
                            "source_kind": item.source_kind,
                            "source_run_id": item.source_run_id,
                        },
                    )
                )
                if admission_decision.waiting:
                    raise CompletionDeliveryDeferred("runtime_budget_approval_required")

        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="runtime_notification_outbox_delivery",
        ) as db:
            existing = (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == item.parent_session_id,
                        ChatTranscriptEvent.causation_id == item.id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if admission is not None and admission_decision is not None:
                    await admission.settle(
                        admission_decision,
                        actual_continuation_wakes=1,
                        reason="completion_outbox_already_delivered",
                    )
                return {
                    "status": "already_delivered",
                    "event_id": str(existing.id),
                    "deduplicated": True,
                }

            parent_session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == item.parent_session_id,
                        ChatSession.agent_id == item.parent_agent_id,
                        ChatSession.user_id == item.parent_user_id,
                        ChatSession.tenant_id == item.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            parent_agent = (
                await db.execute(
                    select(Agent).where(Agent.id == item.parent_agent_id, Agent.tenant_id == item.tenant_id)
                )
            ).scalar_one_or_none()
            owner = (
                await db.execute(select(User).where(User.id == item.parent_user_id, User.tenant_id == item.tenant_id))
            ).scalar_one_or_none()
            if parent_session is None or parent_agent is None or owner is None:
                raise RuntimeError("completion target authority no longer resolves")

            receipt = await continue_parent_session_with_task_notification(
                db=db,
                agent=parent_agent,
                user=owner,
                session=parent_session,
                task_id=item.source_run_id,
                task_type=item.task_type,
                status=item.terminal_status,
                summary=item.summary,
                child_session_id=item.child_session_id,
                child_agent_name=item.child_agent_name,
                source=item.source_kind,
                metadata={
                    **item.metadata,
                    "completion_outbox_id": str(item.id),
                    "causation_id": str(item.id),
                },
                artifacts=item.artifacts,
                resume_parent=item.delivery_mode == "parent_continuation",
            )
            delivered_event = (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == item.parent_session_id,
                        ChatTranscriptEvent.causation_id == item.id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            ).scalar_one_or_none()
            if admission is not None and admission_decision is not None:
                await admission.settle(
                    admission_decision,
                    actual_continuation_wakes=1,
                    reason="completion_outbox_delivered",
                )
            return {
                **dict(receipt or {}),
                "event_id": str(delivered_event.id) if delivered_event is not None else None,
                "deduplicated": False,
            }

    async def drain_once(
        self,
        *,
        worker_id: str,
        deliver: Callable[[ClaimedCompletionNotification], Awaitable[dict[str, Any]]] | None = None,
        limit: int = 20,
    ) -> dict[str, int]:
        claimed = await self.claim_batch(worker_id=worker_id, limit=limit)
        counts = {"claimed": len(claimed), "delivered": 0, "retried": 0, "deferred": 0, "dead_lettered": 0}
        delivery = deliver or self._deliver
        for item in claimed:
            try:
                receipt = await delivery(item)
                acknowledged = await self._mark_delivered(
                    item_id=item.id,
                    worker_id=worker_id,
                    receipt=dict(receipt or {}),
                )
                if acknowledged:
                    counts["delivered"] += 1
            except CompletionDeliveryDeferred as exc:
                deferred = await self._mark_deferred(
                    item=item,
                    worker_id=worker_id,
                    reason=str(exc),
                )
                if deferred:
                    counts["deferred"] += 1
            except Exception as exc:
                outcome = await self._mark_failed(item=item, worker_id=worker_id, error=exc)
                if outcome == "retry":
                    counts["retried"] += 1
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += 1
        return counts
