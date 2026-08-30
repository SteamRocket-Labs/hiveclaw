"""Durable Session V2 tool invocation, effect fence, and result pairing."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.services.session_v2_persistence import SessionEventDraft, append_session_events


_DECISION_OUTCOMES = {"allow", "allow_prepare_only", "require_approval", "deny", "unavailable"}
_TERMINAL_TOOL_LIFECYCLES = {"completed", "failed", "denied", "unavailable", "cancelled"}
UNRESOLVED_TOOL_EFFECT_STATES = frozenset({"effect_started", "needs_reconciliation"})
TOOL_EFFECT_RECONCILIATION_TASK_STATUSES = frozenset({"failed", "needs_reconciliation"})


class ToolEffectReconciliationRequired(RuntimeError):
    """A prior tool may have taken effect but has no canonical terminal receipt."""

    code = "tool_effect_reconciliation_required"

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        run_ids: tuple[uuid.UUID, ...],
        invocation_ids: tuple[uuid.UUID, ...],
    ) -> None:
        super().__init__(self.code)
        self.session_id = session_id
        self.run_ids = run_ids
        self.invocation_ids = invocation_ids

    def http_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "session_id": str(self.session_id),
            "retryable": False,
        }


def unresolved_tool_effect_predicates(
    *,
    tenant_id: Any,
    session_id: Any | None = None,
    run_id: Any | None = None,
) -> tuple[Any, ...]:
    """Return the one exact SQL predicate shared by admission and recovery."""

    predicates: list[Any] = [
        SessionToolInvocation.tenant_id == tenant_id,
        SessionToolInvocation.result_event_id.is_(None),
        SessionToolInvocation.effect_state.in_(UNRESOLVED_TOOL_EFFECT_STATES),
        SessionToolInvocation.recovery_owner.is_not(None),
    ]
    if session_id is not None:
        predicates.append(SessionToolInvocation.session_id == session_id)
    if run_id is not None:
        predicates.append(SessionToolInvocation.run_id == run_id)
    return tuple(predicates)


async def list_unresolved_tool_effects(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    run_ids: tuple[uuid.UUID, ...] | list[uuid.UUID] | None = None,
    terminal_tasks_only: bool = True,
    for_update: bool = False,
    limit: int | None = 200,
) -> list[SessionToolInvocation]:
    """List effect-started invocations that still lack a settlement decision."""

    if run_ids is not None and not run_ids:
        return []
    stmt = select(SessionToolInvocation).where(
        *unresolved_tool_effect_predicates(
            tenant_id=tenant_id,
            session_id=session_id,
        )
    )
    if run_ids is not None:
        stmt = stmt.where(SessionToolInvocation.run_id.in_(tuple(run_ids)))
    if terminal_tasks_only:
        stmt = stmt.join(RuntimeTask, RuntimeTask.id == SessionToolInvocation.run_id).where(
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.status.in_(TOOL_EFFECT_RECONCILIATION_TASK_STATUSES),
        )
    stmt = stmt.order_by(SessionToolInvocation.run_id, SessionToolInvocation.id)
    if limit is not None:
        stmt = stmt.limit(max(1, min(int(limit), 500)))
    if for_update:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars().all())


def tool_effect_reconciliation_summary(
    invocations: list[SessionToolInvocation] | tuple[SessionToolInvocation, ...],
) -> dict[str, Any] | None:
    if not invocations:
        return None
    return {
        "required": True,
        "reason_code": "tool_effect_outcome_unknown",
        "unsettled_count": len(invocations),
        "run_ids": sorted({str(row.run_id) for row in invocations}),
        "invocation_ids": [str(row.id) for row in invocations],
        "tool_names": sorted({str(row.tool_name or "tool") for row in invocations}),
    }


async def assert_session_tool_effects_settled(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    """Fail closed before a new turn or branch can replay an unknown effect."""

    invocations = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        terminal_tasks_only=True,
        limit=1,
    )
    if not invocations:
        return
    raise ToolEffectReconciliationRequired(
        session_id=session_id,
        run_ids=tuple(sorted({row.run_id for row in invocations}, key=str)),
        invocation_ids=tuple(row.id for row in invocations),
    )


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope(result: SessionModelResult) -> dict[str, str]:
    return {
        "level": "round",
        "session_id": str(result.session_id),
        "thread_id": str(result.session_id),
        "turn_id": result.turn_id,
        "run_id": str(result.run_id),
        "round_id": result.round_id,
    }


def _turn_item_id(session_id: uuid.UUID, turn_id: str) -> uuid.UUID:
    return uuid.uuid5(session_id, f"session-turn:{turn_id}")


def _run_scope(result: SessionModelResult) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(result.session_id),
        "thread_id": str(result.session_id),
        "turn_id": result.turn_id,
        "run_id": str(result.run_id),
    }


def _progress_commentary_draft(
    *,
    invocation_id: uuid.UUID,
    item_id: uuid.UUID,
    result: SessionModelResult,
    clean_tool_name: str,
    clean_tool_use_id: str,
    args_payload: Mapping[str, Any],
) -> SessionEventDraft | None:
    """Project model-authored public progress without exposing generic tool arguments."""

    if clean_tool_name != "report_progress":
        return None
    public_message = args_payload.get("message")
    if not isinstance(public_message, str) or not public_message.strip():
        return None
    return SessionEventDraft(
        item_id=uuid.uuid5(invocation_id, "public-progress-commentary"),
        item_kind="assistant_commentary",
        lifecycle="completed",
        scope=_scope(result),
        actor={"type": "assistant"},
        payload={"phase": "commentary", "content": public_message},
        result_id=result.id,
        invocation_id=invocation_id,
        provider_tool_use_id=clean_tool_use_id,
        parent_item_id=item_id,
    )


async def _result_for_invocation(
    db: AsyncSession,
    invocation: SessionToolInvocation,
) -> SessionModelResult:
    result = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.tenant_id == invocation.tenant_id,
            SessionModelResult.session_id == invocation.session_id,
            SessionModelResult.run_id == invocation.run_id,
            SessionModelResult.round_id == invocation.round_id,
            SessionModelResult.provider_request_id == invocation.provider_request_id,
        )
    )
    if result is None:
        raise RuntimeError("tool_invocation_model_result_missing")
    return result


async def prepare_tool_invocation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    provider_request_id: str,
    provider_tool_use_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> SessionToolInvocation:
    """Persist the tool use and a pre-effect fence before execution authority."""

    result = await db.scalar(
        select(SessionModelResult)
        .where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.session_id == session_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.provider_request_id == str(provider_request_id),
        )
        .with_for_update()
    )
    if result is None or result.state in {"failed", "needs_reconciliation"}:
        raise RuntimeError("tool_invocation_requires_committed_model_result")
    clean_tool_use_id = str(provider_tool_use_id or "").strip()
    clean_tool_name = str(tool_name or "").strip()
    if not clean_tool_use_id or not clean_tool_name:
        raise ValueError("provider_tool_use_id and tool_name are required")
    args_payload = _canonical(dict(arguments))
    args_hash = _sha256({"tool_name": clean_tool_name, "arguments": args_payload})
    invocation_id = uuid.uuid5(result.id, f"tool-invocation:{clean_tool_use_id}")
    item_id = uuid.uuid5(invocation_id, "tool-call-item")
    authority_snapshot_hash = _sha256(
        {
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "run_id": str(run_id),
            "round_id": result.round_id,
            "provider_request_id": result.provider_request_id,
            "model_request_hash": result.model_request_hash,
            "provider_tool_use_id": clean_tool_use_id,
            "tool_name": clean_tool_name,
            "args_hash": args_hash,
        }
    )
    effect_key = f"session-tool:{invocation_id}"
    progress_draft = _progress_commentary_draft(
        invocation_id=invocation_id,
        item_id=item_id,
        result=result,
        clean_tool_name=clean_tool_name,
        clean_tool_use_id=clean_tool_use_id,
        args_payload=args_payload,
    )
    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.provider_request_id == result.provider_request_id,
            SessionToolInvocation.provider_tool_use_id == clean_tool_use_id,
        )
        .with_for_update()
    )
    if invocation is not None:
        if (
            invocation.id != invocation_id
            or invocation.args_hash != args_hash
            or invocation.authority_snapshot_hash != authority_snapshot_hash
            or invocation.run_id != run_id
        ):
            raise RuntimeError("provider_tool_use_id_conflict")
        if progress_draft is not None:
            commentary_exists = await db.scalar(
                select(ChatTranscriptEvent.id).where(
                    ChatTranscriptEvent.tenant_id == tenant_id,
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.invocation_id == invocation_id,
                    ChatTranscriptEvent.item_id == progress_draft.item_id,
                    ChatTranscriptEvent.item_kind == "assistant_commentary",
                )
            )
            if commentary_exists is None:
                await append_session_events(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    drafts=[progress_draft],
                )
        return invocation

    invocation = SessionToolInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        round_id=result.round_id,
        provider_request_id=result.provider_request_id,
        provider_tool_use_id=clean_tool_use_id,
        tool_name=clean_tool_name,
        provider_arguments_json=_canonical(arguments),
        invocation_item_id=item_id,
        args_hash=args_hash,
        authority_snapshot_hash=authority_snapshot_hash,
        effect_idempotency_key=effect_key,
        effect_state="prepared_not_started",
        version=1,
    )
    db.add(invocation)
    await db.flush()
    drafts: list[SessionEventDraft] = []
    if progress_draft is not None:
        drafts.append(progress_draft)
    drafts.append(
        SessionEventDraft(
            item_id=item_id,
            item_kind="tool_call",
            lifecycle="started",
            scope=_scope(result),
            actor={"type": "tool"},
            payload={
                "tool_name": clean_tool_name,
                "invocation_id": str(invocation_id),
                "provider_request_id": result.provider_request_id,
                "provider_tool_use_id": clean_tool_use_id,
                "args_hash": args_hash,
                "authority_snapshot_hash": authority_snapshot_hash,
                "authority_snapshot_ref": f"session-model-result:{result.id}",
                "effect_idempotency_key": effect_key,
                "effect_state": "prepared_not_started",
            },
            result_id=result.id,
            invocation_id=invocation_id,
            provider_tool_use_id=clean_tool_use_id,
            content_hash=args_hash,
        )
    )
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=drafts,
    )
    return invocation


async def mark_tool_effect_started(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation_id: uuid.UUID,
    effective_arguments: Mapping[str, Any] | None = None,
    permission_control_id: uuid.UUID | None = None,
) -> list[ChatTranscriptEvent]:
    """CAS the invocation before returning execution authority to the kernel."""

    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.id == invocation_id,
            SessionToolInvocation.tenant_id == tenant_id,
            SessionToolInvocation.session_id == session_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise RuntimeError("tool_invocation_not_found")
    if invocation.effect_state == "effect_started":
        return []
    if invocation.effect_state != "prepared_not_started":
        raise RuntimeError("tool_effect_start_requires_prepared_invocation")
    effective_payload = _canonical(effective_arguments or invocation.provider_arguments_json or {})
    effective_hash = _sha256({"tool_name": invocation.tool_name, "arguments": effective_payload})
    if invocation.permission_state == "waiting":
        raise RuntimeError("tool_effect_start_requires_applied_permission_response")
    if invocation.permission_state == "approved":
        if (
            permission_control_id is None
            or invocation.permission_receipt_ref != f"session-control:{permission_control_id}:applied"
        ):
            raise RuntimeError("tool_effect_start_permission_receipt_mismatch")
    elif permission_control_id is not None:
        raise RuntimeError("tool_effect_start_unexpected_permission_control")
    if invocation.effective_args_hash is not None and invocation.effective_args_hash != effective_hash:
        raise RuntimeError("tool_effect_start_effective_arguments_changed")
    result = await _result_for_invocation(db, invocation)
    fence_ref = f"session-tool-effect:{invocation.id}:generation:{int(invocation.version) + 1}"
    invocation.effect_state = "effect_started"
    invocation.execution_fence_ref = fence_ref
    invocation.effective_arguments_json = effective_payload
    invocation.effective_args_hash = effective_hash
    # Until the executor receipt and matching tool_result commit, a crashed
    # process must reconcile rather than replay a possibly-visible effect.
    invocation.recovery_owner = "session_tool_runtime:effect_receipt_pending"
    invocation.version = int(invocation.version) + 1
    return await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=invocation.invocation_item_id,
                item_kind="tool_call",
                lifecycle="progress",
                scope=_scope(result),
                actor={"type": "runtime"},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_request_id": invocation.provider_request_id,
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "effect_state": "effect_started",
                    "execution_fence_ref": fence_ref,
                    "effective_args_hash": effective_hash,
                    "permission_control_id": str(permission_control_id) if permission_control_id else None,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
            )
        ],
    )


def _settlement_from_evidence(
    invocation: SessionToolInvocation,
    evidence: Mapping[str, Any] | None,
    *,
    expected_input_hash: str,
) -> tuple[str, str, bool, dict[str, Any]] | None:
    if not isinstance(evidence, Mapping) or evidence.get("schema") != "hive.tool_execution_evidence.v1":
        return None
    machine_status = str(evidence.get("status") or "")
    if (
        machine_status == "aborted"
        and invocation.effect_state == "prepared_not_started"
        and str(evidence.get("pre_effect_fence_ref") or "")
    ):
        return "cancelled", "aborted", False, {}
    if machine_status == "cancelled":
        return "cancelled", "cancelled", bool(evidence.get("retryable", False)), {}
    decision = evidence.get("tool_decision")
    if not isinstance(decision, Mapping):
        return None
    outcome = str(decision.get("outcome") or "")
    if outcome not in _DECISION_OUTCOMES:
        return None
    if str(decision.get("input_hash") or "") != expected_input_hash:
        return None
    retryable = bool(evidence.get("retryable", False))
    frame = evidence.get("execution_frame")
    frame_status = str(frame.get("status") or "") if isinstance(frame, Mapping) else ""
    if outcome in {"allow", "allow_prepare_only"}:
        if frame_status == "completed":
            return "completed", "success", retryable, dict(decision)
        if frame_status == "failed":
            return "failed", "failed", retryable, dict(decision)
        return None
    if outcome == "deny":
        return "denied", "denied", retryable, dict(decision)
    if outcome == "unavailable":
        return "unavailable", "unavailable", retryable, dict(decision)
    return "waiting", "approval_required", retryable, dict(decision)


async def _terminal_events_for_invocation(
    db: AsyncSession,
    invocation_id: uuid.UUID,
) -> list[ChatTranscriptEvent]:
    return list(
        (
            await db.execute(
                select(ChatTranscriptEvent)
                .where(
                    or_(
                        ChatTranscriptEvent.invocation_id == invocation_id,
                        and_(
                            ChatTranscriptEvent.item_kind.in_(("run", "turn")),
                            ChatTranscriptEvent.metadata_json["v2_payload"]["invocation_id"].astext
                            == str(invocation_id),
                        ),
                    ),
                    (
                        (ChatTranscriptEvent.item_kind == "tool_result")
                        | (ChatTranscriptEvent.item_kind == "tool_permission")
                        | (
                            (ChatTranscriptEvent.item_kind == "tool_call")
                            & (ChatTranscriptEvent.lifecycle.in_(_TERMINAL_TOOL_LIFECYCLES))
                        )
                    ),
                )
                .order_by(ChatTranscriptEvent.sequence)
            )
        ).scalars()
    )


async def _waiting_events_for_invocation(
    db: AsyncSession,
    invocation_id: uuid.UUID,
) -> list[ChatTranscriptEvent]:
    return list(
        (
            await db.execute(
                select(ChatTranscriptEvent)
                .where(
                    or_(
                        ChatTranscriptEvent.invocation_id == invocation_id,
                        and_(
                            ChatTranscriptEvent.item_kind.in_(("run", "turn")),
                            ChatTranscriptEvent.metadata_json["v2_payload"]["invocation_id"].astext
                            == str(invocation_id),
                        ),
                    ),
                    (
                        (ChatTranscriptEvent.item_kind == "tool_permission")
                        | (ChatTranscriptEvent.item_kind.in_(("run", "turn")))
                        | (
                            (ChatTranscriptEvent.item_kind == "tool_call")
                            & (ChatTranscriptEvent.lifecycle == "waiting")
                        )
                    ),
                )
                .order_by(ChatTranscriptEvent.sequence)
            )
        ).scalars()
    )


async def complete_tool_invocation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation_id: uuid.UUID,
    provider_result_content: str,
    execution_evidence: Mapping[str, Any] | None,
    effective_arguments: Mapping[str, Any] | None = None,
    parts: list[dict[str, Any]] | None = None,
    message_id: uuid.UUID | None = None,
    permission_resolution: Mapping[str, Any] | None = None,
) -> list[ChatTranscriptEvent]:
    """Settle one invocation and append its unique Provider matching result."""

    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.id == invocation_id,
            SessionToolInvocation.tenant_id == tenant_id,
            SessionToolInvocation.session_id == session_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise RuntimeError("tool_invocation_not_found")
    if invocation.result_event_id is not None:
        return await _terminal_events_for_invocation(db, invocation.id)
    result = await _result_for_invocation(db, invocation)
    effective_payload = _canonical(
        effective_arguments or invocation.effective_arguments_json or invocation.provider_arguments_json or {}
    )
    effective_hash = _sha256({"tool_name": invocation.tool_name, "arguments": effective_payload})
    settlement = _settlement_from_evidence(
        invocation,
        execution_evidence,
        expected_input_hash=effective_hash,
    )
    if settlement is None:
        if invocation.effect_state != "needs_reconciliation":
            invocation.effect_state = "needs_reconciliation"
            invocation.recovery_owner = "session_tool_runtime:typed_settlement_missing"
            invocation.version = int(invocation.version) + 1
            return await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=invocation.invocation_item_id,
                        item_kind="tool_call",
                        lifecycle="needs_reconciliation",
                        scope=_scope(result),
                        actor={"type": "runtime"},
                        payload={
                            "invocation_id": str(invocation.id),
                            "provider_request_id": invocation.provider_request_id,
                            "provider_tool_use_id": invocation.provider_tool_use_id,
                            "recovery_owner": invocation.recovery_owner,
                            "execution_fence_ref": invocation.execution_fence_ref,
                        },
                        result_id=result.id,
                        invocation_id=invocation.id,
                        provider_tool_use_id=invocation.provider_tool_use_id,
                    )
                ],
            )
        return []

    lifecycle, outcome, retryable, decision = settlement
    decision_id = str(decision.get("decision_id") or "")
    if str(decision.get("outcome") or "") == "require_approval":
        existing_waiting = await _waiting_events_for_invocation(db, invocation.id)
        if existing_waiting:
            return existing_waiting
        if invocation.effect_state != "prepared_not_started":
            invocation.effect_state = "needs_reconciliation"
            invocation.recovery_owner = "session_tool_runtime:approval_after_effect_authority"
            invocation.version = int(invocation.version) + 1
            return await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=invocation.invocation_item_id,
                        item_kind="tool_call",
                        lifecycle="needs_reconciliation",
                        scope=_scope(result),
                        actor={"type": "runtime"},
                        payload={
                            "invocation_id": str(invocation.id),
                            "provider_tool_use_id": invocation.provider_tool_use_id,
                            "reason_code": "approval_requested_after_effect_authority",
                            "recovery_owner": invocation.recovery_owner,
                        },
                        result_id=result.id,
                        invocation_id=invocation.id,
                        provider_tool_use_id=invocation.provider_tool_use_id,
                    )
                ],
            )
        try:
            permission_item_id = uuid.UUID(str(decision.get("approval_id") or ""))
        except (TypeError, ValueError):
            permission_item_id = uuid.uuid5(invocation.id, "tool-permission-item")
        waiting_events = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=permission_item_id,
                    item_kind="tool_permission",
                    lifecycle="waiting",
                    scope=_scope(result),
                    actor={"type": "runtime"},
                    payload={
                        "invocation_id": str(invocation.id),
                        "provider_tool_use_id": invocation.provider_tool_use_id,
                        "decision_id": decision_id,
                        "approval_id": decision.get("approval_id"),
                        "retryable": retryable,
                    },
                    result_id=result.id,
                    invocation_id=invocation.id,
                    provider_tool_use_id=invocation.provider_tool_use_id,
                ),
                SessionEventDraft(
                    item_id=invocation.invocation_item_id,
                    item_kind="tool_call",
                    lifecycle="waiting",
                    scope=_scope(result),
                    actor={"type": "runtime"},
                    payload={
                        "invocation_id": str(invocation.id),
                        "provider_request_id": invocation.provider_request_id,
                        "provider_tool_use_id": invocation.provider_tool_use_id,
                        "permission_item_id": str(permission_item_id),
                        "decision_id": decision_id,
                        "effect_state": "prepared_not_started",
                    },
                    result_id=result.id,
                    invocation_id=invocation.id,
                    provider_tool_use_id=invocation.provider_tool_use_id,
                ),
                SessionEventDraft(
                    item_id=invocation.run_id,
                    item_kind="run",
                    lifecycle="waiting",
                    scope=_run_scope(result),
                    actor={"type": "runtime"},
                    payload={
                        "reason_code": "tool_permission_required",
                        "invocation_id": str(invocation.id),
                        "permission_item_id": str(permission_item_id),
                    },
                ),
                SessionEventDraft(
                    item_id=_turn_item_id(invocation.session_id, result.turn_id),
                    item_kind="turn",
                    lifecycle="waiting",
                    scope={
                        "level": "turn",
                        "session_id": str(invocation.session_id),
                        "thread_id": str(invocation.session_id),
                        "turn_id": result.turn_id,
                    },
                    actor={"type": "runtime"},
                    payload={
                        "reason_code": "tool_permission_required",
                        "invocation_id": str(invocation.id),
                        "permission_item_id": str(permission_item_id),
                    },
                ),
            ],
        )
        invocation.recovery_owner = "session_tool_runtime:awaiting_permission"
        invocation.effective_arguments_json = effective_payload
        invocation.effective_args_hash = effective_hash
        invocation.permission_item_id = permission_item_id
        invocation.permission_state = "waiting"
        invocation.permission_request_version = int(invocation.permission_request_version) + 1
        invocation.permission_authority_snapshot_hash = invocation.authority_snapshot_hash
        invocation.permission_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        invocation.version = int(invocation.version) + 1
        return waiting_events

    content = str(provider_result_content)
    content_hash = _sha256(content)
    if isinstance((execution_evidence or {}).get("execution_frame"), Mapping):
        receipt_ref = f"tool-frame:{((execution_evidence or {}).get('execution_frame') or {}).get('output_hash')}"
    elif str((execution_evidence or {}).get("pre_effect_fence_ref") or ""):
        receipt_ref = str((execution_evidence or {})["pre_effect_fence_ref"])
    else:
        receipt_ref = f"tool-decision:{decision_id}"
    drafts: list[SessionEventDraft] = []
    if permission_resolution is not None:
        permission_decision = str(permission_resolution.get("decision") or "")
        permission_lifecycle = "denied" if permission_decision == "deny" else "completed"
        drafts.append(
            SessionEventDraft(
                item_id=uuid.uuid5(invocation.id, "tool-permission-item"),
                item_kind="tool_permission",
                lifecycle=permission_lifecycle,
                scope=_scope(result),
                actor={
                    "type": "user",
                    "id": str(permission_resolution.get("resolver_user_id") or ""),
                },
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "decision_id": decision_id,
                    "decision": permission_decision,
                    "control_id": permission_resolution.get("control_id"),
                    "retryable": retryable,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
            )
        )
    drafts.extend(
        [
            SessionEventDraft(
                item_id=invocation.invocation_item_id,
                item_kind="tool_call",
                lifecycle=lifecycle,
                scope=_scope(result),
                actor={"type": "tool"},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_request_id": invocation.provider_request_id,
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "outcome": outcome,
                    "retryable": retryable,
                    "decision_id": decision_id,
                    "execution_fence_ref": invocation.execution_fence_ref,
                    "receipt_ref": receipt_ref,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
                message_id=message_id,
                content_hash=content_hash,
            ),
            SessionEventDraft(
                item_id=uuid.uuid5(invocation.id, "tool-result-item"),
                item_kind="tool_result",
                lifecycle="completed",
                scope=_scope(result),
                actor={"type": "tool"},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_request_id": invocation.provider_request_id,
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "outcome": outcome,
                    "retryable": retryable,
                    "content": content,
                    "content_hash": content_hash,
                    "content_or_error_ref": receipt_ref,
                    "parts": list(parts or []),
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
                message_id=message_id,
                content_hash=content_hash,
            ),
        ]
    )
    events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=drafts,
    )
    result_event = events[-1]
    if invocation.effective_arguments_json is None:
        invocation.effective_arguments_json = effective_payload
    if invocation.effective_args_hash is None:
        invocation.effective_args_hash = effective_hash
    if outcome == "success":
        invocation.effect_state = "effect_committed"
    elif invocation.effect_state == "effect_started":
        invocation.effect_state = "failed"
    else:
        # A denied/unavailable/cancelled request never received effect
        # authority. Keep that mechanical fact distinct from executor failure;
        # result_event_id is the terminal settlement fence.
        invocation.effect_state = "prepared_not_started"
    invocation.receipt_ref = receipt_ref
    invocation.result_event_id = result_event.id
    invocation.recovery_owner = None
    invocation.version = int(invocation.version) + 1
    return events


async def mark_tool_invocation_needs_reconciliation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation_id: uuid.UUID,
    reason_code: str,
    recovery_owner: str,
) -> list[ChatTranscriptEvent]:
    """Quarantine an unsettled invocation after a persistence failure.

    This fallback creates no semantic outcome. It preserves the executed
    effect as unknown and requires the canonical settlement path to be
    reconciled before any replay or later model round.
    """

    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.id == invocation_id,
            SessionToolInvocation.tenant_id == tenant_id,
            SessionToolInvocation.session_id == session_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise RuntimeError("tool_invocation_not_found")
    if invocation.result_event_id is not None:
        return await _terminal_events_for_invocation(db, invocation.id)
    existing = list(
        (
            await db.execute(
                select(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.invocation_id == invocation.id,
                    ChatTranscriptEvent.item_kind == "tool_call",
                    ChatTranscriptEvent.lifecycle == "needs_reconciliation",
                )
                .order_by(ChatTranscriptEvent.sequence)
            )
        ).scalars()
    )
    if existing:
        return existing
    previous_effect_state = invocation.effect_state
    invocation.effect_state = "needs_reconciliation"
    invocation.recovery_owner = str(recovery_owner)
    invocation.version = int(invocation.version) + 1
    result = await _result_for_invocation(db, invocation)
    return await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=invocation.invocation_item_id,
                item_kind="tool_call",
                lifecycle="needs_reconciliation",
                scope=_scope(result),
                actor={"type": "runtime"},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_request_id": invocation.provider_request_id,
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "reason_code": str(reason_code),
                    "previous_effect_state": previous_effect_state,
                    "recovery_owner": invocation.recovery_owner,
                    "execution_fence_ref": invocation.execution_fence_ref,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
            )
        ],
    )


async def acknowledge_unresolved_tool_effects(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    reason: str,
) -> list[ChatTranscriptEvent]:
    """Record an operator's explicit no-replay decision for unknown effects.

    The effect remains ``needs_reconciliation`` because the platform still
    cannot manufacture a success/failure result. Clearing ``recovery_owner``
    releases only the operational hold, backed by canonical reconciled events
    and an operator-authored reason. No ``tool_result`` is created.
    """

    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("tool effect acknowledgement reason is required")
    invocations = await list_unresolved_tool_effects(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        run_ids=(run_id,),
        terminal_tasks_only=True,
        for_update=True,
        limit=None,
    )
    if not invocations:
        return []

    drafts: list[SessionEventDraft] = []
    tool_event_ids: dict[uuid.UUID, uuid.UUID] = {}
    first_result: SessionModelResult | None = None
    for invocation in invocations:
        result = await _result_for_invocation(db, invocation)
        first_result = first_result or result
        event_id = uuid.uuid5(invocation.id, "operator-tool-effect-reconciled")
        tool_event_ids[invocation.id] = event_id
        drafts.append(
            SessionEventDraft(
                event_id=event_id,
                item_id=invocation.invocation_item_id,
                item_kind="tool_call",
                lifecycle="reconciled",
                scope=_scope(result),
                actor={"type": "user", "id": str(actor_user_id)},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "reason_code": "tool_effect_outcome_unknown",
                    "resolution": "operator_acknowledged_unknown_effect",
                    "operator_reason": clean_reason,
                    "execution_fence_ref": invocation.execution_fence_ref,
                    "creates_tool_result": False,
                    "replay_allowed": False,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
            )
        )
    assert first_result is not None
    drafts.append(
        SessionEventDraft(
            event_id=uuid.uuid5(run_id, "operator-tool-effect-recovery-action"),
            item_id=uuid.uuid5(run_id, "operator-tool-effect-recovery-item"),
            item_kind="recovery_action",
            lifecycle="reconciled",
            scope=_run_scope(first_result),
            actor={"type": "user", "id": str(actor_user_id)},
            payload={
                "reason_code": "tool_effect_outcome_unknown",
                "resolution": "operator_acknowledged_unknown_effect",
                "operator_reason": clean_reason,
                "invocation_ids": [str(row.id) for row in invocations],
                "replay_allowed": False,
            },
        )
    )
    events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=drafts,
    )
    for invocation in invocations:
        invocation.effect_state = "needs_reconciliation"
        invocation.receipt_ref = f"session-event://{tool_event_ids[invocation.id]}"
        invocation.recovery_owner = None
        invocation.version = int(invocation.version) + 1
    await db.flush()
    return events


async def apply_tool_permission_response(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    invocation_id: uuid.UUID,
    control_id: uuid.UUID,
    decision: str,
    resolver_user_id: uuid.UUID,
    response_schema: str,
) -> list[ChatTranscriptEvent]:
    """Apply one typed permission response without granting effect authority."""

    from app.models.session_v2 import SessionControlInput

    if decision not in {"allow_once", "allow_session", "deny"}:
        raise ValueError("unsupported_tool_permission_decision")
    invocation = await db.scalar(
        select(SessionToolInvocation)
        .where(
            SessionToolInvocation.id == invocation_id,
            SessionToolInvocation.tenant_id == tenant_id,
            SessionToolInvocation.session_id == session_id,
        )
        .with_for_update()
    )
    if invocation is None:
        raise RuntimeError("tool_invocation_not_found")
    control = await db.scalar(
        select(SessionControlInput)
        .where(
            SessionControlInput.id == control_id,
            SessionControlInput.tenant_id == tenant_id,
            SessionControlInput.session_id == session_id,
            SessionControlInput.kind == "permission_response",
        )
        .with_for_update()
    )
    if control is None or control.status != "applied":
        raise RuntimeError("tool_permission_requires_applied_control")
    if invocation.permission_item_id is None or control.request_item_id != invocation.permission_item_id:
        raise RuntimeError("tool_permission_request_item_mismatch")
    response = dict(control.response_payload_json or {})
    if str(response.get("decision") or "") != decision:
        raise RuntimeError("tool_permission_control_decision_mismatch")
    target_state = "denied" if decision == "deny" else "approved"
    if invocation.permission_state == target_state:
        return list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.invocation_id == invocation.id,
                        ChatTranscriptEvent.item_kind == "tool_permission",
                        ChatTranscriptEvent.lifecycle == ("denied" if decision == "deny" else "completed"),
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
    if invocation.permission_state != "waiting" or invocation.effect_state != "prepared_not_started":
        raise RuntimeError("tool_permission_response_requires_waiting_pre_effect_invocation")
    result = await _result_for_invocation(db, invocation)
    receipt_ref = f"session-control:{control.id}:applied"
    events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=invocation.permission_item_id,
                item_kind="tool_permission",
                lifecycle="denied" if decision == "deny" else "completed",
                scope=_scope(result),
                actor={"type": "user", "id": str(resolver_user_id)},
                payload={
                    "invocation_id": str(invocation.id),
                    "provider_tool_use_id": invocation.provider_tool_use_id,
                    "decision": decision,
                    "control_id": str(control.id),
                    "permission_receipt_ref": receipt_ref,
                },
                result_id=result.id,
                invocation_id=invocation.id,
                provider_tool_use_id=invocation.provider_tool_use_id,
                command_id=control.command_id,
            )
        ],
    )
    invocation.permission_state = target_state
    invocation.permission_response_schema = str(response_schema)
    invocation.permission_receipt_ref = receipt_ref
    invocation.recovery_owner = None if target_state == "denied" else "session_tool_runtime:approved_effect_pending"
    invocation.version = int(invocation.version) + 1
    return events


__all__ = [
    "TOOL_EFFECT_RECONCILIATION_TASK_STATUSES",
    "ToolEffectReconciliationRequired",
    "acknowledge_unresolved_tool_effects",
    "apply_tool_permission_response",
    "assert_session_tool_effects_settled",
    "complete_tool_invocation",
    "list_unresolved_tool_effects",
    "mark_tool_effect_started",
    "mark_tool_invocation_needs_reconciliation",
    "prepare_tool_invocation",
    "tool_effect_reconciliation_summary",
    "unresolved_tool_effect_predicates",
]
