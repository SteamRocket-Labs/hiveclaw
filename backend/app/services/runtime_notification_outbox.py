"""Durable completion delivery for child and background runtime work."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid
from typing import Any, Literal

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import (
    RuntimeResultIntegrationPage,
    RuntimeResultMailboxCursor,
    RuntimeResultObject,
)
from app.models.runtime_task import (
    COMPLETION_OUTBOX_PENDING_SQL,
    COMPLETION_OUTBOX_RETRY_SECONDS,
    COMPLETION_OUTBOX_TASK_TYPES,
    COMPLETION_OUTBOX_TERMINAL_STATUSES,
    RuntimeTask,
)
from app.models.user import User
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import RuntimeBudgetReservation, RuntimeBudgetService
from app.services.runtime_result_store import (
    RuntimeResultDescriptor,
    RuntimeResultIntegrationPage as RuntimeResultIntegrationPageValue,
    build_runtime_result_integration_pages,
    encode_runtime_result_payload,
    runtime_result_object_id,
    runtime_result_ref,
)
from app.services.runtime_result_metrics import record_runtime_result_observed, record_runtime_result_page

DeliveryMode = Literal["parent_continuation", "session_projection"]
_OUTBOX_ID_NAMESPACE = uuid.UUID("0df71dc3-a9b3-4bb9-9886-702a16fbe953")
_CURSOR_ID_NAMESPACE = uuid.UUID("21fd22ce-859b-5e68-a0fe-4bd78ec19b05")
_PAGE_ID_NAMESPACE = uuid.UUID("831ea736-2152-56b1-b7fb-53ed2f58cc43")
_ROUTING_METADATA_KEYS = frozenset(
    {
        "approval_id",
        "approval_status",
        "tool_name",
        "origin_session_id",
        "budget_run_id",
        "agent_team_close_id",
        "team_id",
        "team_name",
        "member_id",
        "member_name",
        "workflow_run_id",
        "parent_agent_id",
        "parent_session_id",
        "runtime_task_id",
        "runtime_task_type",
        "signal_id",
        "trace_id",
        "thread_id",
        "from_agent_id",
        "from_agent",
        "to_agent",
        "to_agent_name",
        "interaction_type",
        "depth",
        "root_runtime_task_id",
        "reconciled_from_terminal_runtime_task",
    }
)


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
    root_runtime_task_id: uuid.UUID | str | None = None
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
    root_runtime_task_id: uuid.UUID | None
    result_object_id: uuid.UUID
    result_ref: str
    result_sha256: str
    result_size_bytes: int
    artifact_count: int
    mailbox_sequence: int
    claim_token: uuid.UUID
    integration_page_id: uuid.UUID | None
    child_session_id: uuid.UUID | None
    child_agent_name: str | None
    delivery_mode: DeliveryMode
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


@dataclass(frozen=True, slots=True)
class ClaimedResultIntegrationPage:
    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_session_id: uuid.UUID
    parent_agent_id: uuid.UUID
    parent_user_id: uuid.UUID
    root_runtime_task_id: uuid.UUID | None
    root_scope_key: str
    integration_epoch: int
    delivery_mode: DeliveryMode
    manifest: dict[str, Any]
    manifest_sha256: str
    coverage: dict[str, Any]
    items: tuple[ClaimedCompletionNotification, ...]
    claim_token: uuid.UUID
    attempt_count: int


def _normalized_base_values(notification: CompletionNotification) -> dict[str, Any]:
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
        "artifacts": list(notification.artifacts or []),
        "metadata": dict(notification.metadata or {}),
        "payload_rank": max(0, min(1000, int(notification.payload_rank))),
        "status": "pending",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }


def _cursor_id(tenant_id: uuid.UUID, parent_session_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_CURSOR_ID_NAMESPACE, f"{tenant_id}:{parent_session_id}")


async def _locked_mailbox_cursor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    parent_session_id: uuid.UUID,
) -> RuntimeResultMailboxCursor:
    stmt = insert(RuntimeResultMailboxCursor).values(
        id=_cursor_id(tenant_id, parent_session_id),
        tenant_id=tenant_id,
        parent_session_id=parent_session_id,
        next_mailbox_sequence=1,
        next_integration_epoch=1,
        last_prepared_sequence=0,
        last_delivered_sequence=0,
        version=1,
    )
    # Concurrent first writers use the same deterministic primary key and the
    # same parent unique key.  PostgreSQL may report either conflict first, so
    # the insert must tolerate every uniqueness conflict before locking the
    # single canonical cursor row below.
    await db.execute(stmt.on_conflict_do_nothing())
    return (
        await db.execute(
            select(RuntimeResultMailboxCursor)
            .where(
                RuntimeResultMailboxCursor.tenant_id == tenant_id,
                RuntimeResultMailboxCursor.parent_session_id == parent_session_id,
            )
            .with_for_update()
        )
    ).scalar_one()


async def _source_runtime_task(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_run_id: str,
) -> RuntimeTask | None:
    try:
        task_id = uuid.UUID(str(source_run_id))
    except (TypeError, ValueError):
        return None
    return (
        await db.execute(
            select(RuntimeTask).where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_id).with_for_update()
        )
    ).scalar_one_or_none()


def _settle_runtime_task_completion_outbox(task: RuntimeTask) -> None:
    task.completion_outbox_settled_at = datetime.now(UTC)
    task.completion_outbox_last_error = None


def _hold_runtime_task_completion_outbox(task: RuntimeTask, *, reason: str, attempted_at: datetime) -> None:
    task.completion_outbox_attempted_at = attempted_at
    task.completion_outbox_attempt_count = int(task.completion_outbox_attempt_count or 0) + 1
    task.completion_outbox_last_error = reason[:100]


def _explicit_root_runtime_task_id(notification: CompletionNotification) -> uuid.UUID | None:
    raw_value = notification.root_runtime_task_id or (notification.metadata or {}).get("root_runtime_task_id")
    if raw_value is None:
        return None
    try:
        return _uuid(raw_value, field="root_runtime_task_id")
    except ValueError:
        return None


def _routing_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key in _ROUTING_METADATA_KEYS}


_COMPLETION_SOURCE_KIND_BY_TASK_TYPE = {
    "team_member": "agent_team",
    "delegation": "a2a_delegation",
    "a2a_delegation": "a2a_delegation",
    "approval_execution": "approval",
}


@dataclass(frozen=True, slots=True)
class _CompletionRoute:
    target_session_id: uuid.UUID
    parent_agent_id: uuid.UUID
    owner_id: uuid.UUID


async def _resolve_completion_route(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    metadata: dict[str, Any],
    attempted_at: datetime,
) -> _CompletionRoute | None:
    """Resolve (target session, parent agent, owner) for a terminal task.

    Routing authority differs by task type. For ``a2a_continuation``, the
    return route and owner come entirely from durable bindings: the run is
    owned by the child agent (``task.parent_agent_id`` == child agent on the
    child session), so the route is derived from the durable child
    ChatSession bound by tenant + ``task.parent_session_id`` +
    ``task.parent_agent_id``, then validated against the parent ChatSession
    and its owner; metadata plays no part in that path. For existing
    (non-``a2a_continuation``) types, parent-agent authority is
    ``task.parent_agent_id`` (metadata can never override it), while the
    legacy target-session/owner metadata fallback remains: the target
    session may still come from ``metadata.parent_session_id`` and the
    owner from ``metadata.user_id``/``metadata.owner_id`` before falling
    back to the task row and the parent session. Every failure is a typed,
    retryable hold on the task row; returns None after holding.
    """

    if task.task_type == "a2a_continuation":
        try:
            run_session_id = _uuid(task.parent_session_id, field="parent_session_id")
        except ValueError:
            _hold_runtime_task_completion_outbox(
                task,
                reason="parent_session_id_invalid",
                attempted_at=attempted_at,
            )
            return None
        child_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == run_session_id,
                    ChatSession.tenant_id == task.tenant_id,
                    ChatSession.agent_id == task.parent_agent_id,
                )
            )
        ).scalar_one_or_none()
        if child_session is None:
            _hold_runtime_task_completion_outbox(
                task,
                reason="child_session_not_found",
                attempted_at=attempted_at,
            )
            return None
        target_value: Any = child_session.parent_session_id
        parent_agent_value: Any = child_session.peer_agent_id
    else:
        target_value = (
            task.child_session_id
            if task.task_type == "trigger"
            else metadata.get("parent_session_id") or task.parent_session_id
        )
        parent_agent_value = task.parent_agent_id
    try:
        target_session_id = _uuid(target_value, field="parent_session_id")
    except ValueError:
        _hold_runtime_task_completion_outbox(
            task,
            reason="parent_session_id_invalid",
            attempted_at=attempted_at,
        )
        return None
    try:
        parent_agent_id = _uuid(parent_agent_value, field="parent_agent_id")
    except ValueError:
        _hold_runtime_task_completion_outbox(
            task,
            reason="parent_agent_id_invalid",
            attempted_at=attempted_at,
        )
        return None
    parent_session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == target_session_id,
                ChatSession.agent_id == parent_agent_id,
                ChatSession.tenant_id == task.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if parent_session is None:
        _hold_runtime_task_completion_outbox(
            task,
            reason="parent_session_not_found",
            attempted_at=attempted_at,
        )
        return None
    if task.task_type == "a2a_continuation":
        owner_value: Any = parent_session.user_id
    else:
        owner_value = metadata.get("user_id") or metadata.get("owner_id") or parent_session.user_id
    try:
        owner_id = _uuid(owner_value, field="parent_user_id")
    except ValueError:
        _hold_runtime_task_completion_outbox(
            task,
            reason="parent_user_id_invalid",
            attempted_at=attempted_at,
        )
        return None
    return _CompletionRoute(
        target_session_id=target_session_id,
        parent_agent_id=parent_agent_id,
        owner_id=owner_id,
    )


def _completion_outbox_eligible(task: RuntimeTask) -> bool:
    """Python mirror of the eligibility shape in ``COMPLETION_OUTBOX_PENDING_SQL``."""

    return (
        getattr(task, "completion_outbox_generation", None) is not None
        and str(task.task_type or "") in COMPLETION_OUTBOX_TASK_TYPES
        and str(task.status or "") in COMPLETION_OUTBOX_TERMINAL_STATUSES
        and not (task.task_type == "trigger" and str(task.status or "") == "skipped")
        and task.parent_agent_id is not None
    )


async def produce_terminal_task_completion_notification(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    attempted_at: datetime,
    reconciled: bool,
) -> uuid.UUID | None:
    """Shared durable completion producer for an eligible terminal RuntimeTask.

    The current direct same-transaction normal caller is the
    ``a2a_continuation`` branch of the web-chat terminal seam
    (``web_chat_runtime._apply_terminal_task_update_and_settle``): the
    enqueue happens inside the SAME transaction as the terminal RuntimeTask
    write, so a rollback undoes both. The reconcile sweep re-enters this
    helper as the idempotent crash/legacy recovery lane for eligible
    terminal rows, with a lower payload rank (10) so an authoritative
    normal-path payload (100) always wins the CAS. Other eligible types
    retain their existing producer/recovery behavior (e.g. ``team_member``
    has its own richer producer); no claim is made here that every task
    type has a normal producer through this helper. Returns the outbox id, or None when
    the task is ineligible or a typed, retryable hold was recorded on the
    task row.
    """

    if not _completion_outbox_eligible(task):
        return None
    metadata = dict(task.metadata_json or {})
    route = await _resolve_completion_route(db, task, metadata=metadata, attempted_at=attempted_at)
    if route is None:
        return None
    child_session_id = None
    if task.task_type != "trigger" and task.child_session_id:
        try:
            child_session_id = _uuid(task.child_session_id, field="child_session_id")
        except ValueError:
            child_session_id = None
    return await enqueue_completion_notification(
        db,
        CompletionNotification(
            tenant_id=task.tenant_id,
            source_kind=_COMPLETION_SOURCE_KIND_BY_TASK_TYPE.get(task.task_type, task.task_type),
            source_run_id=str(task.id),
            parent_session_id=route.target_session_id,
            parent_agent_id=route.parent_agent_id,
            parent_user_id=route.owner_id,
            child_session_id=child_session_id,
            child_agent_name=task.child_agent_name,
            terminal_status=str(task.status),
            task_type=task.task_type,
            summary=str(task.result_summary or f"{task.task_type} finished with status {task.status}."),
            delivery_mode=("session_projection" if task.task_type == "trigger" else "parent_continuation"),
            artifacts=list(metadata.get("artifacts") or []),
            metadata={
                **metadata,
                **(
                    {
                        "model_context": (
                            "[Approval tool result]\n"
                            f"Approval: {metadata.get('approval_id') or 'unknown'}\n"
                            f"Tool: {metadata.get('tool_name') or 'approved_action'}\n"
                            f"Result: {str(task.result_summary or '')}\n"
                            "Continue the original task from this approved tool result."
                        )
                    }
                    if task.task_type == "approval_execution"
                    else {}
                ),
                **({"reconciled_from_terminal_runtime_task": True} if reconciled else {}),
            },
            payload_rank=10 if reconciled else 100,
        ),
    )


async def enqueue_completion_notification(
    db: AsyncSession,
    notification: CompletionNotification,
) -> uuid.UUID:
    """Commit complete bytes once and enqueue only a hash-pinned reference.

    The per-parent cursor is locked before the deterministic outbox identity is
    inspected, so concurrent duplicate returns cannot allocate two mailbox
    sequences or overwrite a newer payload revision.
    """

    values = _normalized_base_values(notification)
    tenant_id = values["tenant_id"]
    parent_session_id = values["parent_session_id"]
    cursor = await _locked_mailbox_cursor(db, tenant_id=tenant_id, parent_session_id=parent_session_id)
    existing = (
        await db.execute(
            select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == values["id"]).with_for_update()
        )
    ).scalar_one_or_none()
    payload_applied = existing is None or int(values["payload_rank"]) > int(existing.payload_rank or 0)
    if existing is not None and not payload_applied:
        # Equal/lower-ranked late producers cannot mutate the accepted binding
        # and must not create unreachable immutable payload objects.
        task = await _source_runtime_task(db, tenant_id=tenant_id, source_run_id=values["source_run_id"])
        if task is not None:
            _settle_runtime_task_completion_outbox(task)
            await db.flush()
        return existing.id

    encoded = encode_runtime_result_payload(
        summary=values["summary"],
        artifacts=values["artifacts"],
        metadata=values["metadata"],
    )
    result_id = runtime_result_object_id(
        tenant_id=tenant_id,
        source_kind=values["source_kind"],
        source_run_id=values["source_run_id"],
        sha256=encoded.sha256,
    )
    result_ref = runtime_result_ref(result_id=result_id, sha256=encoded.sha256)
    result_stmt = insert(RuntimeResultObject).values(
        id=result_id,
        tenant_id=tenant_id,
        source_kind=values["source_kind"],
        source_run_id=values["source_run_id"],
        payload_schema="hive.runtime_result.v1",
        payload_bytes=encoded.payload_bytes,
        sha256=encoded.sha256,
        size_bytes=encoded.size_bytes,
        media_type="application/json",
        encoding="utf-8",
    )
    await db.execute(result_stmt.on_conflict_do_nothing(constraint="uq_runtime_result_objects_source_hash"))

    task = await _source_runtime_task(db, tenant_id=tenant_id, source_run_id=values["source_run_id"])
    root_runtime_task_id = _explicit_root_runtime_task_id(notification)
    if root_runtime_task_id is None and task is not None:
        root_runtime_task_id = task.root_runtime_task_id or task.id
    routing_metadata = _routing_metadata(values["metadata"])

    if existing is None:
        mailbox_sequence = int(cursor.next_mailbox_sequence)
        cursor.next_mailbox_sequence = mailbox_sequence + 1
        cursor.version = int(cursor.version or 0) + 1
        db.add(
            RuntimeNotificationOutbox(
                id=values["id"],
                tenant_id=tenant_id,
                source_kind=values["source_kind"],
                source_run_id=values["source_run_id"],
                parent_session_id=parent_session_id,
                parent_agent_id=values["parent_agent_id"],
                parent_user_id=values["parent_user_id"],
                child_session_id=values["child_session_id"],
                child_agent_name=values["child_agent_name"],
                terminal_status=values["terminal_status"],
                task_type=values["task_type"],
                root_runtime_task_id=root_runtime_task_id,
                result_object_id=result_id,
                result_ref=result_ref,
                result_sha256=encoded.sha256,
                result_size_bytes=encoded.size_bytes,
                artifact_count=len(values["artifacts"]),
                mailbox_sequence=mailbox_sequence,
                delivery_mode=values["delivery_mode"],
                metadata_json=routing_metadata,
                payload_rank=values["payload_rank"],
                status="pending",
                attempt_count=0,
                available_at=values["available_at"],
            )
        )
    elif payload_applied:
        existing.parent_agent_id = values["parent_agent_id"]
        existing.parent_user_id = values["parent_user_id"]
        existing.child_session_id = values["child_session_id"]
        existing.child_agent_name = values["child_agent_name"]
        existing.task_type = values["task_type"]
        existing.root_runtime_task_id = root_runtime_task_id
        existing.result_object_id = result_id
        existing.result_ref = result_ref
        existing.result_sha256 = encoded.sha256
        existing.result_size_bytes = encoded.size_bytes
        existing.artifact_count = len(values["artifacts"])
        existing.delivery_mode = values["delivery_mode"]
        existing.metadata_json = routing_metadata
        existing.payload_rank = values["payload_rank"]
        existing.status = "pending"
        existing.attempt_count = 0
        existing.available_at = values["available_at"]
        existing.integration_page_id = None
        existing.claim_token = None
        existing.lease_expires_at = None
        existing.locked_by = None
        existing.locked_at = None
        existing.last_error = None
        existing.delivery_receipt_json = None
        existing.delivered_at = None

    if task is not None and payload_applied:
        _settle_runtime_task_completion_outbox(task)
        task_metadata = dict(task.metadata_json or {})
        task_metadata["runtime_result_ref"] = result_ref
        task_metadata["runtime_result_sha256"] = encoded.sha256
        task_metadata["runtime_result_size_bytes"] = encoded.size_bytes
        task.metadata_json = task_metadata
        task.result_summary = (
            f"Durable result committed: ref={result_ref} sha256={encoded.sha256} bytes={encoded.size_bytes}."
        )
        if task.root_runtime_task_id is not None:
            from app.services.runtime_root_ledger import RUNTIME_ROOT_STATES, transition_runtime_root_item_by_task

            requested_state = str(task.status or "needs_reconciliation")
            if requested_state in RUNTIME_ROOT_STATES:
                await transition_runtime_root_item_by_task(
                    db,
                    runtime_task_id=task.id,
                    requested_state=requested_state,
                    result_refs=[result_ref],
                    metadata={"runtime_result_sha256": encoded.sha256},
                )
    await db.flush()
    return values["id"]


def _claimed(row: RuntimeNotificationOutbox) -> ClaimedCompletionNotification:
    if row.claim_token is None:
        raise RuntimeError("claimed completion notification is missing its claim token")
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
        root_runtime_task_id=row.root_runtime_task_id,
        result_object_id=row.result_object_id,
        result_ref=row.result_ref,
        result_sha256=row.result_sha256,
        result_size_bytes=int(row.result_size_bytes),
        artifact_count=int(row.artifact_count or 0),
        mailbox_sequence=int(row.mailbox_sequence),
        claim_token=row.claim_token,
        integration_page_id=row.integration_page_id,
        child_session_id=row.child_session_id,
        child_agent_name=row.child_agent_name,
        delivery_mode=row.delivery_mode,  # type: ignore[arg-type]
        metadata=dict(row.metadata_json or {}),
        attempt_count=int(row.attempt_count or 0),
    )


def _result_descriptor(item: ClaimedCompletionNotification) -> RuntimeResultDescriptor:
    return RuntimeResultDescriptor(
        outbox_id=item.id,
        mailbox_sequence=item.mailbox_sequence,
        source_kind=item.source_kind,
        source_run_id=item.source_run_id,
        task_type=item.task_type,
        terminal_status=item.terminal_status,
        child_session_id=item.child_session_id,
        child_agent_name=item.child_agent_name,
        result_ref=item.result_ref,
        result_sha256=item.result_sha256,
        result_size_bytes=item.result_size_bytes,
        artifact_count=item.artifact_count,
    )


def _root_scope_key(*, parent_session_id: uuid.UUID, root_runtime_task_id: uuid.UUID | None) -> str:
    if root_runtime_task_id is not None:
        return f"runtime-root:{root_runtime_task_id}"
    return f"parent-session:{parent_session_id}"


def _integration_page_id(
    *,
    tenant_id: uuid.UUID,
    parent_session_id: uuid.UUID,
    page: RuntimeResultIntegrationPageValue,
    manifest_sha256: str,
) -> uuid.UUID:
    # Preserve the historical one-notification causation identity while giving
    # multi-result pages their own deterministic integration identity.
    if len(page.items) == 1:
        return page.items[0].outbox_id
    return uuid.uuid5(
        _PAGE_ID_NAMESPACE,
        f"{tenant_id}:{parent_session_id}:{page.integration_epoch}:{manifest_sha256}",
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
        receipt["continuation_error"] = error
    else:
        receipt.pop("continuation_error", None)
    approval.execution_receipt = receipt


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
        integration_page_item_limit: int = 25,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._deferred_retry_seconds = max(0, int(deferred_retry_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._integration_page_item_limit = max(1, int(integration_page_item_limit))

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
        lease_expires_at = current + timedelta(seconds=self._lease_seconds)
        effective_limit = max(1, int(limit))
        first_observed: list[tuple[str, int]] = []
        async with self._worker_session("claim") as db:
            claimed_rows: list[RuntimeNotificationOutbox] = []
            page_limit = max(
                1, (effective_limit + self._integration_page_item_limit - 1) // self._integration_page_item_limit
            )
            pages = list(
                (
                    await db.execute(
                        select(RuntimeResultIntegrationPage)
                        .where(
                            or_(
                                and_(
                                    RuntimeResultIntegrationPage.status == "prepared",
                                    exists(
                                        select(RuntimeNotificationOutbox.id).where(
                                            RuntimeNotificationOutbox.integration_page_id
                                            == RuntimeResultIntegrationPage.id,
                                            RuntimeNotificationOutbox.status == "pending",
                                            RuntimeNotificationOutbox.available_at <= current,
                                        )
                                    ),
                                ),
                                and_(
                                    RuntimeResultIntegrationPage.status == "processing",
                                    or_(
                                        RuntimeResultIntegrationPage.lease_expires_at.is_(None),
                                        RuntimeResultIntegrationPage.lease_expires_at <= current,
                                    ),
                                ),
                            )
                        )
                        .order_by(
                            RuntimeResultIntegrationPage.created_at, RuntimeResultIntegrationPage.integration_epoch
                        )
                        .limit(page_limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for page in pages:
                page.status = "processing"
                page.claim_token = uuid.uuid4()
                page.claimed_by = str(worker_id)
                page.lease_expires_at = lease_expires_at
                page.attempt_count = int(page.attempt_count or 0) + 1
                page_rows = list(
                    (
                        await db.execute(
                            select(RuntimeNotificationOutbox)
                            .where(
                                RuntimeNotificationOutbox.integration_page_id == page.id,
                                RuntimeNotificationOutbox.status.in_(("pending", "processing")),
                            )
                            .order_by(RuntimeNotificationOutbox.mailbox_sequence)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in page_rows:
                    if int(row.attempt_count or 0) == 0:
                        first_observed.append((row.source_kind, int(row.result_size_bytes or 0)))
                    row.status = "processing"
                    row.locked_by = str(worker_id)
                    row.locked_at = current
                    row.claim_token = uuid.uuid4()
                    row.lease_expires_at = lease_expires_at
                    row.attempt_count = int(row.attempt_count or 0) + 1
                    await _set_approval_continuation_status(db, row, status="continuing")
                claimed_rows.extend(page_rows)

            remaining = max(0, effective_limit - len(claimed_rows))
            if remaining:
                rows = list(
                    (
                        await db.execute(
                            select(RuntimeNotificationOutbox)
                            .where(
                                RuntimeNotificationOutbox.integration_page_id.is_(None),
                                or_(
                                    and_(
                                        RuntimeNotificationOutbox.status == "pending",
                                        RuntimeNotificationOutbox.available_at <= current,
                                    ),
                                    and_(
                                        RuntimeNotificationOutbox.status == "processing",
                                        or_(
                                            RuntimeNotificationOutbox.lease_expires_at.is_(None),
                                            RuntimeNotificationOutbox.lease_expires_at <= current,
                                        ),
                                    ),
                                ),
                            )
                            .order_by(
                                RuntimeNotificationOutbox.parent_session_id,
                                RuntimeNotificationOutbox.mailbox_sequence,
                            )
                            .limit(remaining)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                rows = []
            for row in rows:
                if int(row.attempt_count or 0) == 0:
                    first_observed.append((row.source_kind, int(row.result_size_bytes or 0)))
                claim_token = uuid.uuid4()
                row.status = "processing"
                row.locked_by = str(worker_id)
                row.locked_at = current
                row.claim_token = claim_token
                row.lease_expires_at = lease_expires_at
                row.attempt_count = int(row.attempt_count or 0) + 1
                await _set_approval_continuation_status(db, row, status="continuing")
            claimed_rows.extend(rows)
            await db.commit()
            claimed = [_claimed(row) for row in claimed_rows]
        for source_kind, size_bytes in first_observed:
            record_runtime_result_observed(source_kind=source_kind, size_bytes=size_bytes)
        return claimed

    async def reconcile_terminal_tasks_once(self, *, limit: int = 100) -> int:
        """Idempotent crash/legacy recovery for a missing completion intent.

        This sweep is ONLY the recovery lane for eligible terminal rows: it
        repairs crash-shaped or legacy terminal rows by re-entering the same
        shared producer (``produce_terminal_task_completion_notification``)
        with a lower payload rank, and stays idempotent across replays. The
        A2A continuation normal producer is atomic instead — it enqueues
        inside the same transaction as the terminal RuntimeTask write at the
        web-chat terminal seam — so a healthy ``a2a_continuation`` run leaves
        this sweep nothing to repair. Other task types may keep their own
        normal producers; this docstring does not claim every normal producer
        uses the shared helper.
        """

        attempted_at = datetime.now(UTC)
        retry_before = attempted_at - timedelta(seconds=COMPLETION_OUTBOX_RETRY_SECONDS)
        async with self._worker_session("reconcile_terminal_tasks") as db:
            tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask)
                        .where(
                            text(COMPLETION_OUTBOX_PENDING_SQL),
                            or_(
                                RuntimeTask.completion_outbox_attempted_at.is_(None),
                                RuntimeTask.completion_outbox_attempted_at <= retry_before,
                            ),
                        )
                        .order_by(
                            RuntimeTask.completion_outbox_attempted_at.is_not(None).asc(),
                            RuntimeTask.completion_outbox_attempted_at.asc(),
                            RuntimeTask.created_at.asc(),
                        )
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            repaired = 0
            for task in tasks:
                existing_outbox_id = await db.scalar(
                    select(RuntimeNotificationOutbox.id)
                    .where(
                        RuntimeNotificationOutbox.tenant_id == task.tenant_id,
                        RuntimeNotificationOutbox.source_run_id == str(task.id),
                    )
                    .limit(1)
                )
                if existing_outbox_id is not None:
                    # A previous app revision may have committed the outbox
                    # during a rolling deploy without knowing this settlement
                    # ledger. The delivery intent is already durable; remove
                    # the task from the recovery index without duplicating it.
                    _settle_runtime_task_completion_outbox(task)
                    repaired += 1
                    continue
                outbox_id = await produce_terminal_task_completion_notification(
                    db,
                    task,
                    attempted_at=attempted_at,
                    reconciled=True,
                )
                if outbox_id is None:
                    # Ineligible (defense in depth; the SQL pre-filters) or a
                    # typed, retryable hold was recorded on the task row.
                    continue
                if task.task_type == "approval_execution":
                    await db.flush()
                    continuation_row = await db.get(RuntimeNotificationOutbox, outbox_id)
                    if continuation_row is not None:
                        await _set_approval_continuation_status(
                            db,
                            continuation_row,
                            status="queued",
                        )
                repaired += 1
            await db.commit()
            return repaired

    async def _mark_delivered(
        self,
        *,
        item_id: uuid.UUID,
        worker_id: str,
        claim_token: uuid.UUID,
        receipt: dict[str, Any],
    ) -> bool:
        now = datetime.now(UTC)
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.status != "processing"
                or row.locked_by != worker_id
                or row.claim_token != claim_token
            ):
                return False
            row.status = "delivered"
            row.delivered_at = now
            row.delivery_receipt_json = receipt
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            row.claim_token = None
            row.lease_expires_at = None
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
            if (
                row is None
                or row.status != "processing"
                or row.locked_by != worker_id
                or row.claim_token != item.claim_token
            ):
                return "stale"
            row.last_error = f"{type(error).__name__}: {error}"
            row.locked_by = None
            row.locked_at = None
            row.claim_token = None
            row.lease_expires_at = None
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
            await _set_approval_continuation_status(
                db,
                row,
                status="needs_reconciliation" if outcome == "dead_letter" else "retrying",
                error=row.last_error,
            )
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
            if (
                row is None
                or row.status != "processing"
                or row.locked_by != worker_id
                or row.claim_token != item.claim_token
            ):
                return False
            row.status = "pending"
            row.available_at = datetime.now(UTC) + timedelta(seconds=self._deferred_retry_seconds)
            row.last_error = reason
            row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
            row.locked_by = None
            row.locked_at = None
            row.claim_token = None
            row.lease_expires_at = None
            await _set_approval_continuation_status(db, row, status="queued", error=reason)
            await db.commit()
            return True

    async def prepare_integration_pages(
        self,
        *,
        worker_id: str,
        claimed: list[ClaimedCompletionNotification],
    ) -> list[ClaimedResultIntegrationPage]:
        """Bind claimed ref-only rows to durable, ordered parent wake pages."""

        if not claimed:
            return []
        claimed_by_id = {item.id: item for item in claimed}
        if len(claimed_by_id) != len(claimed):
            raise RuntimeError("duplicate completion outbox identity in claimed batch")
        current = datetime.now(UTC)
        lease_expires_at = current + timedelta(seconds=self._lease_seconds)
        prepared: list[ClaimedResultIntegrationPage] = []
        newly_prepared: list[tuple[str, int]] = []
        async with self._worker_session("prepare_integration_pages") as db:
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(RuntimeNotificationOutbox.id.in_(tuple(claimed_by_id)))
                        .order_by(
                            RuntimeNotificationOutbox.tenant_id,
                            RuntimeNotificationOutbox.parent_session_id,
                            RuntimeNotificationOutbox.mailbox_sequence,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            valid_rows: list[RuntimeNotificationOutbox] = []
            for row in rows:
                item = claimed_by_id[row.id]
                if row.status == "processing" and row.locked_by == worker_id and row.claim_token == item.claim_token:
                    valid_rows.append(row)

            existing_page_ids = sorted(
                {row.integration_page_id for row in valid_rows if row.integration_page_id is not None},
                key=str,
            )
            for page_id in existing_page_ids:
                page = (
                    await db.execute(
                        select(RuntimeResultIntegrationPage)
                        .where(RuntimeResultIntegrationPage.id == page_id)
                        .with_for_update()
                    )
                ).scalar_one()
                page_rows = [row for row in valid_rows if row.integration_page_id == page_id]
                manifest_ids = {
                    uuid.UUID(str(item["outbox_id"]))
                    for item in list((page.manifest_json or {}).get("items") or [])
                    if isinstance(item, dict) and item.get("outbox_id")
                }
                if manifest_ids != {row.id for row in page_rows}:
                    raise RuntimeError("integration page manifest does not match its claimed outbox rows")
                if page.status != "processing" or page.claimed_by != worker_id or page.claim_token is None:
                    raise RuntimeError("integration page claim fence is stale")
                prepared.append(
                    ClaimedResultIntegrationPage(
                        id=page.id,
                        tenant_id=page.tenant_id,
                        parent_session_id=page.parent_session_id,
                        parent_agent_id=page.parent_agent_id,
                        parent_user_id=page.parent_user_id,
                        root_runtime_task_id=page.root_runtime_task_id,
                        root_scope_key=page.root_scope_key,
                        integration_epoch=int(page.integration_epoch),
                        delivery_mode=page.delivery_mode,  # type: ignore[arg-type]
                        manifest=dict(page.manifest_json or {}),
                        manifest_sha256=page.manifest_sha256,
                        coverage=dict(page.coverage_json or {}),
                        items=tuple(_claimed(row) for row in page_rows),
                        claim_token=page.claim_token,
                        attempt_count=int(page.attempt_count or 0),
                    )
                )

            new_rows = sorted(
                (row for row in valid_rows if row.integration_page_id is None),
                key=lambda row: (
                    row.tenant_id.hex,
                    row.parent_session_id.hex,
                    int(row.mailbox_sequence),
                    row.id.hex,
                ),
            )
            # Root/budget boundaries may not reorder a parent's mailbox.  Use
            # contiguous runs rather than aggregating all rows for one root:
            # A1, B2, A3 must remain three epochs, never [A1,A3] then [B2].
            grouped_rows: list[tuple[tuple[Any, ...], list[RuntimeNotificationOutbox]]] = []
            for row in new_rows:
                group_key = (
                    row.tenant_id,
                    row.parent_session_id,
                    row.parent_agent_id,
                    row.parent_user_id,
                    row.root_runtime_task_id,
                    row.delivery_mode,
                    str((row.metadata_json or {}).get("budget_run_id") or ""),
                )
                if not grouped_rows or grouped_rows[-1][0] != group_key:
                    grouped_rows.append((group_key, [row]))
                else:
                    grouped_rows[-1][1].append(row)

            from app.services.runtime_root_ledger import read_runtime_root_coverage

            for group_key, group_rows in grouped_rows:
                (
                    tenant_id,
                    parent_session_id,
                    parent_agent_id,
                    parent_user_id,
                    root_runtime_task_id,
                    delivery_mode,
                    _budget_run_id,
                ) = group_key
                group_rows.sort(key=lambda row: (int(row.mailbox_sequence), str(row.id)))
                cursor = await _locked_mailbox_cursor(
                    db,
                    tenant_id=tenant_id,
                    parent_session_id=parent_session_id,
                )
                if root_runtime_task_id is not None:
                    coverage = (
                        await read_runtime_root_coverage(
                            db,
                            root_runtime_task_id=root_runtime_task_id,
                        )
                    ).to_dict()
                else:
                    terminal_count = len(group_rows)
                    coverage = {
                        "requested": terminal_count,
                        "admitted": terminal_count,
                        "deferred": 0,
                        "not_admitted": 0,
                        "expected": terminal_count,
                        "terminal": terminal_count,
                        "running": 0,
                        "waiting_approval": 0,
                        "conserved": True,
                    }
                value_pages = build_runtime_result_integration_pages(
                    (_result_descriptor(_claimed(row)) for row in group_rows),
                    page_item_limit=self._integration_page_item_limit,
                    starting_epoch=int(cursor.next_integration_epoch),
                    root_runtime_task_id=root_runtime_task_id,
                    coverage=coverage,
                )
                row_by_id = {row.id: row for row in group_rows}
                for value_page in value_pages:
                    manifest = value_page.to_manifest()
                    manifest_sha256 = str(manifest["manifest_sha256"])
                    page_id = _integration_page_id(
                        tenant_id=tenant_id,
                        parent_session_id=parent_session_id,
                        page=value_page,
                        manifest_sha256=manifest_sha256,
                    )
                    if await db.get(RuntimeResultIntegrationPage, page_id) is not None:
                        # The first single-item page preserves the historical
                        # outbox causation id.  A later higher-rank revision of
                        # that same result needs a distinct immutable epoch.
                        page_id = uuid.uuid5(
                            _PAGE_ID_NAMESPACE,
                            f"{tenant_id}:{parent_session_id}:{value_page.integration_epoch}:revision:{manifest_sha256}",
                        )
                    page_claim_token = uuid.uuid4()
                    page = RuntimeResultIntegrationPage(
                        id=page_id,
                        tenant_id=tenant_id,
                        parent_session_id=parent_session_id,
                        parent_agent_id=parent_agent_id,
                        parent_user_id=parent_user_id,
                        root_runtime_task_id=root_runtime_task_id,
                        root_scope_key=_root_scope_key(
                            parent_session_id=parent_session_id,
                            root_runtime_task_id=root_runtime_task_id,
                        ),
                        integration_epoch=value_page.integration_epoch,
                        delivery_mode=delivery_mode,
                        mailbox_sequence_start=value_page.items[0].mailbox_sequence,
                        mailbox_sequence_end=value_page.items[-1].mailbox_sequence,
                        item_count=len(value_page.items),
                        manifest_json=manifest,
                        manifest_sha256=manifest_sha256,
                        coverage_json=dict(coverage),
                        status="processing",
                        claim_token=page_claim_token,
                        claimed_by=worker_id,
                        lease_expires_at=lease_expires_at,
                        attempt_count=1,
                    )
                    db.add(page)
                    # The outbox FK is assigned below without an ORM
                    # relationship, so make the referenced page visible before
                    # SQLAlchemy flushes the row updates.
                    await db.flush()
                    page_rows: list[RuntimeNotificationOutbox] = []
                    for descriptor in value_page.items:
                        row = row_by_id[descriptor.outbox_id]
                        row.integration_page_id = page_id
                        page_rows.append(row)
                    cursor.last_prepared_sequence = max(
                        int(cursor.last_prepared_sequence or 0),
                        int(value_page.items[-1].mailbox_sequence),
                    )
                    prepared.append(
                        ClaimedResultIntegrationPage(
                            id=page_id,
                            tenant_id=tenant_id,
                            parent_session_id=parent_session_id,
                            parent_agent_id=parent_agent_id,
                            parent_user_id=parent_user_id,
                            root_runtime_task_id=root_runtime_task_id,
                            root_scope_key=page.root_scope_key,
                            integration_epoch=value_page.integration_epoch,
                            delivery_mode=delivery_mode,  # type: ignore[arg-type]
                            manifest=manifest,
                            manifest_sha256=manifest_sha256,
                            coverage=dict(coverage),
                            items=tuple(_claimed(row) for row in page_rows),
                            claim_token=page_claim_token,
                            attempt_count=1,
                        )
                    )
                    newly_prepared.append((delivery_mode, len(page_rows)))
                cursor.next_integration_epoch = int(cursor.next_integration_epoch) + len(value_pages)
                cursor.version = int(cursor.version or 0) + 1
            await db.commit()
        for delivery_mode, item_count in newly_prepared:
            record_runtime_result_page(
                delivery_mode=delivery_mode,
                outcome="prepared",
                item_count=item_count,
            )
        return sorted(prepared, key=lambda page: (page.parent_session_id.hex, page.integration_epoch))

    async def _deliver_page(self, page: ClaimedResultIntegrationPage) -> dict[str, Any]:
        from app.services.agent_session_continuation import continue_parent_session_with_result_page

        async with tenant_scoped_session(
            page.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="runtime_result_page_sequence_preflight",
        ) as sequence_db:
            prior_pending_page = (
                await sequence_db.execute(
                    select(RuntimeResultIntegrationPage.id)
                    .where(
                        RuntimeResultIntegrationPage.tenant_id == page.tenant_id,
                        RuntimeResultIntegrationPage.parent_session_id == page.parent_session_id,
                        RuntimeResultIntegrationPage.integration_epoch < page.integration_epoch,
                        RuntimeResultIntegrationPage.status.in_(("prepared", "processing")),
                    )
                    .order_by(RuntimeResultIntegrationPage.integration_epoch)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if prior_pending_page is not None:
            raise CompletionDeliveryDeferred("prior_integration_page_pending")

        if page.delivery_mode == "parent_continuation" and any(
            item.task_type == "agent_team_close" for item in page.items
        ):
            from app.services.web_chat_runtime import get_active_web_chat_run

            async with tenant_scoped_session(
                page.tenant_id,
                session_factory=self._session_factory,
                require_tenant=True,
                source="runtime_result_page_team_close_preflight",
            ) as preflight_db:
                active_run = await get_active_web_chat_run(
                    db=preflight_db,
                    agent_id=page.parent_agent_id,
                    session_id=page.parent_session_id,
                )
            if active_run is not None:
                raise CompletionDeliveryDeferred("parent_session_active")

        budget_run_ids = {
            str(item.metadata.get("budget_run_id") or "").strip()
            for item in page.items
            if str(item.metadata.get("budget_run_id") or "").strip()
        }
        if len(budget_run_ids) > 1:
            raise RuntimeError("one integration page cannot span multiple runtime budget roots")
        admission: ExecutionAdmission | None = None
        admission_decision: ExecutionAdmissionDecision | None = None
        budget_run_id: uuid.UUID | None = None
        if page.delivery_mode == "parent_continuation" and budget_run_ids:
            try:
                budget_run_id = _uuid(next(iter(budget_run_ids)), field="budget_run_id")
            except ValueError:
                budget_run_id = None
            if budget_run_id is not None:
                admission = ExecutionAdmission(RuntimeBudgetService(session_factory=self._session_factory))
                admission_decision = await admission.admit(
                    RuntimeBudgetReservation(
                        budget_run_id=budget_run_id,
                        reservation_key=f"runtime_result_page:{page.id}:continuation",
                        continuation_wakes=1,
                        reason="runtime_result_page_parent_continuation",
                        metadata={
                            "integration_page_id": str(page.id),
                            "integration_epoch": page.integration_epoch,
                            "root_scope_key": page.root_scope_key,
                            "item_count": len(page.items),
                        },
                    )
                )
                if admission_decision.waiting:
                    raise CompletionDeliveryDeferred("runtime_budget_approval_required")

        async with tenant_scoped_session(
            page.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="runtime_result_page_delivery",
        ) as db:
            existing = (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == page.parent_session_id,
                        ChatTranscriptEvent.causation_id == page.id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if admission is not None and admission_decision is not None:
                    await admission.settle(
                        admission_decision,
                        actual_continuation_wakes=1,
                        reason="runtime_result_page_already_delivered",
                    )
                return {
                    "status": "already_delivered",
                    "event_id": str(existing.id),
                    "integration_page_id": str(page.id),
                    "deduplicated": True,
                }

            parent_session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == page.parent_session_id,
                        ChatSession.agent_id == page.parent_agent_id,
                        ChatSession.user_id == page.parent_user_id,
                        ChatSession.tenant_id == page.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            parent_agent = (
                await db.execute(
                    select(Agent).where(Agent.id == page.parent_agent_id, Agent.tenant_id == page.tenant_id)
                )
            ).scalar_one_or_none()
            owner = (
                await db.execute(select(User).where(User.id == page.parent_user_id, User.tenant_id == page.tenant_id))
            ).scalar_one_or_none()
            if parent_session is None or parent_agent is None or owner is None:
                raise RuntimeError("runtime result page target authority no longer resolves")

            receipt = await continue_parent_session_with_result_page(
                db=db,
                agent=parent_agent,
                user=owner,
                session=parent_session,
                integration_page_id=page.id,
                manifest=page.manifest,
                inherited_budget_run_id=budget_run_id,
                resume_parent=page.delivery_mode == "parent_continuation",
                # Transient admission-time claim fence: threaded, never
                # persisted into any durable authority marker.
                page_claim_token=page.claim_token,
            )
            delivered_event = (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == page.parent_session_id,
                        ChatTranscriptEvent.causation_id == page.id,
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            ).scalar_one_or_none()
            if admission is not None and admission_decision is not None:
                await admission.settle(
                    admission_decision,
                    actual_continuation_wakes=1,
                    reason="runtime_result_page_delivered",
                )
            return {
                **dict(receipt or {}),
                "event_id": str(delivered_event.id) if delivered_event is not None else None,
                "integration_page_id": str(page.id),
                "deduplicated": False,
            }

    async def _mark_page_delivered(
        self,
        *,
        page: ClaimedResultIntegrationPage,
        worker_id: str,
        receipt: dict[str, Any],
    ) -> int:
        now = datetime.now(UTC)
        async with self._worker_session("ack_integration_page") as db:
            stored_page = (
                await db.execute(
                    select(RuntimeResultIntegrationPage)
                    .where(RuntimeResultIntegrationPage.id == page.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                stored_page is None
                or stored_page.status != "processing"
                or stored_page.claimed_by != worker_id
                or stored_page.claim_token != page.claim_token
            ):
                return 0
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(RuntimeNotificationOutbox.integration_page_id == page.id)
                        .order_by(RuntimeNotificationOutbox.mailbox_sequence)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            item_tokens = {item.id: item.claim_token for item in page.items}
            if len(rows) != len(item_tokens) or any(
                row.status != "processing" or row.locked_by != worker_id or row.claim_token != item_tokens.get(row.id)
                for row in rows
            ):
                return 0
            stored_page.status = "delivered"
            stored_page.delivered_at = now
            stored_page.delivery_receipt_json = dict(receipt)
            stored_page.last_error = None
            stored_page.claimed_by = None
            stored_page.claim_token = None
            stored_page.lease_expires_at = None
            for row in rows:
                row.status = "delivered"
                row.delivered_at = now
                row.delivery_receipt_json = {
                    **dict(receipt),
                    "integration_page_id": str(page.id),
                    "mailbox_sequence": int(row.mailbox_sequence),
                }
                row.last_error = None
                row.locked_by = None
                row.locked_at = None
                row.claim_token = None
                row.lease_expires_at = None
                await _set_approval_continuation_status(db, row, status="delivered")
            cursor = await _locked_mailbox_cursor(
                db,
                tenant_id=page.tenant_id,
                parent_session_id=page.parent_session_id,
            )
            cursor.last_delivered_sequence = max(
                int(cursor.last_delivered_sequence or 0),
                int(stored_page.mailbox_sequence_end),
            )
            cursor.version = int(cursor.version or 0) + 1
            await db.commit()
            delivered_count = len(rows)
        record_runtime_result_page(
            delivery_mode=page.delivery_mode,
            outcome="delivered",
            item_count=delivered_count,
        )
        return delivered_count

    async def _mark_page_failed(
        self,
        *,
        page: ClaimedResultIntegrationPage,
        worker_id: str,
        error: Exception,
    ) -> tuple[str, int]:
        now = datetime.now(UTC)
        async with self._worker_session("retry_integration_page") as db:
            stored_page = (
                await db.execute(
                    select(RuntimeResultIntegrationPage)
                    .where(RuntimeResultIntegrationPage.id == page.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                stored_page is None
                or stored_page.status != "processing"
                or stored_page.claimed_by != worker_id
                or stored_page.claim_token != page.claim_token
            ):
                return "stale", 0
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(RuntimeNotificationOutbox.integration_page_id == page.id)
                        .order_by(RuntimeNotificationOutbox.mailbox_sequence)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            item_tokens = {item.id: item.claim_token for item in page.items}
            if any(row.claim_token != item_tokens.get(row.id) or row.locked_by != worker_id for row in rows):
                return "stale", 0
            error_text = f"{type(error).__name__}: {error}"
            dead_letter = int(stored_page.attempt_count or 0) >= self._max_attempts
            stored_page.status = "dead_letter" if dead_letter else "prepared"
            stored_page.last_error = error_text
            stored_page.claimed_by = None
            stored_page.claim_token = None
            stored_page.lease_expires_at = None
            delay = min(300, self._retry_base_seconds * (2 ** max(0, int(stored_page.attempt_count or 1) - 1)))
            for row in rows:
                row.status = "dead_letter" if dead_letter else "pending"
                row.available_at = now + timedelta(seconds=delay)
                row.last_error = error_text
                row.locked_by = None
                row.locked_at = None
                row.claim_token = None
                row.lease_expires_at = None
                await _set_approval_continuation_status(
                    db,
                    row,
                    status="needs_reconciliation" if dead_letter else "retrying",
                    error=error_text,
                )
                if dead_letter and row.source_kind == "agent_team" and row.task_type == "agent_team_close":
                    from app.services.agent_team_runtime_service import reopen_agent_team_close_after_delivery_failure

                    try:
                        team_id = _uuid(
                            (row.metadata_json or {}).get("agent_team_close_id"), field="agent_team_close_id"
                        )
                    except ValueError:
                        team_id = None
                    if team_id is not None:
                        await reopen_agent_team_close_after_delivery_failure(
                            db=db,
                            team_id=team_id,
                            notification_id=row.id,
                            error=error_text,
                        )
            await db.commit()
            outcome = "dead_letter" if dead_letter else "retry"
            affected_count = len(rows)
        record_runtime_result_page(
            delivery_mode=page.delivery_mode,
            outcome=outcome,
            item_count=affected_count,
        )
        return outcome, affected_count

    async def _mark_page_deferred(
        self,
        *,
        page: ClaimedResultIntegrationPage,
        worker_id: str,
        reason: str,
    ) -> int:
        async with self._worker_session("defer_integration_page") as db:
            stored_page = (
                await db.execute(
                    select(RuntimeResultIntegrationPage)
                    .where(RuntimeResultIntegrationPage.id == page.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                stored_page is None
                or stored_page.status != "processing"
                or stored_page.claimed_by != worker_id
                or stored_page.claim_token != page.claim_token
            ):
                return 0
            rows = list(
                (
                    await db.execute(
                        select(RuntimeNotificationOutbox)
                        .where(RuntimeNotificationOutbox.integration_page_id == page.id)
                        .order_by(RuntimeNotificationOutbox.mailbox_sequence)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            item_tokens = {item.id: item.claim_token for item in page.items}
            if any(row.claim_token != item_tokens.get(row.id) or row.locked_by != worker_id for row in rows):
                return 0
            stored_page.status = "prepared"
            stored_page.last_error = reason
            stored_page.attempt_count = max(0, int(stored_page.attempt_count or 0) - 1)
            stored_page.claimed_by = None
            stored_page.claim_token = None
            stored_page.lease_expires_at = None
            available_at = datetime.now(UTC) + timedelta(seconds=self._deferred_retry_seconds)
            for row in rows:
                row.status = "pending"
                row.available_at = available_at
                row.last_error = reason
                row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
                row.locked_by = None
                row.locked_at = None
                row.claim_token = None
                row.lease_expires_at = None
                await _set_approval_continuation_status(db, row, status="queued", error=reason)
            await db.commit()
            deferred_count = len(rows)
        record_runtime_result_page(
            delivery_mode=page.delivery_mode,
            outcome="deferred",
            item_count=deferred_count,
        )
        return deferred_count

    async def drain_once(
        self,
        *,
        worker_id: str,
        deliver: Callable[[ClaimedCompletionNotification], Awaitable[dict[str, Any]]] | None = None,
        limit: int = 20,
    ) -> dict[str, int]:
        claimed = await self.claim_batch(worker_id=worker_id, limit=limit)
        counts = {"claimed": len(claimed), "delivered": 0, "retried": 0, "deferred": 0, "dead_lettered": 0}
        if deliver is not None:
            # Explicit callback is a deterministic unit-test/maintenance seam.
            # Production delivery always goes through durable fan-in pages.
            for item in claimed:
                try:
                    receipt = await deliver(item)
                    acknowledged = await self._mark_delivered(
                        item_id=item.id,
                        worker_id=worker_id,
                        claim_token=item.claim_token,
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

        pages = await self.prepare_integration_pages(worker_id=worker_id, claimed=claimed)
        for page in pages:
            try:
                receipt = await self._deliver_page(page)
                counts["delivered"] += await self._mark_page_delivered(
                    page=page,
                    worker_id=worker_id,
                    receipt=dict(receipt or {}),
                )
            except CompletionDeliveryDeferred as exc:
                counts["deferred"] += await self._mark_page_deferred(
                    page=page,
                    worker_id=worker_id,
                    reason=str(exc),
                )
            except Exception as exc:
                outcome, affected = await self._mark_page_failed(
                    page=page,
                    worker_id=worker_id,
                    error=exc,
                )
                if outcome == "retry":
                    counts["retried"] += affected
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += affected
        return counts
