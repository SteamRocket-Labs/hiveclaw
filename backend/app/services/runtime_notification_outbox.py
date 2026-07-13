"""Durable completion delivery for child and background runtime work."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid
from typing import Any, Literal

from sqlalchemy import Integer, String, and_, case, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService

DeliveryMode = Literal["parent_continuation", "session_projection"]
_OUTBOX_ID_NAMESPACE = uuid.UUID("0df71dc3-a9b3-4bb9-9886-702a16fbe953")
_COMPLETION_SOURCE_KIND_ALIASES = {
    "delegation": "a2a_delegation",
    "a2a_delegation": "a2a_delegation",
    "team_member": "agent_team",
    "approval_execution": "approval",
}


class CompletionDeliveryDeferred(RuntimeError):
    """A governed continuation is waiting for an approval, not failing."""


class CompletionDeliveryNotFound(LookupError):
    """The tenant-scoped delivery reconciliation item does not exist."""


def canonical_completion_source_kind(task_or_source_kind: Any) -> Any:
    """Map RuntimeTask/source aliases to the one durable completion identity."""

    if isinstance(task_or_source_kind, str):
        normalized = task_or_source_kind.strip().lower()
        return _COMPLETION_SOURCE_KIND_ALIASES.get(normalized, normalized)
    return case(
        *((task_or_source_kind == alias, canonical) for alias, canonical in _COMPLETION_SOURCE_KIND_ALIASES.items()),
        else_=task_or_source_kind,
    )


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
    claim_worker_id: str
    claim_locked_at: datetime


@dataclass(frozen=True, slots=True)
class CompletionTargetAuthority:
    valid: bool
    snapshot: dict[str, Any]
    runtime_task: RuntimeTask | None


def completion_notification_id(notification: CompletionNotification) -> uuid.UUID:
    identity = "|".join(
        (
            str(_uuid(notification.tenant_id, field="tenant_id")),
            canonical_completion_source_kind(str(notification.source_kind)),
            str(notification.source_run_id).strip(),
            str(_uuid(notification.parent_session_id, field="parent_session_id")),
            str(notification.terminal_status).strip().lower(),
        )
    )
    return uuid.uuid5(_OUTBOX_ID_NAMESPACE, identity)


def _normalized_values(notification: CompletionNotification) -> dict[str, Any]:
    source_kind = canonical_completion_source_kind(str(notification.source_kind or ""))
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
        where=or_(
            stmt.excluded.payload_rank > RuntimeNotificationOutbox.payload_rank,
            stmt.excluded.parent_agent_id != RuntimeNotificationOutbox.parent_agent_id,
            stmt.excluded.parent_user_id != RuntimeNotificationOutbox.parent_user_id,
            stmt.excluded.child_session_id.is_distinct_from(RuntimeNotificationOutbox.child_session_id),
            stmt.excluded.task_type != RuntimeNotificationOutbox.task_type,
            stmt.excluded.delivery_mode != RuntimeNotificationOutbox.delivery_mode,
        ),
    )
    await db.execute(stmt)
    return values["id"]


def _claimed(row: RuntimeNotificationOutbox) -> ClaimedCompletionNotification:
    if not row.locked_by or row.locked_at is None:
        raise RuntimeError("claimed completion notification is missing its claim fence")
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
        claim_worker_id=str(row.locked_by),
        claim_locked_at=row.locked_at,
    )


def _completion_claim_matches(
    row: RuntimeNotificationOutbox | None,
    item: ClaimedCompletionNotification,
) -> bool:
    """Fence external delivery by the exact monotonically claimed epoch."""

    return bool(
        row is not None
        and row.status == "processing"
        and row.locked_by == item.claim_worker_id
        and row.locked_at == item.claim_locked_at
        and int(row.attempt_count or 0) == item.attempt_count
    )


async def _set_approval_continuation_status(
    db: AsyncSession,
    row: RuntimeNotificationOutbox,
    *,
    status: str,
    error: str | None = None,
) -> None:
    if row.source_kind != "approval":
        return
    try:
        approval_id = _uuid((row.metadata_json or {}).get("approval_id"), field="approval_id")
    except ValueError:
        return
    approval = (
        await db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.tenant_id == row.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        return
    receipt = dict(approval.execution_receipt or {})
    receipt.update(
        {
            "continuation_status": status,
            "continuation_outbox_id": str(row.id),
            "continuation_attempt_count": int(row.attempt_count or 0),
        }
    )
    if error:
        receipt["continuation_error"] = error[:1_000]
    else:
        receipt.pop("continuation_error", None)
    approval.execution_receipt = receipt


def _delivery_failure_kind(error: Exception) -> str:
    text = str(error).lower()
    authority_markers = (
        "authority no longer resolves",
        "authority mismatch",
        "session no longer resolves",
        "tenant no longer resolves",
        "completion target authority",
    )
    return "authority_invalid" if any(marker in text for marker in authority_markers) else "delivery_failure"


async def _resolve_completion_target_authority(
    db: AsyncSession,
    row: RuntimeNotificationOutbox,
    *,
    lock: bool = False,
) -> CompletionTargetAuthority:
    """Resolve the current, task-bound authority for one delivery intent."""

    snapshot: dict[str, Any] = {
        "valid": False,
        "tenant_id": str(row.tenant_id),
        "parent_agent_id": str(row.parent_agent_id),
        "parent_user_id": str(row.parent_user_id),
        "parent_session_id": str(row.parent_session_id),
        "child_session_id": str(row.child_session_id) if row.child_session_id else None,
    }

    def rejected(reason: str, *, task: RuntimeTask | None = None) -> CompletionTargetAuthority:
        return CompletionTargetAuthority(
            valid=False,
            snapshot={**snapshot, "valid": False, "reason": reason},
            runtime_task=task,
        )

    task: RuntimeTask | None = None
    try:
        task_id = _uuid(row.source_run_id, field="source_run_id")
    except ValueError:
        task_id = None
    if task_id is not None:
        task_statement = select(RuntimeTask).where(
            RuntimeTask.id == task_id,
            RuntimeTask.tenant_id == row.tenant_id,
        )
        if lock:
            task_statement = task_statement.with_for_update()
        task = (await db.execute(task_statement)).scalar_one_or_none()
    if task is None and row.task_type != "agent_team_close":
        return rejected("source_runtime_task_not_found")

    expected_child_agent_id = row.parent_agent_id
    if task is not None:
        snapshot["source_runtime_task_id"] = str(task.id)
        expected_child_agent_id = task.child_agent_id or task.parent_agent_id
        snapshot["child_agent_id"] = str(expected_child_agent_id) if expected_child_agent_id else None
        if task.parent_agent_id != row.parent_agent_id:
            return rejected("parent_agent_mismatch", task=task)
        if str(task.task_type or "") != str(row.task_type or ""):
            return rejected("runtime_task_type_mismatch", task=task)
        expected_delivery_sessions = {
            str(value) for value in (task.parent_session_id, task.child_session_id) if str(value or "").strip()
        }
        if expected_delivery_sessions and str(row.parent_session_id) not in expected_delivery_sessions:
            return rejected("parent_session_mismatch", task=task)
        if row.child_session_id is not None and str(task.child_session_id or "") != str(row.child_session_id):
            return rejected("child_session_mismatch", task=task)

    parent_agent_statement = select(Agent.id).where(
        Agent.id == row.parent_agent_id,
        Agent.tenant_id == row.tenant_id,
        Agent.deleted_at.is_(None),
        Agent.deactivated_at.is_(None),
    )
    parent_user_statement = select(User.id).where(
        User.id == row.parent_user_id,
        User.tenant_id == row.tenant_id,
        User.is_active.is_(True),
    )
    parent_session_statement = select(ChatSession.id).where(
        ChatSession.id == row.parent_session_id,
        ChatSession.tenant_id == row.tenant_id,
        ChatSession.agent_id == row.parent_agent_id,
        ChatSession.user_id == row.parent_user_id,
    )
    if lock:
        parent_agent_statement = parent_agent_statement.with_for_update()
        parent_user_statement = parent_user_statement.with_for_update()
        parent_session_statement = parent_session_statement.with_for_update()
    parent_agent = (await db.execute(parent_agent_statement)).scalar_one_or_none()
    if parent_agent is None:
        return rejected("parent_agent_inactive_or_missing", task=task)
    parent_user = (await db.execute(parent_user_statement)).scalar_one_or_none()
    if parent_user is None:
        return rejected("parent_user_inactive_or_missing", task=task)
    parent_session = (await db.execute(parent_session_statement)).scalar_one_or_none()
    if parent_session is None:
        return rejected("parent_session_authority_mismatch", task=task)

    if row.child_session_id is not None:
        if expected_child_agent_id is None:
            return rejected("child_agent_authority_missing", task=task)
        child_agent_statement = select(Agent.id).where(
            Agent.id == expected_child_agent_id,
            Agent.tenant_id == row.tenant_id,
            Agent.deleted_at.is_(None),
            Agent.deactivated_at.is_(None),
        )
        child_session_statement = select(ChatSession.id).where(
            ChatSession.id == row.child_session_id,
            ChatSession.tenant_id == row.tenant_id,
            ChatSession.agent_id == expected_child_agent_id,
            ChatSession.user_id == row.parent_user_id,
        )
        if lock:
            child_agent_statement = child_agent_statement.with_for_update()
            child_session_statement = child_session_statement.with_for_update()
        child_agent = (await db.execute(child_agent_statement)).scalar_one_or_none()
        if child_agent is None:
            return rejected("child_agent_inactive_or_missing", task=task)
        child_session = (await db.execute(child_session_statement)).scalar_one_or_none()
        if child_session is None:
            return rejected("child_session_authority_mismatch", task=task)

    snapshot.update(valid=True, reason=None)
    return CompletionTargetAuthority(valid=True, snapshot=snapshot, runtime_task=task)


async def _completion_target_authority_valid(
    db: AsyncSession,
    row: RuntimeNotificationOutbox,
) -> bool:
    return (await _resolve_completion_target_authority(db, row)).valid


def _system_plan_notification_projection(
    task: RuntimeTask,
    plan: AgentPlanRequest | None,
) -> tuple[str, str, dict[str, Any]]:
    """Map execution truth plus canonical Plan truth to one user-visible state."""

    plan_status = str(getattr(plan, "status", None) or "missing")
    if task.status == "resumable":
        status = "resumable"
        summary = f"Plan {getattr(plan, 'id', 'unknown')} authoring was interrupted and will retry automatically."
    elif task.status == "needs_reconciliation":
        status = "needs_reconciliation"
        summary = f"Plan {getattr(plan, 'id', 'unknown')} authoring is blocked pending recovery reconciliation."
    elif plan_status in {"awaiting_confirmation", "confirmed"}:
        status = "completed"
        summary = f"Plan {plan.id} is ready for confirmation."
    elif plan_status in {"rejected", "superseded", "expired"}:
        status = "skipped"
        summary = f"Plan {plan.id} authoring stopped because the Plan is {plan_status}."
    elif task.status == "killed":
        status = "killed"
        summary = f"Plan {getattr(plan, 'id', 'unknown')} authoring was cancelled before completion."
    else:
        status = "failed"
        summary = (
            f"Plan {getattr(plan, 'id', 'unknown')} authoring ended before a confirmable Plan was produced; "
            "regenerate it."
        )
    return (
        status,
        summary,
        {
            "plan_id": str(getattr(plan, "id", "") or "") or None,
            "plan_status": plan_status,
            "runtime_task_status": str(task.status),
        },
    )


def _build_reconcile_candidate_statement(
    *,
    candidate_limit: int,
    task_ids: set[uuid.UUID] | None,
):
    """Build the bounded, authority-filtered terminal recovery scan."""

    terminal_statuses = ("completed", "failed", "killed", "skipped", "needs_reconciliation")
    standard_supported_types = (
        "subagent",
        "team_member",
        "workflow",
        "delegation",
        "a2a_delegation",
        "trigger",
        "approval_execution",
    )
    source_kind = canonical_completion_source_kind(RuntimeTask.task_type)
    plan_identity = and_(
        cast(AgentPlanRequest.id, String) == RuntimeTask.metadata_json["plan_id"].astext,
        AgentPlanRequest.tenant_id == RuntimeTask.tenant_id,
        AgentPlanRequest.agent_id == RuntimeTask.parent_agent_id,
    )
    plan_ready = exists(
        select(AgentPlanRequest.id).where(
            plan_identity,
            AgentPlanRequest.status.in_(("awaiting_confirmation", "confirmed")),
        )
    ).correlate(RuntimeTask)
    plan_stopped = exists(
        select(AgentPlanRequest.id).where(
            plan_identity,
            AgentPlanRequest.status.in_(("rejected", "superseded", "expired")),
        )
    ).correlate(RuntimeTask)
    metadata_parent_session_id = func.nullif(RuntimeTask.metadata_json["parent_session_id"].astext, "")
    actual_parent_session_id = func.coalesce(metadata_parent_session_id, RuntimeTask.parent_session_id)
    target_session_id = case(
        (RuntimeTask.task_type == "trigger", RuntimeTask.child_session_id),
        (
            and_(
                RuntimeTask.task_type == "subagent",
                actual_parent_session_id.is_(None),
            ),
            RuntimeTask.child_session_id,
        ),
        else_=actual_parent_session_id,
    )
    system_plan_status_satisfied = and_(
        RuntimeTask.task_type == "system_plan_run",
        or_(
            and_(
                RuntimeTask.status == "resumable",
                RuntimeNotificationOutbox.terminal_status == "resumable",
            ),
            and_(
                RuntimeTask.status == "needs_reconciliation",
                RuntimeNotificationOutbox.terminal_status == "needs_reconciliation",
            ),
            and_(
                RuntimeTask.status.notin_(("resumable", "needs_reconciliation")),
                plan_ready,
                RuntimeNotificationOutbox.terminal_status == "completed",
            ),
            and_(
                RuntimeTask.status.notin_(("resumable", "needs_reconciliation")),
                ~plan_ready,
                plan_stopped,
                RuntimeNotificationOutbox.terminal_status == "skipped",
            ),
            and_(
                RuntimeTask.status == "killed",
                ~plan_ready,
                ~plan_stopped,
                RuntimeNotificationOutbox.terminal_status == "killed",
            ),
            and_(
                RuntimeTask.status.in_(("completed", "failed", "skipped")),
                ~plan_ready,
                ~plan_stopped,
                RuntimeNotificationOutbox.terminal_status == "failed",
            ),
        ),
    )
    already_satisfied = exists(
        select(RuntimeNotificationOutbox.id)
        .join(
            ChatSession,
            and_(
                ChatSession.id == RuntimeNotificationOutbox.parent_session_id,
                ChatSession.tenant_id == RuntimeNotificationOutbox.tenant_id,
            ),
        )
        .where(
            RuntimeNotificationOutbox.tenant_id == RuntimeTask.tenant_id,
            RuntimeNotificationOutbox.source_kind == source_kind,
            RuntimeNotificationOutbox.source_run_id == cast(RuntimeTask.id, String),
            RuntimeNotificationOutbox.task_type == RuntimeTask.task_type,
            cast(RuntimeNotificationOutbox.parent_session_id, String) == target_session_id,
            RuntimeNotificationOutbox.parent_agent_id == RuntimeTask.parent_agent_id,
            RuntimeNotificationOutbox.parent_user_id == ChatSession.user_id,
            or_(
                and_(
                    RuntimeTask.task_type != "system_plan_run",
                    RuntimeNotificationOutbox.terminal_status == RuntimeTask.status,
                ),
                system_plan_status_satisfied,
            ),
        )
    ).correlate(RuntimeTask)

    delivery_target_exists = exists(
        select(ChatSession.id)
        .join(
            User,
            and_(
                User.id == ChatSession.user_id,
                User.tenant_id == ChatSession.tenant_id,
            ),
        )
        .where(
            cast(ChatSession.id, String) == target_session_id,
            ChatSession.tenant_id == RuntimeTask.tenant_id,
            ChatSession.agent_id == RuntimeTask.parent_agent_id,
            User.is_active.is_(True),
            or_(
                RuntimeTask.task_type != "system_plan_run",
                and_(
                    RuntimeTask.root_user_id.is_not(None),
                    ChatSession.user_id == RuntimeTask.root_user_id,
                ),
            ),
        )
    ).correlate(RuntimeTask)
    statement = select(RuntimeTask).where(
        or_(
            and_(
                RuntimeTask.task_type.in_(standard_supported_types),
                RuntimeTask.status.in_(terminal_statuses),
            ),
            and_(
                RuntimeTask.task_type == "system_plan_run",
                RuntimeTask.status.in_((*terminal_statuses, "resumable")),
            ),
        ),
        RuntimeTask.tenant_id.is_not(None),
        RuntimeTask.parent_agent_id.is_not(None),
        delivery_target_exists,
        ~already_satisfied,
    )
    if task_ids is not None:
        statement = statement.where(RuntimeTask.id.in_(task_ids))
    # Global age is the fairness boundary: recovery work wins only equal-age
    # ties, so neither recovery nor ordinary terminal lanes can starve.
    return (
        statement.order_by(
            RuntimeTask.created_at.asc(),
            RuntimeTask.status.in_(("resumable", "needs_reconciliation")).desc(),
            RuntimeTask.id.asc(),
        )
        .limit(max(1, int(candidate_limit)))
        .with_for_update(skip_locked=True)
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
        item_ids: set[uuid.UUID] | None = None,
    ) -> list[ClaimedCompletionNotification]:
        if item_ids is not None and not item_ids:
            return []
        current = now or datetime.now(UTC)
        expired_lock = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            statement = select(RuntimeNotificationOutbox).where(
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
            if item_ids is not None:
                statement = statement.where(RuntimeNotificationOutbox.id.in_(item_ids))
            rows = list(
                (
                    await db.execute(
                        statement.order_by(RuntimeNotificationOutbox.available_at, RuntimeNotificationOutbox.created_at)
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
                await _set_approval_continuation_status(db, row, status="continuing")
            await db.commit()
            return [_claimed(row) for row in rows]

    async def reconcile_terminal_tasks_once(
        self,
        *,
        limit: int = 100,
        task_ids: set[uuid.UUID] | None = None,
    ) -> int:
        """Backfill a missing intent after a crash between terminal write and enqueue.

        Normal producers enqueue in their terminal/projection transaction. This
        sweep is the recovery atom for legacy rows and the two paths whose
        terminal RuntimeTask precedes their richer parent projection.
        """

        if task_ids is not None and not task_ids:
            return 0

        repair_limit = max(1, int(limit))
        candidate_batch_size = max(20, min(500, repair_limit * 5))
        async with self._worker_session("reconcile_terminal_tasks") as db:
            tasks = list(
                (
                    await db.execute(
                        _build_reconcile_candidate_statement(
                            candidate_limit=candidate_batch_size,
                            task_ids=task_ids,
                        )
                    )
                )
                .scalars()
                .all()
            )
            repaired = 0
            for task in tasks:
                metadata = dict(task.metadata_json or {})
                source_kind = canonical_completion_source_kind(task.task_type)
                plan = None
                notification_status = str(task.status)
                notification_summary = str(
                    task.result_summary or f"{task.task_type} finished with status {task.status}."
                )
                projection_metadata: dict[str, Any] = {}
                if task.task_type == "system_plan_run":
                    try:
                        plan_id = _uuid(metadata.get("plan_id"), field="plan_id")
                    except ValueError:
                        plan_id = None
                    if plan_id is not None:
                        plan = (
                            await db.execute(
                                select(AgentPlanRequest).where(
                                    AgentPlanRequest.id == plan_id,
                                    AgentPlanRequest.tenant_id == task.tenant_id,
                                    AgentPlanRequest.agent_id == task.parent_agent_id,
                                )
                            )
                        ).scalar_one_or_none()
                    notification_status, notification_summary, projection_metadata = (
                        _system_plan_notification_projection(task, plan)
                    )
                actual_parent_session_id = metadata.get("parent_session_id") or task.parent_session_id
                foreground_subagent = (
                    task.task_type == "subagent"
                    and str(metadata.get("execution_backend") or "").strip() == "foreground_inline"
                )
                foreground_inline_a2a = (
                    task.task_type in {"delegation", "a2a_delegation"}
                    and str(metadata.get("execution_backend") or "").strip() == "foreground_inline"
                )
                headless_subagent = task.task_type == "subagent" and not actual_parent_session_id
                local_projection_only = foreground_subagent or headless_subagent
                target_value = (
                    task.child_session_id
                    if task.task_type == "trigger" or headless_subagent
                    else actual_parent_session_id
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
                owner_id = parent_session.user_id
                recorded_owner = metadata.get("user_id") or metadata.get("owner_id")
                notification_metadata = {
                    key: value for key, value in metadata.items() if key not in {"user_id", "owner_id"}
                }
                if recorded_owner is not None and str(recorded_owner) != str(owner_id):
                    projection_metadata["recorded_parent_user_mismatch"] = {
                        "metadata_user_id": str(recorded_owner),
                        "canonical_session_user_id": str(owner_id),
                    }
                child_session_id = None
                if task.task_type not in {"trigger", "system_plan_run"} and task.child_session_id:
                    try:
                        child_session_id = _uuid(task.child_session_id, field="child_session_id")
                    except ValueError:
                        child_session_id = None
                outbox_id = await enqueue_completion_notification(
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
                        terminal_status=notification_status,
                        task_type=task.task_type,
                        summary=notification_summary,
                        delivery_mode=(
                            "session_projection"
                            if task.task_type in {"trigger", "system_plan_run"}
                            or local_projection_only
                            or foreground_inline_a2a
                            else "parent_continuation"
                        ),
                        artifacts=list(metadata.get("artifacts") or []),
                        metadata={
                            **notification_metadata,
                            **(
                                {
                                    "model_context": (
                                        "[Approval tool result]\n"
                                        f"Approval: {metadata.get('approval_id') or 'unknown'}\n"
                                        f"Tool: {metadata.get('tool_name') or 'approved_action'}\n"
                                        f"Result: {str(task.result_summary or '')[:20_000]}\n"
                                        "Continue the original task from this approved tool result."
                                    )
                                }
                                if task.task_type == "approval_execution"
                                else {}
                            ),
                            **projection_metadata,
                            **({"foreground_inline_result_already_returned": True} if foreground_inline_a2a else {}),
                            **(
                                {
                                    "subagent_terminal_projection_required": child_session_id is not None,
                                    **(
                                        {"actual_parent_session_id": str(actual_parent_session_id)}
                                        if actual_parent_session_id
                                        else {}
                                    ),
                                    **({"local_projection_only": True} if local_projection_only else {}),
                                }
                                if task.task_type == "subagent"
                                else {}
                            ),
                            "reconciled_from_terminal_runtime_task": True,
                        },
                        payload_rank=10,
                    ),
                )
                repaired_intent = (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(RuntimeNotificationOutbox.id == outbox_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if repaired_intent.status == "dead_letter" and await _completion_target_authority_valid(
                    db,
                    repaired_intent,
                ):
                    self._requeue_dead_letter_row(
                        repaired_intent,
                        now=datetime.now(UTC),
                        reason="terminal_repair_corrected_stable_intent_authority",
                    )
                repaired += 1
                if repaired >= repair_limit:
                    break
            await db.commit()
        return repaired

    async def _mark_delivered(
        self,
        *,
        item: ClaimedCompletionNotification,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> bool:
        now = datetime.now(UTC)
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if worker_id != item.claim_worker_id or not _completion_claim_matches(row, item):
                return False
            row.status = "delivered"
            row.delivered_at = now
            row.delivery_receipt_json = receipt
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            await _set_approval_continuation_status(db, row, status="delivered")
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
            if worker_id != item.claim_worker_id or not _completion_claim_matches(row, item):
                return "stale"
            row.last_error = f"{type(error).__name__}: {str(error)[:1000]}"
            row.locked_by = None
            row.locked_at = None
            if int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
                metadata = dict(row.metadata_json or {})
                previous_reconciliation = dict(metadata.get("delivery_reconciliation") or {})
                authority = await _resolve_completion_target_authority(db, row)
                failure_kind = _delivery_failure_kind(error)
                metadata["delivery_reconciliation"] = {
                    **previous_reconciliation,
                    "schema": "runtime_notification_delivery_reconciliation.v1",
                    "status": "needs_reconciliation",
                    "delivery_only": True,
                    "execution_terminal_status": row.terminal_status,
                    "outbox_id": str(row.id),
                    "attempt_count": int(row.attempt_count or 0),
                    "last_error": row.last_error,
                    "failure_kind": failure_kind,
                    "authority_snapshot": authority.snapshot,
                    "automatic_retry_count": int(previous_reconciliation.get("automatic_retry_count") or 0),
                    "retry_entry": "runtime_notification_outbox.retry_runtime_notification_delivery",
                    "updated_at": now.isoformat(),
                }
                row.metadata_json = metadata
                row.delivery_receipt_json = {
                    "status": "needs_reconciliation",
                    "delivery_only": True,
                    "execution_terminal_status": row.terminal_status,
                    "outbox_id": str(row.id),
                    "failure_kind": failure_kind,
                }
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
            await _set_approval_continuation_status(
                db,
                row,
                status="needs_reconciliation" if outcome == "dead_letter" else "retrying",
                error=row.last_error,
            )
            await db.commit()
            return outcome

    @staticmethod
    def _requeue_dead_letter_row(
        row: RuntimeNotificationOutbox,
        *,
        now: datetime,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
        automatic: bool = False,
    ) -> None:
        metadata = dict(row.metadata_json or {})
        previous = dict(metadata.get("delivery_reconciliation") or {})
        metadata["delivery_reconciliation"] = {
            **previous,
            "schema": "runtime_notification_delivery_reconciliation.v1",
            "status": "retry_requested",
            "delivery_only": True,
            "execution_terminal_status": row.terminal_status,
            "outbox_id": str(row.id),
            "retry_reason": str(reason),
            "retry_requested_at": now.isoformat(),
            "automatic_retry_count": int(previous.get("automatic_retry_count") or 0) + (1 if automatic else 0),
            **({"retry_actor_user_id": str(actor_user_id)} if actor_user_id is not None else {}),
        }
        row.metadata_json = metadata
        row.status = "pending"
        row.available_at = now
        row.locked_by = None
        row.locked_at = None
        row.last_error = None
        row.attempt_count = 0

    async def retry_recoverable_dead_letters_once(self, *, limit: int = 100) -> int:
        """Requeue once after a persisted invalid->valid authority transition."""

        now = datetime.now(UTC)
        retry_limit = max(1, int(limit))
        candidate_limit = max(101, min(500, retry_limit * 5))
        retried = 0
        async with self._worker_session("retry_recoverable_dead_letters") as db:
            reconciliation_json = RuntimeNotificationOutbox.metadata_json["delivery_reconciliation"]
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(
                            RuntimeNotificationOutbox.status == "dead_letter",
                            RuntimeNotificationOutbox.source_kind == "subagent",
                            RuntimeNotificationOutbox.task_type == "subagent",
                            RuntimeNotificationOutbox.available_at <= now,
                            reconciliation_json["failure_kind"].astext == "authority_invalid",
                            reconciliation_json["authority_snapshot"]["valid"].astext == "false",
                            cast(
                                func.coalesce(
                                    func.nullif(reconciliation_json["automatic_retry_count"].astext, ""),
                                    "0",
                                ),
                                Integer,
                            )
                            < 1,
                        )
                        .order_by(
                            RuntimeNotificationOutbox.available_at,
                            RuntimeNotificationOutbox.updated_at,
                            RuntimeNotificationOutbox.id,
                        )
                        .limit(candidate_limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                reconciliation = dict((row.metadata_json or {}).get("delivery_reconciliation") or {})
                if not await _completion_target_authority_valid(db, row):
                    metadata = dict(row.metadata_json or {})
                    metadata["delivery_reconciliation"] = {
                        **reconciliation,
                        "status": "authority_still_invalid",
                        "authority_snapshot": {
                            **dict(reconciliation.get("authority_snapshot") or {}),
                            "valid": False,
                        },
                        "deferred_at": now.isoformat(),
                    }
                    row.metadata_json = metadata
                    row.available_at = now + timedelta(seconds=self._deferred_retry_seconds)
                    continue
                self._requeue_dead_letter_row(
                    row,
                    now=now,
                    reason="delivery_target_authority_repaired",
                    automatic=True,
                )
                retried += 1
                if retried >= retry_limit:
                    break
            await db.commit()
        return retried

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
            if worker_id != item.claim_worker_id or not _completion_claim_matches(row, item):
                return False
            row.status = "pending"
            row.available_at = datetime.now(UTC) + timedelta(seconds=self._deferred_retry_seconds)
            row.last_error = reason
            row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
            row.locked_by = None
            row.locked_at = None
            await _set_approval_continuation_status(db, row, status="queued", error=reason)
            await db.commit()
            return True

    async def _prepare_local_projection(self, item: ClaimedCompletionNotification) -> None:
        """Repair durable local facts before the external/LLM continuation lane.

        This step runs for both authoritative Subagent intents and terminal-task
        sweep backfills. It is deliberately inside the outbox retry boundary so
        a process crash or transient DB failure is retried by the default worker.
        """

        if item.source_kind != "subagent" or item.task_type != "subagent" or item.child_session_id is None:
            return
        if not (
            item.metadata.get("subagent_terminal_projection_required") is True
            or item.metadata.get("reconciled_from_terminal_runtime_task") is True
        ):
            return
        raw_parent_session_id = item.metadata.get("actual_parent_session_id") or item.metadata.get("parent_session_id")
        if raw_parent_session_id is None and item.delivery_mode == "parent_continuation":
            raw_parent_session_id = item.parent_session_id
        try:
            actual_parent_session_id = _uuid(raw_parent_session_id, field="actual_parent_session_id")
        except ValueError:
            actual_parent_session_id = None

        from app.services.subagent_run_service import repair_subagent_terminal_projection_from_notification

        await repair_subagent_terminal_projection_from_notification(
            notification_id=item.id,
            tenant_id=item.tenant_id,
            run_id=item.source_run_id,
            parent_agent_id=item.parent_agent_id,
            parent_user_id=item.parent_user_id,
            parent_session_id=actual_parent_session_id,
            child_session_id=item.child_session_id,
            status=item.terminal_status,
            summary=item.summary,
        )

    async def _deliver(self, item: ClaimedCompletionNotification) -> dict[str, Any]:
        foreground_inline_a2a = (
            item.task_type in {"delegation", "a2a_delegation"}
            and str(item.metadata.get("execution_backend") or "").strip() == "foreground_inline"
        )
        resume_parent = item.delivery_mode == "parent_continuation" and not foreground_inline_a2a

        from app.services.agent_session_continuation import continue_parent_session_with_task_notification

        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="runtime_notification_outbox_delivery",
        ) as db:
            authority_row = (
                await db.execute(
                    select(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.id == item.id,
                        RuntimeNotificationOutbox.tenant_id == item.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not _completion_claim_matches(authority_row, item):
                raise RuntimeError("completion delivery claim is stale")

            # Keep the claim row locked across projection, budget admission and
            # continuation. A lease reclaimer therefore cannot advance the
            # attempt epoch after this fence and before the external side effect.
            await self._prepare_local_projection(item)
            if item.metadata.get("local_projection_only") is True:
                return {
                    "status": "local_projection_repaired",
                    "completion_outbox_id": str(item.id),
                    "deduplicated": True,
                }

            if item.task_type == "agent_team_close" and resume_parent:
                from app.services.web_chat_runtime import get_active_web_chat_run

                active_run = await get_active_web_chat_run(
                    db=db,
                    agent_id=item.parent_agent_id,
                    session_id=item.parent_session_id,
                )
                if active_run is not None:
                    raise CompletionDeliveryDeferred("parent_session_active")

            if (
                authority_row is None
                or not (await _resolve_completion_target_authority(db, authority_row, lock=True)).valid
            ):
                raise RuntimeError("completion target authority no longer resolves")

            admission: ExecutionAdmission | None = None
            admission_decision: ExecutionAdmissionDecision | None = None
            raw_budget_run_id = item.metadata.get("budget_run_id")
            if resume_parent and raw_budget_run_id:
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
                await db.execute(
                    select(User).where(
                        User.id == item.parent_user_id,
                        User.tenant_id == item.tenant_id,
                        User.is_active.is_(True),
                    )
                )
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
                    **({"foreground_inline_result_already_returned": True} if foreground_inline_a2a else {}),
                    "completion_outbox_id": str(item.id),
                    "causation_id": str(item.id),
                },
                artifacts=item.artifacts,
                resume_parent=resume_parent,
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

    async def _deliver_custom_with_claim_gate(
        self,
        item: ClaimedCompletionNotification,
        delivery: Callable[[ClaimedCompletionNotification], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Keep tests/adapters on the same pre-side-effect claim fence."""

        async with self._worker_session("custom_delivery_claim_gate") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if not _completion_claim_matches(row, item):
                raise RuntimeError("completion delivery claim is stale")
            await self._prepare_local_projection(item)
            return await delivery(item)

    async def drain_once(
        self,
        *,
        worker_id: str,
        deliver: Callable[[ClaimedCompletionNotification], Awaitable[dict[str, Any]]] | None = None,
        limit: int = 20,
        item_ids: set[uuid.UUID] | None = None,
    ) -> dict[str, int]:
        claimed = await self.claim_batch(worker_id=worker_id, limit=limit, item_ids=item_ids)
        counts = {"claimed": len(claimed), "delivered": 0, "retried": 0, "deferred": 0, "dead_lettered": 0}
        for item in claimed:
            try:
                receipt = (
                    await self._deliver(item)
                    if deliver is None
                    else await self._deliver_custom_with_claim_gate(item, deliver)
                )
                acknowledged = await self._mark_delivered(
                    item=item,
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


def _delivery_reconciliation_payload(
    row: RuntimeNotificationOutbox,
    *,
    execution_terminal_status: str | None = None,
    authority_valid: bool | None = None,
    live_authority_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(row.metadata_json or {})
    reconciliation = dict(metadata.get("delivery_reconciliation") or {})
    execution_status = str(
        execution_terminal_status or reconciliation.get("execution_terminal_status") or row.terminal_status
    )
    authority_snapshot = {
        **dict(reconciliation.get("authority_snapshot") or {}),
        "tenant_id": str(row.tenant_id),
        "parent_agent_id": str(row.parent_agent_id),
        "parent_user_id": str(row.parent_user_id),
        "parent_session_id": str(row.parent_session_id),
        "child_session_id": str(row.child_session_id) if row.child_session_id else None,
        **dict(live_authority_snapshot or {}),
        **({"valid": authority_valid} if authority_valid is not None else {}),
    }
    return {
        "delivery_id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "source_kind": row.source_kind,
        "source_run_id": row.source_run_id,
        "parent_agent_id": str(row.parent_agent_id),
        "parent_user_id": str(row.parent_user_id),
        "task_type": row.task_type,
        "status": row.status,
        "execution_terminal_status": execution_status,
        "delivery_only": True,
        "does_not_rerun_execution": True,
        "retryable": bool(
            authority_snapshot.get("valid") is True and _delivery_retryable(row, execution_status=execution_status)
        ),
        "attempt_count": int(row.attempt_count or 0),
        "last_error": row.last_error,
        "parent_session_id": str(row.parent_session_id),
        "child_session_id": str(row.child_session_id) if row.child_session_id else None,
        "summary": row.summary,
        "authority_snapshot": authority_snapshot,
        "reconciliation": reconciliation,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _delivery_execution_status(
    db: AsyncSession,
    row: RuntimeNotificationOutbox,
) -> str:
    try:
        runtime_task_id = _uuid(row.source_run_id, field="source_run_id")
    except ValueError:
        return str(row.terminal_status)
    task_status = (
        await db.execute(
            select(RuntimeTask.status).where(
                RuntimeTask.id == runtime_task_id,
                RuntimeTask.tenant_id == row.tenant_id,
            )
        )
    ).scalar_one_or_none()
    return str(task_status or row.terminal_status)


def _delivery_retryable(
    row: RuntimeNotificationOutbox,
    *,
    execution_status: str,
) -> bool:
    """Return the single operator retry gate for delivery-only reconciliation."""

    if row.status != "dead_letter":
        return False
    if execution_status in {"completed", "failed", "killed", "skipped", "needs_reconciliation"}:
        return True
    return bool(
        row.source_kind == "system_plan_run"
        and row.task_type == "system_plan_run"
        and row.delivery_mode == "session_projection"
        and execution_status == "resumable"
    )


async def list_runtime_notification_delivery_reconciliations(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str = "dead_letter",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Tenant-scoped operator queue for completion delivery, never execution."""

    rows = list(
        (
            await db.execute(
                select(RuntimeNotificationOutbox)
                .where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.status == status,
                )
                .order_by(RuntimeNotificationOutbox.updated_at.desc())
                .limit(max(1, min(200, int(limit))))
            )
        )
        .scalars()
        .all()
    )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        execution_status = await _delivery_execution_status(db, row)
        authority = await _resolve_completion_target_authority(db, row)
        payloads.append(
            _delivery_reconciliation_payload(
                row,
                execution_terminal_status=execution_status,
                live_authority_snapshot=authority.snapshot,
            )
        )
    return payloads


async def retry_runtime_notification_delivery(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    delivery_id: uuid.UUID,
    reason: str,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    """Requeue exactly one dead-letter delivery without reopening RuntimeTask."""

    normalized_reason = str(reason or "").strip()
    if len(normalized_reason) < 8:
        raise ValueError("Delivery retry reason must contain at least 8 characters")
    row = (
        await db.execute(
            select(RuntimeNotificationOutbox)
            .where(
                RuntimeNotificationOutbox.id == delivery_id,
                RuntimeNotificationOutbox.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.status != "dead_letter":
        raise CompletionDeliveryNotFound("Dead-letter completion delivery not found")
    try:
        runtime_task_id = _uuid(row.source_run_id, field="source_run_id")
    except ValueError as exc:
        raise ValueError("Delivery retry requires an addressable terminal RuntimeTask") from exc
    execution_status = (
        await db.execute(
            select(RuntimeTask.status).where(
                RuntimeTask.id == runtime_task_id,
                RuntimeTask.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    execution_status = str(execution_status)
    if not _delivery_retryable(row, execution_status=execution_status):
        raise ValueError("Delivery retry requires terminal truth or a resumable System Plan session projection")
    authority = await _resolve_completion_target_authority(db, row, lock=True)
    if not authority.valid:
        raise ValueError("Delivery retry target authority does not currently resolve")
    RuntimeNotificationOutboxService._requeue_dead_letter_row(
        row,
        now=datetime.now(UTC),
        reason=normalized_reason,
        actor_user_id=actor_user_id,
    )
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=row.parent_agent_id,
            action="runtime_notification_delivery_retry",
            details={
                "outbox_id": str(row.id),
                "source_kind": row.source_kind,
                "source_run_id": row.source_run_id,
                "reason": normalized_reason,
                "delivery_only": True,
                "does_not_rerun_execution": True,
                "execution_terminal_status": execution_status,
            },
        )
    )
    payload = _delivery_reconciliation_payload(
        row,
        execution_terminal_status=execution_status,
        live_authority_snapshot=authority.snapshot,
    )
    await db.commit()
    return payload
