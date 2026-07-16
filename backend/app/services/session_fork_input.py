"""Dispatch ``fork_side_thread`` HumanInput to a deterministic real branch."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.chat_transcript import lock_transcript_session
from app.services.conversation_branch_service import create_conversation_branch
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    SessionEventDraft,
    append_session_events,
)


@dataclass(frozen=True, slots=True)
class ForkSideThreadReceipt:
    command_id: uuid.UUID
    input_id: uuid.UUID
    status: str
    branch_session_id: uuid.UUID | None = None
    branch_run_id: uuid.UUID | None = None
    anchor_event_id: uuid.UUID | None = None
    reason_code: str | None = None
    provider_response_ref: str | None = None
    replayed: bool = False


def _session_scope(session_id: uuid.UUID) -> dict[str, str]:
    return {"level": "session", "session_id": str(session_id), "thread_id": str(session_id)}


def _runtime_content(parts: list[dict[str, Any]]) -> str:
    if len(parts) == 1 and isinstance(parts[0], dict):
        for key in ("text", "content"):
            value = parts[0].get(key)
            if isinstance(value, str) and value.strip():
                return value
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_settlement_ref(value: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    fields = str(value or "").split(":")
    if len(fields) < 3 or fields[0] != "fork-side-thread":
        return None
    try:
        return uuid.UUID(fields[1]), uuid.UUID(fields[2])
    except ValueError:
        return None


async def _fork_input_for_branch_run(
    db: AsyncSession,
    *,
    branch_run_id: uuid.UUID,
) -> SessionTurnInput | None:
    """Find the source-session input without forging a same-session run FK.

    ``SessionTurnInput.target_run_id`` is authority-bound to the input's own
    session.  A fork run belongs to the branch session, so the cross-session
    identity lives in the typed settlement receipt and is verified again
    against the RuntimeTask metadata before any transition.
    """

    branch_run = await db.get(RuntimeTask, branch_run_id)
    if branch_run is None:
        return None
    metadata = dict(branch_run.metadata_json or {})
    if "session_v2_fork_input_id" not in metadata and "session_v2_fork_source_session_id" not in metadata:
        return None
    try:
        source_input_id = uuid.UUID(str(metadata["session_v2_fork_input_id"]))
        source_session_id = uuid.UUID(str(metadata["session_v2_fork_source_session_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("fork_input_branch_runtime_identity_missing") from exc
    input_row = await db.scalar(
        select(SessionTurnInput)
        .where(
            SessionTurnInput.id == source_input_id,
            SessionTurnInput.intent == "fork_side_thread",
            SessionTurnInput.tenant_id == branch_run.tenant_id,
            SessionTurnInput.session_id == source_session_id,
        )
        .with_for_update()
    )
    if input_row is None:
        return None
    parsed = _parse_settlement_ref(input_row.settlement_ref)
    source_session = await db.get(ChatSession, source_session_id)
    if (
        parsed is None
        or parsed[1] != branch_run_id
        or branch_run.parent_session_id != str(parsed[0])
        or source_session is None
        or source_session.tenant_id != branch_run.tenant_id
        or source_session.agent_id != branch_run.parent_agent_id
    ):
        raise RuntimeError("fork_input_branch_receipt_authority_mismatch")
    return input_row


async def _locked_input(
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
        raise ValueError("fork_input_not_found")
    admission = await db.scalar(
        select(SessionInputAdmission)
        .where(
            SessionInputAdmission.input_id == input_id,
            SessionInputAdmission.input_revision == input_row.revision,
        )
        .with_for_update()
    )
    command = await db.get(SessionCommand, input_row.command_id)
    if admission is None or command is None or admission.command_id != input_row.command_id:
        raise RuntimeError("fork_input_authority_chain_broken")
    if (
        command.principal_id != authority.principal_id
        or command.tenant_id != authority.tenant_id
        or command.session_id != authority.session_id
    ):
        raise ValueError("fork_input_principal_mismatch")
    return input_row, admission, command


async def _reject_fork(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_row: SessionTurnInput,
    command: SessionCommand,
    reason_code: str,
) -> ForkSideThreadReceipt:
    if input_row.status == "rejected":
        return ForkSideThreadReceipt(
            command.id,
            input_row.id,
            "rejected",
            reason_code=str((command.rejection_json or {}).get("reason_code") or reason_code),
            replayed=True,
        )
    input_row.status = "rejected"
    input_row.settlement_ref = f"session-input:{input_row.id}:rejected:{reason_code}"
    input_row.version = int(input_row.version) + 1
    command.status = "rejected"
    command.rejection_json = {"reason_code": reason_code}
    command.receipt_ref = input_row.settlement_ref
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=input_row.id,
                item_kind="human_input",
                lifecycle="rejected",
                scope=_session_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_row.id),
                    "intent": input_row.intent,
                    "reason_code": reason_code,
                },
                command_id=command.id,
                input_id=input_row.id,
            )
        ],
    )
    return ForkSideThreadReceipt(command.id, input_row.id, "rejected", reason_code=reason_code)


async def dispatch_fork_side_thread(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    agent: Agent,
    user: User,
    source_session: ChatSession,
    input_id: uuid.UUID | str,
    runtime_metadata: dict[str, Any] | None = None,
) -> ForkSideThreadReceipt:
    """Fork after the exact accepted sequence and start one deterministic run."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    input_row, admission, command = await _locked_input(db, authority=authority, input_id=input_uuid)
    parsed = _parse_settlement_ref(input_row.settlement_ref)
    if input_row.status in {"queued", "bound", "applied", "needs_reconciliation"} and parsed is not None:
        return ForkSideThreadReceipt(
            command.id,
            input_row.id,
            input_row.status,
            branch_session_id=parsed[0],
            branch_run_id=parsed[1],
            reason_code="provider_delivery_unknown" if input_row.status == "needs_reconciliation" else None,
            replayed=True,
        )
    if input_row.intent != "fork_side_thread":
        raise ValueError("human_input_is_not_fork")
    if admission.state != "admitted" or input_row.status != "accepted":
        raise ValueError("fork_input_not_dispatchable")
    if input_row.fork_after_sequence is None:
        return await _reject_fork(
            db,
            authority=authority,
            input_row=input_row,
            command=command,
            reason_code="fork_sequence_required",
        )
    anchor = await db.scalar(
        select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == authority.session_id,
            ChatTranscriptEvent.agent_id == authority.agent_id,
            ChatTranscriptEvent.tenant_id == authority.tenant_id,
            ChatTranscriptEvent.sequence == input_row.fork_after_sequence,
        )
    )
    if anchor is None:
        return await _reject_fork(
            db,
            authority=authority,
            input_row=input_row,
            command=command,
            reason_code="fork_sequence_not_found",
        )

    branch_session_id = uuid.uuid5(input_row.id, "fork-side-thread-session")
    branch_run_id = uuid.uuid5(input_row.id, "fork-side-thread-run")
    branch = await create_conversation_branch(
        db=db,
        agent=agent,
        user=user,
        source_session=source_session,
        mode="branch",
        anchor_event_id=anchor.id,
        branch_session_id=branch_session_id,
        include_anchor_override=True,
    )
    from app.services.web_chat_runtime import start_web_chat_run

    await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=branch.session,
        content=_runtime_content(list(input_row.content_parts_json or [])),
        display_content="",
        append_user_message=True,
        run_id=branch_run_id,
        parts=list(input_row.content_parts_json or []),
        extra_metadata={
            **dict(runtime_metadata or {}),
            "source": "fork_side_thread",
            "source_session_id": str(source_session.id),
            "source_input_id": str(input_row.id),
            "fork_after_sequence": input_row.fork_after_sequence,
            "anchor_event_id": str(anchor.id),
            "session_v2_fork_input_id": str(input_row.id),
            "session_v2_fork_source_session_id": str(source_session.id),
        },
    )

    # ``start_web_chat_run`` commits the branch + RuntimeTask.  Re-lock the
    # source aggregate and settle its delivery receipt in the next crash-safe
    # transaction; replay finds the deterministic branch/run and finishes it.
    input_row, _admission, command = await _locked_input(db, authority=authority, input_id=input_uuid)
    if input_row.status == "accepted":
        input_row.status = "queued"
        input_row.target_turn_id = f"turn-{branch_run_id.hex}"
        # The branch RuntimeTask belongs to a different ChatSession.  Keep the
        # source input's same-session ``target_run_id`` empty and carry the
        # cross-session identity in the typed settlement receipt instead.
        input_row.target_run_id = None
        input_row.settlement_ref = f"fork-side-thread:{branch_session_id}:{branch_run_id}"
        input_row.version = int(input_row.version) + 1
        command.receipt_ref = f"{input_row.settlement_ref}:queued"
        await append_session_events(
            db,
            tenant_id=authority.tenant_id,
            agent_id=authority.agent_id,
            session_id=authority.session_id,
            drafts=[
                SessionEventDraft(
                    item_id=input_row.id,
                    item_kind="human_input",
                    lifecycle="queued",
                    scope=_session_scope(authority.session_id),
                    actor={"type": "runtime"},
                    payload={
                        "input_id": str(input_row.id),
                        "intent": input_row.intent,
                        "fork_after_sequence": input_row.fork_after_sequence,
                        "anchor_event_id": str(anchor.id),
                        "branch_session_id": str(branch_session_id),
                        "branch_run_id": str(branch_run_id),
                        "target_turn_id": input_row.target_turn_id,
                        "delivery_state": "awaiting_provider_receipt",
                        "settlement_ref": input_row.settlement_ref,
                    },
                    command_id=command.id,
                    input_id=input_row.id,
                )
            ],
        )
    return ForkSideThreadReceipt(
        command.id,
        input_row.id,
        input_row.status,
        branch_session_id=branch_session_id,
        branch_run_id=branch_run_id,
        anchor_event_id=anchor.id,
        replayed=bool(branch.branch.get("replayed")),
    )


