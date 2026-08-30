"""Canonical adapters for production HumanInput and ControlInput ingress.

HTTP, WebSocket, command and channel surfaces may keep their transport-shaped
endpoints, but they must all enter the durable Session V2 command plane here
before a RuntimeTask or cancellation signal is created.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.models.user import User
from app.services.session_v2_persistence import (
    HumanInputReceipt,
    IdempotencyConflict,
    SESSION_COMMAND_AUTHORITY_STAMP_KEY,
    accept_human_input,
    a2a_delegation_peer_command_stamp,
    resolve_a2a_delegation_peer_authority,
    resolve_runtime_result_integration_authority,
    resolve_session_mutation_authority,
    runtime_result_integration_command_stamp,
)
from app.services.session_tool_runtime import assert_session_tool_effects_settled


def content_parts_from_live_ingress(
    *,
    content: str,
    parts: Iterable[dict[str, Any]] | None = None,
    attachments: Iterable[dict[str, Any]] | None = None,
    display_content: str = "",
    file_name: str = "",
    role: str = "user",
) -> list[dict[str, Any]]:
    """Preserve every ingress byte/reference without semantic pruning."""

    normalized_role = str(role or "user").strip().lower()
    if normalized_role not in {"user", "system"}:
        raise ValueError("live input role must be user or system")
    result: list[dict[str, Any]] = []
    if content:
        result.append(
            {
                "type": "text",
                "text": content,
                **({"display_content": display_content} if display_content else {}),
                **({"file_name": file_name} if file_name else {}),
                **({"role": normalized_role} if normalized_role != "user" else {}),
            }
        )
    result.extend(dict(part) for part in (parts or ()) if isinstance(part, dict))
    for attachment in attachments or ():
        if isinstance(attachment, dict):
            result.append({"type": "attachment_ref", "attachment": dict(attachment)})
    if not result:
        raise ValueError("content_parts must not be empty")
    return result


def _runtime_content(content_parts: list[dict[str, Any]]) -> str:
    if len(content_parts) == 1 and isinstance(content_parts[0].get("text"), str):
        return str(content_parts[0]["text"])
    return json.dumps(content_parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _input_id(value: uuid.UUID | str | None) -> uuid.UUID:
    if value is None:
        return uuid.uuid4()
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def _resolve_live_intent(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    requested_kind: str,
    expected_turn_id: str | None,
    expected_run_id: uuid.UUID | str | None,
) -> tuple[str, str | None, uuid.UUID | None]:
    if requested_kind != "auto":
        run_uuid = (
            expected_run_id
            if isinstance(expected_run_id, uuid.UUID)
            else uuid.UUID(str(expected_run_id))
            if expected_run_id
            else None
        )
        return requested_kind, expected_turn_id, run_uuid

    from app.services.web_chat_runtime import get_active_web_chat_run

    active = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    if not active:
        return "start_turn", None, None
    run_id = active.get("run_id")
    turn_id = active.get("turn_id")
    if not run_id or not turn_id:
        raise RuntimeError("active_run_missing_session_v2_target")
    return "steer_current_turn", str(turn_id), uuid.UUID(str(run_id))


async def submit_live_human_input(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
    source: str,
    input_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    requested_kind: str = "auto",
    expected_turn_id: str | None = None,
    expected_run_id: uuid.UUID | str | None = None,
    terminal_fallback: str = "queue_next_turn",
    request_item_id: uuid.UUID | str | None = None,
    fork_after_sequence: int | None = None,
    display_content: str = "",
    file_name: str = "",
    attachments: Iterable[dict[str, Any]] | None = None,
    parts: Iterable[dict[str, Any]] | None = None,
    plan_mode_requested: bool = False,
    runtime_metadata: dict[str, Any] | None = None,
    role: str = "user",
    a2a_peer_agent_id: uuid.UUID | str | None = None,
    runtime_result_page_id: uuid.UUID | str | None = None,
    runtime_result_page_claim_token: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Accept, Hook-admit and dispatch one production HumanInput."""

    from app.services.credential_boundary_loader import (
        RuntimeIngressSecretBoundaryUnavailable,
        exact_secret_redaction_receipt,
        redact_runtime_ingress_payload,
    )

    try:
        redaction = await redact_runtime_ingress_payload(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            reply_target=getattr(session, "delivery_target_json", None),
            payload={
                "content": content,
                "display_content": display_content,
                "file_name": file_name,
                "attachments": list(attachments or ()),
                "parts": list(parts or ()),
            },
        )
    except RuntimeIngressSecretBoundaryUnavailable as exc:
        raise RuntimeError("human_input_secret_boundary_unavailable") from exc
    protected = dict(redaction.value)
    content = str(protected["content"])
    display_content = str(protected["display_content"])
    file_name = str(protected["file_name"])
    attachments = list(protected["attachments"])
    parts = list(protected["parts"])

    input_uuid = _input_id(input_id)
    clean_source = str(source or "runtime").strip() or "runtime"
    key = str(idempotency_key or f"{clean_source}:human-input:{input_uuid}")
    runtime_metadata = {**dict(runtime_metadata or {}), "source": clean_source}
    redaction_receipt = exact_secret_redaction_receipt(
        redaction,
        phase="session_v2_human_input",
    )
    if redaction_receipt is not None:
        runtime_metadata["exact_secret_ingress_redaction"] = redaction_receipt
    if plan_mode_requested:
        runtime_metadata["plan_mode_requested"] = True
    content_parts = content_parts_from_live_ingress(
        content=content,
        parts=parts,
        attachments=attachments,
        display_content=display_content,
        file_name=file_name,
        role=role,
    )
    if a2a_peer_agent_id is not None and runtime_result_page_id is not None:
        raise ValueError("a2a peer and runtime result integration lanes are mutually exclusive")
    command_authority_stamp: dict[str, str] | None = None
    existing_row: SessionTurnInput | None = None
    if runtime_result_page_id is not None:
        # Narrow server-derived runtime result return lane: never the user
        # writable gate, never the a2a peer lane.  Only this lane resolves
        # the idempotency lookup BEFORE authority, because its admission vs
        # replay lifecycle split depends on it: a fresh accept must observe
        # the durable claimed processing delivery state with exact claim
        # equality; an already-accepted input (idempotent replay)
        # revalidates immutable route facts only, exactly like fresh-worker
        # recovery.  Every other lane keeps the original
        # authority-before-lookup ordering.
        existing_row = await db.get(SessionTurnInput, input_uuid)
        page_uuid = (
            runtime_result_page_id
            if isinstance(runtime_result_page_id, uuid.UUID)
            else uuid.UUID(str(runtime_result_page_id))
        )
        authority = await resolve_runtime_result_integration_authority(
            db,
            page_id=page_uuid,
            agent_id=agent.id,
            session_id=session.id,
            action="mutate_session_input",
            require_delivery_state=existing_row is None,
            expected_claim_token=runtime_result_page_claim_token,
        )
        command_authority_stamp = runtime_result_integration_command_stamp(page_id=page_uuid)
    elif a2a_peer_agent_id is not None:
        # Narrow server-derived A2A delegation peer lane: never the user
        # writable gate, always the full durable binding revalidation.
        authority = await resolve_a2a_delegation_peer_authority(
            db,
            peer_agent_id=a2a_peer_agent_id,
            agent_id=agent.id,
            session_id=session.id,
            action="mutate_session_input",
        )
        # The resolver already proved the durable delegation task binding,
        # so the session's runtime_task_id is present here.
        assert session.runtime_task_id is not None
        command_authority_stamp = a2a_delegation_peer_command_stamp(
            peer_agent_id=a2a_peer_agent_id
            if isinstance(a2a_peer_agent_id, uuid.UUID)
            else uuid.UUID(str(a2a_peer_agent_id)),
            delegation_runtime_task_id=session.runtime_task_id,
        )
    else:
        authority = await resolve_session_mutation_authority(
            db,
            user=user,
            agent_id=agent.id,
            session_id=session.id,
            action="mutate_session_input",
        )
    if runtime_result_page_id is None:
        # Ordinary owner/peer lanes: exactly one idempotency lookup AFTER
        # authority resolution.  The runtime result lane already performed
        # its single pre-authority lookup above (fresh admission and replay
        # alike), so it never queries twice.
        existing_row = await db.get(SessionTurnInput, input_uuid)
    existing_command = None
    if existing_row is not None:
        existing_command = await db.get(SessionCommand, existing_row.command_id)
        if existing_command is None:
            raise RuntimeError("accepted input is missing its command")
        if existing_row.tenant_id != authority.tenant_id or existing_row.session_id != authority.session_id:
            raise IdempotencyConflict(command=existing_command)
        original_target = dict(existing_command.target_json or {})
        original_turn_id = original_target.get("expected_turn_id")
        original_run_id = (
            uuid.UUID(str(original_target["expected_run_id"])) if original_target.get("expected_run_id") else None
        )
        original_request_item_id = (
            uuid.UUID(str(original_target["request_item_id"])) if original_target.get("request_item_id") else None
        )
        original_fork_after_sequence = original_target.get("fork_after_sequence")
        original_runtime_metadata = dict(original_target.get("runtime_metadata") or {})
        supplied_run_id = (
            expected_run_id
            if isinstance(expected_run_id, uuid.UUID)
            else uuid.UUID(str(expected_run_id))
            if expected_run_id
            else None
        )
        replay_conflicts = (
            (requested_kind != "auto" and requested_kind != existing_command.command_kind)
            or (expected_turn_id is not None and expected_turn_id != original_turn_id)
            or (supplied_run_id is not None and supplied_run_id != original_run_id)
            or (request_item_id is not None and uuid.UUID(str(request_item_id)) != original_request_item_id)
            or (fork_after_sequence is not None and int(fork_after_sequence) != original_fork_after_sequence)
            or (runtime_metadata is not None and dict(runtime_metadata) != original_runtime_metadata)
        )
        if replay_conflicts:
            raise IdempotencyConflict(command=existing_command)
        kind = existing_command.command_kind
        target_turn_id = str(original_turn_id) if original_turn_id is not None else None
        target_run_id = original_run_id
        terminal_fallback = str(original_target.get("terminal_fallback") or terminal_fallback)
        request_item_id = original_request_item_id
        fork_after_sequence = original_fork_after_sequence
        runtime_metadata = original_runtime_metadata
    else:
        kind, target_turn_id, target_run_id = await _resolve_live_intent(
            db,
            agent=agent,
            session=session,
            requested_kind=requested_kind,
            expected_turn_id=expected_turn_id,
            expected_run_id=expected_run_id,
        )
    if existing_row is None and kind in {"start_turn", "fork_side_thread"}:
        # Exact admission fence: an unknown prior tool effect must be resolved
        # by a platform operator before a fresh run or side branch can repeat
        # the same external action. Idempotent replays of an already accepted
        # command remain readable and never create a second command.
        await assert_session_tool_effects_settled(
            db,
            tenant_id=authority.tenant_id,
            session_id=authority.session_id,
        )
    intent = {
        "kind": kind,
        "input_id": str(input_uuid),
        "idempotency_key": key,
        "session_id": str(session.id),
        "content_parts": content_parts,
        **({"expected_turn_id": target_turn_id} if target_turn_id is not None else {}),
        **({"expected_run_id": str(target_run_id)} if target_run_id is not None else {}),
        **({"terminal_fallback": terminal_fallback} if kind == "steer_current_turn" else {}),
        **({"request_item_id": str(request_item_id)} if request_item_id is not None else {}),
        **({"fork_after_sequence": fork_after_sequence} if fork_after_sequence is not None else {}),
        **({"runtime_metadata": dict(runtime_metadata)} if runtime_metadata else {}),
        **(
            {SESSION_COMMAND_AUTHORITY_STAMP_KEY: dict(command_authority_stamp)}
            if command_authority_stamp is not None
            else {}
        ),
    }
    if existing_row is not None and existing_command is not None:
        replay_request = {"input_id": str(input_uuid), "content_parts": content_parts}
        replay_target = {
            name: intent[name]
            for name in (
                "expected_turn_id",
                "expected_run_id",
                "request_item_id",
                "fork_after_sequence",
                "terminal_fallback",
                "runtime_metadata",
                SESSION_COMMAND_AUTHORITY_STAMP_KEY,
            )
            if name in intent
        }
        if (
            existing_command.idempotency_key != key
            or existing_command.command_kind != kind
            or existing_command.request_json != replay_request
            or existing_command.target_json != replay_target
        ):
            raise IdempotencyConflict(command=existing_command)
        accepted_event = await db.scalar(
            select(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.command_id == existing_command.id,
                ChatTranscriptEvent.item_kind == "human_input",
                ChatTranscriptEvent.lifecycle == "accepted",
            )
            .order_by(ChatTranscriptEvent.sequence)
        )
        if accepted_event is None:
            raise RuntimeError("accepted command is missing its canonical event")
        accepted = HumanInputReceipt(
            command_id=existing_command.id,
            input_id=existing_row.id,
            idempotency_key=existing_command.idempotency_key,
            intent=existing_row.intent,
            revision=existing_row.revision,
            status=existing_row.status,
            accepted_sequence=accepted_event.sequence,
            queue_priority=existing_row.queue_priority,
            queue_ordinal=existing_row.queue_ordinal,
            target_turn_id=existing_row.target_turn_id,
            target_run_id=str(existing_row.target_run_id) if existing_row.target_run_id else None,
            bound_round_id=existing_row.bound_round_id,
            rolled_over_to_turn_id=existing_row.rolled_over_to_turn_id,
            replayed=True,
        )
    else:
        accepted = await accept_human_input(db, authority=authority, intent=intent)
    await db.commit()

    if accepted.replayed:
        replay_row = await db.get(SessionTurnInput, input_uuid)
        replay_admission = (
            await db.scalar(
                select(SessionInputAdmission).where(
                    SessionInputAdmission.input_id == input_uuid,
                    SessionInputAdmission.input_revision == replay_row.revision,
                )
            )
            if replay_row is not None
            else None
        )
        if replay_row is None or replay_admission is None:
            raise RuntimeError("accepted replay is missing its durable aggregate")
        replay_dispatch = dict(replay_admission.dispatch_receipt_json or {})
        replay_run: dict[str, Any] | None = None
        replay_is_settled = replay_row.status != "accepted"
        if kind == "start_turn":
            replay_run_id = uuid.uuid5(input_uuid, "session-v2-runtime-run")
            runtime_task = await db.get(RuntimeTask, replay_run_id)
            if runtime_task is not None:
                replay_run = {
                    "run_id": str(runtime_task.id),
                    "status": runtime_task.status,
                    "replayed": True,
                }
                replay_is_settled = True
        if replay_dispatch.get("run"):
            replay_run = dict(replay_dispatch["run"])
        elif replay_dispatch.get("run_id"):
            replay_run = {
                "run_id": str(replay_dispatch["run_id"]),
                "status": str(replay_dispatch.get("status") or "pending"),
                "replayed": True,
            }
        if replay_admission.dispatch_state == "dispatched":
            replay_is_settled = True
        if replay_admission.state in {"rejected", "cancelled", "needs_reconciliation"}:
            replay_is_settled = True
        if replay_is_settled:
            return _human_input_payload(
                accepted=accepted,
                row=replay_row,
                admission_state=replay_admission.state,
                reason_code=None,
                dispatch_status=f"replayed:{replay_admission.dispatch_state}",
                run=replay_run,
                dispatch_receipt=replay_dispatch,
            )

    from app.services.session_input_admission import run_user_prompt_admission

    outcome = await run_user_prompt_admission(
        db,
        authority=authority,
        input_id=input_uuid,
        worker_id=f"live-input:{clean_source}:{input_uuid}",
    )
    dispatch_receipt: dict[str, Any] = {}
    run_payload: dict[str, Any] | None = None
    dispatch_status = "not_dispatched"
    effective_admission_state = outcome.state
    effective_reason_code = outcome.reason_code
    if outcome.state == "admitted":
        from app.services.session_input_dispatch import dispatch_admitted_input_fast_path

        dispatched = await dispatch_admitted_input_fast_path(
            db,
            admission_id=outcome.admission_id,
            worker_id=f"live-dispatch:{clean_source}:{input_uuid}",
        )
        dispatch_receipt = dict(dispatched.receipt or {})
        dispatch_status = dispatched.state
        if dispatched.state == "needs_reconciliation":
            effective_admission_state = "needs_reconciliation"
            effective_reason_code = str(dispatch_receipt.get("code") or "") or None
        if dispatch_receipt.get("run"):
            run_payload = dict(dispatch_receipt["run"])
        elif dispatch_receipt.get("run_id"):
            run_payload = {
                "run_id": str(dispatch_receipt["run_id"]),
                "status": str(dispatch_receipt.get("status") or "pending"),
                "replayed": bool(dispatch_receipt.get("replayed")),
            }

    row = await db.get(SessionTurnInput, input_uuid)
    if row is None:
        raise RuntimeError("accepted input disappeared")
    return _human_input_payload(
        accepted=accepted,
        row=row,
        admission_state=effective_admission_state,
        reason_code=effective_reason_code,
        dispatch_status=dispatch_status,
        run=run_payload,
        dispatch_receipt=dispatch_receipt,
    )


