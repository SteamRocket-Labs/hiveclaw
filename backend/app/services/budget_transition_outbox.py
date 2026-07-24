"""Durable projection of runtime-budget facts to their addressed consumers."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.budget_transition_outbox import BudgetTransitionOutbox
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.external_principal import ExternalPrincipal
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
from app.services.channel_delivery_service import ChannelDeliveryService, DeliveryResult
from app.services.chat_transcript import append_session_event

_OUTBOX_NAMESPACE = uuid.UUID("09a56d4e-eb1f-49de-8ad4-90eecce464ca")
_TRANSITION_EVENTS = ("denial", "overrun_approved", "overrun_rejected", "expired", "cancelled", "circuit_break")


class BudgetTransitionPermanentError(RuntimeError):
    """The snapshotted consumer no longer has delivery authority."""


class BudgetTransitionRetryableError(RuntimeError):
    """No external side effect happened, or the provider explicitly allows retry."""


class BudgetTransitionAmbiguousError(RuntimeError):
    """A channel provider may have accepted the message without a receipt."""


def budget_transition_copy(transition: str) -> str:
    return {
        "waiting_budget_approval": "运行额度已达上限，任务已安全暂停并等待管理员批准。",
        "summary_only": "运行额度已达上限，系统只会整理已完成结果，不再启动新工作。",
        "exhausted": "运行额度已达上限，未开始的后续工作已停止；已完成结果仍可查看。",
        "hard_stopped": "运行保护机制已停止后续工作；已完成结果仍可查看。",
        "approved": "运行额度已获批准，等待中的工作将自动继续。",
        "rejected": "运行额度申请未获批准，等待中的工作已停止；已完成结果仍可查看。",
        "expired": "本次运行已超时，未开始的后续工作已停止。",
        "cancelled": "管理员已暂停本次运行；已完成结果仍可查看。",
    }.get(str(transition), "运行状态已更新。")


def transition_for_event(event: RuntimeBudgetEvent) -> str | None:
    if event.event_type == "overrun_approved":
        return "approved"
    if event.event_type == "overrun_rejected":
        return "rejected"
    if event.event_type in {"expired", "cancelled"}:
        return event.event_type
    if event.event_type in {"denial", "circuit_break"}:
        target = str((event.metadata_json or {}).get("target_status") or "").strip()
        if target in {"waiting_budget_approval", "summary_only", "exhausted", "hard_stopped"}:
            return target
    return None


def _uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class ClaimedBudgetTransition:
    id: uuid.UUID
    tenant_id: uuid.UUID
    budget_run_id: uuid.UUID
    budget_event_id: uuid.UUID
    transition: str
    session_id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID | None
    external_principal_id: uuid.UUID | None
    runtime_task_id: uuid.UUID | None
    channel_config_id: uuid.UUID | None
    channel: str
    content: str
    delivery_target: dict[str, Any]
    metadata: dict[str, Any]
    delivery_receipts: dict[str, Any]
    attempt_count: int


def _claimed(row: BudgetTransitionOutbox) -> ClaimedBudgetTransition:
    return ClaimedBudgetTransition(
        id=row.id,
        tenant_id=row.tenant_id,
        budget_run_id=row.budget_run_id,
        budget_event_id=row.budget_event_id,
        transition=row.transition,
        session_id=row.session_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        external_principal_id=row.external_principal_id,
        runtime_task_id=row.runtime_task_id,
        channel_config_id=row.channel_config_id,
        channel=row.channel,
        content=row.content,
        delivery_target=dict(row.delivery_target_json or {}),
        metadata=dict(row.metadata_json or {}),
        delivery_receipts=dict(row.delivery_receipts_json or {}),
        attempt_count=int(row.attempt_count or 0),
    )


async def enqueue_budget_transition(
    db: AsyncSession,
    *,
    run: RuntimeBudgetRun,
    event: RuntimeBudgetEvent,
    transition: str,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Persist one immutable delivery intent in the budget decision transaction."""

    tenant_id = _uuid(run.tenant_id)
    session_id = _uuid(run.root_session_id)
    agent_id = _uuid(run.root_agent_id)
    if tenant_id is None or session_id is None or agent_id is None:
        return None
    await db.flush()
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.tenant_id == tenant_id,
                ChatSession.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if session is None or (session.user_id is None and session.external_principal_id is None):
        return None
    raw_target = dict(session.delivery_target_json or {})
    raw_target.setdefault("channel", str(session.source_channel or "web"))
    target = ChannelDeliveryService.normalize_reply_target(raw_target) or {"channel": "web"}
    channel = str(target.get("channel") or session.source_channel or "web").strip().lower()
    if channel in {"", "unknown"}:
        channel = "web"
        target = {"channel": "web"}
    config_id: uuid.UUID | None = None
    if channel != "web":
        config_id = _uuid(target.get("channel_config_id"))
        if config_id is None:
            config_id = (
                await db.execute(
                    select(ChannelConfig.id).where(
                        ChannelConfig.tenant_id == tenant_id,
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == channel,
                    )
                )
            ).scalar_one_or_none()
    outbox_id = uuid.uuid5(_OUTBOX_NAMESPACE, str(event.id))
    values = {
        "id": outbox_id,
        "tenant_id": tenant_id,
        "budget_run_id": run.id,
        "budget_event_id": event.id,
        "transition": str(transition),
        "session_id": session_id,
        "agent_id": agent_id,
        "user_id": session.user_id or run.root_user_id,
        "external_principal_id": session.external_principal_id or run.root_external_principal_id,
        "runtime_task_id": run.root_runtime_task_id,
        "channel_config_id": config_id,
        "channel": channel,
        "content": str(content or budget_transition_copy(transition)),
        "delivery_target_json": target,
        "metadata_json": {
            **dict(metadata or {}),
            "budget_run_id": str(run.id),
            "budget_event_id": str(event.id),
            "budget_event_type": event.event_type,
            "target_status": transition,
        },
        "delivery_receipts_json": {},
        "status": "pending",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }
    await db.execute(
        insert(BudgetTransitionOutbox)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_budget_transition_outbox_event")
    )
    return outbox_id


