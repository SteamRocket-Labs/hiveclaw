"""Session V2 HumanInput mailbox, revision and safe-boundary settlement."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import (
    SessionCommand,
    SessionInputAdmission,
    SessionTurnInput,
    SessionTurnReplacement,
)
from app.services.chat_transcript import lock_transcript_session
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    SessionEventDraft,
    append_session_events,
)


_ACTIVE_RUN_STATUSES = frozenset({"pending", "running"})
_UNBOUND_STATUSES = frozenset({"accepted", "queued"})


class InputRevisionConflict(RuntimeError):
    def __init__(self, *, current_revision: int):
        super().__init__("input_revision_conflict")
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class InputMailboxReceipt:
    command_id: uuid.UUID
    input_id: uuid.UUID
    intent: str
    revision: int
    status: str
    queue_priority: str
    queue_ordinal: int
    target_turn_id: str | None = None
    target_run_id: str | None = None
    bound_round_id: str | None = None
    rolled_over_to_turn_id: str | None = None
    reason_code: str | None = None
    replayed: bool = False


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_scope(session_id: uuid.UUID) -> dict[str, str]:
    return {"level": "session", "session_id": str(session_id), "thread_id": str(session_id)}


def _turn_scope(session_id: uuid.UUID, turn_id: str) -> dict[str, str]:
    return {
        "level": "turn",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
    }


def _run_scope(session_id: uuid.UUID, turn_id: str, run_id: uuid.UUID) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
    }


def _receipt(row: SessionTurnInput, *, reason_code: str | None = None, replayed: bool = False) -> InputMailboxReceipt:
    return InputMailboxReceipt(
        command_id=row.command_id,
        input_id=row.id,
        intent=row.intent,
        revision=row.revision,
        status=row.status,
        queue_priority=row.queue_priority,
        queue_ordinal=row.queue_ordinal,
        target_turn_id=row.target_turn_id,
        target_run_id=str(row.target_run_id) if row.target_run_id else None,
        bound_round_id=row.bound_round_id,
        rolled_over_to_turn_id=row.rolled_over_to_turn_id,
        reason_code=reason_code,
        replayed=replayed,
    )


async def _locked_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID,
) -> tuple[SessionTurnInput, SessionInputAdmission, SessionCommand]:
    await lock_transcript_session(db, session_id=authority.session_id)
    row = await db.scalar(
        select(SessionTurnInput)
        .where(
            SessionTurnInput.id == input_id,
            SessionTurnInput.tenant_id == authority.tenant_id,
            SessionTurnInput.session_id == authority.session_id,
        )
        .with_for_update()
    )
    if row is None:
        raise ValueError("human_input_not_found")
    admission = await db.scalar(
        select(SessionInputAdmission)
        .where(
            SessionInputAdmission.input_id == input_id,
            SessionInputAdmission.input_revision == row.revision,
        )
        .with_for_update()
    )
    command = await db.get(SessionCommand, row.command_id)
    if admission is None or command is None or admission.command_id != row.command_id:
        raise RuntimeError("human_input_authority_chain_broken")
    if (
        command.principal_id != authority.principal_id
        or command.tenant_id != authority.tenant_id
        or command.session_id != authority.session_id
    ):
        raise ValueError("human_input_principal_mismatch")
    return row, admission, command


async def revise_unbound_human_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    expected_revision: int,
    content_parts: list[dict[str, Any]],
) -> InputMailboxReceipt:
    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    row, admission, _command = await _locked_input(db, authority=authority, input_id=input_uuid)
    if row.status not in _UNBOUND_STATUSES or row.bound_round_id is not None:
        raise ValueError("human_input_already_bound")
    if admission.state in {"hook_running", "hook_result_committed"}:
        raise ValueError("human_input_admission_in_progress")
    if int(row.revision) != int(expected_revision):
        raise InputRevisionConflict(current_revision=row.revision)
    if not isinstance(content_parts, list):
        raise ValueError("content_parts must be an array")
    previous_hash = row.content_hash
    previous_revision = int(row.revision)
    row.content_parts_json = list(content_parts)
    row.content_hash = _sha256(content_parts)
    row.revision = previous_revision + 1
    row.status = "accepted"
    row.settlement_ref = None
    row.recovery_owner = None
    row.version = int(row.version) + 1
    # The authority trigger validates the new attempt against the current
    # input revision. Force the parent CAS update before its child INSERT;
    # both remain in this transaction.
    await db.flush([row])
    new_admission_id = uuid.uuid5(
        row.command_id,
        f"input-admission:revision:{row.revision}",
    )
    new_hook_run_id = uuid.uuid5(
        row.command_id,
        f"UserPromptSubmit:revision:{row.revision}",
    )
    new_admission = SessionInputAdmission(
        id=new_admission_id,
        tenant_id=authority.tenant_id,
        session_id=authority.session_id,
        command_id=row.command_id,
        input_id=row.id,
        input_revision=row.revision,
        state="admission_pending",
        hook_run_id=new_hook_run_id,
        hook_idempotency_key=(f"user-prompt-submit:{row.command_id}:revision:{row.revision}"),
        additional_context_refs_json=[],
        carry_forward="none",
        version=1,
    )
    db.add(new_admission)
    drafts = [
        SessionEventDraft(
            item_id=row.id,
            item_kind="human_input",
            lifecycle="revised",
            scope=_session_scope(authority.session_id),
            actor=authority.event_actor(),
            payload={
                "input_id": str(row.id),
                "revision": row.revision,
                "previous_revision": previous_revision,
                "previous_content_hash": previous_hash,
                "content_hash": row.content_hash,
                "content_parts": content_parts,
            },
            command_id=row.command_id,
            input_id=row.id,
            content_hash=row.content_hash,
        )
    ]
    if admission.state == "admission_pending":
        admission.state = "cancelled"
        admission.version = int(admission.version) + 1
        drafts.append(
            SessionEventDraft(
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="cancelled",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(row.id),
                    "input_revision": previous_revision,
                    "reason_code": "superseded_by_input_revision",
                    "superseded_by_revision": row.revision,
                    "state_version": admission.version,
                },
                command_id=row.command_id,
                input_id=row.id,
            )
        )
    drafts.append(
        SessionEventDraft(
            item_id=new_admission.id,
            item_kind="input_admission",
            lifecycle="prepared",
            scope=_session_scope(authority.session_id),
            actor={"type": "runtime"},
            payload={
                "admission_id": str(new_admission.id),
                "input_id": str(row.id),
                "input_revision": row.revision,
                "hook_run_id": str(new_hook_run_id),
                "state_version": 1,
                "carry_forward": "none",
            },
            command_id=row.command_id,
            input_id=row.id,
        )
    )
    await db.flush()
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=drafts,
    )
    return _receipt(row)


async def cancel_unbound_human_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    expected_revision: int,
) -> InputMailboxReceipt:
    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    row, admission, command = await _locked_input(db, authority=authority, input_id=input_uuid)
    if row.status == "cancelled":
        return _receipt(row, replayed=True)
    if row.status not in _UNBOUND_STATUSES or row.bound_round_id is not None:
        raise ValueError("human_input_already_bound")
    if int(row.revision) != int(expected_revision):
        raise InputRevisionConflict(current_revision=row.revision)
    if admission.state in {"hook_running", "hook_result_committed"}:
        raise ValueError("human_input_admission_in_progress")
    row.status = "cancelled"
    row.revision = int(row.revision) + 1
    row.version = int(row.version) + 1
    row.settlement_ref = f"session-input:{row.id}:cancelled:revision:{row.revision}"
    command.status = "rejected"
    command.rejection_json = {"reason_code": "cancelled_before_bind"}
    command.receipt_ref = row.settlement_ref
    drafts = [
        SessionEventDraft(
            item_id=row.id,
            item_kind="human_input",
            lifecycle="cancelled",
            scope=_session_scope(authority.session_id),
            actor=authority.event_actor(),
            payload={
                "input_id": str(row.id),
                "revision": row.revision,
                "reason_code": "cancelled_before_bind",
                "settlement_ref": row.settlement_ref,
            },
            command_id=row.command_id,
            input_id=row.id,
        )
    ]
    # Admission terminal facts are immutable.  Cancelling an admitted-but-not-
    # bound mailbox item settles only the HumanInput; it must not rewrite the
    # already durable ``input_admission.admitted`` truth into cancelled.
    if admission.state == "admission_pending":
        admission.state = "cancelled"
        admission.version = int(admission.version) + 1
        drafts.insert(
            0,
            SessionEventDraft(
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="cancelled",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(row.id),
                    "reason_code": "cancelled_before_bind",
                    "state_version": admission.version,
                },
                command_id=row.command_id,
                input_id=row.id,
            ),
        )
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=drafts,
    )
    return _receipt(row, reason_code="cancelled_before_bind")


async def _reject_admitted_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    row: SessionTurnInput,
    command: SessionCommand,
    reason_code: str,
) -> InputMailboxReceipt:
    row.status = "rejected"
    row.version = int(row.version) + 1
    row.settlement_ref = f"session-input:{row.id}:rejected:{reason_code}"
    command.status = "rejected"
    command.rejection_json = {"reason_code": reason_code}
    command.receipt_ref = row.settlement_ref
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=row.id,
                item_kind="human_input",
                lifecycle="rejected",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={"input_id": str(row.id), "intent": row.intent, "reason_code": reason_code},
                command_id=row.command_id,
                input_id=row.id,
            )
        ],
    )
    return _receipt(row, reason_code=reason_code)


async def reject_admitted_human_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    reason_code: str,
) -> InputMailboxReceipt:
    """Terminally reject an admitted input whose exact intent became impossible."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    row, admission, command = await _locked_input(db, authority=authority, input_id=input_uuid)
    if row.status == "rejected":
        return _receipt(row, reason_code=reason_code, replayed=True)
    if admission.state != "admitted" or row.status != "accepted":
        raise ValueError("human_input_not_rejectable_after_admission")
    return await _reject_admitted_input(
        db,
        authority=authority,
        row=row,
        command=command,
        reason_code=reason_code,
    )


