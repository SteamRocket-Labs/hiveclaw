"""Durable post-Hook dispatcher for every admitted Session V2 HumanInput.

Admission proves the Hook decision.  This worker-owned lane is the only owner
that turns an admitted intent into a mailbox item, RuntimeTask, replacement
saga, or side-thread branch.  All effects use IDs derived from ``input_id`` so
an ACK-loss retry observes the original effect instead of creating another.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.chat_transcript import lock_transcript_session
from app.services.session_v2_persistence import AuthenticatedSessionAuthority, resolve_session_mutation_authority


_ACTIVE_RUN_STATUSES = frozenset({"pending", "running"})


@dataclass(frozen=True, slots=True)
class InputDispatchOutcome:
    admission_id: uuid.UUID
    input_id: uuid.UUID
    state: str
    receipt: dict[str, Any]
    deferred: bool = False


def _runtime_content(row: SessionTurnInput) -> str:
    parts = list(row.content_parts_json or [])
    if len(parts) == 1 and isinstance(parts[0], dict):
        value = parts[0].get("text") or parts[0].get("content")
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_receipt(value: Any) -> Any:
    """Normalize typed dataclass/UUID receipts before storing them in JSONB."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


async def _dispatch_context(
    db: AsyncSession,
    *,
    admission_id: uuid.UUID,
) -> tuple[
    SessionInputAdmission,
    SessionTurnInput,
    SessionCommand,
    AuthenticatedSessionAuthority,
    Agent,
    User,
    ChatSession,
]:
    admission = await db.get(SessionInputAdmission, admission_id)
    if admission is None:
        raise RuntimeError("input_dispatch_admission_missing")
    row = await db.get(SessionTurnInput, admission.input_id)
    command = await db.get(SessionCommand, admission.command_id)
    session = await db.get(ChatSession, admission.session_id)
    if (
        row is None
        or command is None
        or session is None
        or row.command_id != command.id
        or row.revision != admission.input_revision
        or admission.state != "admitted"
        or command.principal_id is None
    ):
        raise RuntimeError("input_dispatch_authority_chain_broken")
    agent = await db.get(Agent, session.agent_id)
    user = await db.get(User, command.principal_id)
    if (
        agent is None
        or user is None
        or agent.tenant_id != admission.tenant_id
        or user.tenant_id != admission.tenant_id
        or session.tenant_id != admission.tenant_id
    ):
        raise RuntimeError("input_dispatch_authority_mismatch")
    authority = await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent.id,
        session_id=session.id,
        action="mutate_session_input",
    )
    return admission, row, command, authority, agent, user, session


def _runtime_metadata(command: SessionCommand) -> dict[str, Any]:
    return dict((command.target_json or {}).get("runtime_metadata") or {})


async def _start_input_runtime(
    db: AsyncSession,
    *,
    row: SessionTurnInput,
    command: SessionCommand,
    agent: Agent,
    user: User,
    session: ChatSession,
    successor: bool,
) -> dict[str, Any]:
    from app.services.web_chat_runtime import start_web_chat_run

    run_id = uuid.uuid5(row.id, "session-v2-successor-run" if successor else "session-v2-runtime-run")
    turn_id = row.target_turn_id or f"turn-{uuid.uuid5(row.id, 'session-v2-turn').hex}"
    metadata = _runtime_metadata(command)
    runtime_task_type = str(metadata.pop("runtime_task_type", "web_chat_turn") or "web_chat_turn")
    budget_interactive = bool(metadata.pop("budget_interactive", True))
    plan_mode_requested = bool(metadata.pop("plan_mode_requested", False))
    rolled_over = (
        successor
        and row.intent == "steer_current_turn"
        and row.status == "rolled_over"
        and row.rolled_over_to_turn_id == turn_id
    )
    input_metadata = (
        {"session_v2_rolled_over_input_id": str(row.id)} if rolled_over else {"session_v2_input_id": str(row.id)}
    )
    payload = await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=_runtime_content(row),
        display_content="",
        plan_mode_requested=plan_mode_requested,
        append_user_message=False,
        run_id=run_id,
        runtime_task_type=runtime_task_type,
        budget_interactive=budget_interactive,
        parts=list(row.content_parts_json or []),
        extra_metadata={
            **metadata,
            "turn_id": turn_id,
            "intent_id": str(row.id),
            **input_metadata,
            "session_v2_command_id": str(command.id),
            "session_v2_successor": successor,
        },
    )
    return {"kind": "runtime", "run_id": str(run_id), "turn_id": turn_id, "run": dict(payload)}


