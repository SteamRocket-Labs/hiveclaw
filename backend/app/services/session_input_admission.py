"""Crash-safe UserPromptSubmit admission for durable Session V2 inputs.

The database owns admission state and receipts.  Hook code runs only after a
committed ``hook.started`` boundary and its result is committed before any
Turn or provider work can be created.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.invocation_span import InvocationSpan
from app.models.chat_session import ChatSession
from app.models.session_v2 import (
    SessionCarryForward,
    SessionCommand,
    SessionInputAdmission,
    SessionTurnInput,
)
from app.runtime.hooks import HookEvent, HookResult, emit_hook
from app.services.chat_transcript import lock_transcript_session
from app.services.session_v2_persistence import (
    AuthenticatedSessionAuthority,
    SessionEventDraft,
    append_session_events,
    resolve_session_command_authority,
)


HookExecutor = Callable[..., Awaitable[HookResult | None]]
ManagedHookResultLookup = Callable[..., Awaitable["ManagedHookLookup"]]
_HOOK_TERMINAL_LIFECYCLES = frozenset({"completed", "failed", "blocked", "prevented", "cancelled"})


@dataclass(frozen=True, slots=True)
class AdmissionClaim:
    admission_id: uuid.UUID
    input_id: uuid.UUID
    hook_run_id: uuid.UUID
    state: str
    claimed: bool


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    admission_id: uuid.UUID
    input_id: uuid.UUID
    hook_run_id: uuid.UUID
    state: str
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedHookLookup:
    found: bool
    result: HookResult | None


async def current_input_admission(
    db: AsyncSession,
    *,
    input_id: uuid.UUID,
    input_revision: int,
    for_update: bool = False,
) -> SessionInputAdmission | None:
    statement = select(SessionInputAdmission).where(
        SessionInputAdmission.input_id == input_id,
        SessionInputAdmission.input_revision == int(input_revision),
    )
    if for_update:
        statement = statement.with_for_update()
    return await db.scalar(statement)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _scope(session_id: uuid.UUID) -> dict[str, str]:
    return {"level": "session", "session_id": str(session_id), "thread_id": str(session_id)}


def _prompt_bytes(parts: list[dict[str, Any]]) -> str:
    """Preserve the full input without applying semantic selection or truncation."""

    if len(parts) == 1 and isinstance(parts[0], dict):
        part = parts[0]
        for key in ("text", "content"):
            value = part.get(key)
            if isinstance(value, str):
                return value
    return _canonical_json(parts)


def _hook_payload(result: HookResult | None) -> tuple[str, dict[str, Any]]:
    if result is None:
        return "completed", {"decision": "allow"}
    raw = asdict(result)
    if result.failure:
        return "failed", {
            "decision": "executor_failure",
            "failure_code": result.failure_code or "hook_executor_failed",
            "retryable": bool(result.retryable),
            "reason": result.reason,
            "raw_result": raw,
        }
    if result.block:
        return "blocked", {"decision": "block", "reason": result.reason, "raw_result": raw}
    if result.prevent_continuation:
        return "prevented", {
            "decision": "prevent_continuation",
            "reason": result.stop_reason or result.reason,
            "raw_result": raw,
        }
    return "completed", {"decision": "allow", "reason": result.reason, "raw_result": raw}


async def _locked_admission(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID,
) -> tuple[SessionInputAdmission, SessionTurnInput]:
    await lock_transcript_session(db, session_id=authority.session_id)
    input_row = await db.get(SessionTurnInput, input_id)
    if input_row is None or input_row.tenant_id != authority.tenant_id or input_row.session_id != authority.session_id:
        raise ValueError("input_admission_not_found")
    admission = await current_input_admission(
        db,
        input_id=input_id,
        input_revision=input_row.revision,
        for_update=True,
    )
    if admission is None:
        raise ValueError("input_admission_not_found")
    if input_row is None or input_row.command_id != admission.command_id:
        raise RuntimeError("input_admission_authority_mismatch")
    command = await db.get(SessionCommand, admission.command_id)
    if (
        command is None
        or command.principal_id != authority.principal_id
        or command.session_id != authority.session_id
        or command.tenant_id != authority.tenant_id
    ):
        raise ValueError("input_admission_principal_mismatch")
    return admission, input_row


async def claim_user_prompt_admission(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    worker_id: str,
    lease_duration: timedelta = timedelta(minutes=2),
) -> AdmissionClaim:
    """CAS ``admission_pending`` to ``hook_running`` and commit started evidence.

    A stale ``hook_running`` lease is deliberately *not* reclaimed here.  A
    non-idempotent Hook may already have performed an effect; only an external
    idempotency lookup can prove a result.  The generic recovery path therefore
    records reconciliation instead of executing it again.
    """

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    admission, input_row = await _locked_admission(db, authority=authority, input_id=input_uuid)
    now = datetime.now(timezone.utc)
    if admission.state != "admission_pending":
        return AdmissionClaim(admission.id, input_uuid, admission.hook_run_id, admission.state, False)

    clean_worker = str(worker_id or "").strip()
    if not clean_worker:
        raise ValueError("worker_id is required")
    if (
        admission.lease_owner
        and admission.lease_owner != clean_worker
        and admission.lease_expires_at is not None
        and admission.lease_expires_at > now
    ):
        return AdmissionClaim(admission.id, input_uuid, admission.hook_run_id, admission.state, False)
    hook_item_id = admission.hook_item_id or uuid.uuid5(admission.hook_run_id, "session-v2-hook-item")
    admission.state = "hook_running"
    admission.lease_owner = clean_worker
    admission.lease_expires_at = now + lease_duration
    admission.version = int(admission.version) + 1
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="started",
                scope=_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(input_uuid),
                    "hook_run_id": str(admission.hook_run_id),
                    "state_version": admission.version,
                    "lease_owner": clean_worker,
                    "lease_expires_at": admission.lease_expires_at.isoformat(),
                },
                command_id=admission.command_id,
                input_id=input_uuid,
            ),
            SessionEventDraft(
                item_id=hook_item_id,
                item_kind="hook",
                lifecycle="started",
                scope=_scope(authority.session_id),
                actor={"type": "hook"},
                payload={
                    "boundary": "UserPromptSubmit",
                    "hook_run_id": str(admission.hook_run_id),
                    "hook_idempotency_key": admission.hook_idempotency_key,
                    "failure_policy": "continue",
                    "input_id": str(input_row.id),
                },
                command_id=admission.command_id,
                input_id=input_uuid,
            ),
        ],
    )
    # The tenant-binding trigger requires the referenced Hook Item to exist.
    # Assign the FK-like evidence ref only after both rows were flushed, while
    # still inside the same transaction.
    admission.hook_item_id = hook_item_id
    await db.flush()
    return AdmissionClaim(admission.id, input_uuid, admission.hook_run_id, admission.state, True)


async def _mark_uncertain_hook_reconciliation(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    admission: SessionInputAdmission,
    input_row: SessionTurnInput,
    recovery_owner: str,
    reason_code: str = "legacy_hook_effect_uncertain",
) -> AdmissionOutcome:
    admission.state = "needs_reconciliation"
    admission.recovery_owner = recovery_owner
    admission.lease_owner = None
    admission.lease_expires_at = None
    admission.version = int(admission.version) + 1
    input_row.status = "needs_reconciliation"
    input_row.recovery_owner = recovery_owner
    input_row.version = int(input_row.version) + 1
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="needs_reconciliation",
                scope=_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(input_row.id),
                    "hook_run_id": str(admission.hook_run_id),
                    "reason_code": reason_code,
                    "recovery_owner": recovery_owner,
                    "state_version": admission.version,
                },
                command_id=admission.command_id,
                input_id=input_row.id,
            ),
            SessionEventDraft(
                item_id=input_row.id,
                item_kind="human_input",
                lifecycle="needs_reconciliation",
                scope=_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_row.id),
                    "reason_code": reason_code,
                    "recovery_owner": recovery_owner,
                },
                command_id=admission.command_id,
                input_id=input_row.id,
            ),
        ],
    )
    return AdmissionOutcome(
        admission.id,
        input_row.id,
        admission.hook_run_id,
        "needs_reconciliation",
        reason_code,
    )


async def commit_user_prompt_hook_result(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    worker_id: str,
    result: HookResult | None,
) -> AdmissionOutcome:
    """Persist the immutable Hook terminal result before admission settlement."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    admission, input_row = await _locked_admission(db, authority=authority, input_id=input_uuid)
    if admission.state == "hook_result_committed":
        return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, admission.state)
    if admission.state not in {"hook_running"}:
        return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, admission.state)
    if admission.lease_owner != worker_id:
        raise ValueError("input_admission_lease_lost")

    lifecycle, result_payload = _hook_payload(result)
    result_hash = _sha256(result_payload)
    contexts = list(result.additional_contexts if result is not None else [])
    context_refs = [
        {
            "type": "hook_additional_context",
            "id": f"{admission.hook_run_id}:{index}:{_sha256(value)}",
            "locator": f"session-event-item:{admission.hook_item_id}#additional_contexts/{index}",
        }
        for index, value in enumerate(contexts)
    ]
    admission.hook_result_hash = result_hash
    admission.additional_context_refs_json = context_refs
    admission.state = "hook_result_committed"
    admission.lease_owner = None
    admission.lease_expires_at = None
    admission.version = int(admission.version) + 1
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=[
            SessionEventDraft(
                item_id=admission.hook_item_id,
                item_kind="hook",
                lifecycle=lifecycle,
                scope=_scope(authority.session_id),
                actor={"type": "hook"},
                payload={
                    "boundary": "UserPromptSubmit",
                    "hook_run_id": str(admission.hook_run_id),
                    "hook_idempotency_key": admission.hook_idempotency_key,
                    "failure_policy": "continue",
                    "result_hash": result_hash,
                    "additional_context_refs": context_refs,
                    "additional_contexts": contexts,
                    **result_payload,
                },
                command_id=admission.command_id,
                input_id=input_uuid,
                evidence_refs=tuple(context_refs),
                content_hash=result_hash,
            ),
            SessionEventDraft(
                item_id=admission.id,
                item_kind="input_admission",
                lifecycle="sealed",
                scope=_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "admission_id": str(admission.id),
                    "input_id": str(input_uuid),
                    "hook_run_id": str(admission.hook_run_id),
                    "hook_result_hash": result_hash,
                    "hook_lifecycle": lifecycle,
                    "additional_context_refs": context_refs,
                    "state_version": admission.version,
                },
                command_id=admission.command_id,
                input_id=input_uuid,
                content_hash=result_hash,
            ),
        ],
    )
    return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, admission.state)


