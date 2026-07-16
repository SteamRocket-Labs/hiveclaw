"""Typed, idempotent Session V2 control-input authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionControlInput, SessionToolInvocation
from app.services.chat_transcript import lock_transcript_session
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    IdempotencyConflict,
    SessionEventDraft,
    append_session_events,
    register_session_command,
)


_ACTIVE_RUN_STATUSES = frozenset({"pending", "running", "suspended", "resumable"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "killed", "skipped", "needs_reconciliation"})
_TOOL_PERMISSION_DECISIONS = frozenset({"allow_once", "allow_session", "deny"})
_TOOL_PERMISSION_RESPONSE_SCHEMA = "hive.tool_permission_response.v1"


@dataclass(frozen=True, slots=True)
class ControlInputReceipt:
    command_id: uuid.UUID
    control_id: uuid.UUID
    status: str
    accepted_sequence: int | None = None
    reason_code: str | None = None
    recovery_action: str | None = None
    replayed: bool = False


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _turn_id(task: RuntimeTask) -> str:
    metadata = dict(task.metadata_json or {})
    return str(metadata.get("turn_id") or f"turn-{task.id.hex}")


def _run_scope(session_id: uuid.UUID, task: RuntimeTask) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": _turn_id(task),
        "run_id": str(task.id),
    }


def _run_scope_for_id(session_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, str]:
    # A rejected unknown target cannot claim a run-scoped event: the database
    # authority trigger correctly requires every event.run_id to resolve to an
    # existing RuntimeTask. Keep the attempted id only in the typed payload.
    return {
        "level": "session",
        "session_id": str(session_id),
        "thread_id": str(session_id),
    }


async def _append_control_rejected_event(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    command: SessionCommand,
    control_id: uuid.UUID,
    run_id: uuid.UUID,
    reason_code: str,
    task: RuntimeTask | None = None,
) -> int:
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=control_id,
                item_kind="control_input",
                lifecycle="rejected",
                scope=(
                    _run_scope(authority.session_id, task)
                    if task is not None
                    else _run_scope_for_id(authority.session_id, run_id)
                ),
                actor={"type": "runtime"},
                payload={
                    "control_id": str(control_id),
                    "expected_run_id": str(run_id),
                    "reason_code": reason_code,
                },
                command_id=command.id,
            )
        ],
    )
    return events[0].sequence


async def _locked_run(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    run_id: uuid.UUID,
) -> RuntimeTask | None:
    await lock_transcript_session(db, session_id=authority.session_id)
    return await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == authority.tenant_id,
            RuntimeTask.parent_agent_id == authority.agent_id,
            RuntimeTask.parent_session_id == str(authority.session_id),
        )
        .with_for_update()
    )


async def _locked_control(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID,
) -> tuple[SessionControlInput, SessionCommand]:
    await lock_transcript_session(db, session_id=authority.session_id)
    control = await db.scalar(
        select(SessionControlInput)
        .where(
            SessionControlInput.id == control_id,
            SessionControlInput.tenant_id == authority.tenant_id,
            SessionControlInput.session_id == authority.session_id,
        )
        .with_for_update()
    )
    if control is None:
        raise ValueError("control_input_not_found")
    command = await db.get(SessionCommand, control.command_id)
    if (
        command is None
        or command.principal_id != authority.principal_id
        or command.tenant_id != authority.tenant_id
        or command.session_id != authority.session_id
    ):
        raise ValueError("control_input_principal_mismatch")
    return control, command


async def _accepted_sequence(db: AsyncSession, command_id: uuid.UUID) -> int | None:
    return await db.scalar(
        select(ChatTranscriptEvent.sequence)
        .where(
            ChatTranscriptEvent.command_id == command_id,
            ChatTranscriptEvent.item_kind == "control_input",
            ChatTranscriptEvent.lifecycle == "accepted",
        )
        .order_by(ChatTranscriptEvent.sequence)
    )


def _permission_recovery_action(decision: str, status: str) -> str | None:
    if status == "applied" and decision in {"allow_once", "allow_session"}:
        return "execute_approved_tool_effect"
    return None


def _permission_response_receipt(
    *,
    control: SessionControlInput,
    command: SessionCommand,
    accepted_sequence: int | None,
    replayed: bool,
) -> ControlInputReceipt:
    response = dict(control.response_payload_json or {})
    decision = str(response.get("decision") or "")
    rejection = dict(command.rejection_json or {})
    return ControlInputReceipt(
        command.id,
        control.id,
        control.status,
        accepted_sequence=accepted_sequence,
        reason_code=str(rejection.get("reason_code")) if rejection.get("reason_code") else None,
        recovery_action=_permission_recovery_action(decision, control.status),
        replayed=replayed,
    )


async def _locked_permission_invocation(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    invocation_id: uuid.UUID,
) -> SessionToolInvocation:
    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.id == invocation_id,
            SessionToolInvocation.tenant_id == authority.tenant_id,
            SessionToolInvocation.session_id == authority.session_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise ValueError("tool_permission_invocation_not_found")
    return invocation


def _validate_permission_binding(
    invocation: SessionToolInvocation,
    *,
    permission_item_id: uuid.UUID,
    permission_request_version: int,
    permission_authority_snapshot_hash: str,
    expected_run_id: uuid.UUID,
) -> None:
    if invocation.permission_item_id != permission_item_id:
        raise ValueError("tool_permission_request_item_mismatch")
    if int(invocation.permission_request_version) != int(permission_request_version):
        raise ValueError("tool_permission_request_version_mismatch")
    if (
        not invocation.permission_authority_snapshot_hash
        or invocation.permission_authority_snapshot_hash != permission_authority_snapshot_hash
    ):
        raise ValueError("tool_permission_authority_snapshot_mismatch")
    if invocation.run_id != expected_run_id:
        raise ValueError("tool_permission_expected_run_mismatch")
    expires_at = invocation.permission_expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("tool_permission_request_expired")


async def accept_tool_permission_response(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID | str,
    idempotency_key: str,
    invocation_id: uuid.UUID | str,
    permission_item_id: uuid.UUID | str,
    permission_request_version: int,
    permission_authority_snapshot_hash: str,
    expected_run_id: uuid.UUID | str,
    decision: str,
    response_schema: str,
    causation_command_id: uuid.UUID | str | None = None,
    command_id: uuid.UUID | str | None = None,
) -> ControlInputReceipt:
    """Accept one optimistic-concurrency-bound tool permission response."""

    control_uuid = control_id if isinstance(control_id, uuid.UUID) else uuid.UUID(str(control_id))
    invocation_uuid = invocation_id if isinstance(invocation_id, uuid.UUID) else uuid.UUID(str(invocation_id))
    request_item_uuid = (
        permission_item_id if isinstance(permission_item_id, uuid.UUID) else uuid.UUID(str(permission_item_id))
    )
    run_uuid = expected_run_id if isinstance(expected_run_id, uuid.UUID) else uuid.UUID(str(expected_run_id))
    clean_decision = str(decision or "").strip()
    clean_schema = str(response_schema or "").strip()
    clean_authority_hash = str(permission_authority_snapshot_hash or "").strip()
    if clean_decision not in _TOOL_PERMISSION_DECISIONS:
        raise ValueError("unsupported_tool_permission_decision")
    if clean_schema != _TOOL_PERMISSION_RESPONSE_SCHEMA:
        raise ValueError("unsupported_tool_permission_response_schema")
    if int(permission_request_version) <= 0:
        raise ValueError("tool_permission_request_version_must_be_positive")

    # Match the tool-runtime lock order (invocation -> session) so permission
    # acceptance cannot deadlock a concurrent settlement writer.
    invocation = await _locked_permission_invocation(
        db,
        authority=authority,
        invocation_id=invocation_uuid,
    )
    _validate_permission_binding(
        invocation,
        permission_item_id=request_item_uuid,
        permission_request_version=int(permission_request_version),
        permission_authority_snapshot_hash=clean_authority_hash,
        expected_run_id=run_uuid,
    )
    await lock_transcript_session(db, session_id=authority.session_id)

    response_payload = {"decision": clean_decision}
    response_hash = _sha256(response_payload)
    semantic_row = (
        await db.execute(
            select(SessionControlInput, SessionCommand)
            .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
            .where(
                SessionControlInput.tenant_id == authority.tenant_id,
                SessionControlInput.session_id == authority.session_id,
                SessionControlInput.kind == "permission_response",
                SessionControlInput.request_item_id == request_item_uuid,
                SessionControlInput.request_version == int(permission_request_version),
            )
            .order_by(SessionCommand.created_at, SessionCommand.id)
            .limit(1)
            .with_for_update()
        )
    ).first()
    if semantic_row is not None:
        existing_control, existing_command = semantic_row
        if (
            existing_command.principal_id != authority.principal_id
            or existing_control.expected_run_id != run_uuid
            or existing_control.authority_snapshot_hash != clean_authority_hash
            or existing_control.response_schema != clean_schema
            or existing_control.response_payload_hash != response_hash
        ):
            raise IdempotencyConflict(command=existing_command)
        return _permission_response_receipt(
            control=existing_control,
            command=existing_command,
            accepted_sequence=await _accepted_sequence(db, existing_command.id),
            replayed=True,
        )

    if invocation.permission_state != "waiting" or invocation.effect_state != "prepared_not_started":
        raise ValueError("tool_permission_response_requires_waiting_pre_effect_invocation")
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
    if task is None or task.status not in _ACTIVE_RUN_STATUSES:
        raise ValueError("tool_permission_active_run_not_found")

    control_id_owner = await db.get(SessionControlInput, control_uuid)
    if control_id_owner is not None:
        owner_command = await db.get(SessionCommand, control_id_owner.command_id)
        if owner_command is None:
            raise RuntimeError("control_input_command_missing")
        raise IdempotencyConflict(command=owner_command)

    causation_uuid = (
        causation_command_id
        if isinstance(causation_command_id, uuid.UUID)
        else uuid.UUID(str(causation_command_id))
        if causation_command_id is not None
        else None
    )
    registered = await register_session_command(
        db,
        authority=authority,
        namespace="control_input",
        command_kind="permission_response",
        idempotency_key=idempotency_key,
        request_payload={
            "control_id": str(control_uuid),
            "kind": "permission_response",
            "decision": clean_decision,
            "response_schema": clean_schema,
        },
        target_payload={
            "invocation_id": str(invocation.id),
            "permission_item_id": str(request_item_uuid),
            "permission_request_version": int(permission_request_version),
            "permission_authority_snapshot_hash": clean_authority_hash,
            "expected_run_id": str(run_uuid),
        },
        causation_command_id=causation_uuid,
        command_id=command_id,
    )
    command = registered.command
    if registered.replayed:
        existing_control = await db.scalar(
            select(SessionControlInput).where(SessionControlInput.command_id == command.id)
        )
        if existing_control is None:
            raise RuntimeError("permission_response_control_missing")
        return _permission_response_receipt(
            control=existing_control,
            command=command,
            accepted_sequence=await _accepted_sequence(db, command.id),
            replayed=True,
        )

    control = SessionControlInput(
        id=control_uuid,
        tenant_id=authority.tenant_id,
        session_id=authority.session_id,
        command_id=command.id,
        kind="permission_response",
        expected_run_id=run_uuid,
        request_item_id=request_item_uuid,
        request_version=int(permission_request_version),
        authority_snapshot_hash=clean_authority_hash,
        response_schema=clean_schema,
        response_payload_json=response_payload,
        response_payload_hash=response_hash,
        status="accepted",
        version=1,
    )
    db.add(control)
    await db.flush()
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=control.id,
                item_kind="control_input",
                lifecycle="accepted",
                scope=_run_scope(authority.session_id, task),
                actor={"type": "user", "id": str(authority.principal_id)},
                payload={
                    "control_id": str(control.id),
                    "kind": "permission_response",
                    "invocation_id": str(invocation.id),
                    "expected_run_id": str(run_uuid),
                    "permission_item_id": str(request_item_uuid),
                    "permission_request_version": int(permission_request_version),
                    "permission_authority_snapshot_hash": clean_authority_hash,
                    "response_schema": clean_schema,
                    "response_payload_hash": response_hash,
                    "decision": clean_decision,
                },
                command_id=command.id,
            )
        ],
    )
    command.receipt_ref = f"session-control:{control.id}:accepted:{events[0].sequence}"
    return ControlInputReceipt(
        command.id,
        control.id,
        "accepted",
        accepted_sequence=events[0].sequence,
    )


async def apply_permission_response_control_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID | str,
) -> ControlInputReceipt:
    """Atomically apply the control and its tool-permission consequence."""

    control_uuid = control_id if isinstance(control_id, uuid.UUID) else uuid.UUID(str(control_id))
    locator = (
        await db.execute(
            select(SessionControlInput, SessionCommand)
            .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
            .where(
                SessionControlInput.id == control_uuid,
                SessionControlInput.tenant_id == authority.tenant_id,
                SessionControlInput.session_id == authority.session_id,
            )
        )
    ).first()
    if locator is None:
        raise ValueError("control_input_not_found")
    located_control, located_command = locator
    if located_command.principal_id != authority.principal_id:
        raise ValueError("control_input_principal_mismatch")
    target = dict(located_command.target_json or {})
    try:
        invocation_uuid = uuid.UUID(str(target["invocation_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("permission_response_invocation_binding_missing") from exc

    invocation = await _locked_permission_invocation(
        db,
        authority=authority,
        invocation_id=invocation_uuid,
    )
    await lock_transcript_session(db, session_id=authority.session_id)
    row = (
        await db.execute(
            select(SessionControlInput, SessionCommand)
            .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
            .where(
                SessionControlInput.id == control_uuid,
                SessionControlInput.tenant_id == authority.tenant_id,
                SessionControlInput.session_id == authority.session_id,
            )
            .with_for_update()
        )
    ).first()
    if row is None:
        raise ValueError("control_input_not_found")
    control, command = row
    if command.principal_id != authority.principal_id:
        raise ValueError("control_input_principal_mismatch")
    if control.kind != "permission_response" or command.command_kind != "permission_response":
        raise ValueError("permission_response_control_required")
    response = dict(control.response_payload_json or {})
    decision = str(response.get("decision") or "")
    if control.status == "applied":
        return _permission_response_receipt(
            control=control,
            command=command,
            accepted_sequence=await _accepted_sequence(db, command.id),
            replayed=True,
        )
    if control.status != "accepted" or command.status != "accepted":
        return _permission_response_receipt(
            control=control,
            command=command,
            accepted_sequence=await _accepted_sequence(db, command.id),
            replayed=True,
        )
    if decision not in _TOOL_PERMISSION_DECISIONS:
        raise ValueError("unsupported_tool_permission_decision")
    if control.response_schema != _TOOL_PERMISSION_RESPONSE_SCHEMA:
        raise ValueError("unsupported_tool_permission_response_schema")
    if control.response_payload_hash != _sha256(response):
        raise ValueError("permission_response_payload_hash_mismatch")
    if control.request_item_id is None or control.request_version is None:
        raise ValueError("permission_response_request_binding_missing")
    _validate_permission_binding(
        invocation,
        permission_item_id=control.request_item_id,
        permission_request_version=control.request_version,
        permission_authority_snapshot_hash=control.authority_snapshot_hash,
        expected_run_id=control.expected_run_id,
    )
    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == control.expected_run_id,
            RuntimeTask.tenant_id == authority.tenant_id,
            RuntimeTask.parent_agent_id == authority.agent_id,
            RuntimeTask.parent_session_id == str(authority.session_id),
        )
        .with_for_update()
    )
    if task is None or task.status not in _ACTIVE_RUN_STATUSES:
        raise ValueError("tool_permission_active_run_not_found")

    control.status = "applied"
    control.settlement_ref = f"session-control:{control.id}:applied"
    control.recovery_owner = None
    control.version = int(control.version) + 1
    command.status = "applied"
    command.rejection_json = None
    command.receipt_ref = control.settlement_ref
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=control.id,
                item_kind="control_input",
                lifecycle="applied",
                scope=_run_scope(authority.session_id, task),
                actor={"type": "user", "id": str(authority.principal_id)},
                payload={
                    "control_id": str(control.id),
                    "kind": "permission_response",
                    "invocation_id": str(invocation.id),
                    "expected_run_id": str(control.expected_run_id),
                    "permission_item_id": str(control.request_item_id),
                    "permission_request_version": control.request_version,
                    "permission_authority_snapshot_hash": control.authority_snapshot_hash,
                    "decision": decision,
                    "settlement_ref": control.settlement_ref,
                    "state_version": control.version,
                },
                command_id=command.id,
            )
        ],
    )
    await db.flush()

    from app.services.session_tool_runtime import (
        apply_tool_permission_response as apply_runtime_permission_response,
        complete_tool_invocation,
    )

    await apply_runtime_permission_response(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        invocation_id=invocation.id,
        control_id=control.id,
        decision=decision,
        resolver_user_id=authority.principal_id,
        response_schema=control.response_schema,
    )
    if decision == "deny":
        denial_payload = {
            "schema": "hive.tool_permission_result.v1",
            "status": "denied",
            "reason_code": "permission_denied",
            "control_id": str(control.id),
            "invocation_id": str(invocation.id),
        }
        await complete_tool_invocation(
            db,
            tenant_id=authority.tenant_id,
            agent_id=authority.agent_id,
            session_id=authority.session_id,
            invocation_id=invocation.id,
            provider_result_content=json.dumps(
                denial_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            execution_evidence={
                "schema": "hive.tool_execution_evidence.v1",
                "status": "settled",
                "retryable": False,
                "tool_decision": {
                    "schema": "hive.tool_decision.v1",
                    "decision_id": f"permission-response:{control.id}:v{control.request_version}",
                    "outcome": "deny",
                    "input_hash": invocation.args_hash,
                    "policy_snapshot_hash": control.authority_snapshot_hash,
                },
                "execution_frame": None,
            },
        )
    return ControlInputReceipt(
        command.id,
        control.id,
        "applied",
        accepted_sequence=await _accepted_sequence(db, command.id),
        recovery_action=_permission_recovery_action(decision, "applied"),
        replayed=False,
    )


async def accept_cancel_control_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID | str,
    idempotency_key: str,
    expected_run_id: uuid.UUID | str,
    causation_command_id: uuid.UUID | str | None = None,
    command_id: uuid.UUID | str | None = None,
) -> ControlInputReceipt:
    """Accept a cancel command and its canonical event in one transaction."""

    control_uuid = control_id if isinstance(control_id, uuid.UUID) else uuid.UUID(str(control_id))
    run_uuid = expected_run_id if isinstance(expected_run_id, uuid.UUID) else uuid.UUID(str(expected_run_id))
    authority_hash = _sha256(
        {
            "tenant_id": authority.tenant_id,
            "agent_id": authority.agent_id,
            "principal_id": authority.principal_id,
            "session_id": authority.session_id,
            "expected_run_id": run_uuid,
            "authority_source": authority.authority_source,
        }
    )
    causation_uuid = (
        causation_command_id
        if isinstance(causation_command_id, uuid.UUID)
        else uuid.UUID(str(causation_command_id))
        if causation_command_id is not None
        else None
    )

    # A cancel is semantically unique for one authenticated principal, run and
    # causation chain. Network retries are allowed to arrive with a fresh
    # idempotency key/control id; replay the canonical receipt instead of
    # creating competing cancellation authorities or surfacing a 500.
    await lock_transcript_session(db, session_id=authority.session_id)
    semantic_statement = (
        select(SessionControlInput, SessionCommand)
        .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
        .where(
            SessionControlInput.tenant_id == authority.tenant_id,
            SessionControlInput.session_id == authority.session_id,
            SessionControlInput.kind == "cancel_run",
            SessionControlInput.expected_run_id == run_uuid,
            SessionCommand.principal_id == authority.principal_id,
            SessionCommand.causation_command_id == causation_uuid,
        )
        .order_by(SessionCommand.created_at, SessionCommand.id)
        .limit(1)
        .with_for_update()
    )
    semantic_result = await db.execute(semantic_statement)
    semantic_row = semantic_result.first()
    if semantic_row is not None:
        existing_control, existing_command = semantic_row
        rejection = dict(existing_command.rejection_json or {})
        return ControlInputReceipt(
            existing_command.id,
            existing_control.id,
            existing_control.status,
            accepted_sequence=await _accepted_sequence(db, existing_command.id),
            reason_code=str(rejection.get("reason_code")) if rejection.get("reason_code") else None,
            replayed=True,
        )

    control_id_owner = await db.get(SessionControlInput, control_uuid)
    if control_id_owner is not None:
        owner_command = await db.get(SessionCommand, control_id_owner.command_id)
        if owner_command is None:
            raise RuntimeError("control_input_command_missing")
        return ControlInputReceipt(
            owner_command.id,
            control_id_owner.id,
            "rejected",
            accepted_sequence=await _accepted_sequence(db, owner_command.id),
            reason_code="control_id_conflict",
            recovery_action="replay_with_server_receipt",
            replayed=True,
        )

    registered = await register_session_command(
        db,
        authority=authority,
        namespace="control_input",
        command_kind="cancel_run",
        idempotency_key=idempotency_key,
        request_payload={"control_id": str(control_uuid), "kind": "cancel_run"},
        target_payload={"expected_run_id": str(run_uuid), "authority_snapshot_hash": authority_hash},
        causation_command_id=causation_uuid,
        command_id=command_id,
    )
    command = registered.command
    if registered.replayed:
        control = await db.scalar(select(SessionControlInput).where(SessionControlInput.command_id == command.id))
        if control is None:
            rejection = dict(command.rejection_json or {})
            return ControlInputReceipt(
                command.id,
                control_uuid,
                command.status,
                reason_code=str(rejection.get("reason_code") or "control_input_rejected"),
                replayed=True,
            )
        return ControlInputReceipt(
            command.id,
            control.id,
            control.status,
            accepted_sequence=await _accepted_sequence(db, command.id),
            replayed=True,
        )

    task = await _locked_run(db, authority=authority, run_id=run_uuid)
    if task is None or task.status not in _ACTIVE_RUN_STATUSES:
        command.status = "rejected"
        command.rejection_json = {"reason_code": "active_run_not_found"}
        rejected_sequence = await _append_control_rejected_event(
            db,
            authority=authority,
            command=command,
            control_id=control_uuid,
            run_id=run_uuid,
            reason_code="active_run_not_found",
            task=task,
        )
        command.receipt_ref = f"session-control:{control_uuid}:rejected:{rejected_sequence}"
        return ControlInputReceipt(
            command.id,
            control_uuid,
            "rejected",
            reason_code="active_run_not_found",
            replayed=False,
        )
    response_hash = _sha256({})
    control = SessionControlInput(
        id=control_uuid,
        tenant_id=authority.tenant_id,
        session_id=authority.session_id,
        command_id=command.id,
        kind="cancel_run",
        expected_run_id=run_uuid,
        authority_snapshot_hash=authority_hash,
        response_payload_json={},
        response_payload_hash=response_hash,
        status="accepted",
        version=1,
    )
    db.add(control)
    await db.flush()
    events = await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=control_uuid,
                item_kind="control_input",
                lifecycle="accepted",
                scope=_run_scope(authority.session_id, task),
                actor={"type": "user", "id": str(authority.principal_id)},
                payload={
                    "control_id": str(control_uuid),
                    "kind": "cancel_run",
                    "expected_run_id": str(run_uuid),
                    "authority_snapshot_hash": authority_hash,
                    "response_payload_hash": response_hash,
                    "request_version": 1,
                },
                command_id=command.id,
            )
        ],
    )
    command.receipt_ref = f"session-control:{control_uuid}:accepted:{events[0].sequence}"
    return ControlInputReceipt(
        command.id,
        control_uuid,
        "accepted",
        accepted_sequence=events[0].sequence,
        replayed=False,
    )


async def begin_cancel_control_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID | str,
    worker_id: str,
) -> ControlInputReceipt:
    """Record cancelling intent without prematurely drawing a cancelled Run."""

    control_uuid = control_id if isinstance(control_id, uuid.UUID) else uuid.UUID(str(control_id))
    control, command = await _locked_control(db, authority=authority, control_id=control_uuid)
    if control.status != "accepted":
        return ControlInputReceipt(
            command.id,
            control.id,
            control.status,
            accepted_sequence=await _accepted_sequence(db, command.id),
            replayed=True,
        )
    task = await _locked_run(db, authority=authority, run_id=control.expected_run_id)
    if task is None or task.status not in _ACTIVE_RUN_STATUSES:
        control.status = "rejected"
        control.settlement_ref = f"session-control:{control.id}:rejected:run_not_active"
        control.recovery_owner = None
        control.version = int(control.version) + 1
        command.status = "rejected"
        command.rejection_json = {"reason_code": "run_not_active_before_cancel_start"}
        rejected_sequence = await _append_control_rejected_event(
            db,
            authority=authority,
            command=command,
            control_id=control.id,
            run_id=control.expected_run_id,
            reason_code="run_not_active_before_cancel_start",
            task=task,
        )
        command.receipt_ref = f"{control.settlement_ref}:{rejected_sequence}"
        return ControlInputReceipt(
            command.id,
            control.id,
            "rejected",
            accepted_sequence=await _accepted_sequence(db, command.id),
            reason_code="run_not_active_before_cancel_start",
        )
    metadata = dict(task.metadata_json or {})
    metadata.update(
        {
            "cancel_control_id": str(control.id),
            "cancel_command_id": str(command.id),
            "cancel_state": "cancelling",
            "cancel_requested_by": str(authority.principal_id),
            "cancel_worker_id": str(worker_id),
        }
    )
    task.metadata_json = metadata
    control.status = "applying"
    control.recovery_owner = str(worker_id)
    control.version = int(control.version) + 1
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=control.id,
                item_kind="control_input",
                lifecycle="started",
                scope=_run_scope(authority.session_id, task),
                actor={"type": "runtime"},
                payload={
                    "control_id": str(control.id),
                    "expected_run_id": str(task.id),
                    "worker_id": str(worker_id),
                    "state_version": control.version,
                },
                command_id=command.id,
            ),
            SessionEventDraft(
                item_id=task.id,
                item_kind="run",
                lifecycle="cancelling",
                scope=_run_scope(authority.session_id, task),
                actor={"type": "runtime"},
                payload={"run_id": str(task.id), "control_id": str(control.id)},
                command_id=command.id,
            ),
        ],
    )
    return ControlInputReceipt(
        command.id,
        control.id,
        "applying",
        accepted_sequence=await _accepted_sequence(db, command.id),
    )


async def settle_cancel_control_input(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    control_id: uuid.UUID | str,
    execution_fence_ref: str,
) -> ControlInputReceipt:
    """Observe a committed terminal fence; never manufacture Run terminality."""

    clean_fence = str(execution_fence_ref or "").strip()
    if not clean_fence:
        raise ValueError("execution_fence_ref is required")
    control_uuid = control_id if isinstance(control_id, uuid.UUID) else uuid.UUID(str(control_id))
    control, command = await _locked_control(db, authority=authority, control_id=control_uuid)
    if control.status in {"applied", "rejected", "failed", "needs_reconciliation"}:
        rejection = dict(command.rejection_json or {})
        return ControlInputReceipt(
            command.id,
            control.id,
            control.status,
            accepted_sequence=await _accepted_sequence(db, command.id),
            reason_code=str(rejection.get("reason_code")) if rejection.get("reason_code") else None,
            replayed=True,
        )
    task = await _locked_run(db, authority=authority, run_id=control.expected_run_id)
    if task is None:
        raise RuntimeError("cancel target disappeared after acceptance")
    if task.status in _ACTIVE_RUN_STATUSES:
        return ControlInputReceipt(
            command.id,
            control.id,
            control.status,
            accepted_sequence=await _accepted_sequence(db, command.id),
            reason_code="terminal_fence_not_committed",
            recovery_action="wait_for_runtime_terminal_commit",
            replayed=True,
        )
    await settle_pending_controls_for_run(
        db,
        task=task,
        execution_fence_ref=clean_fence,
        terminal_source="explicit_cancel_settlement",
    )
    await db.flush()
    rejection = dict(command.rejection_json or {})
    return ControlInputReceipt(
        command.id,
        control.id,
        control.status,
        accepted_sequence=await _accepted_sequence(db, command.id),
        reason_code=str(rejection.get("reason_code")) if rejection.get("reason_code") else None,
    )


async def settle_pending_controls_for_run(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    execution_fence_ref: str,
    terminal_source: str,
) -> dict[str, int]:
    """CAS-settle pending cancels from the RuntimeTask terminal fact.

    The terminal writer must place ``terminal_execution_fence_ref`` on the Run
    in this transaction before calling here. This function never changes Run
    status, so an arbitrary string cannot manufacture a cancelled Run.
    """

    clean_fence = str(execution_fence_ref or "").strip()
    metadata = dict(task.metadata_json or {})
    if not clean_fence:
        raise ValueError("execution_fence_ref is required")
    if task.status not in _TERMINAL_RUN_STATUSES:
        raise ValueError("runtime_task_terminal_status_required")
    if str(metadata.get("terminal_execution_fence_ref") or "") != clean_fence:
        raise ValueError("runtime_task_terminal_fence_mismatch")
    session_id = uuid.UUID(str(task.parent_session_id))
    if task.parent_agent_id is None or task.tenant_id is None:
        raise ValueError("runtime_task_session_authority_missing")

    await lock_transcript_session(db, session_id=session_id)
    locked_task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == task.id,
            RuntimeTask.tenant_id == task.tenant_id,
            RuntimeTask.parent_agent_id == task.parent_agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if locked_task is None:
        raise RuntimeError("runtime_task_disappeared_during_control_settlement")
    locked_metadata = dict(locked_task.metadata_json or {})
    if locked_task.status not in _TERMINAL_RUN_STATUSES:
        raise ValueError("runtime_task_terminal_status_required")
    if str(locked_metadata.get("terminal_execution_fence_ref") or "") != clean_fence:
        raise ValueError("runtime_task_terminal_fence_mismatch")

    controls = list(
        (
            await db.execute(
                select(SessionControlInput)
                .where(
                    SessionControlInput.tenant_id == locked_task.tenant_id,
                    SessionControlInput.session_id == session_id,
                    SessionControlInput.expected_run_id == locked_task.id,
                    SessionControlInput.kind == "cancel_run",
                    SessionControlInput.status.in_(("accepted", "applying")),
                )
                .order_by(SessionControlInput.id)
                .with_for_update()
            )
        ).scalars()
    )
    counts = {"applied": 0, "rejected": 0}
    for control in controls:
        command = await db.get(SessionCommand, control.command_id)
        if command is None:
            raise RuntimeError("control_input_command_missing")
        cancel_effect_committed = locked_task.status == "killed" and str(
            locked_metadata.get("cancel_control_id") or ""
        ) == str(control.id)
        control.recovery_owner = None
        control.version = int(control.version) + 1
        if cancel_effect_committed:
            locked_metadata.update(
                {
                    "cancel_state": "cancelled",
                    "cancelled_by_user": True,
                    "cancel_execution_fence_ref": clean_fence,
                }
            )
            locked_task.metadata_json = locked_metadata
            control.status = "applied"
            control.settlement_ref = f"session-control:{control.id}:applied:{_sha256(clean_fence)}"
            command.status = "applied"
            command.rejection_json = None
            command.receipt_ref = control.settlement_ref
            lifecycle = "applied"
            reason_code = None
            counts["applied"] += 1
        else:
            reason_code = "run_terminal_before_cancel_effect"
            control.status = "rejected"
            control.settlement_ref = f"session-control:{control.id}:rejected:{_sha256(clean_fence)}"
            command.status = "rejected"
            command.rejection_json = {"reason_code": reason_code}
            command.receipt_ref = control.settlement_ref
            lifecycle = "rejected"
            counts["rejected"] += 1

        drafts = [
            SessionEventDraft(
                item_id=control.id,
                item_kind="control_input",
                lifecycle=lifecycle,
                scope=_run_scope(session_id, locked_task),
                actor={"type": "runtime"},
                payload={
                    "control_id": str(control.id),
                    "expected_run_id": str(locked_task.id),
                    "execution_fence_ref": clean_fence,
                    "terminal_status": locked_task.status,
                    "terminal_source": str(terminal_source),
                    "settlement_ref": control.settlement_ref,
                    "state_version": control.version,
                    **({"reason_code": reason_code} if reason_code else {}),
                },
                command_id=command.id,
            )
        ]
        if cancel_effect_committed:
            drafts.append(
                SessionEventDraft(
                    item_id=locked_task.id,
                    item_kind="run",
                    lifecycle="cancelled",
                    scope=_run_scope(session_id, locked_task),
                    actor={"type": "runtime"},
                    payload={
                        "run_id": str(locked_task.id),
                        "control_id": str(control.id),
                        "execution_fence_ref": clean_fence,
                        "terminal_source": str(terminal_source),
                    },
                    command_id=command.id,
                )
            )
        await append_session_events(
            db,
            tenant_id=locked_task.tenant_id,
            agent_id=locked_task.parent_agent_id,
            session_id=session_id,
            drafts=drafts,
        )
    return counts


async def recover_stale_cancel_control_inputs_once(
    db: AsyncSession,
    *,
    worker_id: str,
    signal_callback: Callable[..., Awaitable[Any]] | None = None,
    stale_after: timedelta = timedelta(seconds=5),
    limit: int = 100,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Recover accepted/applying cancels on the existing task-worker tick.

    This sweep owns its transaction. It commits the durable ``applying`` state
    before emitting the idempotent external signal. A crash after that commit
    is recovered by re-signalling the same applying row on the next tick.
    """

    claimed_at = datetime.now(timezone.utc)
    cutoff = claimed_at - stale_after
    candidate_statement = (
        select(SessionControlInput.id)
        .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
        .where(
            SessionControlInput.kind == "cancel_run",
            SessionControlInput.status.in_(("accepted", "applying")),
            SessionCommand.updated_at <= cutoff,
        )
        .order_by(SessionCommand.updated_at, SessionControlInput.id)
        .limit(max(1, int(limit)))
    )
    if tenant_id is not None:
        candidate_statement = candidate_statement.where(SessionControlInput.tenant_id == tenant_id)
    candidate_ids = list((await db.execute(candidate_statement)).scalars())
    counts = {
        "claimed": 0,
        "started": 0,
        "signalled": 0,
        "settled": 0,
        "unavailable": 0,
        "local_delivered": 0,
    }
    signals: list[dict[str, Any]] = []
    for control_id in candidate_ids:
        locator = (
            await db.execute(select(SessionControlInput.session_id).where(SessionControlInput.id == control_id))
        ).scalar_one_or_none()
        if locator is None:
            continue
        await lock_transcript_session(db, session_id=locator)
        row = (
            await db.execute(
                select(SessionControlInput, SessionCommand, RuntimeTask)
                .join(SessionCommand, SessionCommand.id == SessionControlInput.command_id)
                .join(RuntimeTask, RuntimeTask.id == SessionControlInput.expected_run_id)
                .where(
                    SessionControlInput.id == control_id,
                    SessionControlInput.status.in_(("accepted", "applying")),
                    SessionCommand.updated_at <= cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).first()
        if row is None:
            continue
        control, command, task = row
        counts["claimed"] += 1
        if task.status in _TERMINAL_RUN_STATUSES:
            terminal_fence = str((task.metadata_json or {}).get("terminal_execution_fence_ref") or "")
            if terminal_fence:
                settled = await settle_pending_controls_for_run(
                    db,
                    task=task,
                    execution_fence_ref=terminal_fence,
                    terminal_source="cancel_recovery_sweep",
                )
                counts["settled"] += int(settled["applied"]) + int(settled["rejected"])
            continue
        if task.status not in _ACTIVE_RUN_STATUSES:
            continue
        if control.status == "accepted":
            metadata = dict(task.metadata_json or {})
            metadata.update(
                {
                    "cancel_control_id": str(control.id),
                    "cancel_command_id": str(command.id),
                    "cancel_state": "cancelling",
                    "cancel_requested_by": str(command.principal_id),
                    "cancel_worker_id": str(worker_id),
                }
            )
            task.metadata_json = metadata
            control.status = "applying"
            counts["started"] += 1
            await append_session_events(
                db,
                tenant_id=task.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=locator,
                drafts=[
                    SessionEventDraft(
                        item_id=control.id,
                        item_kind="control_input",
                        lifecycle="started",
                        scope=_run_scope(locator, task),
                        actor={"type": "runtime"},
                        payload={
                            "control_id": str(control.id),
                            "expected_run_id": str(task.id),
                            "worker_id": str(worker_id),
                            "recovered": True,
                            "state_version": int(control.version) + 1,
                        },
                        command_id=command.id,
                    ),
                    SessionEventDraft(
                        item_id=task.id,
                        item_kind="run",
                        lifecycle="cancelling",
                        scope=_run_scope(locator, task),
                        actor={"type": "runtime"},
                        payload={
                            "run_id": str(task.id),
                            "control_id": str(control.id),
                            "recovered": True,
                        },
                        command_id=command.id,
                    ),
                ],
            )
        control.recovery_owner = str(worker_id)
        control.version = int(control.version) + 1
        # SessionCommand.updated_at is the durable recovery lease timestamp.
        # The locked-query cutoff is rechecked after SKIP LOCKED so concurrent
        # worker instances cannot both emit the same signal in one lease window.
        command.updated_at = claimed_at
        if task.parent_agent_id is None:
            raise RuntimeError("cancel_target_agent_missing")
        signals.append(
            {
                "control_id": control.id,
                "command_id": command.id,
                "attempt_version": control.version,
                "run_id": task.id,
                "agent_id": task.parent_agent_id,
                "session_id": locator,
                "user_id": command.principal_id,
            }
        )

    # Deliberate durable/effect boundary: no signal before applying is durable.
    await db.commit()
    if signal_callback is None:
        from app.services.web_chat_runtime import signal_web_chat_cancel

        signal_callback = signal_web_chat_cancel
    for signal in signals:
        callback_payload = {
            key: value for key, value in signal.items() if key not in {"control_id", "command_id", "attempt_version"}
        }
        try:
            result = await signal_callback(**callback_payload)
        except Exception as exc:  # noqa: BLE001 - delivery failures are durable, typed, and retryable.
            local_delivered = bool(getattr(exc, "local_delivered", False))
            error_class = str(getattr(exc, "error_class", None) or type(exc).__name__)
            counts["unavailable"] += 1
            counts["local_delivered"] += int(local_delivered)
            await _record_cancel_signal_delivery_attempt(
                db,
                control_id=signal["control_id"],
                command_id=signal["command_id"],
                attempt_version=int(signal["attempt_version"]),
                worker_id=worker_id,
                delivery_state="unavailable",
                local_delivered=local_delivered,
                cross_process_delivered=False,
                retryable=bool(getattr(exc, "retryable", True)),
                error_class=error_class,
            )
            await db.commit()
            continue

        local_delivered = bool(getattr(result, "local_delivered", False))
        cross_process_delivered = bool(getattr(result, "cross_process_delivered", True))
        if not cross_process_delivered:
            error_class = str(getattr(result, "error_class", None) or "cancel_signal_delivery_unavailable")
            counts["unavailable"] += 1
            counts["local_delivered"] += int(local_delivered)
            await _record_cancel_signal_delivery_attempt(
                db,
                control_id=signal["control_id"],
                command_id=signal["command_id"],
                attempt_version=int(signal["attempt_version"]),
                worker_id=worker_id,
                delivery_state="unavailable",
                local_delivered=local_delivered,
                cross_process_delivered=False,
                retryable=bool(getattr(result, "retryable", True)),
                error_class=error_class,
            )
            await db.commit()
            continue

        counts["signalled"] += 1
        counts["local_delivered"] += int(local_delivered)
        await _record_cancel_signal_delivery_attempt(
            db,
            control_id=signal["control_id"],
            command_id=signal["command_id"],
            attempt_version=int(signal["attempt_version"]),
            worker_id=worker_id,
            delivery_state="delivered",
            local_delivered=local_delivered,
            cross_process_delivered=True,
            retryable=False,
            error_class=None,
        )
        await db.commit()
    return counts


async def _record_cancel_signal_delivery_attempt(
    db: AsyncSession,
    *,
    control_id: uuid.UUID,
    command_id: uuid.UUID,
    attempt_version: int,
    worker_id: str,
    delivery_state: str,
    local_delivered: bool,
    cross_process_delivered: bool,
    retryable: bool,
    error_class: str | None,
) -> None:
    """Persist transport facts without manufacturing Run terminality."""

    control = await db.get(SessionControlInput, control_id)
    if control is None:
        raise RuntimeError("cancel_signal_delivery_authority_missing")
    await lock_transcript_session(db, session_id=control.session_id)
    await db.refresh(control)
    command = await db.get(SessionCommand, command_id)
    if command is None:
        raise RuntimeError("cancel_signal_delivery_authority_missing")
    await db.refresh(command)
    task = await db.get(RuntimeTask, control.expected_run_id)
    if task is None:
        raise RuntimeError("cancel_signal_delivery_run_missing")
    await db.refresh(task)
    if task.parent_agent_id is None:
        raise RuntimeError("cancel_signal_delivery_run_missing")

    attempt_id = f"cancel-signal-delivery:{control.id}:{attempt_version}"
    metadata = dict(task.metadata_json or {})
    delivery_payload: dict[str, Any] = {
        "attempt_id": attempt_id,
        "delivery_state": delivery_state,
        "local_delivered": bool(local_delivered),
        "cross_process_delivered": bool(cross_process_delivered),
        "retryable": bool(retryable),
        "error_class": error_class,
    }
    metadata["cancel_signal_delivery"] = delivery_payload
    task.metadata_json = metadata
    control.recovery_owner = None
    command.updated_at = datetime.now(timezone.utc)

    lifecycle = "completed" if cross_process_delivered else "failed"
    await append_session_events(
        db,
        tenant_id=control.tenant_id,
        agent_id=task.parent_agent_id,
        session_id=control.session_id,
        drafts=[
            SessionEventDraft(
                item_id=uuid.uuid5(control.id, f"cancel-signal-delivery:{attempt_version}"),
                item_kind="recovery_action",
                lifecycle=lifecycle,
                scope=_run_scope(control.session_id, task),
                actor={"type": "runtime"},
                payload={
                    "control_id": str(control.id),
                    "expected_run_id": str(task.id),
                    "worker_id": str(worker_id),
                    **delivery_payload,
                    **({"recovery_action": "retry_after_cancel_signal_lease"} if retryable else {}),
                },
                command_id=command.id,
            )
        ],
    )