async def queue_admitted_human_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
) -> InputMailboxReceipt:
    """Settle intent preconditions without silently changing the requested intent."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    row, admission, command = await _locked_input(db, authority=authority, input_id=input_uuid)
    if row.status != "accepted":
        # A re-dispatched steer that was already mailed into its target run
        # may have outlived it: the target terminalized without ever binding
        # the mailbox item.  Re-check the mechanical run status and settle it
        # through the same terminal rollover; every other settled row stays a
        # pure replay.
        if (
            row.status == "queued"
            and row.intent == "steer_current_turn"
            and row.terminal_fallback == "queue_next_turn"
            and row.target_run_id is not None
            and row.bound_round_id is None
        ):
            task = await db.scalar(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id == row.target_run_id,
                    RuntimeTask.tenant_id == authority.tenant_id,
                    RuntimeTask.parent_agent_id == authority.agent_id,
                    RuntimeTask.parent_session_id == str(authority.session_id),
                )
                .with_for_update()
            )
            if task is not None and task.status not in _ACTIVE_RUN_STATUSES:
                return await rollover_terminal_steer(db, authority=authority, row=row, command=command)
        return _receipt(row, replayed=True)
    if admission.state != "admitted":
        raise ValueError("human_input_not_admitted")

    if row.intent == "queue_next_turn":
        turn_id = row.target_turn_id or f"turn-{uuid.uuid5(row.id, 'queued-turn').hex}"
        row.target_turn_id = turn_id
        row.status = "queued"
        row.version = int(row.version) + 1
        await append_session_events(
            db,
            tenant_id=authority.tenant_id,
            agent_id=authority.agent_id,
            session_id=authority.session_id,
            drafts=[
                SessionEventDraft(
                    item_id=row.id,
                    item_kind="human_input",
                    lifecycle="queued",
                    scope=_session_scope(authority.session_id),
                    actor={"type": "runtime"},
                    payload={
                        "input_id": str(row.id),
                        "intent": row.intent,
                        "queue_priority": row.queue_priority,
                        "queue_ordinal": row.queue_ordinal,
                        "target_turn_id": turn_id,
                    },
                    command_id=row.command_id,
                    input_id=row.id,
                ),
                SessionEventDraft(
                    item_id=uuid.uuid5(row.id, "turn-item"),
                    item_kind="turn",
                    lifecycle="accepted",
                    scope=_turn_scope(authority.session_id, turn_id),
                    actor={"type": "runtime"},
                    payload={"turn_id": turn_id, "input_id": str(row.id)},
                    command_id=row.command_id,
                    input_id=row.id,
                ),
                SessionEventDraft(
                    item_id=uuid.uuid5(row.id, "turn-item"),
                    item_kind="turn",
                    lifecycle="queued",
                    scope=_turn_scope(authority.session_id, turn_id),
                    actor={"type": "runtime"},
                    payload={
                        "turn_id": turn_id,
                        "input_id": str(row.id),
                        "queue_ordinal": row.queue_ordinal,
                    },
                    command_id=row.command_id,
                    input_id=row.id,
                ),
            ],
        )
        return _receipt(row)

    if row.intent in {"steer_current_turn", "answer_request"}:
        if row.intent == "answer_request":
            question = await db.scalar(
                select(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == authority.session_id,
                    ChatTranscriptEvent.item_id == row.request_item_id,
                    ChatTranscriptEvent.item_kind == "user_question",
                )
                .order_by(ChatTranscriptEvent.sequence.desc())
            )
            if question is None or question.lifecycle not in {"created", "waiting"}:
                return await _reject_admitted_input(
                    db,
                    authority=authority,
                    row=row,
                    command=command,
                    reason_code="user_question_not_waiting",
                )
            question_scope = dict(question.scope_json or {})
            try:
                question_run_id = uuid.UUID(str(question_scope.get("run_id")))
            except (TypeError, ValueError):
                return await _reject_admitted_input(
                    db,
                    authority=authority,
                    row=row,
                    command=command,
                    reason_code="user_question_missing_run_scope",
                )
            question_turn_id = str(question_scope.get("turn_id") or "")
            if not question_turn_id:
                return await _reject_admitted_input(
                    db,
                    authority=authority,
                    row=row,
                    command=command,
                    reason_code="user_question_missing_turn_scope",
                )
            row.target_run_id = question_run_id
            row.target_turn_id = question_turn_id
        task = await db.scalar(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == row.target_run_id,
                RuntimeTask.tenant_id == authority.tenant_id,
                RuntimeTask.parent_agent_id == authority.agent_id,
                RuntimeTask.parent_session_id == str(authority.session_id),
            )
            .with_for_update()
        )
        active_turn_id = (
            str((task.metadata_json or {}).get("turn_id") or f"turn-{task.id.hex}") if task is not None else None
        )
        if task is None or active_turn_id != row.target_turn_id or task.status not in _ACTIVE_RUN_STATUSES:
            if row.intent == "steer_current_turn" and row.terminal_fallback == "queue_next_turn":
                return await rollover_terminal_steer(
                    db,
                    authority=authority,
                    row=row,
                    command=command,
                )
            return await _reject_admitted_input(
                db,
                authority=authority,
                row=row,
                command=command,
                reason_code="target_run_not_active",
            )
        row.status = "queued"
        row.version = int(row.version) + 1
        await append_session_events(
            db,
            tenant_id=authority.tenant_id,
            agent_id=authority.agent_id,
            session_id=authority.session_id,
            drafts=[
                SessionEventDraft(
                    item_id=row.id,
                    item_kind="human_input",
                    lifecycle="queued",
                    scope=_session_scope(authority.session_id),
                    actor={"type": "runtime"},
                    payload={
                        "input_id": str(row.id),
                        "intent": row.intent,
                        "queue_priority": row.queue_priority,
                        "queue_ordinal": row.queue_ordinal,
                        "target_turn_id": row.target_turn_id,
                        "target_run_id": str(row.target_run_id),
                        "request_item_id": str(row.request_item_id) if row.request_item_id else None,
                    },
                    command_id=row.command_id,
                    input_id=row.id,
                )
            ],
        )
        return _receipt(row)

    raise ValueError(f"intent {row.intent} is not a mailbox queue intent")


async def rollover_terminal_steer(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    row: SessionTurnInput,
    command: SessionCommand,
) -> InputMailboxReceipt:
    if row.intent != "steer_current_turn" or row.terminal_fallback != "queue_next_turn":
        raise ValueError("terminal rollover is not allowed for this input")
    if row.status == "rolled_over":
        return _receipt(row, reason_code="target_run_terminal", replayed=True)
    from_turn_id = row.target_turn_id
    from_run_id = row.target_run_id
    turn_id = row.rolled_over_to_turn_id or f"turn-{uuid.uuid5(row.id, 'terminal-rollover').hex}"
    # ``rolled_over`` is the terminal settlement of the original steer.  The
    # successor Turn is a separate durable effect and may never regress this
    # HumanInput back into the mailbox lifecycle.
    row.status = "rolled_over"
    row.rolled_over_to_turn_id = turn_id
    row.target_turn_id = turn_id
    row.target_run_id = None
    row.settlement_ref = f"session-input:{row.id}:rolled-over:{turn_id}"
    row.version = int(row.version) + 1
    command.status = "applied"
    command.receipt_ref = row.settlement_ref
    turn_item_id = uuid.uuid5(row.id, "rollover-turn-item")
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=row.id,
                item_kind="human_input",
                lifecycle="rolled_over",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(row.id),
                    "from_turn_id": from_turn_id,
                    "from_run_id": str(from_run_id) if from_run_id else None,
                    "rolled_over_to_turn_id": turn_id,
                    "queue_ordinal": row.queue_ordinal,
                    "settlement_ref": row.settlement_ref,
                },
                command_id=row.command_id,
                input_id=row.id,
            ),
            SessionEventDraft(
                item_id=turn_item_id,
                item_kind="turn",
                lifecycle="accepted",
                scope=_turn_scope(authority.session_id, turn_id),
                actor={"type": "runtime"},
                payload={"turn_id": turn_id, "input_id": str(row.id), "rollover": True},
                command_id=row.command_id,
                input_id=row.id,
            ),
            SessionEventDraft(
                item_id=turn_item_id,
                item_kind="turn",
                lifecycle="queued",
                scope=_turn_scope(authority.session_id, turn_id),
                actor={"type": "runtime"},
                payload={"turn_id": turn_id, "input_id": str(row.id), "queue_ordinal": row.queue_ordinal},
                command_id=row.command_id,
                input_id=row.id,
            ),
        ],
    )
    return _receipt(row, reason_code="target_run_terminal")


async def bind_admitted_inputs_to_round(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_id: str,
    model_request_snapshot_ref: str,
) -> list[SessionTurnInput]:
    """Bind every eligible next-priority input FIFO at a provider pre-dispatch boundary."""

    await lock_transcript_session(db, session_id=session_id)
    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .with_for_update()
    )
    if task is None:
        return []
    rows = list(
        (
            await db.execute(
                select(SessionTurnInput)
                .join(SessionInputAdmission, SessionInputAdmission.input_id == SessionTurnInput.id)
                .where(
                    SessionTurnInput.tenant_id == tenant_id,
                    SessionTurnInput.session_id == session_id,
                    SessionTurnInput.target_run_id == run_id,
                    SessionTurnInput.target_turn_id == turn_id,
                    SessionTurnInput.status == "queued",
                    SessionInputAdmission.input_revision == SessionTurnInput.revision,
                    or_(
                        SessionTurnInput.intent.in_(
                            ("start_turn", "steer_current_turn", "queue_next_turn", "answer_request")
                        ),
                        and_(
                            SessionTurnInput.intent == "interrupt_and_replace",
                            exists(
                                select(SessionTurnReplacement.id).where(
                                    SessionTurnReplacement.tenant_id == tenant_id,
                                    SessionTurnReplacement.session_id == session_id,
                                    SessionTurnReplacement.replacement_input_id == SessionTurnInput.id,
                                    SessionTurnReplacement.replacement_turn_id == turn_id,
                                    SessionTurnReplacement.state.in_(("replacement_admitted", "completed")),
                                )
                            ),
                        ),
                    ),
                    SessionInputAdmission.state == "admitted",
                )
                .order_by(
                    case((SessionTurnInput.queue_priority == "next", 0), else_=1),
                    SessionTurnInput.queue_ordinal,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    if not rows:
        return []
    drafts: list[SessionEventDraft] = []
    for row in rows:
        row.status = "bound"
        row.bound_round_id = str(round_id)
        row.model_request_snapshot_ref = model_request_snapshot_ref
        row.version = int(row.version) + 1
        drafts.append(
            SessionEventDraft(
                item_id=row.id,
                item_kind="human_input",
                lifecycle="bound",
                scope=_session_scope(session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(row.id),
                    "target_turn_id": turn_id,
                    "target_run_id": str(run_id),
                    "bound_round_id": str(round_id),
                    "model_request_snapshot_ref": model_request_snapshot_ref,
                    "queue_priority": row.queue_priority,
                    "queue_ordinal": row.queue_ordinal,
                },
                command_id=row.command_id,
                input_id=row.id,
            )
        )
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=drafts,
    )
    return rows


async def mark_bound_inputs_applied(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    round_id: str,
    provider_response_ref: str,
) -> list[SessionTurnInput]:
    """Settle bound inputs only after a proven provider response/stream receipt."""

    await lock_transcript_session(db, session_id=session_id)
    rows = list(
        (
            await db.execute(
                select(SessionTurnInput)
                .where(
                    SessionTurnInput.tenant_id == tenant_id,
                    SessionTurnInput.session_id == session_id,
                    SessionTurnInput.target_run_id == run_id,
                    SessionTurnInput.bound_round_id == str(round_id),
                    SessionTurnInput.status == "bound",
                )
                .order_by(SessionTurnInput.queue_ordinal)
                .with_for_update()
            )
        ).scalars()
    )
    drafts: list[SessionEventDraft] = []
    for row in rows:
        row.status = "applied"
        row.settlement_ref = f"session-input:{row.id}:applied:{_sha256(provider_response_ref)}"
        row.version = int(row.version) + 1
        command = await db.get(SessionCommand, row.command_id)
        if command is None:
            raise RuntimeError("bound input command disappeared")
        command.status = "applied"
        command.receipt_ref = row.settlement_ref
        drafts.append(
            SessionEventDraft(
                item_id=row.id,
                item_kind="human_input",
                lifecycle="applied",
                scope=_session_scope(session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(row.id),
                    "bound_round_id": str(round_id),
                    "provider_response_ref": provider_response_ref,
                    "settlement_ref": row.settlement_ref,
                },
                command_id=row.command_id,
                input_id=row.id,
            )
        )
    if drafts:
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=drafts,
        )
    return rows


def input_parts_to_runtime_messages(rows: Iterable[SessionTurnInput]) -> list[dict[str, Any]]:
    """Losslessly expose already-bound input parts to the model boundary."""

    messages: list[dict[str, Any]] = []
    for row in rows:
        parts = list(row.content_parts_json or [])
        role = "user"
        if parts and isinstance(parts[0], dict):
            candidate_role = str(parts[0].get("role") or "user").strip().lower()
            if candidate_role in {"user", "system"}:
                role = candidate_role
        if len(parts) == 1 and isinstance(parts[0], dict) and isinstance(parts[0].get("text"), str):
            content: Any = parts[0]["text"]
        else:
            content = parts
        messages.append(
            {
                "role": role,
                "content": content,
                "session_input_id": str(row.id),
                "bound_round_id": row.bound_round_id,
            }
        )
    return messages