async def _start_fifo_successor_if_ready(
    db: AsyncSession,
    *,
    admission: SessionInputAdmission,
    row: SessionTurnInput,
    command: SessionCommand,
    agent: Agent,
    user: User,
    session: ChatSession,
) -> dict[str, Any] | None:
    await lock_transcript_session(db, session_id=session.id)
    active = await db.scalar(
        select(RuntimeTask.id).where(
            RuntimeTask.tenant_id == admission.tenant_id,
            RuntimeTask.parent_agent_id == agent.id,
            RuntimeTask.parent_session_id == str(session.id),
            RuntimeTask.status.in_(_ACTIVE_RUN_STATUSES),
        )
    )
    if active is not None:
        return None
    first = await db.scalar(
        select(SessionTurnInput)
        .join(
            SessionInputAdmission,
            (SessionInputAdmission.input_id == SessionTurnInput.id)
            & (SessionInputAdmission.input_revision == SessionTurnInput.revision),
        )
        .where(
            SessionTurnInput.tenant_id == admission.tenant_id,
            SessionTurnInput.session_id == session.id,
            SessionTurnInput.target_run_id.is_(None),
            or_(
                (SessionTurnInput.intent == "queue_next_turn") & (SessionTurnInput.status == "queued"),
                (SessionTurnInput.intent == "steer_current_turn")
                & (SessionTurnInput.status == "rolled_over")
                & SessionTurnInput.rolled_over_to_turn_id.is_not(None),
            ),
            SessionInputAdmission.state == "admitted",
            SessionInputAdmission.dispatch_state.in_(("pending", "dispatching")),
        )
        .order_by(SessionTurnInput.queue_ordinal, SessionTurnInput.id)
        .with_for_update(skip_locked=True)
    )
    if first is None or first.id != row.id:
        return None
    return await _start_input_runtime(
        db,
        row=row,
        command=command,
        agent=agent,
        user=user,
        session=session,
        successor=True,
    )


async def _dispatch_one(
    db: AsyncSession,
    *,
    admission_id: uuid.UUID,
) -> tuple[dict[str, Any], bool]:
    admission, row, command, authority, agent, user, session = await _dispatch_context(
        db,
        admission_id=admission_id,
    )
    if row.intent == "start_turn":
        try:
            return (
                await _start_input_runtime(
                    db,
                    row=row,
                    command=command,
                    agent=agent,
                    user=user,
                    session=session,
                    successor=False,
                ),
                False,
            )
        except HTTPException as exc:
            if exc.status_code != 409 or exc.detail != "A web chat run is already active for this branch":
                raise
            from app.services.session_human_input import reject_admitted_human_input

            rejected = await reject_admitted_human_input(
                db,
                authority=authority,
                input_id=row.id,
                reason_code="active_turn_conflict_after_admission",
            )
            await db.commit()
            return (_json_receipt({"kind": "rejected", **asdict(rejected)}), False)
    if row.intent in {"steer_current_turn", "queue_next_turn", "answer_request"}:
        from app.services.session_human_input import queue_admitted_human_input

        mailbox = await queue_admitted_human_input(db, authority=authority, input_id=row.id)
        await db.commit()
        row = await db.get(SessionTurnInput, row.id)
        assert row is not None
        if row.intent == "queue_next_turn" or row.rolled_over_to_turn_id is not None:
            run = await _start_fifo_successor_if_ready(
                db,
                admission=admission,
                row=row,
                command=command,
                agent=agent,
                user=user,
                session=session,
            )
            if run is None:
                return ({"kind": "successor", "status": "waiting_for_terminal"}, True)
            return (run, False)
        return (_json_receipt({"kind": "mailbox", **asdict(mailbox)}), False)
    if row.intent == "interrupt_and_replace":
        from app.services.runtime_task_worker import notify_runtime_task_worker
        from app.services.session_turn_replacement import request_turn_replacement

        saga = await request_turn_replacement(db, authority=authority, input_id=row.id)
        await db.commit()
        await notify_runtime_task_worker(reason="turn_replacement_requested", runtime_task_id=saga.old_run_id)
        return (_json_receipt({"kind": "replacement", **asdict(saga)}), False)
    if row.intent == "fork_side_thread":
        from app.services.session_fork_input import dispatch_fork_side_thread

        fork = await dispatch_fork_side_thread(
            db=db,
            authority=authority,
            agent=agent,
            user=user,
            source_session=session,
            input_id=row.id,
            runtime_metadata=_runtime_metadata(command),
        )
        await db.commit()
        return (_json_receipt({"kind": "fork", **asdict(fork)}), False)
    raise ValueError(f"unsupported admitted input intent: {row.intent}")


