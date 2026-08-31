"""Crash-safe Session V2 stop-and-replace saga.

The replacement HumanInput remains the external command.  This module owns the
deterministic internal saga command and cancel child, and does not create a Turn
fact until the old attempt has a durable execution fence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.models.runtime_task import RuntimeTask
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.session_v2 import (
    SessionCommand,
    SessionControlInput,
    SessionInputAdmission,
    SessionTurnInput,
    SessionTurnReplacement,
)
from app.models.user import User
from app.services.chat_transcript import lock_transcript_session
from app.services.session_control_input import (
    accept_cancel_control_input,
    begin_cancel_control_input,
    settle_cancel_control_input,
)
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    SessionEventDraft,
    append_session_events,
    register_session_command,
    resolve_session_mutation_authority,
)


_ACTIVE_RUN_STATUSES = frozenset({"pending", "running"})
_RECONCILIATION_OWNER = "runtime_task_worker:turn_replacement_reconciliation"
_RECONCILIATION_SLO = timedelta(minutes=15)
_IRRECOVERABLE_RECOVERY_MARKERS = (
    "authority_mismatch",
    "authority_chain_broken",
    "recovery_chain_broken",
    "principal_revoked",
    "cancel_recovery_chain_broken",
    "cancel_terminal_without_run_fence",
    "old_run_terminal_fence_missing",
    "old_run_not_fenced",
    "input_has_no_runtime_content",
    "run_authority_mismatch",
)


@dataclass(frozen=True, slots=True)
class TurnReplacementReceipt:
    saga_id: uuid.UUID
    parent_command_id: uuid.UUID
    saga_command_id: uuid.UUID
    replacement_input_id: uuid.UUID
    replacement_turn_id: str
    old_run_id: uuid.UUID
    old_turn_id: str
    state: str
    cancel_control_id: uuid.UUID | None = None
    cancel_command_id: uuid.UUID | None = None
    replayed: bool = False
    reason_code: str | None = None


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
    return {**_turn_scope(session_id, turn_id), "level": "run", "run_id": str(run_id)}


def _receipt(saga: SessionTurnReplacement, *, parent_command_id: uuid.UUID, replayed: bool = False):
    return TurnReplacementReceipt(
        saga_id=saga.id,
        parent_command_id=parent_command_id,
        saga_command_id=saga.command_id,
        replacement_input_id=saga.replacement_input_id,
        replacement_turn_id=saga.replacement_turn_id,
        old_run_id=saga.old_run_id,
        old_turn_id=saga.old_turn_id,
        state=saga.state,
        cancel_control_id=saga.cancel_control_id,
        cancel_command_id=saga.cancel_command_id,
        replayed=replayed,
    )


async def _locked_input_chain(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID,
) -> tuple[SessionTurnInput, SessionInputAdmission, SessionCommand]:
    await lock_transcript_session(db, session_id=authority.session_id)
    input_row = await db.scalar(
        select(SessionTurnInput)
        .where(
            SessionTurnInput.id == input_id,
            SessionTurnInput.tenant_id == authority.tenant_id,
            SessionTurnInput.session_id == authority.session_id,
        )
        .with_for_update()
    )
    if input_row is None:
        raise ValueError("replacement_input_not_found")
    admission = await db.scalar(
        select(SessionInputAdmission)
        .where(
            SessionInputAdmission.input_id == input_id,
            SessionInputAdmission.input_revision == input_row.revision,
        )
        .with_for_update()
    )
    parent_command = await db.get(SessionCommand, input_row.command_id)
    if admission is None or parent_command is None or admission.command_id != input_row.command_id:
        raise RuntimeError("replacement_input_authority_chain_broken")
    if (
        parent_command.tenant_id != authority.tenant_id
        or parent_command.session_id != authority.session_id
        or parent_command.principal_id != authority.principal_id
    ):
        raise ValueError("replacement_input_principal_mismatch")
    return input_row, admission, parent_command


async def _locked_saga(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID,
) -> tuple[SessionTurnReplacement, SessionCommand, SessionCommand]:
    await lock_transcript_session(db, session_id=authority.session_id)
    saga = await db.scalar(
        select(SessionTurnReplacement)
        .where(
            SessionTurnReplacement.id == saga_id,
            SessionTurnReplacement.tenant_id == authority.tenant_id,
            SessionTurnReplacement.session_id == authority.session_id,
        )
        .with_for_update()
    )
    if saga is None:
        raise ValueError("turn_replacement_not_found")
    saga_command = await db.get(SessionCommand, saga.command_id)
    parent_command = (
        await db.get(SessionCommand, saga_command.causation_command_id) if saga_command is not None else None
    )
    if (
        saga_command is None
        or parent_command is None
        or saga_command.namespace != "turn_replacement"
        or parent_command.namespace != "human_input"
        or parent_command.principal_id != authority.principal_id
        or parent_command.tenant_id != authority.tenant_id
        or parent_command.session_id != authority.session_id
    ):
        raise RuntimeError("turn_replacement_authority_chain_broken")
    return saga, saga_command, parent_command


async def request_turn_replacement(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
) -> TurnReplacementReceipt:
    """Create the admitted input-first saga without touching the old Run."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    input_row, admission, parent_command = await _locked_input_chain(
        db,
        authority=authority,
        input_id=input_uuid,
    )
    if input_row.intent != "interrupt_and_replace":
        raise ValueError("human_input_is_not_replacement")
    if admission.state != "admitted":
        raise ValueError("replacement_input_not_admitted")
    if input_row.target_run_id is None or not input_row.target_turn_id:
        raise ValueError("replacement_target_is_required")

    saga_id = uuid.uuid5(
        parent_command.id,
        f"turn_replacement_saga:{input_row.target_run_id}:{input_row.id}",
    )
    existing = await db.get(SessionTurnReplacement, saga_id)
    if existing is not None:
        return _receipt(existing, parent_command_id=parent_command.id, replayed=True)

    old_run = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == input_row.target_run_id,
            RuntimeTask.tenant_id == authority.tenant_id,
            RuntimeTask.parent_agent_id == authority.agent_id,
            RuntimeTask.parent_session_id == str(authority.session_id),
        )
        .with_for_update()
    )
    if old_run is None:
        raise ValueError("replacement_old_run_not_found")
    actual_turn_id = str((old_run.metadata_json or {}).get("turn_id") or f"turn-{old_run.id.hex}")
    if actual_turn_id != input_row.target_turn_id:
        raise ValueError("replacement_old_turn_mismatch")

    saga_command_id = uuid.uuid5(
        parent_command.id,
        f"turn_replacement:{old_run.id}:{input_row.id}",
    )
    registered = await register_session_command(
        db,
        authority=authority,
        namespace="turn_replacement",
        command_kind="turn_replacement",
        idempotency_key=str(saga_command_id),
        request_payload={
            "parent_command_id": str(parent_command.id),
            "replacement_input_id": str(input_row.id),
        },
        target_payload={"old_turn_id": actual_turn_id, "old_run_id": str(old_run.id)},
        causation_command_id=parent_command.id,
        command_id=saga_command_id,
    )
    if registered.replayed:
        replay = await db.get(SessionTurnReplacement, saga_id)
        if replay is None:
            raise RuntimeError("turn_replacement_command_without_saga")
        return _receipt(replay, parent_command_id=parent_command.id, replayed=True)

    replacement_turn_id = f"turn-{uuid.uuid5(saga_id, 'replacement-turn').hex}"
    saga = SessionTurnReplacement(
        id=saga_id,
        tenant_id=authority.tenant_id,
        session_id=authority.session_id,
        command_id=saga_command_id,
        old_turn_id=actual_turn_id,
        old_run_id=old_run.id,
        cancel_control_id=None,
        cancel_command_id=None,
        replacement_turn_id=replacement_turn_id,
        replacement_input_id=input_row.id,
        state="requested",
        generation=1,
    )
    db.add(saga)
    await db.flush()
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="requested",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "parent_command_id": str(parent_command.id),
                    "saga_command_id": str(saga_command_id),
                    "replacement_input_id": str(input_row.id),
                    "replacement_turn_id": replacement_turn_id,
                    "old_turn_id": actual_turn_id,
                    "old_run_id": str(old_run.id),
                    "state": "requested",
                },
                command_id=saga_command_id,
                input_id=input_row.id,
            )
        ],
    )
    saga.last_event_id = events[0].id
    registered.command.receipt_ref = f"turn-replacement:{saga.id}:requested:{events[0].sequence}"
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def accept_replacement_cancel(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
) -> TurnReplacementReceipt:
    """Create the saga-owned deterministic cancel child exactly once."""

    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    saga, saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state != "requested":
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)

    cancel_command_id = uuid.uuid5(saga_command.id, f"cancel_run:{saga.old_run_id}")
    cancel_control_id = uuid.uuid5(cancel_command_id, "control_input")
    accepted = await accept_cancel_control_input(
        db,
        authority=authority,
        control_id=cancel_control_id,
        idempotency_key=str(cancel_command_id),
        expected_run_id=saga.old_run_id,
        causation_command_id=saga_command.id,
        command_id=cancel_command_id,
    )
    if accepted.status != "accepted":
        raise RuntimeError(f"replacement_cancel_not_accepted:{accepted.reason_code or accepted.status}")
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="cancelling",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "old_run_id": str(saga.old_run_id),
                    "cancel_control_id": str(cancel_control_id),
                    "cancel_command_id": str(cancel_command_id),
                    "state": "cancel_accepted",
                },
                command_id=saga.command_id,
                input_id=saga.replacement_input_id,
            )
        ],
    )
    saga.cancel_control_id = cancel_control_id
    saga.cancel_command_id = cancel_command_id
    saga.state = "cancel_accepted"
    saga.last_event_id = events[0].id
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def begin_replacement_cancel(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
    worker_id: str,
) -> TurnReplacementReceipt:
    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    saga, _saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state != "cancel_accepted" or saga.cancel_control_id is None:
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)
    outcome = await begin_cancel_control_input(
        db,
        authority=authority,
        control_id=saga.cancel_control_id,
        worker_id=worker_id,
    )
    if outcome.status not in {"applying", "applied"}:
        raise RuntimeError(f"replacement_cancel_start_failed:{outcome.reason_code or outcome.status}")
    return _receipt(saga, parent_command_id=parent_command.id, replayed=outcome.replayed)


