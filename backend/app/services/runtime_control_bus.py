from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import get_redis
from app.runtime.hooks import HookEvent, emit_hook


RUNTIME_CONTROL_SCHEMA = "hive.runtime.control.v1"
RUNTIME_CONTROL_CHANNEL = "hive:runtime:control"
RUNTIME_CONTROL_RECONNECT_SECONDS = 2.0
RUNTIME_CONTROL_PUBLISH_TIMEOUT_SECONDS = 2.0
TRANSCRIPT_PROJECTION_SWEEP_SECONDS = 5.0
_T0_TOO_LARGE_ERROR_PREFIX = "T0LedgerIntegrityError: t0_record_too_large:"
_TRANSCRIPT_PROJECTION_WAKE: asyncio.Event | None = None
SESSION_LIFECYCLE_MESSAGE_LIMIT = 100
_STATE: dict[str, Any] = {
    "running": False,
    "received": 0,
    "last_type": None,
    "last_error": None,
    "restart_count": 0,
    "last_restart_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def publish_runtime_control_event(payload: dict[str, Any]) -> None:
    envelope = {
        "schema": RUNTIME_CONTROL_SCHEMA,
        "created_at": _now_iso(),
        **payload,
    }
    redis = await get_redis()
    # Publishing is an advisory wake-up, never the transactional truth. A
    # stalled Redis socket must not keep a DB transaction (and its transcript /
    # RuntimeTask row locks) open indefinitely; the projection sweeper recovers
    # committed pending rows when this bounded publish fails.
    await asyncio.wait_for(
        redis.publish(RUNTIME_CONTROL_CHANNEL, json.dumps(envelope, ensure_ascii=False, default=str)),
        timeout=RUNTIME_CONTROL_PUBLISH_TIMEOUT_SECONDS,
    )


async def publish_web_chat_cancel(
    *,
    run_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    user_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "web_chat_cancel",
            "run_id": str(run_id).replace("-", ""),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id is not None else None,
        }
    )


async def publish_business_task_cancel(
    *,
    task_id: str | Any,
    runtime_task_id: str | Any,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "business_task_cancel",
            "task_id": str(task_id),
            "runtime_task_id": str(runtime_task_id),
        }
    )


async def publish_delegation_cancel(
    *,
    task_id: str,
    parent_agent_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "delegation_cancel",
            "task_id": str(task_id),
            "parent_agent_id": str(parent_agent_id) if parent_agent_id is not None else None,
        }
    )


async def publish_subagent_cancel(
    *,
    run_id: str | Any,
    parent_agent_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "subagent_cancel",
            "run_id": str(run_id).replace("-", ""),
            "parent_agent_id": str(parent_agent_id) if parent_agent_id is not None else None,
        }
    )


async def publish_transcript_t0_bridge(
    *,
    transcript_event_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    tenant_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "transcript_t0_bridge",
            "transcript_event_id": str(transcript_event_id),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
        }
    )


async def publish_session_lifecycle_hook(
    *,
    event: str | HookEvent,
    agent_id: str | Any,
    session_id: str | Any | None,
    messages: list[dict[str, Any]] | None = None,
    source: str,
    metadata: dict[str, Any] | None = None,
    origin: Any | None = None,
) -> None:
    event_value = event.value if isinstance(event, HookEvent) else str(event)
    clean_metadata = dict(metadata or {})
    if messages is not None:
        clean_metadata.setdefault("message_count", len(messages))
    await publish_runtime_control_event(
        {
            "type": "session_lifecycle_hook",
            "event": event_value,
            "agent_id": str(agent_id),
            "session_id": str(session_id) if session_id is not None else None,
            "source": source,
            "metadata": clean_metadata,
            **({"origin": origin.reference()} if origin is not None else {}),
        }
    )