async def mark_fork_input_bound(
    db: AsyncSession,
    *,
    branch_run_id: uuid.UUID | str,
    round_id: str,
    model_request_snapshot_ref: str,
) -> ForkSideThreadReceipt:
    """Bind a fork input to the branch's exact pre-dispatch request snapshot."""

    run_uuid = branch_run_id if isinstance(branch_run_id, uuid.UUID) else uuid.UUID(str(branch_run_id))
    input_row = await _fork_input_for_branch_run(db, branch_run_id=run_uuid)
    if input_row is None:
        raise ValueError("fork_input_for_run_not_found")
    parsed = _parse_settlement_ref(input_row.settlement_ref)
    if parsed is None or parsed[1] != run_uuid:
        raise RuntimeError("fork_input_branch_receipt_missing")
    if input_row.status in {"bound", "applied", "needs_reconciliation"}:
        return ForkSideThreadReceipt(
            input_row.command_id,
            input_row.id,
            input_row.status,
            branch_session_id=parsed[0],
            branch_run_id=run_uuid,
            replayed=True,
        )
    if input_row.status != "queued":
        raise ValueError("fork_input_not_queueable_for_bind")
    source_session = await db.get(ChatSession, input_row.session_id)
    if source_session is None:
        raise RuntimeError("fork_source_session_missing")
    input_row.status = "bound"
    input_row.bound_round_id = str(round_id)
    input_row.model_request_snapshot_ref = str(model_request_snapshot_ref)
    input_row.version = int(input_row.version) + 1
    await append_session_events(
        db,
        tenant_id=input_row.tenant_id,
        agent_id=source_session.agent_id,
        session_id=input_row.session_id,
        drafts=[
            SessionEventDraft(
                item_id=input_row.id,
                item_kind="human_input",
                lifecycle="bound",
                scope=_session_scope(input_row.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_row.id),
                    "branch_session_id": str(parsed[0]),
                    "branch_run_id": str(run_uuid),
                    "bound_round_id": str(round_id),
                    "model_request_snapshot_ref": str(model_request_snapshot_ref),
                },
                command_id=input_row.command_id,
                input_id=input_row.id,
            )
        ],
    )
    return ForkSideThreadReceipt(
        input_row.command_id,
        input_row.id,
        "bound",
        branch_session_id=parsed[0],
        branch_run_id=run_uuid,
    )