def _human_input_payload(
    *,
    accepted: HumanInputReceipt,
    row: SessionTurnInput,
    admission_state: str,
    reason_code: str | None,
    dispatch_status: str,
    run: dict[str, Any] | None,
    dispatch_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "hive.human_input_receipt",
        "schema_version": 2,
        "command_id": str(row.command_id),
        "input_id": str(row.id),
        "idempotency_key": accepted.idempotency_key,
        "intent": row.intent,
        "revision": row.revision,
        "status": row.status,
        "accepted_sequence": accepted.accepted_sequence,
        "queue_priority": row.queue_priority,
        "queue_ordinal": row.queue_ordinal,
        "target_turn_id": row.target_turn_id,
        "target_run_id": str(row.target_run_id) if row.target_run_id else None,
        "bound_round_id": row.bound_round_id,
        "rolled_over_to_turn_id": row.rolled_over_to_turn_id,
        "admission_state": admission_state,
        "reason_code": reason_code,
        "dispatch_status": dispatch_status,
        "replayed": accepted.replayed,
        "run": run,
        "dispatch": dict(dispatch_receipt or {}),
    }


async def submit_live_cancel_input(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    run_id: uuid.UUID | str,
    source: str,
    idempotency_key: str | None = None,
    control_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Accept a durable cancel before signaling the running worker."""

    from app.services.session_control_input import accept_cancel_control_input, begin_cancel_control_input
    from app.services.web_chat_runtime import signal_web_chat_cancel

    run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    control_uuid = (
        control_id
        if isinstance(control_id, uuid.UUID)
        else uuid.UUID(str(control_id))
        if control_id is not None
        else uuid.uuid5(run_uuid, f"cancel:{user.id}")
    )
    authority = await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent.id,
        session_id=session.id,
        action="mutate_session_control",
    )
    accepted = await accept_cancel_control_input(
        db,
        authority=authority,
        control_id=control_uuid,
        idempotency_key=str(idempotency_key or f"cancel-run:{run_uuid}"),
        expected_run_id=run_uuid,
    )
    await db.commit()
    receipt = accepted
    if accepted.status != "rejected":
        receipt = await begin_cancel_control_input(
            db,
            authority=authority,
            control_id=control_uuid,
            worker_id=f"cancel-ingress:{source}:{control_uuid}",
        )
        await db.commit()
        if receipt.status == "applying" and not receipt.replayed:
            await signal_web_chat_cancel(
                run_id=run_uuid,
                agent_id=agent.id,
                session_id=session.id,
                user_id=user.id,
            )
    return {"schema": "hive.control_input_receipt", "schema_version": 2, **asdict(receipt)}


__all__ = [
    "IdempotencyConflict",
    "content_parts_from_live_ingress",
    "submit_live_cancel_input",
    "submit_live_human_input",
]