async def fence_replacement_old_run(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
    execution_fence_ref: str | None = None,
) -> TurnReplacementReceipt:
    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    saga, _saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state == "old_run_fenced" or saga.state.startswith("replacement_") or saga.state == "completed":
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)
    if saga.state != "cancel_accepted" or saga.cancel_control_id is None:
        raise ValueError("replacement_cancel_not_accepted")
    old_run = await db.get(RuntimeTask, saga.old_run_id)
    if old_run is None or old_run.status not in {
        "completed",
        "failed",
        "killed",
        "skipped",
        "needs_reconciliation",
    }:
        raise RuntimeError("replacement_old_run_terminal_fence_not_committed")
    committed_fence_ref = str((old_run.metadata_json or {}).get("terminal_execution_fence_ref") or "").strip()
    if not committed_fence_ref:
        raise RuntimeError("replacement_old_run_terminal_fence_missing")
    supplied_fence_ref = str(execution_fence_ref or "").strip()
    if supplied_fence_ref and supplied_fence_ref != committed_fence_ref:
        raise ValueError("replacement_old_run_terminal_fence_mismatch")
    outcome = await settle_cancel_control_input(
        db,
        authority=authority,
        control_id=saga.cancel_control_id,
        execution_fence_ref=committed_fence_ref,
    )
    terminal_race = (
        old_run.status in {"completed", "failed", "skipped"}
        and outcome.status == "rejected"
        and outcome.reason_code == "run_terminal_before_cancel_effect"
    )
    if outcome.status != "applied" and not terminal_race:
        raise RuntimeError(f"replacement_old_run_not_fenced:{outcome.reason_code or outcome.status}")
    cancel_outcome = "old_run_terminal_before_cancel_effect" if terminal_race else "cancel_effect_committed"
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="fenced",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "old_run_id": str(saga.old_run_id),
                    "cancel_control_id": str(saga.cancel_control_id),
                    "execution_fence_ref": committed_fence_ref,
                    "old_run_status": old_run.status,
                    "cancel_outcome": cancel_outcome,
                    "state": "old_run_fenced",
                },
                command_id=saga.command_id,
                input_id=saga.replacement_input_id,
            )
        ],
    )
    saga.state = "old_run_fenced"
    saga.last_event_id = events[0].id
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def queue_fenced_replacement(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
) -> TurnReplacementReceipt:
    """Create the replacement Turn facts only after the old Run fence."""

    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    saga, _saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state in {"replacement_queued", "replacement_admitted", "completed"}:
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)
    if saga.state != "old_run_fenced":
        raise ValueError("replacement_old_run_not_fenced")
    input_row = await db.scalar(
        select(SessionTurnInput).where(SessionTurnInput.id == saga.replacement_input_id).with_for_update()
    )
    if input_row is None or input_row.status != "accepted" or input_row.intent != "interrupt_and_replace":
        raise RuntimeError("replacement_input_not_queueable")
    turn_item_id = uuid.uuid5(saga.id, "replacement-turn-item")
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="queued",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "replacement_input_id": str(input_row.id),
                    "replacement_turn_id": saga.replacement_turn_id,
                    "state": "replacement_queued",
                },
                command_id=saga.command_id,
                input_id=input_row.id,
            ),
            SessionEventDraft(
                item_id=input_row.id,
                item_kind="human_input",
                lifecycle="queued",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_row.id),
                    "intent": input_row.intent,
                    "queue_priority": input_row.queue_priority,
                    "queue_ordinal": input_row.queue_ordinal,
                    "target_turn_id": saga.replacement_turn_id,
                },
                command_id=input_row.command_id,
                input_id=input_row.id,
            ),
            SessionEventDraft(
                item_id=turn_item_id,
                item_kind="turn",
                lifecycle="accepted",
                scope=_turn_scope(authority.session_id, saga.replacement_turn_id),
                actor={"type": "runtime"},
                payload={"turn_id": saga.replacement_turn_id, "input_id": str(input_row.id)},
                command_id=input_row.command_id,
                input_id=input_row.id,
            ),
            SessionEventDraft(
                item_id=turn_item_id,
                item_kind="turn",
                lifecycle="queued",
                scope=_turn_scope(authority.session_id, saga.replacement_turn_id),
                actor={"type": "runtime"},
                payload={
                    "turn_id": saga.replacement_turn_id,
                    "input_id": str(input_row.id),
                    "queue_ordinal": input_row.queue_ordinal,
                },
                command_id=input_row.command_id,
                input_id=input_row.id,
            ),
        ],
    )
    input_row.status = "queued"
    input_row.target_turn_id = saga.replacement_turn_id
    input_row.version = int(input_row.version) + 1
    saga.state = "replacement_queued"
    saga.last_event_id = events[0].id
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def admit_replacement_run(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
    run_id: uuid.UUID | str,
) -> TurnReplacementReceipt:
    """Bind the queued replacement Turn to its newly created RuntimeTask."""

    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    saga, _saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state in {"replacement_admitted", "completed"}:
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)
    if saga.state != "replacement_queued":
        raise ValueError("replacement_turn_not_queued")
    input_row = await db.scalar(
        select(SessionTurnInput).where(SessionTurnInput.id == saga.replacement_input_id).with_for_update()
    )
    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_uuid,
            RuntimeTask.tenant_id == authority.tenant_id,
            RuntimeTask.parent_agent_id == authority.agent_id,
            RuntimeTask.parent_session_id == str(authority.session_id),
        )
        .with_for_update()
    )
    if (
        input_row is None
        or input_row.status != "queued"
        or input_row.target_turn_id != saga.replacement_turn_id
        or task is None
        or str((task.metadata_json or {}).get("turn_id") or "") != saga.replacement_turn_id
    ):
        raise RuntimeError("replacement_run_authority_mismatch")
    # The run-scoped event trigger verifies that an input's current delivery
    # target is this exact RuntimeTask.  Move the aggregate target first inside
    # the same transaction; the saga still remains replacement_queued until
    # both canonical events are durable below.
    input_row.target_run_id = run_uuid
    input_row.version = int(input_row.version) + 1
    await db.flush()
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="admitted",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "replacement_turn_id": saga.replacement_turn_id,
                    "replacement_run_id": str(run_uuid),
                    "state": "replacement_admitted",
                },
                command_id=saga.command_id,
                input_id=input_row.id,
            ),
            SessionEventDraft(
                item_id=run_uuid,
                item_kind="run",
                lifecycle="queued",
                scope=_run_scope(authority.session_id, saga.replacement_turn_id, run_uuid),
                actor={"type": "runtime"},
                payload={
                    "run_id": str(run_uuid),
                    "input_id": str(input_row.id),
                    "replacement_saga_id": str(saga.id),
                },
                command_id=input_row.command_id,
                input_id=input_row.id,
            ),
        ],
    )
    saga.state = "replacement_admitted"
    saga.last_event_id = events[0].id
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def complete_turn_replacement(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    saga_id: uuid.UUID | str,
) -> TurnReplacementReceipt:
    """Settle only the handoff command; the HumanInput remains unconsumed."""

    saga_uuid = saga_id if isinstance(saga_id, uuid.UUID) else uuid.UUID(str(saga_id))
    saga, saga_command, parent_command = await _locked_saga(db, authority=authority, saga_id=saga_uuid)
    if saga.state == "completed":
        return _receipt(saga, parent_command_id=parent_command.id, replayed=True)
    if saga.state != "replacement_admitted":
        raise ValueError("replacement_run_not_admitted")
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="completed",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "replacement_turn_id": saga.replacement_turn_id,
                    "state": "completed",
                    "human_input_applied": False,
                },
                command_id=saga.command_id,
                input_id=saga.replacement_input_id,
            )
        ],
    )
    saga.state = "completed"
    saga.last_event_id = events[0].id
    saga_command.status = "applied"
    saga_command.receipt_ref = f"turn-replacement:{saga.id}:completed:{events[0].sequence}"
    await db.flush()
    return _receipt(saga, parent_command_id=parent_command.id)