async def _terminal_hook_payload(
    db: AsyncSession,
    *,
    admission: SessionInputAdmission,
) -> tuple[str, dict[str, Any]]:
    event = await db.scalar(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.session_id == admission.session_id,
            ChatTranscriptEvent.item_id == admission.hook_item_id,
            ChatTranscriptEvent.item_kind == "hook",
            ChatTranscriptEvent.lifecycle.in_(_HOOK_TERMINAL_LIFECYCLES),
        )
        .order_by(ChatTranscriptEvent.sequence.desc())
    )
    if event is None:
        raise RuntimeError("committed hook result is missing its immutable event")
    payload = dict((event.metadata_json or {}).get("v2_payload") or {})
    if admission.hook_result_hash != payload.get("result_hash"):
        raise RuntimeError("committed hook result hash mismatch")
    return str(event.lifecycle), payload


async def settle_user_prompt_admission(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
) -> AdmissionOutcome:
    """Apply a saved Hook result exactly once; this path never re-runs a Hook."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    admission, input_row = await _locked_admission(db, authority=authority, input_id=input_uuid)
    if admission.state in {"admitted", "rejected", "cancelled", "needs_reconciliation"}:
        return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, admission.state)
    if admission.state != "hook_result_committed":
        raise ValueError("input_admission_result_not_committed")
    hook_lifecycle, hook_payload = await _terminal_hook_payload(db, admission=admission)
    command = await db.get(SessionCommand, admission.command_id)
    if command is None:
        raise RuntimeError("input command missing during admission settlement")

    if hook_lifecycle == "blocked":
        target_state = "rejected"
        input_lifecycle = "rejected"
        reason_code = "user_prompt_submit_blocked"
    elif hook_lifecycle == "prevented":
        target_state = "cancelled"
        input_lifecycle = "cancelled"
        reason_code = "user_prompt_submit_prevented"
    else:
        # Executor failure is explicitly fail-open for UserPromptSubmit.
        target_state = "admitted"
        input_lifecycle = None
        reason_code = "hook_executor_failed_continue" if hook_lifecycle == "failed" else None

    admission.state = target_state
    admission.dispatch_state = "pending" if target_state == "admitted" else "not_applicable"
    admission.dispatch_receipt_json = {}
    admission.dispatch_last_error = None
    admission.carry_forward = "next_admitted_turn" if target_state == "cancelled" else "none"
    admission.version = int(admission.version) + 1
    input_row.version = int(input_row.version) + 1
    drafts = [
        SessionEventDraft(
            item_id=admission.id,
            item_kind="input_admission",
            lifecycle=target_state,
            scope=_scope(authority.session_id),
            actor={"type": "runtime"},
            payload={
                "admission_id": str(admission.id),
                "input_id": str(input_uuid),
                "hook_run_id": str(admission.hook_run_id),
                "hook_result_hash": admission.hook_result_hash,
                "hook_lifecycle": hook_lifecycle,
                "state_version": admission.version,
                "additional_context_refs": admission.additional_context_refs_json,
                "carry_forward": admission.carry_forward,
                "reason_code": reason_code,
            },
            command_id=admission.command_id,
            input_id=input_uuid,
        )
    ]
    if input_lifecycle is not None:
        input_row.status = input_lifecycle
        input_row.settlement_ref = f"session-input:{input_uuid}:{input_lifecycle}"
        command.status = "rejected"
        command.rejection_json = {"reason_code": reason_code}
        command.receipt_ref = input_row.settlement_ref
        drafts.append(
            SessionEventDraft(
                item_id=input_uuid,
                item_kind="human_input",
                lifecycle=input_lifecycle,
                scope=_scope(authority.session_id),
                actor={"type": "runtime"},
                payload={
                    "input_id": str(input_uuid),
                    "revision": input_row.revision,
                    "intent": input_row.intent,
                    "reason_code": reason_code,
                    "hook_run_id": str(admission.hook_run_id),
                    "carry_forward": admission.carry_forward,
                },
                command_id=admission.command_id,
                input_id=input_uuid,
            )
        )
    if target_state == "cancelled":
        carry_id = uuid.uuid5(admission.id, "prevented-prompt-context")
        context_item_id = uuid.uuid5(carry_id, "context-source")
        existing = await db.get(SessionCarryForward, carry_id)
        if existing is None:
            db.add(
                SessionCarryForward(
                    id=carry_id,
                    tenant_id=authority.tenant_id,
                    session_id=authority.session_id,
                    purpose="prevented_prompt_context",
                    source_admission_id=admission.id,
                    source_input_id=input_uuid,
                    source_hook_run_id=admission.hook_run_id,
                    source_evidence_refs_json=[
                        {"type": "human_input", "id": str(input_uuid)},
                        {"type": "hook", "id": str(admission.hook_item_id)},
                    ],
                    context_source_item_id=context_item_id,
                    state="pending",
                    claim_generation=0,
                    recovery_owner="session_input_carry_forward_worker",
                    version=1,
                )
            )
    await append_session_events(
        db,
        tenant_id=authority.tenant_id,
        agent_id=authority.agent_id,
        session_id=authority.session_id,
        drafts=drafts,
    )
    return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, target_state, reason_code)


async def _default_user_prompt_hook_executor(**kwargs: Any) -> HookResult | None:
    hook_run_id = str(kwargs.pop("hook_run_id"))
    metadata = dict(kwargs.pop("metadata", {}) or {})
    metadata["hook_run_id"] = hook_run_id
    return await emit_hook(
        HookEvent.USER_PROMPT_SUBMIT,
        evidence_mode="independent",
        metadata=metadata,
        **kwargs,
    )


async def _lookup_managed_hook_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    hook_run_id: uuid.UUID,
) -> ManagedHookLookup:
    spans = list(
        (
            await db.execute(
                select(InvocationSpan)
                .where(
                    InvocationSpan.tenant_id == tenant_id,
                    InvocationSpan.span_type == "hook",
                    InvocationSpan.name == f"hook.{HookEvent.USER_PROMPT_SUBMIT.value}",
                    InvocationSpan.metadata_json["hook_run_id"].astext == str(hook_run_id),
                )
                .order_by(InvocationSpan.created_at.desc(), InvocationSpan.id.desc())
                .limit(2)
            )
        ).scalars()
    )
    if not spans:
        return ManagedHookLookup(found=False, result=None)
    payloads = [dict((span.metadata_json or {}).get("hook_result_payload") or {}) for span in spans]
    hashes = [str((span.metadata_json or {}).get("hook_result_hash") or "") for span in spans]
    if any(not digest or digest != _sha256(payload) for payload, digest in zip(payloads, hashes, strict=True)):
        return ManagedHookLookup(found=False, result=None)
    if len(payloads) > 1 and payloads[0] != payloads[1]:
        return ManagedHookLookup(found=False, result=None)
    payload = payloads[0]
    result = HookResult(
        block=bool(payload.get("block")),
        reason=str(payload.get("reason") or ""),
        additional_contexts=[str(value) for value in list(payload.get("additional_contexts") or [])],
        prevent_continuation=bool(payload.get("prevent_continuation")),
        stop_reason=str(payload.get("stop_reason") or ""),
        failure=bool(payload.get("failure")),
        retryable=bool(payload.get("retryable")),
        failure_code=str(payload.get("failure_code") or "") or None,
    )
    return ManagedHookLookup(found=True, result=result)


async def _recovery_authority(
    db: AsyncSession,
    *,
    admission: SessionInputAdmission,
) -> AuthenticatedSessionAuthority:
    input_row = await db.get(SessionTurnInput, admission.input_id)
    command = await db.get(SessionCommand, admission.command_id)
    session = await db.get(ChatSession, admission.session_id)
    if (
        input_row is None
        or command is None
        or session is None
        or input_row.revision != admission.input_revision
        or input_row.command_id != admission.command_id
        or command.principal_id is None
    ):
        raise RuntimeError("input_admission_recovery_authority_chain_broken")
    if session.tenant_id != admission.tenant_id:
        raise RuntimeError("input_admission_recovery_authority_mismatch")
    context = await resolve_session_command_authority(
        db,
        command=command,
        session=session,
        action="mutate_session_input",
    )
    return context.authority


async def recover_stale_input_admissions_once(
    db: AsyncSession,
    *,
    worker_id: str,
    managed_result_lookup: ManagedHookResultLookup | None = None,
    pending_hook_executor: HookExecutor = _default_user_prompt_hook_executor,
    stale_after: timedelta = timedelta(minutes=2),
    tenant_id: uuid.UUID | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Recover both safe pre-Hook claims and uncertain post-Hook attempts.

    ``admission_pending`` proves that ``hook.started`` was never committed, so
    the worker may safely claim and execute the Hook.  ``hook_running`` is the
    opposite boundary: an external Hook effect may already have happened, and
    recovery must use its managed idempotency receipt or quarantine the input.
    """

    now = datetime.now(timezone.utc)
    stale_before = now - stale_after
    statement = (
        select(SessionInputAdmission)
        .join(SessionTurnInput, SessionTurnInput.id == SessionInputAdmission.input_id)
        .where(
            SessionInputAdmission.input_revision == SessionTurnInput.revision,
            or_(
                and_(
                    SessionInputAdmission.state == "admission_pending",
                    SessionInputAdmission.updated_at <= stale_before,
                ),
                and_(
                    SessionInputAdmission.state == "hook_running",
                    SessionInputAdmission.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(SessionInputAdmission.updated_at, SessionInputAdmission.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        statement = statement.where(SessionInputAdmission.tenant_id == tenant_id)
    admissions = list((await db.execute(statement)).scalars())
    claimed_ids: list[uuid.UUID] = []
    for admission in admissions:
        admission.lease_owner = str(worker_id)
        admission.lease_expires_at = now + timedelta(minutes=1)
        admission.version = int(admission.version) + 1
        claimed_ids.append(admission.id)
    await db.commit()

    counts = {"claimed": len(claimed_ids), "recovered": 0, "needs_reconciliation": 0}
    for admission_id in claimed_ids:
        admission = await db.get(SessionInputAdmission, admission_id)
        if admission is None:
            continue
        input_row = await db.get(SessionTurnInput, admission.input_id)
        if input_row is None or input_row.revision != admission.input_revision:
            continue
        authority = await _recovery_authority(db, admission=admission)
        if admission.state == "admission_pending":
            outcome = await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=admission.input_id,
                worker_id=worker_id,
                hook_executor=pending_hook_executor,
            )
            if outcome.state == "needs_reconciliation":
                counts["needs_reconciliation"] += 1
            else:
                counts["recovered"] += 1
            continue
        if managed_result_lookup is None:
            lookup = await _lookup_managed_hook_result(
                db,
                tenant_id=admission.tenant_id,
                hook_run_id=admission.hook_run_id,
            )
        else:
            lookup = await managed_result_lookup(
                hook_run_id=admission.hook_run_id,
                hook_idempotency_key=admission.hook_idempotency_key,
                tenant_id=admission.tenant_id,
                session_id=admission.session_id,
                input_id=admission.input_id,
                input_revision=admission.input_revision,
            )
        if not lookup.found:
            await _mark_uncertain_hook_reconciliation(
                db,
                authority=authority,
                admission=admission,
                input_row=input_row,
                recovery_owner="managed_hook_result_lookup",
                reason_code="managed_hook_result_unknown",
            )
            await db.commit()
            counts["needs_reconciliation"] += 1
            continue
        await commit_user_prompt_hook_result(
            db,
            authority=authority,
            input_id=admission.input_id,
            worker_id=worker_id,
            result=lookup.result,
        )
        await db.commit()
        await settle_user_prompt_admission(
            db,
            authority=authority,
            input_id=admission.input_id,
        )
        await db.commit()
        counts["recovered"] += 1
    return counts


async def run_user_prompt_admission(
    db: AsyncSession,
    *,
    authority: AuthenticatedSessionAuthority,
    input_id: uuid.UUID | str,
    worker_id: str,
    hook_executor: HookExecutor = _default_user_prompt_hook_executor,
    managed_hook: bool = False,
) -> AdmissionOutcome:
    """Run the full durable admission lane across explicit commit boundaries."""

    input_uuid = input_id if isinstance(input_id, uuid.UUID) else uuid.UUID(str(input_id))
    claim = await claim_user_prompt_admission(
        db,
        authority=authority,
        input_id=input_uuid,
        worker_id=worker_id,
    )
    if claim.claimed:
        # Hook.started is authoritative before external Hook execution.
        await db.commit()
        input_row = await db.get(SessionTurnInput, input_uuid)
        assert input_row is not None
        try:
            result = await hook_executor(
                hook_run_id=str(claim.hook_run_id),
                agent_id=authority.agent_id,
                session_id=str(authority.session_id),
                prompt=_prompt_bytes(list(input_row.content_parts_json or [])),
                source="session_input_admission",
                metadata={
                    "tenant_id": str(authority.tenant_id),
                    "principal_type": authority.principal_type,
                    "principal_id": str(authority.principal_id),
                    "command_id": str(input_row.command_id),
                    "input_id": str(input_uuid),
                },
            )
        except Exception as exc:  # executor failure is an observable fail-open boundary
            result = HookResult(
                failure=True,
                failure_code="hook_executor_exception",
                reason=type(exc).__name__,
                retryable=False,
            )
        await commit_user_prompt_hook_result(
            db,
            authority=authority,
            input_id=input_uuid,
            worker_id=worker_id,
            result=result,
        )
        # Immutable result is authoritative before settlement/Turn admission.
        await db.commit()
    elif claim.state == "hook_running":
        admission, input_row = await _locked_admission(db, authority=authority, input_id=input_uuid)
        now = datetime.now(timezone.utc)
        if admission.lease_expires_at is None or admission.lease_expires_at > now:
            return AdmissionOutcome(admission.id, input_uuid, admission.hook_run_id, admission.state)
        # Managed hooks require a provider-specific lookup/dedup adapter.  In
        # its absence, both managed and legacy effects remain quarantined.
        recovery_owner = "managed_hook_result_lookup" if managed_hook else "legacy_hook_manual_reconciliation"
        outcome = await _mark_uncertain_hook_reconciliation(
            db,
            authority=authority,
            admission=admission,
            input_row=input_row,
            recovery_owner=recovery_owner,
        )
        await db.commit()
        return outcome
    elif claim.state in {"admitted", "rejected", "cancelled", "needs_reconciliation"}:
        return AdmissionOutcome(claim.admission_id, input_uuid, claim.hook_run_id, claim.state)

    outcome = await settle_user_prompt_admission(db, authority=authority, input_id=input_uuid)
    await db.commit()
    return outcome