async def _default_text_sender(**kwargs: Any) -> DeliveryResult:
    idempotency_key = str(kwargs.pop("idempotency_key"))
    detail = dict(kwargs.pop("extra_detail", {}) or {})
    detail["idempotency_key"] = idempotency_key
    return await ChannelDeliveryService.send_text(extra_detail=detail, **kwargs)


class BudgetTransitionOutboxService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
        text_sender: Callable[..., Awaitable[DeliveryResult]] | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._text_sender = text_sender or _default_text_sender

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"budget_transition_outbox.{operation}") as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedBudgetTransition]:
        current = now or datetime.now(UTC)
        expired = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(BudgetTransitionOutbox)
                        .where(
                            or_(
                                and_(
                                    BudgetTransitionOutbox.status == "pending",
                                    BudgetTransitionOutbox.available_at <= current,
                                ),
                                and_(
                                    BudgetTransitionOutbox.status == "processing",
                                    BudgetTransitionOutbox.locked_at <= expired,
                                ),
                            )
                        )
                        .order_by(BudgetTransitionOutbox.available_at, BudgetTransitionOutbox.created_at)
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

    async def project_transcript(self, *, item: ClaimedBudgetTransition, worker_id: str) -> None:
        async with self._worker_session("transcript") as db:
            row = (
                await db.execute(
                    select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                raise BudgetTransitionRetryableError("budget transition delivery lease was lost")
            receipts = dict(row.delivery_receipts_json or {})
            if (receipts.get("transcript") or {}).get("state") == "delivered":
                return
            session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == row.session_id,
                        ChatSession.tenant_id == row.tenant_id,
                        ChatSession.agent_id == row.agent_id,
                    )
                )
            ).scalar_one_or_none()
            if session is None:
                raise BudgetTransitionPermanentError("addressed session no longer exists")
            existing = (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == row.session_id,
                        ChatTranscriptEvent.causation_id == row.budget_event_id,
                        ChatTranscriptEvent.event_type == "runtime_budget_transition",
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                result = await append_session_event(
                    db=db,
                    agent_id=row.agent_id,
                    tenant_id=row.tenant_id,
                    session_id=row.session_id,
                    actor_type="system",
                    event_type="runtime_budget_transition",
                    content=row.content,
                    role="system",
                    user_id=row.user_id,
                    external_principal_id=row.external_principal_id,
                    run_id=row.runtime_task_id,
                    runtime_task_id=row.runtime_task_id,
                    root_session_id=row.session_id,
                    metadata={
                        **dict(row.metadata_json or {}),
                        "budget_transition_outbox_id": str(row.id),
                        "causation_id": str(row.budget_event_id),
                    },
                    visibility_scope="direct_user",
                    listed_surface="chat",
                    source="runtime_budget",
                    bridge_to_t0=False,
                )
                existing = result.transcript_event
            receipts["transcript"] = {
                "state": "delivered",
                "event_id": str(existing.id),
                "sequence": int(existing.sequence),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            row.delivery_receipts_json = receipts
            await db.commit()
        try:
            from app.services.web_chat_runtime import broadcast_web_chat_event

            await broadcast_web_chat_event(
                item.agent_id,
                item.session_id,
                {
                    "type": "runtime_budget_transition",
                    "event_type": "runtime_budget_transition",
                    "content": item.content,
                    "budget_run_id": str(item.budget_run_id),
                    "budget_event_id": str(item.budget_event_id),
                    "transition": item.transition,
                },
            )
        except Exception:
            # The committed transcript is the replay/backfill authority. A live
            # socket miss must not replay the durable event or external channel.
            pass

    async def _channel_authority(self, item: ClaimedBudgetTransition) -> None:
        if item.channel == "web":
            return
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="budget_transition_channel_authority",
        ) as db:
            if item.channel_config_id is None:
                raise BudgetTransitionPermanentError("external channel target has no configuration snapshot")
            config = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.id == item.channel_config_id,
                        ChannelConfig.tenant_id == item.tenant_id,
                        ChannelConfig.agent_id == item.agent_id,
                        ChannelConfig.channel_type == item.channel,
                    )
                )
            ).scalar_one_or_none()
            if config is None or not config.is_configured or not config.is_connected:
                raise BudgetTransitionPermanentError("external channel configuration was revoked or replaced")
            if item.external_principal_id is not None:
                principal = (
                    await db.execute(
                        select(ExternalPrincipal).where(
                            ExternalPrincipal.id == item.external_principal_id,
                            ExternalPrincipal.tenant_id == item.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if principal is None or principal.status != "active":
                    raise BudgetTransitionPermanentError("external principal was revoked")
                if principal.channel_config_id != item.channel_config_id:
                    raise BudgetTransitionPermanentError("external principal installation changed")

    async def _write_channel_receipt(
        self,
        *,
        item_id: uuid.UUID,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> None:
        async with self._worker_session("channel_receipt") as db:
            row = (
                await db.execute(
                    select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                raise BudgetTransitionAmbiguousError("delivery lease was lost while committing channel receipt")
            receipts = dict(row.delivery_receipts_json or {})
            receipts["channel"] = receipt
            row.delivery_receipts_json = receipts
            await db.commit()

    async def project_channel(self, *, item: ClaimedBudgetTransition, worker_id: str) -> None:
        async with self._worker_session("channel_state") as db:
            row = await db.get(BudgetTransitionOutbox, item.id)
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                raise BudgetTransitionRetryableError("budget transition delivery lease was lost")
            channel_receipt = dict((row.delivery_receipts_json or {}).get("channel") or {})
        if channel_receipt.get("state") == "delivered" or channel_receipt.get("state") == "not_required":
            return
        if channel_receipt.get("state") == "sending":
            raise BudgetTransitionAmbiguousError("channel provider call has no committed receipt")
        if item.channel == "web":
            await self._write_channel_receipt(
                item_id=item.id,
                worker_id=worker_id,
                receipt={"state": "not_required", "reason": "web transcript is the delivery surface"},
            )
            return
        await self._channel_authority(item)
        idempotency_key = f"{item.id}:channel"
        await self._write_channel_receipt(
            item_id=item.id,
            worker_id=worker_id,
            receipt={
                "state": "sending",
                "idempotency_key": idempotency_key,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="budget_transition_channel_provider_call",
        ) as db:
            try:
                result = await self._text_sender(
                    db=db,
                    agent_id=item.agent_id,
                    tenant_id=item.tenant_id,
                    reply_target=item.delivery_target,
                    text=item.content,
                    delivery_mode="budget_transition_outbox",
                    idempotency_key=idempotency_key,
                    extra_detail={
                        "budget_transition_outbox_id": str(item.id),
                        "budget_event_id": str(item.budget_event_id),
                    },
                )
            except Exception as exc:
                raise BudgetTransitionAmbiguousError(
                    f"channel provider result is unknown: {type(exc).__name__}: {exc}"
                ) from exc
        await self._write_channel_receipt(
            item_id=item.id,
            worker_id=worker_id,
            receipt={
                "state": "delivered" if result.ok else "failed",
                "idempotency_key": idempotency_key,
                "provider_status": result.status,
                "message": result.message,
                "retryable": bool(result.retryable),
                "detail": dict(result.detail or {}),
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        if result.ok:
            return
        if result.retryable:
            raise BudgetTransitionRetryableError(result.message)
        raise BudgetTransitionPermanentError(result.message)

    async def _mark_delivered(self, *, item_id: uuid.UUID, worker_id: str) -> bool:
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "delivered"
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            await db.commit()
            return True

    async def _mark_failure(
        self,
        *,
        item: ClaimedBudgetTransition,
        worker_id: str,
        error: Exception,
    ) -> str:
        async with self._worker_session("failure") as db:
            row = (
                await db.execute(
                    select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return "stale"
            row.last_error = f"{type(error).__name__}: {error}"
            row.locked_by = None
            row.locked_at = None
            if isinstance(error, BudgetTransitionAmbiguousError):
                row.status = "needs_reconciliation"
                outcome = "needs_reconciliation"
            elif isinstance(error, BudgetTransitionPermanentError) or int(row.attempt_count or 0) >= self._max_attempts:
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
        counts = {
            "claimed": len(claimed),
            "delivered": 0,
            "retried": 0,
            "dead_lettered": 0,
            "needs_reconciliation": 0,
        }
        for item in claimed:
            try:
                await self.project_transcript(item=item, worker_id=worker_id)
                await self.project_channel(item=item, worker_id=worker_id)
                if await self._mark_delivered(item_id=item.id, worker_id=worker_id):
                    counts["delivered"] += 1
            except Exception as exc:  # classified into safe retry vs ambiguous external side effect.
                if not isinstance(
                    exc,
                    (BudgetTransitionRetryableError, BudgetTransitionPermanentError, BudgetTransitionAmbiguousError),
                ):
                    exc = BudgetTransitionRetryableError(f"{type(exc).__name__}: {exc}")
                outcome = await self._mark_failure(item=item, worker_id=worker_id, error=exc)
                if outcome == "retry":
                    counts["retried"] += 1
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += 1
                elif outcome == "needs_reconciliation":
                    counts["needs_reconciliation"] += 1
        return counts

    async def reconcile_budget_events_once(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> int:
        """Backfill legacy/crash-window user transitions from mechanical facts."""

        async with self._worker_session("reconcile") as db:
            already = exists(
                select(BudgetTransitionOutbox.id).where(BudgetTransitionOutbox.budget_event_id == RuntimeBudgetEvent.id)
            )
            statement = (
                select(RuntimeBudgetEvent, RuntimeBudgetRun)
                .join(RuntimeBudgetRun, RuntimeBudgetRun.id == RuntimeBudgetEvent.budget_run_id)
                .where(
                    RuntimeBudgetEvent.event_type.in_(_TRANSITION_EVENTS),
                    RuntimeBudgetEvent.tenant_id.is_not(None),
                    RuntimeBudgetRun.root_agent_id.is_not(None),
                    RuntimeBudgetRun.root_session_id.is_not(None),
                    ~already,
                )
            )
            if tenant_id is not None:
                statement = statement.where(RuntimeBudgetEvent.tenant_id == tenant_id)
            rows = list(
                (
                    await db.execute(
                        statement.order_by(RuntimeBudgetEvent.created_at)
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            repaired = 0
            for event, run in rows:
                transition = transition_for_event(event)
                if transition is None:
                    continue
                outbox_id = await enqueue_budget_transition(
                    db,
                    run=run,
                    event=event,
                    transition=transition,
                    metadata={"reconciled_from_budget_event": True},
                )
                repaired += int(outbox_id is not None)
            await db.commit()
            return repaired

    async def list_deliveries(
        self,
        *,
        tenant_id: uuid.UUID,
        budget_run_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BudgetTransitionOutbox]:
        statement = select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.tenant_id == tenant_id)
        if budget_run_id is not None:
            statement = statement.where(BudgetTransitionOutbox.budget_run_id == budget_run_id)
        if status:
            statement = statement.where(BudgetTransitionOutbox.status == status)
        async with tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="budget_transition_delivery_list",
        ) as db:
            rows = (
                await db.execute(
                    statement.order_by(BudgetTransitionOutbox.created_at.desc()).limit(max(1, min(500, int(limit))))
                )
            ).scalars()
            return list(rows.all())

    async def resolve_delivery(
        self,
        *,
        tenant_id: uuid.UUID,
        outbox_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        action: str,
        reason: str,
    ) -> BudgetTransitionOutbox:
        """Apply an explicit operator decision to an ambiguous/dead delivery.

        ``mark_delivered`` is only valid for an ambiguous provider call. ``retry``
        preserves a committed transcript receipt while explicitly accepting the
        channel duplicate risk recorded in the audit trail.
        """

        normalized_action = str(action or "").strip().lower()
        normalized_reason = str(reason or "").strip()
        if normalized_action not in {"retry", "mark_delivered"}:
            raise ValueError("delivery resolution action must be retry or mark_delivered")
        if not normalized_reason:
            raise ValueError("delivery resolution reason is required")
        async with tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="budget_transition_delivery_resolution",
        ) as db:
            row = (
                await db.execute(
                    select(BudgetTransitionOutbox)
                    .where(
                        BudgetTransitionOutbox.id == outbox_id,
                        BudgetTransitionOutbox.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("budget transition delivery not found")
            if row.status not in {"dead_letter", "needs_reconciliation"}:
                raise ValueError("only dead-letter or ambiguous deliveries can be resolved")
            previous_status = row.status
            previous_error = row.last_error
            receipts = dict(row.delivery_receipts_json or {})
            if normalized_action == "mark_delivered":
                channel_receipt = dict(receipts.get("channel") or {})
                if previous_status != "needs_reconciliation" or channel_receipt.get("state") != "sending":
                    raise ValueError("mark_delivered requires an ambiguous in-flight channel call")
                receipts["channel"] = {
                    **channel_receipt,
                    "state": "operator_confirmed_delivered",
                    "actor_user_id": str(actor_user_id),
                    "reason": normalized_reason,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
                row.status = "delivered"
                row.delivered_at = datetime.now(UTC)
            else:
                receipts.pop("channel", None)
                row.status = "pending"
                row.attempt_count = 0
                row.available_at = datetime.now(UTC)
                row.delivered_at = None
            row.delivery_receipts_json = receipts
            row.locked_by = None
            row.locked_at = None
            row.last_error = None
            resolution = {
                "action": normalized_action,
                "reason": normalized_reason,
                "actor_user_id": str(actor_user_id),
                "previous_status": previous_status,
                "previous_error": previous_error,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            metadata = dict(row.metadata_json or {})
            metadata["operator_resolutions"] = [*list(metadata.get("operator_resolutions") or []), resolution]
            row.metadata_json = metadata
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    user_id=actor_user_id,
                    agent_id=row.agent_id,
                    action="budget_transition_delivery_resolved",
                    details={"outbox_id": str(row.id), **resolution},
                )
            )
            await db.commit()
            await db.refresh(row)
            return row


__all__ = [
    "BudgetTransitionOutboxService",
    "ClaimedBudgetTransition",
    "budget_transition_copy",
    "enqueue_budget_transition",
    "transition_for_event",
]