async def _replacement_recovery_context(
    db: AsyncSession,
    *,
    saga_id: uuid.UUID,
) -> tuple[
    SessionTurnReplacement,
    AuthenticatedSessionAuthority,
    Agent,
    User,
    ChatSession,
    SessionTurnInput,
]:
    saga = await db.get(SessionTurnReplacement, saga_id)
    if saga is None:
        raise ValueError("turn_replacement_not_found")
    saga_command = await db.get(SessionCommand, saga.command_id)
    parent_command = (
        await db.get(SessionCommand, saga_command.causation_command_id)
        if saga_command is not None and saga_command.causation_command_id is not None
        else None
    )
    input_row = await db.get(SessionTurnInput, saga.replacement_input_id)
    old_run = await db.get(RuntimeTask, saga.old_run_id)
    if saga_command is None or parent_command is None or input_row is None or old_run is None:
        raise RuntimeError("turn_replacement_recovery_chain_broken")
    agent = await db.get(Agent, old_run.parent_agent_id)
    user = await db.get(User, parent_command.principal_id)
    session = await db.get(ChatSession, saga.session_id)
    if (
        agent is None
        or user is None
        or session is None
        or agent.tenant_id != saga.tenant_id
        or user.tenant_id != saga.tenant_id
        or session.tenant_id != saga.tenant_id
        or session.agent_id != agent.id
        or parent_command.session_id != session.id
        or parent_command.command_kind != "interrupt_and_replace"
    ):
        raise RuntimeError("turn_replacement_recovery_authority_mismatch")
    authority = await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent.id,
        session_id=session.id,
        action="mutate_session_input",
    )
    return saga, authority, agent, user, session, input_row