def _uuid_or_none(value: str | Any | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _load_session_lifecycle_messages(
    *,
    agent_id: str | Any | None,
    session_id: str | Any | None,
    origin: Any,
    limit: int = SESSION_LIFECYCLE_MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    from app.database import async_session, tenant_scoped_session
    from app.services.session_lifecycle_environment import SessionLifecycleBinding

    async with tenant_scoped_session(
        origin.tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_control_bus.lifecycle_messages",
    ) as db:
        return await SessionLifecycleBinding(origin).messages(db, limit=limit)


async def _unprojected_predecessor_transcript_event_ids(
    db: AsyncSession,
    *,
    transcript_event: Any,
) -> list[UUID]:
    """Return every unfinished predecessor, earliest first.

    Per-session transcript order is the cloud truth. Projecting a later row
    first would make the portable T0 artifact disagree with that truth, so a
    worker drains predecessors before touching the requested row. The whole
    committed unfinished prefix is returned in one ordered pass so a single
    bridge call can drain a backlog of any size.
    """
    if not isinstance(db, AsyncSession):
        return []
    from app.models.chat_transcript_event import ChatTranscriptEvent

    result = await db.execute(
        select(ChatTranscriptEvent.id)
        .where(
            ChatTranscriptEvent.session_id == transcript_event.session_id,
            ChatTranscriptEvent.sequence < transcript_event.sequence,
            ChatTranscriptEvent.projection_status.in_(("pending", "failed", "projecting")),
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    return list(result.scalars())


# Deterministic prefix for mechanical terminal-owner recovery. Canonical
# owners can reopen after their boundary, so their key also names the segment.
TERMINAL_OWNER_SEGMENT_RECOVERY_IDEMPOTENCY_KEY = "t0-terminal-owner-recovery"


def _terminal_transcript_recovery_tasks(transcript_event: Any, *, canonical_owner: bool = False) -> Any:
    """The existing terminal/scope proof, optionally backed by a committed boundary."""
    from sqlalchemy import String, and_, cast, func, or_
    from app.models.runtime_task import TERMINAL_BOUNDARY_TERMINAL_STATUSES, RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox as Boundary

    def session_key(value):
        return func.lower(func.replace(value, "-", ""))

    session_id = transcript_event.session_id
    session_id = str(session_id) if isinstance(session_id, UUID) else session_id
    session_id = session_key(cast(session_id, String))
    no_boundary = and_(RuntimeTask.terminal_boundary_generation.is_(None),
        RuntimeTask.terminal_boundary_enqueued_at.is_(None))
    boundary_proof = no_boundary
    if canonical_owner:
        committed_boundary = select(Boundary.id).where(
            Boundary.runtime_task_id == RuntimeTask.id,
            Boundary.tenant_id == RuntimeTask.tenant_id,
            Boundary.terminal_status == RuntimeTask.status,
            Boundary.agent_id.in_((RuntimeTask.parent_agent_id, RuntimeTask.child_agent_id)),
            session_key(Boundary.session_id).in_((session_key(RuntimeTask.parent_session_id),
                session_key(RuntimeTask.child_session_id))),
        ).correlate(RuntimeTask).exists()
        boundary_proof = or_(no_boundary, and_(RuntimeTask.terminal_boundary_generation.is_not(None),
            RuntimeTask.terminal_boundary_enqueued_at.is_not(None), committed_boundary))
    return select(RuntimeTask).where(
        RuntimeTask.tenant_id == transcript_event.tenant_id,
        or_(RuntimeTask.parent_agent_id == transcript_event.agent_id,
            RuntimeTask.child_agent_id == transcript_event.agent_id),
        or_(session_key(RuntimeTask.parent_session_id) == session_id,
            session_key(RuntimeTask.child_session_id) == session_id),
        RuntimeTask.status.in_(TERMINAL_BOUNDARY_TERMINAL_STATUSES),
        RuntimeTask.completed_at.is_not(None),
        boundary_proof,
    )


async def _seal_terminal_boundary_pending_owner(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Mechanically seal a stale open segment whose terminal owner cannot.

    A canonical processor drains the transcript before sealing; a mismatching
    run in that prefix can prevent it from reaching the seal. A late event
    can also reopen a finished owner's segment. Prove the same terminal and
    scope facts as nonboundary recovery, plus a committed canonical outbox
    when the owner has that lane. This mechanical seal neither acknowledges
    the outbox nor invents semantic success. Unproven facts still fail closed.
    """

    from app.memory.t0.ledger import seal_t0_session_segment
    from app.models.runtime_task import RuntimeTask

    owner_uuid = _uuid_or_none(getattr(boundary_error, "active_runtime_task_id", None))
    if owner_uuid is None:
        return False
    owner_task = (
        await db.execute(_terminal_transcript_recovery_tasks(transcript_event, canonical_owner=True)
            .where(RuntimeTask.id == owner_uuid).with_for_update())
    ).scalar_one_or_none()
    if owner_task is None:
        return False
    canonical_owner = owner_task.terminal_boundary_generation is not None
    recovery_key = f"{TERMINAL_OWNER_SEGMENT_RECOVERY_IDEMPOTENCY_KEY}:{owner_uuid}"
    if canonical_owner:
        recovery_key += f":{boundary_error.active_segment_id}"
    seal_t0_session_segment(
        agent_id=transcript_event.agent_id,
        session_id=transcript_event.session_id,
        reason="terminal_owner_segment_recovery",
        metadata={
            "recovery": "terminal_boundary_owner" if canonical_owner else "terminal_nonboundary_owner",
            "owner_task_type": owner_task.task_type,
            "owner_status": owner_task.status,
            "owner_completed_at": owner_task.completed_at.isoformat(),
            "recovered_for_transcript_event": str(transcript_event.id),
            "incoming_runtime_task_id": getattr(boundary_error, "incoming_runtime_task_id", None),
            "active_segment_id": getattr(boundary_error, "active_segment_id", None),
        },
        idempotency_key=recovery_key,
        expected_runtime_task_id=owner_uuid,
    )
    return True


async def _incoming_transcript_run_is_orphan(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Whether the mismatching incoming run can never own a T0 segment.

    A committed transcript row can outlive its RuntimeTask (the task row is
    deleted while the transcript truth remains).  Such a run has no turn
    identity: no writer can enqueue a canonical terminal boundary for it and
    the nonboundary seal helper cannot prove owner facts about a task that
    does not exist, so waiting on a seal can never terminate.  Returning True
    lets the caller project the row as a guest of the session's active
    segment instead; any run with a live RuntimeTask row keeps the
    fail-closed owner mismatch.
    """

    if not isinstance(db, AsyncSession):
        return False
    incoming_uuid = _uuid_or_none(getattr(boundary_error, "incoming_runtime_task_id", None))
    if incoming_uuid is None or transcript_event.tenant_id is None:
        return False
    from app.models.runtime_task import RuntimeTask

    found = await db.scalar(
        select(RuntimeTask.id)
        .where(
            RuntimeTask.id == incoming_uuid,
            RuntimeTask.tenant_id == transcript_event.tenant_id,
        )
        .limit(1)
    )
    return found is None


async def _incoming_transcript_run_is_terminal_nonboundary(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Whether the mismatching incoming run is terminal with no boundary lane.

    A terminal task of a type that never enters the canonical terminal
    boundary outbox (plain ``subagent``) can neither seal the active segment
    nor wait on a canonical boundary of its own, so the caller projects it as
    a guest event instead. Any unproven fact returns False so the caller
    fails closed.
    """

    if not isinstance(db, AsyncSession):
        return False
    incoming_uuid = _uuid_or_none(getattr(boundary_error, "incoming_runtime_task_id", None))
    if incoming_uuid is None or transcript_event.tenant_id is None:
        return False
    from app.models.runtime_task import TERMINAL_BOUNDARY_TERMINAL_STATUSES, RuntimeTask

    incoming_task = (
        await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == incoming_uuid,
                RuntimeTask.tenant_id == transcript_event.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    return incoming_task is not None and (
        transcript_event.agent_id in {incoming_task.parent_agent_id, incoming_task.child_agent_id}
        and transcript_event.session_id
        in {_uuid_or_none(incoming_task.parent_session_id), _uuid_or_none(incoming_task.child_session_id)}
        and incoming_task.status in TERMINAL_BOUNDARY_TERMINAL_STATUSES
        and incoming_task.completed_at is not None
        and incoming_task.terminal_boundary_generation is None
        and incoming_task.terminal_boundary_enqueued_at is None
    )


async def _is_child_task_parent_projection(db: AsyncSession, *, transcript_event: Any) -> bool:
    """A scoped child return joins its parent turn without becoming its owner."""
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    if (getattr(transcript_event, "actor_type", None) != "system"
        or transcript_event.event_type not in {"runtime_action_completed", "runtime_action_failed",
            "runtime_action_blocked", "child_session"}
        or (transcript_event.metadata_json or {}).get("source") != "agent"):
        return False
    run_id = _uuid_or_none(transcript_event.run_id)
    if run_id is None:
        return False
    task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == run_id,
        RuntimeTask.tenant_id == transcript_event.tenant_id,
        RuntimeTask.parent_agent_id == transcript_event.agent_id,
        RuntimeTask.parent_session_id == str(transcript_event.session_id)))
    child_session_id = _uuid_or_none(task.child_session_id) if task is not None else None
    if child_session_id is None or child_session_id == transcript_event.session_id or task.child_agent_id is None:
        return False
    return await db.scalar(select(ChatSession.id).where(ChatSession.id == child_session_id,
        ChatSession.tenant_id == transcript_event.tenant_id, ChatSession.agent_id == task.child_agent_id)) is not None


async def _is_settled_question_followup(db: AsyncSession, *, transcript_event: Any) -> bool:
    """A user review after a delivered turn cannot reopen that turn's segment."""
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

    if (getattr(transcript_event, "item_kind", None) != "user_question"
        or getattr(transcript_event, "actor_type", None) != "user"
        or getattr(transcript_event, "run_id", None) is None):
        return False
    task = await db.get(RuntimeTask, transcript_event.run_id)
    if (task is None or task.status != "completed" or task.completed_at is None
        or task.tenant_id != transcript_event.tenant_id
        or transcript_event.agent_id not in {task.parent_agent_id, task.child_agent_id}
        or transcript_event.session_id not in {_uuid_or_none(task.parent_session_id), _uuid_or_none(task.child_session_id)}):
        return False
    boundary = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
        RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
        RuntimeTerminalBoundaryOutbox.tenant_id == transcript_event.tenant_id,
        RuntimeTerminalBoundaryOutbox.agent_id == transcript_event.agent_id,
        RuntimeTerminalBoundaryOutbox.session_id == str(transcript_event.session_id),
        RuntimeTerminalBoundaryOutbox.status == "delivered",
        RuntimeTerminalBoundaryOutbox.terminal_status == "completed",
    ).order_by(RuntimeTerminalBoundaryOutbox.created_at.desc()).limit(1))
    if boundary is None:
        return False
    receipt = boundary.delivery_receipt_json or {}
    binding = boundary.binding_json or {}
    sequence = receipt.get("terminal_sequence")
    return bool(type(sequence) is int and sequence < transcript_event.sequence
        and binding.get("terminal_sequence") == sequence
        and _uuid_or_none(receipt.get("terminal_event_id")) is not None
        and receipt.get("terminal_event_id") == binding.get("terminal_event_id")
        and receipt.get("boundary_id") == str(boundary.id)
        and _uuid_or_none(receipt.get("t0_boundary_id")) is not None)


async def _merge_transcript_bridge_metadata(db, event, metadata):
    if not isinstance(db, AsyncSession):
        event.metadata_json = metadata
        return
    from sqlalchemy import func, text, update
    from sqlalchemy.orm.attributes import set_committed_value
    from app.models.chat_transcript_event import ChatTranscriptEvent

    # The read projection may contain a bounded head of historical raw output.
    # Merge only bridge bookkeeping in SQL; never write that head over stored evidence.
    patch = {key: value for key, value in metadata.items() if key.startswith("t0_bridge_")}
    projection = {key: getattr(event, key) for key in (
        "projection_status", "projection_attempts", "projection_error", "projected_at")}
    # Persist this locked tenant event's projection and bridge keys in one statement.
    with db.no_autoflush:
        await db.execute(update(ChatTranscriptEvent).where(ChatTranscriptEvent.id == event.id,
            ChatTranscriptEvent.tenant_id == event.tenant_id).values(
                metadata_json=func.coalesce(ChatTranscriptEvent.metadata_json, text("'{}'::jsonb")).op("||")(patch),
                **projection,
            ).execution_options(synchronize_session=False))
    for key, value in projection.items():
        set_committed_value(event, key, value)
    set_committed_value(event, "metadata_json", metadata)


async def bridge_transcript_event_to_t0(
    *,
    transcript_event_id: str | Any,
    attempts: int = 40,
    retry_delay_seconds: float = 0.25,
    _batch_db: AsyncSession | None = None,
    _batch_event: Any = None,
    _after_transcript_event_id: Any = None,
) -> bool:
    event_uuid = _uuid_or_none(transcript_event_id)
    if event_uuid is None:
        _STATE["last_error"] = f"ValueError: invalid transcript_event_id {transcript_event_id!r}"
        return False

    from app.database import async_session, tenant_scoped_session
    from app.memory.t0.ledger import T0SegmentBoundaryPending, append_t0_session_event, find_t0_transcript_event
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.tenant_resolver import resolve_tenant_for_transcript_event

    @asynccontextmanager
    async def projection_session(tenant_id):
        if _batch_db is not None:
            yield _batch_db
        else:
            async with tenant_scoped_session(tenant_id, session_factory=async_session,
                require_tenant=True, source="runtime_control_bus.transcript_t0_bridge") as db:
                yield db

    attempts = max(1, attempts)
    for attempt in range(attempts):
        predecessor_event_ids: list[UUID] = []
        projection_failed = False
        tenant_id = _batch_event.tenant_id if _batch_event is not None else await resolve_tenant_for_transcript_event(
            event_uuid,
            session_factory=async_session,
        )
        if tenant_id is not None:
            async with projection_session(tenant_id) as db:
                if _batch_event is None:
                    result = await db.execute(
                        select(ChatTranscriptEvent)
                        .where(
                            ChatTranscriptEvent.id == event_uuid,
                            ChatTranscriptEvent.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                    transcript_event = result.scalar_one_or_none()
                else:
                    transcript_event = _batch_event
                if transcript_event is not None:
                    if str(getattr(transcript_event, "projection_error", None) or "").startswith(_T0_TOO_LARGE_ERROR_PREFIX):
                        return False
                    metadata = dict(transcript_event.metadata_json or {})
                    if (
                        metadata.get("t0_bridge_relayed_at")
                        or getattr(transcript_event, "projection_status", None) == "projected"
                    ):
                        return True

                    predecessor_event_ids = [] if _batch_event is not None else await _unprojected_predecessor_transcript_event_ids(
                        db,
                        transcript_event=transcript_event,
                    )
                    if not predecessor_event_ids:
                        transcript_event.projection_status = "projecting"
                        transcript_event.projection_attempts = (
                            int(getattr(transcript_event, "projection_attempts", 0) or 0) + 1
                        )
                        transcript_event.projection_error = None

                        chat_message = None
                        if transcript_event.message_id:
                            chat_message = await db.get(ChatMessage, transcript_event.message_id)
                        actor_id = getattr(chat_message, "user_id", None) or transcript_event.agent_id
                        role = metadata.get("t0_role") or metadata.get("role")
                        source = str(metadata.get("source") or "runtime_control")

                        def _append_t0_event(*, join_active_segment: bool = False) -> Any:
                            append_metadata = metadata
                            append_run_id = transcript_event.run_id
                            if join_active_segment:
                                # Session deliveries and guest runs cannot own
                                # or boundary a segment, so they join the active
                                # segment. The FK on run_id means
                                # a run identity rides metadata when the
                                # column is NULL, so record the same
                                # column-or-metadata identity the ledger used
                                # for the failed match.
                                append_metadata = {
                                    key: value
                                    for key, value in metadata.items()
                                    if key not in ("runtime_task_id", "turn_id")
                                }
                                original_run_id = transcript_event.run_id or metadata.get("runtime_task_id")
                                if original_run_id is not None:
                                    append_metadata["t0_bridge_joined_segment_run_id"] = str(original_run_id)
                                if metadata.get("turn_id"):
                                    append_metadata["t0_bridge_joined_segment_turn_id"] = str(metadata["turn_id"])
                                append_run_id = None
                            return append_t0_session_event(
                                agent_id=transcript_event.agent_id,
                                session_id=transcript_event.session_id,
                                event_type=transcript_event.event_type,
                                role=str(role) if role else None,
                                content=transcript_event.content or "",
                                message_id=transcript_event.message_id,
                                actor_id=actor_id,
                                tenant_id=transcript_event.tenant_id,
                                runtime_task_id=append_run_id,
                                source=source,
                                metadata=append_metadata,
                                created_at=transcript_event.created_at,
                            )

                        try:
                            predecessor = _after_transcript_event_id
                            if _batch_event is None and transcript_event.sequence > 1:
                                with db.no_autoflush:
                                    predecessor = await db.scalar(select(ChatTranscriptEvent.id).where(
                                        ChatTranscriptEvent.session_id == transcript_event.session_id,
                                        ChatTranscriptEvent.sequence < transcript_event.sequence,
                                        ChatTranscriptEvent.projection_status == "projected",
                                    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
                            existing_t0_event = find_t0_transcript_event(
                                agent_id=transcript_event.agent_id, session_id=transcript_event.session_id,
                                transcript_event_id=event_uuid, transcript_sequence=transcript_event.sequence,
                                after_transcript_event_id=predecessor,
                            )
                            if existing_t0_event is None:
                                try:
                                    # File delivery can follow its run's sealed
                                    # terminal boundary. Keep its original run
                                    # provenance without reopening that run's
                                    # segment, including for already saved rows.
                                    session_delivery = (
                                        transcript_event.event_type == "remote_a2a_artifacts"
                                        and transcript_event.actor_type == "system"
                                        and source == "remote_a2a_return"
                                    )
                                    t0_result = _append_t0_event(join_active_segment=session_delivery
                                        or await _is_settled_question_followup(db, transcript_event=transcript_event)
                                        or await _is_child_task_parent_projection(db, transcript_event=transcript_event))
                                except T0SegmentBoundaryPending as boundary_error:
                                    # Three disjoint recovery cases; every
                                    # unproven fact still re-raises and fails
                                    # closed, and each retry is the single
                                    # recovery attempt. First, a run with no
                                    # RuntimeTask row can never own or
                                    # boundary a T0 segment, so it joins the
                                    # active segment as a guest instead of
                                    # waiting on a seal that cannot exist.
                                    # Otherwise, a proven terminal owner is
                                    # sealed (canonical owners also need their
                                    # committed outbox); terminal nonboundary
                                    # incoming run joins as a guest when that
                                    # seal is refused.
                                    if await _incoming_transcript_run_is_orphan(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event(join_active_segment=True)
                                    elif await _seal_terminal_boundary_pending_owner(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event()
                                    elif await _incoming_transcript_run_is_terminal_nonboundary(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event(join_active_segment=True)
                                    else:
                                        raise
                                segment_id = t0_result.segment_id
                                t0_event_id = t0_result.event_id
                                t0_sequence = t0_result.sequence
                            else:
                                segment_id = existing_t0_event.segment_id
                                t0_event_id = existing_t0_event.event_id
                                t0_sequence = existing_t0_event.sequence
                        except Exception as exc:  # noqa: BLE001 - persist retryable projection failure.
                            projection_error = f"{type(exc).__name__}: {exc}"
                            transcript_event.projection_status = "failed"
                            transcript_event.projection_error = projection_error
                            metadata.update(
                                {
                                    "t0_bridge_pending": True,
                                    "t0_bridge_last_error": projection_error,
                                    "t0_bridge_attempts": transcript_event.projection_attempts,
                                }
                            )
                            await _merge_transcript_bridge_metadata(db, transcript_event, metadata)
                            _STATE["last_error"] = projection_error
                            projection_failed = True
                        else:
                            metadata.update(
                                {
                                    "t0_bridge_pending": False,
                                    "t0_bridge_relayed_at": _now_iso(),
                                    "t0_bridge_relay_source": "runtime_control_bus",
                                    "t0_bridge_segment_id": segment_id,
                                    "t0_bridge_event_id": t0_event_id,
                                    "t0_bridge_sequence": t0_sequence,
                                }
                            )
                            transcript_event.projection_status = "projected"
                            transcript_event.projection_error = None
                            transcript_event.projected_at = datetime.now(timezone.utc)
                            await _merge_transcript_bridge_metadata(db, transcript_event, metadata)
                            return True

        if projection_failed and attempt >= attempts - 1:
            return False

        if predecessor_event_ids:
            # One bridge call must drain the whole committed unfinished
            # prefix, not one row per retry attempt: a canonical terminal
            # boundary waits on this drain, and its outbox retry envelope is
            # far shorter than a one-row-at-a-time catch-up. Each predecessor
            # is bridged as the session's earliest unfinished row in
            # transcript order, and the first failure stops the drain so an
            # exhausted or failing frontier is never bypassed.
            drained = True
            for prior_event_id in predecessor_event_ids:
                if not await bridge_transcript_event_to_t0(
                    transcript_event_id=prior_event_id,
                    attempts=1,
                    retry_delay_seconds=retry_delay_seconds,
                ):
                    drained = False
                    break
            if drained:
                continue

        if attempt < attempts - 1:
            await asyncio.sleep(retry_delay_seconds)

    _STATE["last_error"] = f"LookupError: transcript_event {event_uuid} not visible after {attempts} attempts"
    return False


async def sweep_pending_transcript_t0_bridges(*, limit: int = 100, max_attempts: int | None = None,
    retry_failed: bool = True, session_id: UUID | None = None) -> int:
    """Recover committed transcript projections after publish/process failure.

    Select only each session's unfinished frontier. Live wakes handle pending
    rows; periodic sweeps retry failed frontiers when the existing recovery
    proofs can move them. Explicit max_attempts still bounds maintenance calls.
    """
    from sqlalchemy import Uuid, and_, case, cast, func, or_
    from sqlalchemy.orm import aliased

    from app.database import async_session, enter_rls_bypass, tenant_scoped_session
    from app.memory.t0.ledger import replay_t0_session_events_tail
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    limit = max(1, int(limit))
    statuses = ("pending", "failed") if retry_failed else ("pending",)

    # The typed ledger diagnostic carries the exact UUIDs checked by recovery.
    # Only retry scheduling uses these strings; recovery re-proves authority.
    error = ChatTranscriptEvent.projection_error
    def diagnostic_run_id(field):
        value = func.split_part(func.split_part(error, f"{field}='", 2), "'", 1)
        # Invalid/missing IDs stay unknown; compare UUID columns directly so
        # each proof uses the existing task primary key rather than a scan.
        return cast(case((value.op("~")("^[0-9a-f]{32}$"), value), else_=None), Uuid)

    owner_id = diagnostic_run_id("active_runtime_task_id")
    incoming_id = diagnostic_run_id("incoming_runtime_task_id")
    owner_ready = _terminal_transcript_recovery_tasks(ChatTranscriptEvent, canonical_owner=True).where(
        RuntimeTask.id == owner_id).correlate(ChatTranscriptEvent).exists()
    incoming_ready = _terminal_transcript_recovery_tasks(ChatTranscriptEvent).where(
        RuntimeTask.id == incoming_id).correlate(ChatTranscriptEvent).exists()
    incoming_orphan = and_(incoming_id.is_not(None), ~select(RuntimeTask.id).where(
        RuntimeTask.tenant_id == ChatTranscriptEvent.tenant_id,
        RuntimeTask.id == incoming_id,
    ).correlate(ChatTranscriptEvent).exists())
    retry_ready = or_(ChatTranscriptEvent.projection_status != "failed", error.is_(None),
        ~error.startswith("T0SegmentBoundaryPending:"),
        owner_ready, incoming_ready, incoming_orphan)

    earlier_row = aliased(ChatTranscriptEvent)
    no_earlier_unfinished = ~(
        select(earlier_row.id)
        .where(
            earlier_row.session_id == ChatTranscriptEvent.session_id,
            earlier_row.sequence < ChatTranscriptEvent.sequence,
            earlier_row.projection_status.in_(("pending", "projecting", "failed")),
        )
        .exists()
    )
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="sweep pending transcript T0 projections") as bypass_db:
            result = await bypass_db.execute(
                select(ChatTranscriptEvent.id, ChatTranscriptEvent.tenant_id, ChatTranscriptEvent.session_id)
                .where(
                    ChatTranscriptEvent.projection_status.in_(statuses),
                    (ChatTranscriptEvent.projection_error.is_(None)
                        | ~ChatTranscriptEvent.projection_error.startswith(_T0_TOO_LARGE_ERROR_PREFIX)),
                    no_earlier_unfinished,
                    *([ChatTranscriptEvent.projection_attempts < max(1, int(max_attempts))] if max_attempts is not None else []),
                    *([ChatTranscriptEvent.session_id == session_id] if session_id is not None else []),
                )
                .order_by(ChatTranscriptEvent.sequence.asc())
                .limit(max(1, int(limit)))
            )
            sessions = list(result.all())

    projected = 0
    for frontier_id, tenant_id, session_id in sessions:
        if projected >= limit:
            break
        try:
            session_projected = 0
            async with tenant_scoped_session(tenant_id, session_factory=async_session, require_tenant=True,
                source="runtime_control_bus.transcript_t0_batch") as db:
                # One ordered owner and transaction for this session's poll batch.
                frontier = (await db.execute(select(ChatTranscriptEvent.id, ChatTranscriptEvent.agent_id,
                    ChatTranscriptEvent.projection_error, retry_ready.label("retry_ready")).where(
                    ChatTranscriptEvent.id == frontier_id,
                    ChatTranscriptEvent.tenant_id == tenant_id,
                    ChatTranscriptEvent.projection_status.in_(statuses),
                ).with_for_update(skip_locked=True))).first()
                if frontier is None:
                    continue
                if not frontier.retry_ready:
                    # PRE_COMPACTION and other existing checkpoints can seal a
                    # live owner without changing any RuntimeTask facts. Only
                    # leave the frontier dormant while that exact segment is
                    # still the open tail; a changed ledger gets the normal
                    # append/recovery checks again.
                    tail = replay_t0_session_events_tail(agent_id=frontier.agent_id,
                        session_id=session_id, limit=1)
                    stalled_segment = str(frontier.projection_error or "").partition(
                        "active_segment_id=")[2].partition(" ")[0]
                    if tail and tail[-1].segment_id == stalled_segment and tail[-1].event_type != "segment_boundary":
                        continue
                rows = list((await db.scalars(select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.tenant_id == tenant_id,
                    ChatTranscriptEvent.projection_status.in_(("pending", "failed", "projecting")),
                ).order_by(ChatTranscriptEvent.sequence).limit(max(1, limit - projected)).with_for_update())).all())
                predecessor = await db.scalar(select(ChatTranscriptEvent.id).where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.sequence < rows[0].sequence,
                    ChatTranscriptEvent.projection_status == "projected",
                ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1)) if rows else None
                for row in rows:
                    if (max_attempts is not None and row.projection_attempts >= max_attempts) or row.projection_status not in statuses:
                        break
                    if not await bridge_transcript_event_to_t0(transcript_event_id=row.id, attempts=1,
                        _batch_db=db, _batch_event=row, _after_transcript_event_id=predecessor):
                        break
                    predecessor = row.id
                    session_projected += 1
            projected += session_projected
        except Exception as exc:  # noqa: BLE001 - one session must not block other ledgers.
            _STATE["last_error"] = f"projection_session:{session_id}:{type(exc).__name__}:{exc}"
            logger.warning("[RuntimeControlBus] transcript projection session {} failed: {}", session_id, exc)
    return projected


async def _run_transcript_projection_sweeper(
    *,
    interval_seconds: float = TRANSCRIPT_PROJECTION_SWEEP_SECONDS,
) -> None:
    global _TRANSCRIPT_PROJECTION_WAKE
    wake = _TRANSCRIPT_PROJECTION_WAKE = asyncio.Event()
    loop = asyncio.get_running_loop()
    next_retry = loop.time()
    try:
        while True:
            wake.clear()
            periodic = loop.time() >= next_retry
            if periodic:
                next_retry = loop.time() + max(0.1, interval_seconds)
            try:
                await sweep_pending_transcript_t0_bridges(retry_failed=periodic)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the next sweep remains a recovery path.
                _STATE["last_error"] = f"projection_sweeper:{type(exc).__name__}:{exc}"
                logger.warning("[RuntimeControlBus] transcript projection sweep failed: {}", exc)
            if wake.is_set():
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=max(0.01, next_retry - loop.time()))
            except asyncio.TimeoutError:
                pass
    finally:
        if _TRANSCRIPT_PROJECTION_WAKE is wake:
            _TRANSCRIPT_PROJECTION_WAKE = None


async def handle_runtime_control_message(message: dict[str, Any]) -> bool:
    if message.get("schema") != RUNTIME_CONTROL_SCHEMA:
        return False
    event_type = str(message.get("type") or "")
    _STATE["received"] = int(_STATE.get("received") or 0) + 1
    _STATE["last_type"] = event_type

    if event_type == "browser_control_request":
        from app.services.browser_control import browser_control

        return browser_control.enqueue_rpc(message.get("tenant_id"), message.get("resource_id"), message.get("lease_id"))

    if event_type == "web_chat_cancel":
        from app.services.web_chat_runtime import apply_remote_web_chat_cancel

        return bool(await _maybe_await(apply_remote_web_chat_cancel(str(message.get("run_id") or ""))))

    if event_type == "business_task_cancel":
        from app.services.task_executor import apply_remote_business_task_cancel

        return bool(apply_remote_business_task_cancel(str(message.get("runtime_task_id") or "")))

    if event_type == "delegation_cancel":
        from app.agents.orchestrator import apply_remote_async_delegation_cancel

        return bool(await _maybe_await(apply_remote_async_delegation_cancel(str(message.get("task_id") or ""))))

    if event_type == "subagent_cancel":
        from app.services.subagent_run_service import apply_remote_subagent_cancel

        return bool(await _maybe_await(apply_remote_subagent_cancel(str(message.get("run_id") or ""))))

    if event_type == "session_lifecycle_hook":
        from app.services.execution_environments import EnvironmentError, command_environment_context
        from app.services.session_lifecycle_environment import SessionLifecycleBinding, SessionLifecycleOrigin

        try:
            origin = SessionLifecycleOrigin.from_reference(message.get("origin"))
            if (
                origin.agent_id != _uuid_or_none(message.get("agent_id"))
                or origin.session_id != _uuid_or_none(message.get("session_id"))
                or origin.event.value != str(message.get("event") or "")
            ):
                raise EnvironmentError("lifecycle_origin_mismatch", 403)
            messages = await _maybe_await(
                _load_session_lifecycle_messages(agent_id=origin.agent_id, session_id=origin.session_id, origin=origin)
            )
        except EnvironmentError as exc:
            _STATE["last_error"] = exc.code
            return False
        except SQLAlchemyError:
            _STATE["last_error"] = "lifecycle_source_unavailable"
            return False
        metadata = dict(message.get("metadata") or {}) if isinstance(message.get("metadata"), dict) else {}
        metadata.update(
            {
                "tenant_id": str(origin.tenant_id),
                "user_id": str(origin.user_id),
                "agent_id": str(origin.agent_id),
                "session_id": str(origin.session_id),
            }
        )
        metadata.pop("runtime_task_id", None)
        with command_environment_context(SessionLifecycleBinding(origin).frame()):
            await emit_hook(
                origin.event,
                evidence_mode="independent",
                agent_id=origin.agent_id,
                session_id=str(origin.session_id),
                messages=messages,
                source=str(message.get("source") or "runtime_control"),
                metadata=metadata,
            )
        return True

    return False


async def _listen_runtime_control_once() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(RUNTIME_CONTROL_CHANNEL)
    async for raw_message in pubsub.listen():
        if raw_message.get("type") != "message":
            continue
        try:
            raw = raw_message.get("data")
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            if payload.get("schema") == RUNTIME_CONTROL_SCHEMA and payload.get("type") == "transcript_t0_bridge":
                # The existing durable sweeper owns projection. Keeping its
                # filesystem/DB work off this listener lets cancellation reach
                # an in-flight provider even when the transcript is behind.
                if _TRANSCRIPT_PROJECTION_WAKE is not None:
                    _TRANSCRIPT_PROJECTION_WAKE.set()
            else:
                await handle_runtime_control_message(payload)
        except Exception as exc:  # noqa: BLE001 - one malformed control event must not stop listener.
            _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[RuntimeControlBus] failed to handle control message: {}", exc)


async def start_runtime_control_listener(
    *,
    reconnect_delay_seconds: float = RUNTIME_CONTROL_RECONNECT_SECONDS,
) -> None:
    _STATE.update({"running": True, "last_error": None})
    projection_sweeper = asyncio.create_task(
        _run_transcript_projection_sweeper(),
        name="transcript-t0-projection-sweeper",
    )
    try:
        while True:
            try:
                await _listen_runtime_control_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - process can still run; reconnect keeps control plane live.
                _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
                _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
                _STATE["last_restart_at"] = _now_iso()
                logger.warning("[RuntimeControlBus] listener reconnecting after error: {}", exc)
                await asyncio.sleep(max(0.0, reconnect_delay_seconds))
                continue
            _STATE["last_error"] = "RuntimeControlListenerEnded: pubsub listener returned"
            _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
            _STATE["last_restart_at"] = _now_iso()
            logger.warning("[RuntimeControlBus] listener returned; reconnecting")
            await asyncio.sleep(max(0.0, reconnect_delay_seconds))
    except asyncio.CancelledError:
        raise
    finally:
        _STATE["running"] = False
        projection_sweeper.cancel()
        try:
            await projection_sweeper
        except asyncio.CancelledError:
            pass


def runtime_control_bus_snapshot() -> dict[str, Any]:
    return dict(_STATE)