async def recover_admitted_session_inputs_once(
    db: AsyncSession,
    *,
    worker_id: str,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: uuid.UUID | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Claim admitted inputs globally with SKIP LOCKED and replay safe effects."""

    now = datetime.now(timezone.utc)
    statement = (
        select(SessionInputAdmission)
        .join(SessionTurnInput, SessionTurnInput.id == SessionInputAdmission.input_id)
        .where(
            SessionInputAdmission.input_revision == SessionTurnInput.revision,
            SessionInputAdmission.state == "admitted",
            or_(
                SessionInputAdmission.dispatch_state == "pending",
                (SessionInputAdmission.dispatch_state == "dispatching")
                & (SessionInputAdmission.lease_expires_at <= now),
            ),
        )
        .order_by(SessionTurnInput.queue_ordinal, SessionInputAdmission.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        statement = statement.where(SessionInputAdmission.tenant_id == tenant_id)
    claimed = list((await db.execute(statement)).scalars())
    lease_seconds = max(1.0, float(stale_after.total_seconds()) or 5.0)
    claimed_ids: list[uuid.UUID] = []
    for admission in claimed:
        admission.dispatch_state = "dispatching"
        admission.dispatch_attempts = int(admission.dispatch_attempts) + 1
        admission.dispatch_last_error = None
        admission.lease_owner = str(worker_id)
        admission.lease_expires_at = now + timedelta(seconds=lease_seconds)
        admission.version = int(admission.version) + 1
        claimed_ids.append(admission.id)
    await db.commit()

    counts = {"claimed": len(claimed_ids), "dispatched": 0, "deferred": 0, "retried": 0}
    for admission_id in claimed_ids:
        try:
            receipt, deferred = await _dispatch_one(db, admission_id=admission_id)
            admission = await db.scalar(
                select(SessionInputAdmission).where(SessionInputAdmission.id == admission_id).with_for_update()
            )
            if admission is None:
                continue
            if deferred:
                admission.dispatch_state = "pending"
                admission.dispatch_receipt_json = receipt
                counts["deferred"] += 1
            else:
                admission.dispatch_state = "dispatched"
                admission.dispatch_receipt_json = receipt
                counts["dispatched"] += 1
            admission.lease_owner = None
            admission.lease_expires_at = None
            admission.version = int(admission.version) + 1
            await db.commit()
        except Exception as exc:  # the deterministic effect may have committed before ACK
            await db.rollback()
            admission = await db.scalar(
                select(SessionInputAdmission).where(SessionInputAdmission.id == admission_id).with_for_update()
            )
            if admission is not None and admission.state == "admitted":
                admission.dispatch_state = "pending"
                admission.dispatch_last_error = f"{type(exc).__name__}: {exc}"[:2000]
                admission.lease_owner = None
                admission.lease_expires_at = None
                admission.version = int(admission.version) + 1
                await db.commit()
            counts["retried"] += 1
    return counts


async def dispatch_admitted_input_fast_path(
    db: AsyncSession,
    *,
    admission_id: uuid.UUID,
    worker_id: str,
) -> InputDispatchOutcome:
    """Optional latency path; the durable worker remains authoritative."""

    admission = await db.scalar(
        select(SessionInputAdmission).where(SessionInputAdmission.id == admission_id).with_for_update()
    )
    if admission is None:
        raise ValueError("input_admission_not_found")
    if admission.dispatch_state == "dispatched":
        return InputDispatchOutcome(
            admission.id,
            admission.input_id,
            admission.dispatch_state,
            dict(admission.dispatch_receipt_json or {}),
        )
    if admission.state != "admitted":
        return InputDispatchOutcome(admission.id, admission.input_id, admission.dispatch_state, {})
    now = datetime.now(timezone.utc)
    if (
        admission.dispatch_state == "dispatching"
        and admission.lease_expires_at is not None
        and admission.lease_expires_at > now
    ):
        return InputDispatchOutcome(
            admission.id,
            admission.input_id,
            admission.dispatch_state,
            dict(admission.dispatch_receipt_json or {}),
            deferred=True,
        )
    # Release the aggregate lock, but never clear another worker's lease.  The
    # durable claim query owns pending/stale-dispatching CAS transitions.
    await db.commit()
    await recover_admitted_session_inputs_once(
        db,
        worker_id=worker_id,
        tenant_id=admission.tenant_id,
        limit=1,
    )
    current = await db.get(SessionInputAdmission, admission.id)
    assert current is not None
    return InputDispatchOutcome(
        current.id,
        current.input_id,
        current.dispatch_state,
        dict(current.dispatch_receipt_json or {}),
        deferred=current.dispatch_state == "pending",
    )


__all__ = [
    "InputDispatchOutcome",
    "dispatch_admitted_input_fast_path",
    "recover_admitted_session_inputs_once",
]