def _replacement_content(input_row: SessionTurnInput) -> str:
    texts = [
        str(part.get("text") or "")
        for part in list(input_row.content_parts_json or [])
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n\n".join(value for value in texts if value).strip()


async def _start_replacement_runtime(
    *,
    db: AsyncSession,
    authority: AuthenticatedSessionAuthority,
    saga: SessionTurnReplacement,
    agent: Agent,
    user: User,
    session: ChatSession,
    input_row: SessionTurnInput,
    run_id: uuid.UUID,
) -> None:
    from app.services.web_chat_runtime import start_web_chat_run

    content = _replacement_content(input_row)
    if not content:
        raise RuntimeError("replacement_input_has_no_runtime_content")
    await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=content,
        append_user_message=False,
        run_id=run_id,
        extra_metadata={
            "turn_id": saga.replacement_turn_id,
            "intent_id": str(input_row.id),
            "session_v2_input_id": str(input_row.id),
            "session_v2_command_id": str(input_row.command_id),
            "session_v2_replacement_saga_id": str(saga.id),
        },
    )
    # Crash-safe replay: start_web_chat_run may find the deterministic child
    # already committed and return it without re-running its admission branch.
    current = await db.get(SessionTurnReplacement, saga.id)
    if current is not None and current.state == "replacement_queued":
        await admit_replacement_run(
            db,
            authority=authority,
            saga_id=current.id,
            run_id=run_id,
        )


