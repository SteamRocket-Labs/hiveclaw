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
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.services.chat_transcript import lock_transcript_session
from app.services.runtime_terminal_settlement import TERMINAL_SETTLEMENT_STATUSES
from app.services.session_human_input import SESSION_TARGETABLE_RUN_STATUSES
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    SessionEventDraft,
    append_session_events,
    resolve_session_command_authority,
)
from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES


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


_TOOL_EFFECT_RECONCILIATION_CODE = "tool_effect_reconciliation_required"
_TOOL_EFFECT_DISPATCH_RECOVERY_OWNER = "platform_admin:tool_effect_reconciliation"


def _tool_effect_reconciliation_detail(exc: HTTPException) -> dict[str, Any] | None:
    detail = exc.detail
    if (
        exc.status_code != 409
        or not isinstance(detail, dict)
        or str(detail.get("code") or "") != _TOOL_EFFECT_RECONCILIATION_CODE
        or detail.get("retryable") is not False
    ):
        return None
    return dict(detail)


async def _hold_input_dispatch_for_tool_effect(
    db: AsyncSession,
    *,
    admission: SessionInputAdmission,
    row: SessionTurnInput,
    command: SessionCommand,
    authority: AuthenticatedSessionAuthority,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Terminally hold an admitted input instead of retrying a prior effect.

    The input itself never reached a new RuntimeTask. Its accepted bytes stay
    durable, while the admission becomes an explicit no-replay recovery item.
    """

    settlement_ref = f"session-input:{row.id}:tool-effect-reconciliation"
    receipt = {
        "kind": "needs_reconciliation",
        "code": _TOOL_EFFECT_RECONCILIATION_CODE,
        "input_id": str(row.id),
        "session_id": str(authority.session_id),
        "retryable": False,
        "replay_allowed": False,
        "settlement_ref": settlement_ref,
    }
    admission.state = "needs_reconciliation"
    admission.dispatch_state = "needs_reconciliation"
    admission.dispatch_receipt_json = receipt
    admission.dispatch_last_error = None
    admission.recovery_owner = _TOOL_EFFECT_DISPATCH_RECOVERY_OWNER
    admission.lease_owner = None
    admission.lease_expires_at = None
    admission.version = int(admission.version) + 1
    row.status = "needs_reconciliation"
    row.settlement_ref = settlement_ref
    row.recovery_owner = _TOOL_EFFECT_DISPATCH_RECOVERY_OWNER
    row.version = int(row.version) + 1
    command.status = "needs_reconciliation"
    command.receipt_ref = settlement_ref
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                event_id=uuid.uuid5(admission.id, "tool-effect-dispatch-reconciliation"),
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="needs_reconciliation",
                scope={
                    "level": "session",
                    "session_id": str(authority.session_id),
                    "thread_id": str(authority.session_id),
                },
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(row.id),
                    "reason_code": _TOOL_EFFECT_RECONCILIATION_CODE,
                    "recovery_owner": _TOOL_EFFECT_DISPATCH_RECOVERY_OWNER,
                    "state_version": admission.version,
                    "retryable": False,
                    "replay_allowed": False,
                    "blocked_session_id": str(detail.get("session_id") or authority.session_id),
                },
                command_id=command.id,
                input_id=row.id,
            ),
            SessionEventDraft(
                event_id=uuid.uuid5(row.id, "tool-effect-dispatch-reconciliation"),
                item_id=row.id,
                item_kind="human_input",
                lifecycle="needs_reconciliation",
                scope={
                    "level": "session",
                    "session_id": str(authority.session_id),
                    "thread_id": str(authority.session_id),
                },
                actor={"type": "runtime"},
                payload={
                    "input_id": str(row.id),
                    "intent": row.intent,
                    "reason_code": _TOOL_EFFECT_RECONCILIATION_CODE,
                    "recovery_owner": _TOOL_EFFECT_DISPATCH_RECOVERY_OWNER,
                    "settlement_ref": settlement_ref,
                    "retryable": False,
                    "replay_allowed": False,
                },
                command_id=command.id,
                input_id=row.id,
            ),
        ],
    )
    return receipt


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
    Any,
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
    if session.tenant_id != admission.tenant_id:
        raise RuntimeError("input_dispatch_authority_mismatch")
    context = await resolve_session_command_authority(
        db,
        command=command,
        session=session,
        action="mutate_session_input",
    )
    return admission, row, command, context.authority, context.agent, context.actor, session


def _runtime_metadata(command: SessionCommand) -> dict[str, Any]:
    return dict((command.target_json or {}).get("runtime_metadata") or {})


async def _start_input_runtime(
    db: AsyncSession,
    *,
    row: SessionTurnInput,
    command: SessionCommand,
    agent: Agent,
    user: Any,
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
    user: Any,
    session: ChatSession,
) -> dict[str, Any] | None:
    await lock_transcript_session(db, session_id=session.id)
    # Only an EXECUTABLE CHAT run in an active-like status occupies the
    # session: the ``uq_runtime_tasks_active_web_chat_session`` partial unique
    # index and ``web_chat_runtime._find_active_run`` both predicate on the
    # same executable-chat task types (``EXECUTABLE_CHAT_TASK_TYPES``) and
    # the same active-like statuses.  A suspended (awaiting permission) or
    # resumable chat run is the same active-like turn and later becomes
    # running again, so starting a FIFO successor beside it would be rejected
    # by web-chat ingress with a 409 and retry-churn this admission on every
    # sweep.  An unrelated non-chat RuntimeTask (workflow, business_task,
    # subagent, trigger, ...) may legally share the tenant/agent/session
    # binding in any status WITHOUT occupying the web-chat session — ingress
    # does not 409 on it — so the guard must not defer behind it either.
    active = await db.scalar(
        select(RuntimeTask.id).where(
            RuntimeTask.tenant_id == admission.tenant_id,
            RuntimeTask.parent_agent_id == agent.id,
            RuntimeTask.parent_session_id == str(session.id),
            RuntimeTask.task_type.in_(EXECUTABLE_CHAT_TASK_TYPES),
            RuntimeTask.status.in_(SESSION_TARGETABLE_RUN_STATUSES),
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
            tool_effect_detail = _tool_effect_reconciliation_detail(exc)
            if tool_effect_detail is not None:
                return (
                    await _hold_input_dispatch_for_tool_effect(
                        db,
                        admission=admission,
                        row=row,
                        command=command,
                        authority=authority,
                        detail=tool_effect_detail,
                    ),
                    False,
                )
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
            try:
                run = await _start_fifo_successor_if_ready(
                    db,
                    admission=admission,
                    row=row,
                    command=command,
                    agent=agent,
                    user=user,
                    session=session,
                )
            except HTTPException as exc:
                tool_effect_detail = _tool_effect_reconciliation_detail(exc)
                if tool_effect_detail is None:
                    raise
                return (
                    await _hold_input_dispatch_for_tool_effect(
                        db,
                        admission=admission,
                        row=row,
                        command=command,
                        authority=authority,
                        detail=tool_effect_detail,
                    ),
                    False,
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

        try:
            fork = await dispatch_fork_side_thread(
                db=db,
                authority=authority,
                agent=agent,
                user=user,
                source_session=session,
                input_id=row.id,
                runtime_metadata=_runtime_metadata(command),
            )
        except HTTPException as exc:
            tool_effect_detail = _tool_effect_reconciliation_detail(exc)
            if tool_effect_detail is None:
                raise
            return (
                await _hold_input_dispatch_for_tool_effect(
                    db,
                    admission=admission,
                    row=row,
                    command=command,
                    authority=authority,
                    detail=tool_effect_detail,
                ),
                False,
            )
        await db.commit()
        return (_json_receipt({"kind": "fork", **asdict(fork)}), False)
    raise ValueError(f"unsupported admitted input intent: {row.intent}")


def _begin_dispatch_claim(
    admission: SessionInputAdmission,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: float,
) -> None:
    admission.dispatch_state = "dispatching"
    admission.dispatch_attempts = int(admission.dispatch_attempts) + 1
    admission.dispatch_last_error = None
    admission.lease_owner = str(worker_id)
    admission.lease_expires_at = now + timedelta(seconds=lease_seconds)
    admission.version = int(admission.version) + 1


async def _dispatch_claimed_admissions(
    db: AsyncSession,
    claimed_ids: list[uuid.UUID],
) -> dict[str, int]:
    counts = {"claimed": len(claimed_ids), "dispatched": 0, "deferred": 0, "retried": 0}
    for admission_id in claimed_ids:
        try:
            receipt, deferred = await _dispatch_one(db, admission_id=admission_id)
            admission = await db.scalar(
                select(SessionInputAdmission).where(SessionInputAdmission.id == admission_id).with_for_update()
            )
            if admission is None:
                continue
            if admission.state == "needs_reconciliation":
                # The dispatch lane terminally consumed this admission into a
                # typed no-replay hold. It is no longer claimable; count it as
                # a handled dispatch without claiming that a RuntimeTask ran.
                counts["dispatched"] += 1
            elif deferred:
                admission.dispatch_state = "pending"
                admission.dispatch_receipt_json = receipt
                counts["deferred"] += 1
                admission.lease_owner = None
                admission.lease_expires_at = None
                admission.version = int(admission.version) + 1
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


def _claimed_admission_ids(
    admissions: list[SessionInputAdmission],
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: float,
) -> list[uuid.UUID]:
    claimed_ids: list[uuid.UUID] = []
    for admission in admissions:
        _begin_dispatch_claim(admission, worker_id=worker_id, now=now, lease_seconds=lease_seconds)
        claimed_ids.append(admission.id)
    return claimed_ids


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
    claimed_ids = _claimed_admission_ids(
        claimed,
        worker_id=worker_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    await db.commit()
    return await _dispatch_claimed_admissions(db, claimed_ids)


async def recover_dispatched_terminal_steers_once(
    db: AsyncSession,
    *,
    worker_id: str,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: uuid.UUID | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Re-claim dispatched steers whose target run terminalized after mailing.

    A steer successfully dispatched into an active run is a settled mailbox
    admission, so the pending/stale-dispatching sweep never re-examines it.
    When the target run later terminalizes without binding the mailbox item,
    this lane re-claims the admission through the same canonical dispatch
    path, which rolls the steer over (``terminal_fallback=queue_next_turn``)
    and starts its deterministic FIFO successor run.  The claim predicate is
    purely mechanical: intent/status/fallback columns plus an exact
    tenant/session-scoped RuntimeTask join on exact terminal-status
    membership.  ``suspended`` (awaiting permission) and ``resumable`` are
    nonterminal: the same run becomes running again and can still bind the
    mailbox item, so a negated active-set predicate would prematurely roll
    the steer away from its live target.
    """

    now = datetime.now(timezone.utc)
    statement = (
        select(SessionInputAdmission)
        .join(SessionTurnInput, SessionTurnInput.id == SessionInputAdmission.input_id)
        .join(
            RuntimeTask,
            (RuntimeTask.id == SessionTurnInput.target_run_id)
            & (RuntimeTask.tenant_id == SessionTurnInput.tenant_id)
            & (RuntimeTask.parent_session_id == cast(SessionTurnInput.session_id, String)),
        )
        .where(
            SessionInputAdmission.input_revision == SessionTurnInput.revision,
            SessionInputAdmission.state == "admitted",
            SessionInputAdmission.dispatch_state == "dispatched",
            SessionTurnInput.intent == "steer_current_turn",
            SessionTurnInput.status == "queued",
            SessionTurnInput.terminal_fallback == "queue_next_turn",
            SessionTurnInput.target_run_id.is_not(None),
            RuntimeTask.status.in_(TERMINAL_SETTLEMENT_STATUSES),
        )
        .order_by(SessionTurnInput.queue_ordinal, SessionInputAdmission.id)
        .limit(max(1, int(limit)))
        .with_for_update(of=(SessionInputAdmission, SessionTurnInput), skip_locked=True)
    )
    if tenant_id is not None:
        statement = statement.where(SessionInputAdmission.tenant_id == tenant_id)
    claimed = list((await db.execute(statement)).scalars())
    lease_seconds = max(1.0, float(stale_after.total_seconds()) or 5.0)
    claimed_ids = _claimed_admission_ids(
        claimed,
        worker_id=worker_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    await db.commit()
    return await _dispatch_claimed_admissions(db, claimed_ids)


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
    "recover_dispatched_terminal_steers_once",
]