async def settle_fork_input_provider_delivery(
    db: AsyncSession,
    *,
    branch_run_id: uuid.UUID | str,
    provider_response_ref: str,
    delivery_state: str,
) -> ForkSideThreadReceipt:
    """Settle only from a proven response, or freeze an ambiguous delivery."""

    run_uuid = branch_run_id if isinstance(branch_run_id, uuid.UUID) else uuid.UUID(str(branch_run_id))
    clean_ref = str(provider_response_ref or "").strip()
    if not clean_ref:
        raise ValueError("provider_response_ref is required")
    state = str(delivery_state or "").strip().lower()
    if state not in {"delivered", "unknown"}:
        raise ValueError("fork provider delivery state must be delivered or unknown")
    input_row = await _fork_input_for_branch_run(db, branch_run_id=run_uuid)
    if input_row is None:
        raise ValueError("fork_input_for_run_not_found")
    parsed = _parse_settlement_ref(input_row.settlement_ref)
    if parsed is None or parsed[1] != run_uuid:
        raise RuntimeError("fork_input_branch_receipt_missing")
    digest = hashlib.sha256(clean_ref.encode("utf-8")).hexdigest()
    if input_row.status in {"applied", "needs_reconciliation"}:
        if not str(input_row.settlement_ref or "").endswith(f":{digest}"):
            raise ValueError("fork_input_provider_receipt_conflict")
        return ForkSideThreadReceipt(
            input_row.command_id,
            input_row.id,
            input_row.status,
            branch_session_id=parsed[0],
            branch_run_id=run_uuid,
            reason_code="provider_delivery_unknown" if input_row.status == "needs_reconciliation" else None,
            provider_response_ref=clean_ref,
            replayed=True,
        )
    if input_row.status != "bound" or not input_row.model_request_snapshot_ref:
        raise RuntimeError("fork_input_provider_delivery_without_bound_snapshot")
    command = await db.get(SessionCommand, input_row.command_id)
    source_session = await db.get(ChatSession, input_row.session_id)
    branch_run = await db.get(RuntimeTask, run_uuid)
    if command is None or source_session is None or branch_run is None:
        raise RuntimeError("fork_input_provider_delivery_authority_chain_broken")
    branch_metadata = dict(branch_run.metadata_json or {})
    if (
        branch_run.tenant_id != input_row.tenant_id
        or branch_run.parent_agent_id != source_session.agent_id
        or branch_run.parent_session_id != str(parsed[0])
        or str(branch_metadata.get("session_v2_fork_input_id") or "") != str(input_row.id)
        or str(branch_metadata.get("session_v2_fork_source_session_id") or "") != str(input_row.session_id)
    ):
        raise RuntimeError("fork_input_provider_delivery_authority_mismatch")
    if state == "unknown":
        input_row.status = "needs_reconciliation"
        input_row.recovery_owner = "runtime_task_worker:fork_provider_delivery"
        lifecycle = "needs_reconciliation"
        reason_code = "provider_delivery_unknown"
        command.receipt_ref = f"fork-side-thread:{parsed[0]}:{run_uuid}:needs_reconciliation:{digest}"
    else:
        input_row.status = "applied"
        input_row.recovery_owner = None
        lifecycle = "applied"
        reason_code = None
        command.status = "applied"
        command.receipt_ref = f"fork-side-thread:{parsed[0]}:{run_uuid}:applied:{digest}"
    input_row.settlement_ref = command.receipt_ref
    input_row.version = int(input_row.version) + 1
    await append_session_events(
        db,
        tenant_id=input_row.tenant_id,
        agent_id=source_session.agent_id,
        session_id=input_row.session_id,
        drafts=[
            SessionEventDraft(
                item_id=input_row.id,
                item_kind="human_input",
                lifecycle=lifecycle,
                scope=_session_scope(input_row.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_row.id),
                    "branch_session_id": str(parsed[0]),
                    "branch_run_id": str(run_uuid),
                    "bound_round_id": input_row.bound_round_id,
                    "provider_response_ref": clean_ref,
                    "delivery_state": state,
                    "reason_code": reason_code,
                    "recovery_owner": input_row.recovery_owner,
                    "settlement_ref": input_row.settlement_ref,
                },
                command_id=input_row.command_id,
                input_id=input_row.id,
            )
        ],
    )
    return ForkSideThreadReceipt(
        input_row.command_id,
        input_row.id,
        input_row.status,
        branch_session_id=parsed[0],
        branch_run_id=run_uuid,
        reason_code=reason_code,
        provider_response_ref=clean_ref,
    )


__all__ = [
    "ForkSideThreadReceipt",
    "dispatch_fork_side_thread",
    "mark_fork_input_bound",
    "settle_fork_input_provider_delivery",
]