def _replacement_recovery_is_irrecoverable(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return True
    reason = str(exc).lower()
    return any(marker in reason for marker in _IRRECOVERABLE_RECOVERY_MARKERS)


async def _quarantine_turn_replacement(
    db: AsyncSession,
    *,
    saga_id: uuid.UUID,
    worker_id: str,
    reason: str,
) -> None:
    """Persist an explicit repair contract for a provably broken saga chain."""

    saga = await db.scalar(select(SessionTurnReplacement).where(SessionTurnReplacement.id == saga_id).with_for_update())
    if saga is None or saga.state in {"completed", "failed", "needs_reconciliation"}:
        return
    resume_state = saga.state
    saga_command = await db.get(SessionCommand, saga.command_id)
    input_row = await db.get(SessionTurnInput, saga.replacement_input_id)
    session = await db.get(ChatSession, saga.session_id)
    if saga_command is None or input_row is None or session is None:
        raise RuntimeError("turn_replacement_quarantine_evidence_chain_broken")
    now = datetime.now(timezone.utc)
    recovery_slo_at = now + _RECONCILIATION_SLO
    recovery_payload = {
        "reason_code": str(reason)[:500],
        "resume_state": resume_state,
        "recovery_owner": _RECONCILIATION_OWNER,
        "recovery_slo_at": recovery_slo_at.isoformat(),
        "worker_id": str(worker_id),
        "retry_generation": int(saga.generation),
    }
    events = await append_session_events(
        db,
        tenant_id=saga.tenant_id,
        agent_id=session.agent_id,
        session_id=saga.session_id,
        drafts=[
            SessionEventDraft(
                item_id=saga.id,
                item_kind="turn_replacement",
                lifecycle="needs_reconciliation",
                scope=_session_scope(saga.session_id),
                actor={"type": "runtime"},
                payload={
                    "saga_id": str(saga.id),
                    "state": "needs_reconciliation",
                    **recovery_payload,
                },
                command_id=saga.command_id,
                input_id=saga.replacement_input_id,
            )
        ],
    )
    saga.state = "needs_reconciliation"
    saga.last_event_id = events[0].id
    saga.lease_owner = None
    saga.lease_expires_at = None
    saga_command.status = "needs_reconciliation"
    saga_command.rejection_json = recovery_payload
    saga_command.receipt_ref = f"turn-replacement:{saga.id}:needs-reconciliation:{events[0].sequence}"
    input_row.recovery_owner = _RECONCILIATION_OWNER
    input_row.version = int(input_row.version) + 1
    await db.flush()


async def recover_turn_replacements_once(
    db: AsyncSession,
    *,
    worker_id: str,
    signal_callback: Callable[..., Awaitable[None]] | None = None,
    stale_after: timedelta = timedelta(seconds=5),
    tenant_id: uuid.UUID | None = None,
    saga_ids: tuple[uuid.UUID, ...] | None = None,
    limit: int = 50,
    max_transitions_per_saga: int = 8,
) -> dict[str, int]:
    """Lease and advance durable replacement sagas on the existing worker."""

    now = datetime.now(timezone.utc)
    claim_statement = (
        select(SessionTurnReplacement)
        .where(
            SessionTurnReplacement.state.notin_(("completed", "failed", "needs_reconciliation")),
            (SessionTurnReplacement.lease_expires_at.is_(None) | (SessionTurnReplacement.lease_expires_at <= now)),
        )
        .order_by(SessionTurnReplacement.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        claim_statement = claim_statement.where(SessionTurnReplacement.tenant_id == tenant_id)
    if saga_ids is not None:
        claim_statement = claim_statement.where(SessionTurnReplacement.id.in_(saga_ids))
    claimed = list((await db.execute(claim_statement)).scalars())
    lease_seconds = max(1.0, float(stale_after.total_seconds()) or 30.0)
    claimed_ids: list[uuid.UUID] = []
    for saga in claimed:
        saga.lease_owner = str(worker_id)
        saga.lease_expires_at = now + timedelta(seconds=lease_seconds)
        saga.generation = int(saga.generation) + 1
        claimed_ids.append(saga.id)
    await db.commit()

    if signal_callback is None:
        from app.services.web_chat_runtime import signal_web_chat_cancel

        signal_callback = signal_web_chat_cancel
    counts = {
        "claimed": len(claimed_ids),
        "transitioned": 0,
        "signalled": 0,
        "started_runs": 0,
        "completed": 0,
        "retryable_failures": 0,
        "needs_reconciliation": 0,
    }
    for saga_id in claimed_ids:
        transitions = 0
        release_lease = True
        try:
            while transitions < max(1, int(max_transitions_per_saga)):
                saga, authority, agent, user, session, input_row = await _replacement_recovery_context(
                    db,
                    saga_id=saga_id,
                )
                state = saga.state
                if state == "requested":
                    await accept_replacement_cancel(db, authority=authority, saga_id=saga.id)
                    await db.commit()
                elif state == "cancel_accepted":
                    control = (
                        await db.get(SessionControlInput, saga.cancel_control_id)
                        if saga.cancel_control_id is not None
                        else None
                    )
                    old_run = await db.get(RuntimeTask, saga.old_run_id)
                    if control is None or old_run is None:
                        raise RuntimeError("replacement_cancel_recovery_chain_broken")
                    if old_run.status in {
                        "completed",
                        "failed",
                        "killed",
                        "skipped",
                        "needs_reconciliation",
                    }:
                        await fence_replacement_old_run(db, authority=authority, saga_id=saga.id)
                        await db.commit()
                    elif control.status == "accepted":
                        await begin_replacement_cancel(
                            db,
                            authority=authority,
                            saga_id=saga.id,
                            worker_id=worker_id,
                        )
                        await db.commit()
                        await signal_callback(
                            run_id=saga.old_run_id,
                            agent_id=agent.id,
                            session_id=session.id,
                            user_id=user.id,
                        )
                        counts["signalled"] += 1
                    elif control.status == "applying":
                        break
                    else:
                        raise RuntimeError(f"replacement_cancel_terminal_without_run_fence:{control.status}")
                elif state == "old_run_fenced":
                    await queue_fenced_replacement(db, authority=authority, saga_id=saga.id)
                    await db.commit()
                elif state == "replacement_queued":
                    replacement_run_id = uuid.uuid5(saga.id, "replacement-run")
                    await _start_replacement_runtime(
                        db=db,
                        authority=authority,
                        saga=saga,
                        agent=agent,
                        user=user,
                        session=session,
                        input_row=input_row,
                        run_id=replacement_run_id,
                    )
                    current = await db.get(SessionTurnReplacement, saga.id)
                    if current is not None and current.state == "replacement_queued":
                        await admit_replacement_run(
                            db,
                            authority=authority,
                            saga_id=saga.id,
                            run_id=replacement_run_id,
                        )
                    await db.commit()
                    counts["started_runs"] += 1
                elif state == "replacement_admitted":
                    await complete_turn_replacement(db, authority=authority, saga_id=saga.id)
                    await db.commit()
                    counts["completed"] += 1
                else:
                    break
                transitions += 1
                counts["transitioned"] += 1
                if state == "cancel_accepted" or state == "replacement_admitted":
                    break
        except Exception as exc:  # noqa: BLE001 - classify before retry or quarantine.
            await db.rollback()
            if _replacement_recovery_is_irrecoverable(exc):
                await _quarantine_turn_replacement(
                    db,
                    saga_id=saga_id,
                    worker_id=worker_id,
                    reason=f"{type(exc).__name__}:{exc}",
                )
                await db.commit()
                counts["needs_reconciliation"] += 1
            else:
                current = await db.scalar(
                    select(SessionTurnReplacement).where(SessionTurnReplacement.id == saga_id).with_for_update()
                )
                if current is not None and current.lease_owner == str(worker_id):
                    retry_delay = max(1.0, float(stale_after.total_seconds()) or 5.0)
                    current.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
                    await db.commit()
                    release_lease = False
                counts["retryable_failures"] += 1
                logger.warning(
                    "turn replacement {} transient recovery failure at generation {}: {}: {}",
                    saga_id,
                    getattr(current, "generation", "unknown"),
                    type(exc).__name__,
                    exc,
                )
        finally:
            if release_lease:
                current = await db.get(SessionTurnReplacement, saga_id)
                if current is not None and current.lease_owner == str(worker_id):
                    current.lease_owner = None
                    current.lease_expires_at = None
                    await db.commit()
    return counts
