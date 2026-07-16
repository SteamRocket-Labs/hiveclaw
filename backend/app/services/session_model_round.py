"""Durable Session V2 model-round input binding and provider receipts."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import (
    SessionCarryForward,
    SessionModelResult,
    SessionTurnInput,
)
from app.services.session_human_input import (
    bind_admitted_inputs_to_round,
    input_parts_to_runtime_messages,
    mark_bound_inputs_applied,
)
from app.services.session_v2_persistence import SessionEventDraft, append_session_events


class ModelRoundNeedsReconciliation(RuntimeError):
    """The provider-send fence is ambiguous and must never be replayed blindly."""


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    elif not isinstance(value, (dict, list, tuple, str, int, float, bool, type(None))):
        value = {
            key: getattr(value, key)
            for key in (
                "role",
                "content",
                "tool_calls",
                "tool_call_id",
                "reasoning_content",
                "reasoning_signature",
            )
            if getattr(value, key, None) is not None
        }
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _round_id(run_id: uuid.UUID, round_index: int, continuation_index: int = 0) -> str:
    base = f"{run_id}:round:{int(round_index)}"
    if int(continuation_index) > 0:
        return f"{base}:output-continuation:{int(continuation_index)}"
    return base


def _result_id(run_id: uuid.UUID, round_id: str) -> uuid.UUID:
    return uuid.uuid5(run_id, f"session-model-result:{round_id}")


def _snapshot_ref(result_id: uuid.UUID) -> str:
    return f"session-model-result:{result_id}"


def _scope(session_id: uuid.UUID, turn_id: str, run_id: uuid.UUID, round_id: str) -> dict[str, str]:
    return {
        "level": "round",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
        "round_id": round_id,
    }


def _run_scope(session_id: uuid.UUID, turn_id: str, run_id: uuid.UUID) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
    }


async def _bound_rows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    round_id: str,
) -> list[SessionTurnInput]:
    return list(
        (
            await db.execute(
                select(SessionTurnInput)
                .where(
                    SessionTurnInput.tenant_id == tenant_id,
                    SessionTurnInput.session_id == session_id,
                    SessionTurnInput.target_run_id == run_id,
                    SessionTurnInput.bound_round_id == round_id,
                    SessionTurnInput.status.in_(("bound", "applied")),
                )
                .order_by(SessionTurnInput.queue_ordinal)
            )
        ).scalars()
    )


async def _carry_context_messages(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: str,
    round_id: str,
    snapshot_ref: str,
) -> tuple[list[SessionCarryForward], list[dict[str, Any]]]:
    carries = list(
        (
            await db.execute(
                select(SessionCarryForward)
                .join(SessionTurnInput, SessionTurnInput.id == SessionCarryForward.source_input_id)
                .where(
                    SessionCarryForward.tenant_id == tenant_id,
                    SessionCarryForward.session_id == session_id,
                    SessionCarryForward.state.in_(("pending", "turn_claimed", "round_bound")),
                )
                .order_by(SessionTurnInput.queue_ordinal)
                .with_for_update()
            )
        ).scalars()
    )
    selected: list[SessionCarryForward] = []
    messages: list[dict[str, Any]] = []
    for carry in carries:
        if carry.state in {"turn_claimed", "round_bound"} and carry.target_turn_id != turn_id:
            continue
        if carry.state == "round_bound" and carry.target_round_id != round_id:
            continue
        hook_event = await db.scalar(
            select(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.input_id == carry.source_input_id,
                ChatTranscriptEvent.item_kind == "hook",
                ChatTranscriptEvent.lifecycle.in_(("completed", "failed", "prevented", "blocked")),
            )
            .order_by(ChatTranscriptEvent.sequence.desc())
        )
        if hook_event is None:
            carry.state = "needs_reconciliation"
            carry.recovery_owner = "session_model_round:missing_hook_evidence"
            carry.version = int(carry.version) + 1
            continue
        payload = dict((hook_event.metadata_json or {}).get("v2_payload") or {})
        contexts = list(payload.get("additional_contexts") or [])
        context_parts = [
            {
                "type": "text",
                "text": value if isinstance(value, str) else json.dumps(_canonical(value), ensure_ascii=False),
            }
            for value in contexts
        ]
        carry.state = "round_bound"
        carry.target_turn_id = turn_id
        carry.target_round_id = round_id
        carry.claim_generation = int(carry.claim_generation) + 1
        carry.claim_owner = f"model-round:{round_id}"
        carry.claim_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        carry.model_request_snapshot_ref = snapshot_ref
        carry.version = int(carry.version) + 1
        selected.append(carry)
        messages.append(
            {
                "role": "system",
                "content": "\n\n".join(part["text"] for part in context_parts),
                "llm_parts": context_parts,
                "session_carry_forward_id": str(carry.id),
                "source_evidence_refs": list(carry.source_evidence_refs_json or []),
            }
        )
    return selected, messages


async def bind_round_inputs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
) -> list[dict[str, Any]]:
    """Bind admitted inputs FIFO and return only durable rows for this exact round."""

    round_id = _round_id(run_id, round_index)
    result_id = _result_id(run_id, round_id)
    ref = _snapshot_ref(result_id)
    await bind_admitted_inputs_to_round(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        round_id=round_id,
        model_request_snapshot_ref=ref,
    )
    rows = await _bound_rows(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        round_id=round_id,
    )
    placeholder = await db.get(SessionModelResult, result_id)
    if placeholder is None:
        placeholder_snapshot = {
            "phase": "awaiting_exact_snapshot",
            "bound_input_ids": [str(row.id) for row in rows],
        }
        placeholder = SessionModelResult(
            id=result_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            round_id=round_id,
            provider_request_id=f"hive:{run_id}:round:{round_index}:attempt:1",
            state="prepared",
            model_request_hash=_sha256(placeholder_snapshot),
            model_request_snapshot_json=placeholder_snapshot,
            bound_input_ids_json=[str(row.id) for row in rows],
            reconciliation_owner="awaiting_exact_snapshot",
            version=1,
        )
        db.add(placeholder)
        await db.flush()
    carry_messages: list[dict[str, Any]] = []
    if int(round_index) == 1:
        _, carry_messages = await _carry_context_messages(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            round_id=round_id,
            snapshot_ref=ref,
        )
    return [*input_parts_to_runtime_messages(rows), *carry_messages]


async def prepare_model_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
    messages: Iterable[Any],
    tools: Any,
    provider: str,
    model: str,
    wire_request: dict[str, Any] | None = None,
    continuation_index: int = 0,
    logical_root_result_id: uuid.UUID | str | None = None,
    provider_idempotency_supported: bool = False,
    provider_idempotency_key_applied: bool = False,
    attempt_owner: str = "direct",
) -> str:
    """Persist the exact post-transform provider request before any bytes are sent."""

    round_id = _round_id(run_id, round_index, continuation_index)
    result_id = _result_id(run_id, round_id)
    rows = await _bound_rows(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        round_id=round_id,
    )
    carries = list(
        (
            await db.execute(
                select(SessionCarryForward).where(
                    SessionCarryForward.tenant_id == tenant_id,
                    SessionCarryForward.session_id == session_id,
                    SessionCarryForward.target_turn_id == turn_id,
                    SessionCarryForward.target_round_id == round_id,
                    SessionCarryForward.state == "round_bound",
                )
            )
        ).scalars()
    )
    assembly_plan = None
    if int(continuation_index) == 0 and int(round_index) > 1:
        previous_result = await db.scalar(
            select(SessionModelResult)
            .join(
                ChatTranscriptEvent,
                ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id,
            )
            .where(
                SessionModelResult.tenant_id == tenant_id,
                SessionModelResult.run_id == run_id,
                SessionModelResult.state == "round_committed",
            )
            .order_by(ChatTranscriptEvent.sequence.desc())
            .limit(1)
        )
        if previous_result is not None:
            from app.services.session_round_obligation import commit_next_round_plan

            assembly_plan = await commit_next_round_plan(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_id,
                source_result_id=previous_result.id,
                next_round_id=round_id,
            )
    result = await db.scalar(
        select(SessionModelResult)
        .where(SessionModelResult.id == result_id, SessionModelResult.tenant_id == tenant_id)
        .with_for_update()
    )
    request_lane = f"round:{int(round_index)}"
    if int(continuation_index) > 0:
        request_lane += f":output-continuation:{int(continuation_index)}"
    if result is not None and result.state == "failed" and bool((result.seal_json or {}).get("retry_safe")):
        candidate_provider_request_id = f"hive:{run_id}:{request_lane}:attempt:{int(result.version) + 1}"
    elif result is not None:
        candidate_provider_request_id = result.provider_request_id
    else:
        candidate_provider_request_id = f"hive:{run_id}:{request_lane}:attempt:1"
    canonical_wire_request = _canonical(
        wire_request
        or {
            "messages": [_canonical(message) for message in messages],
            "tools": _canonical(tools or []),
            "temperature": None,
            "max_tokens": None,
            "reasoning": {},
        }
    )
    snapshot = {
        "provider": provider,
        "model": model,
        # The exact post-transform semantic request passed to the provider
        # adapter.  Top-level mirrors remain for compatibility readers.
        "wire_request": canonical_wire_request,
        "messages": list(canonical_wire_request.get("messages") or []),
        "tools": _canonical(canonical_wire_request.get("tools") or []),
        "temperature": canonical_wire_request.get("temperature"),
        "max_tokens": canonical_wire_request.get("max_tokens"),
        "reasoning": _canonical(canonical_wire_request.get("reasoning") or {}),
        "bound_input_ids": [str(row.id) for row in rows],
        "carry_forward_ids": [str(carry.id) for carry in carries],
        "continuation_index": int(continuation_index),
        "logical_root_result_id": str(logical_root_result_id or result_id),
        "assembly_plan_id": str(assembly_plan.id) if assembly_plan is not None else None,
        "assembly_plan_hash": assembly_plan.plan_hash if assembly_plan is not None else None,
        "assembly_plan_generation": int(assembly_plan.plan_generation) if assembly_plan is not None else None,
        "request_fence": {
            "hive_provider_request_id": candidate_provider_request_id,
            "provider_idempotency_supported": bool(provider_idempotency_supported),
            "provider_idempotency_key_applied": bool(provider_idempotency_key_applied),
        },
    }
    snapshot_hash = _sha256(snapshot)
    emit_prepared_event = False
    if result is not None:
        if (result.model_request_snapshot_json or {}).get("phase") == "awaiting_exact_snapshot":
            result.model_request_hash = snapshot_hash
            result.model_request_snapshot_json = snapshot
            result.bound_input_ids_json = [str(row.id) for row in rows]
            result.reconciliation_owner = attempt_owner
            result.version = int(result.version) + 1
            emit_prepared_event = True
        elif result.state == "prepared" and result.model_request_hash == snapshot_hash:
            if result.reconciliation_owner == attempt_owner:
                # A crash may have happened after the prepared aggregate but
                # before the committed assembly plan was marked dispatched.
                # Continue to the exact plan dispatch fence below.
                emit_prepared_event = False
            else:
                result.state = "needs_reconciliation"
                result.reconciliation_owner = f"session_model_round:ambiguous_owner:{attempt_owner}"
                result.version = int(result.version) + 1
                raise ModelRoundNeedsReconciliation("model_round_provider_send_is_ambiguous")
        elif result.state == "failed" and bool((result.seal_json or {}).get("retry_safe")):
            result.version = int(result.version) + 1
            result.provider_request_id = candidate_provider_request_id
            result.state = "prepared"
            result.model_request_hash = snapshot_hash
            result.model_request_snapshot_json = snapshot
            result.bound_input_ids_json = [str(row.id) for row in rows]
            result.seal_json = None
            result.reconciliation_owner = attempt_owner
            emit_prepared_event = True
        else:
            result.state = "needs_reconciliation"
            result.reconciliation_owner = "session_model_round:ambiguous_prepare"
            result.version = int(result.version) + 1
            raise ModelRoundNeedsReconciliation("model_round_provider_send_is_ambiguous")
    else:
        provider_request_id = candidate_provider_request_id
        result = SessionModelResult(
            id=result_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            round_id=round_id,
            provider_request_id=provider_request_id,
            state="prepared",
            model_request_hash=snapshot_hash,
            model_request_snapshot_json=snapshot,
            bound_input_ids_json=[str(row.id) for row in rows],
            reconciliation_owner=attempt_owner,
            version=1,
        )
        db.add(result)
        await db.flush()
        emit_prepared_event = True
    if emit_prepared_event:
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=result.id,
                    item_kind="result_commit",
                    lifecycle="prepared",
                    scope=_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "runtime"},
                    payload={
                        "provider_request_id": result.provider_request_id,
                        "model_request_hash": snapshot_hash,
                        "model_request_snapshot_ref": _snapshot_ref(result.id),
                        "bound_input_ids": result.bound_input_ids_json,
                        "carry_forward_ids": snapshot["carry_forward_ids"],
                        "request_fence": snapshot["request_fence"],
                    },
                    result_id=result.id,
                    content_hash=snapshot_hash,
                )
            ],
        )
    if int(round_index) == 1 and int(continuation_index) == 0:
        # A fork-side input is owned by its source Session, while the exact
        # provider snapshot is owned by the branch Run.  Bind those two facts
        # in this same pre-dispatch transaction; a provider receipt must never
        # be able to settle a merely queued fork input.
        from app.services.session_fork_input import mark_fork_input_bound

        try:
            await mark_fork_input_bound(
                db,
                branch_run_id=run_id,
                round_id=round_id,
                model_request_snapshot_ref=_snapshot_ref(result.id),
            )
        except ValueError as exc:
            if str(exc) != "fork_input_for_run_not_found":
                raise
    if assembly_plan is not None:
        from app.services.session_round_obligation import dispatch_committed_plan

        await dispatch_committed_plan(
            db,
            plan_id=assembly_plan.id,
            claim_owner=f"provider-request:{result.provider_request_id}",
        )
    return result.provider_request_id


async def append_model_stream_delta(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    provider_request_id: str,
    content: str,
    phase: str,
    lifecycle: str = "delta",
) -> ChatTranscriptEvent:
    """Commit one visible Provider batch and its outbox before live delivery."""

    phase_contract = {
        "unknown": ("assistant_text", {"audience": "direct_user"}),
        "commentary": ("assistant_commentary", {"audience": "direct_user"}),
        "reasoning_summary": ("assistant_reasoning_summary", {"audience": "direct_user"}),
        "reasoning_private": (
            "assistant_reasoning_private",
            {"audience": "private_provider", "redaction_paths": ["/payload/content"]},
        ),
        "final": ("assistant_final", {"audience": "direct_user"}),
    }
    if phase not in phase_contract:
        raise ValueError("unsupported assistant stream phase")
    if lifecycle not in {"delta", "snapshot"}:
        raise ValueError("unsupported assistant stream lifecycle")
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
    if result is None:
        raise ModelRoundNeedsReconciliation("stream batch has no prepared model request")
    if result.state not in {"prepared", "streaming"}:
        raise ModelRoundNeedsReconciliation("stream batch arrived outside prepared model result")
    item_kind, visibility = phase_contract[phase]
    block_name = "assistant-visible-text:0" if phase == "unknown" else f"assistant-{phase}:0"
    item_id = uuid.uuid5(result.id, block_name)
    last_ordinal = await db.scalar(
        select(func.max(ChatTranscriptEvent.ordinal)).where(
            ChatTranscriptEvent.tenant_id == tenant_id,
            ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.item_id == item_id,
        )
    )
    ordinal = int(last_ordinal) + 1 if last_ordinal is not None else 0
    drafts = [
        SessionEventDraft(
            item_id=item_id,
            item_kind=item_kind,
            lifecycle=lifecycle,
            scope=_scope(session_id, result.turn_id, run_id, result.round_id),
            actor={"type": "assistant", "agent_id": str(agent_id)},
            visibility=visibility,
            payload={
                "phase": phase,
                "content": str(content),
                "provider_request_id": str(provider_request_id),
                "block_index": 0,
            },
            result_id=result.id,
            ordinal=ordinal,
            content_hash=_sha256(str(content)),
        )
    ]
    first_stream_batch = result.state == "prepared"
    if first_stream_batch:
        drafts.append(
            SessionEventDraft(
                item_id=result.id,
                item_kind="result_commit",
                lifecycle="streaming",
                scope=_scope(session_id, result.turn_id, run_id, result.round_id),
                actor={"type": "runtime"},
                payload={
                    "provider_request_id": str(provider_request_id),
                    "first_content_item_id": str(item_id),
                },
                result_id=result.id,
            )
        )
    events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=drafts,
    )
    result.state = "streaming"
    result.last_content_sequence = events[0].sequence
    result.version = int(result.version) + 1
    return events[0]


async def seal_model_response(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
    provider_request_id: str,
    response: dict[str, Any],
    continuation_index: int = 0,
    logical_round_complete: bool = True,
    pending_obligations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Commit the immutable Provider result seal without declaring Run terminal."""

    round_id = _round_id(run_id, round_index, continuation_index)
    result = await db.scalar(
        select(SessionModelResult)
        .where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.round_id == round_id,
        )
        .with_for_update()
    )
    if result is None or result.provider_request_id != provider_request_id:
        raise ModelRoundNeedsReconciliation("provider_response_has_no_matching_prepared_request")
    if result.state in {"sealed", "round_committed"}:
        return dict(result.seal_json or {})
    if result.state not in {"prepared", "streaming"}:
        result.state = "needs_reconciliation"
        result.reconciliation_owner = "session_model_round:unexpected_response_state"
        raise ModelRoundNeedsReconciliation("provider_response_state_is_ambiguous")
    response_payload = _canonical(response)
    response_ref = f"provider-response:{provider_request_id}:{_sha256(response_payload)}"
    from app.services.session_round_obligation import discover_round_obligations

    obligation_specs = (
        await discover_round_obligations(
            db,
            result=result,
            response=response_payload,
            explicit=pending_obligations,
        )
        if logical_round_complete and int(continuation_index) == 0
        else []
    )
    content = response_payload.get("content")
    content_bytes = content if isinstance(content, str) else ""
    content_hash = _sha256(content_bytes)
    content_item_id = uuid.uuid5(result.id, "assistant-visible-text:0")
    drafts: list[SessionEventDraft] = []
    prior_content_first_sequence: int | None = None
    prior_content_last_ordinal: int | None = None
    if int(continuation_index) == 0 and content_bytes:
        prior_content_first_sequence, prior_content_last_ordinal = (
            await db.execute(
                select(
                    func.min(ChatTranscriptEvent.sequence),
                    func.max(ChatTranscriptEvent.ordinal),
                ).where(
                    ChatTranscriptEvent.tenant_id == tenant_id,
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_id == content_item_id,
                )
            )
        ).one()
        next_ordinal = int(prior_content_last_ordinal) + 1 if prior_content_last_ordinal is not None else 0
        drafts.extend(
            [
                SessionEventDraft(
                    item_id=content_item_id,
                    item_kind="assistant_text",
                    lifecycle="snapshot",
                    scope=_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={
                        "phase": "unknown",
                        "content": content_bytes,
                        "provider_request_id": provider_request_id,
                        "block_index": 0,
                    },
                    result_id=result.id,
                    ordinal=next_ordinal,
                    content_hash=content_hash,
                ),
                SessionEventDraft(
                    item_id=content_item_id,
                    item_kind="assistant_text",
                    lifecycle="completed",
                    scope=_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    payload={
                        "phase": "unknown",
                        "content": "",
                        "provider_request_id": provider_request_id,
                        "block_index": 0,
                    },
                    result_id=result.id,
                    ordinal=next_ordinal + 1,
                    content_hash=content_hash,
                ),
            ]
        )
    visible_content_draft_count = len(drafts)
    private_reasoning = response_payload.get("reasoning_content")
    private_reasoning_bytes = private_reasoning if isinstance(private_reasoning, str) else ""
    if private_reasoning_bytes:
        private_item_id = uuid.uuid5(result.id, "assistant-reasoning_private:0")
        private_last_ordinal = await db.scalar(
            select(func.max(ChatTranscriptEvent.ordinal)).where(
                ChatTranscriptEvent.tenant_id == tenant_id,
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.item_id == private_item_id,
            )
        )
        private_next_ordinal = int(private_last_ordinal) + 1 if private_last_ordinal is not None else 0
        private_visibility = {
            "audience": "private_provider",
            "redaction_paths": ["/payload/content"],
        }
        private_hash = _sha256(private_reasoning_bytes)
        drafts.extend(
            [
                SessionEventDraft(
                    item_id=private_item_id,
                    item_kind="assistant_reasoning_private",
                    lifecycle="snapshot",
                    scope=_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    visibility=private_visibility,
                    payload={
                        "phase": "reasoning_private",
                        "content": private_reasoning_bytes,
                        "provider_request_id": provider_request_id,
                        "block_index": 0,
                    },
                    result_id=result.id,
                    ordinal=private_next_ordinal,
                    content_hash=private_hash,
                ),
                SessionEventDraft(
                    item_id=private_item_id,
                    item_kind="assistant_reasoning_private",
                    lifecycle="completed",
                    scope=_scope(session_id, turn_id, run_id, round_id),
                    actor={"type": "assistant", "agent_id": str(agent_id)},
                    visibility=private_visibility,
                    payload={
                        "phase": "reasoning_private",
                        "content": "",
                        "provider_request_id": provider_request_id,
                        "block_index": 0,
                    },
                    result_id=result.id,
                    ordinal=private_next_ordinal + 1,
                    content_hash=private_hash,
                ),
            ]
        )
    pending_snapshot = [
        {
            "obligation_id": str(
                uuid.uuid5(
                    result.id,
                    f"round-obligation:{spec.kind}:{spec.source_generation}:{spec.source_ref}",
                )
            ),
            "kind": spec.kind,
            "source_generation": spec.source_generation,
            "source_ref": spec.source_ref,
            "payload": _canonical(spec.payload),
        }
        for spec in obligation_specs
    ]
    pending_snapshot.sort(key=lambda value: (value["kind"], value["obligation_id"]))
    verdict = "continue" if pending_snapshot else "terminal_candidate"
    block_ledger = (
        [
            {
                "item_id": str(content_item_id),
                "kind": "assistant_text",
                "block_index": 0,
                "content_hash": content_hash,
            }
        ]
        if content_bytes and int(continuation_index) == 0
        else []
    )
    sealed_events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            *drafts,
            SessionEventDraft(
                item_id=result.id,
                item_kind="result_commit",
                lifecycle="sealed",
                scope=_scope(session_id, turn_id, run_id, round_id),
                actor={"type": "runtime"},
                payload={
                    "provider_request_id": provider_request_id,
                    "provider_response_ref": response_ref,
                    "finish_reason": response_payload.get("finish_reason"),
                    "continuation_verdict": verdict,
                    "logical_round_complete": bool(logical_round_complete),
                    "continuation_index": int(continuation_index),
                },
                result_id=result.id,
                content_hash=content_hash,
            ),
        ],
    )
    assistant_events = sealed_events[: len(drafts)]
    content_events = sealed_events[:visible_content_draft_count]
    first_sequence = min((event.sequence for event in sealed_events), default=0)
    last_content_sequence = max((event.sequence for event in assistant_events), default=0)
    if block_ledger and content_events:
        block_ledger[0]["first_sequence"] = prior_content_first_sequence or content_events[0].sequence
        block_ledger[0]["last_sequence"] = content_events[-1].sequence
    seal = {
        "result_id": str(result.id),
        "provider_request_id": provider_request_id,
        "run_id": str(run_id),
        "round_id": round_id,
        "first_sequence": first_sequence,
        "last_content_sequence": last_content_sequence,
        "content_hash": content_hash,
        "block_ledger": block_ledger,
        "finish_reason": str(response_payload.get("finish_reason") or "unknown"),
        "usage": _canonical(response_payload.get("usage") or {}),
        "continuation": {
            "verdict": verdict,
            "pending_obligations": pending_snapshot,
            "obligation_snapshot_hash": _sha256(pending_snapshot),
        },
        "provider_response_ref": response_ref,
        "provider_receipt_ref": response_payload.get("provider_receipt_ref"),
        "semantic_content": content_bytes,
        "response": response_payload,
        "logical_round_complete": bool(logical_round_complete),
        "continuation_index": int(continuation_index),
    }
    result.state = "sealed"
    result.last_content_sequence = last_content_sequence or result.last_content_sequence
    result.seal_json = seal
    result.version = int(result.version) + 1

    assembly_plan_id = (result.model_request_snapshot_json or {}).get("assembly_plan_id")
    if assembly_plan_id:
        from app.services.session_round_obligation import settle_dispatched_plan

        await settle_dispatched_plan(
            db,
            plan_id=uuid.UUID(str(assembly_plan_id)),
            provider_response_ref=response_ref,
        )
    if int(continuation_index) == 0:
        try:
            from app.services.session_fork_input import settle_fork_input_provider_delivery
        except ImportError:
            settle_fork_input_provider_delivery = None
        if settle_fork_input_provider_delivery is not None:
            try:
                await settle_fork_input_provider_delivery(
                    db,
                    branch_run_id=run_id,
                    provider_response_ref=response_ref,
                    delivery_state="delivered",
                )
            except ValueError as exc:
                if str(exc) != "fork_input_for_run_not_found":
                    raise
    return seal


async def commit_sealed_model_round(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
    provider_request_id: str,
    continuation_index: int = 0,
) -> dict[str, Any]:
    """Persist the complete obligation registry and round-committed fence."""

    round_id = _round_id(run_id, round_index, continuation_index)
    result = await db.scalar(
        select(SessionModelResult)
        .where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.round_id == round_id,
        )
        .with_for_update()
    )
    if result is None or result.provider_request_id != provider_request_id:
        raise ModelRoundNeedsReconciliation("sealed_result_has_no_matching_request")
    if result.state == "round_committed":
        return dict(result.seal_json or {})
    if result.state != "sealed" or not result.seal_json:
        raise ModelRoundNeedsReconciliation("round_commit_requires_durable_result_seal")
    from app.services.session_round_obligation import ObligationSpec, persist_round_obligations

    snapshots = list((result.seal_json.get("continuation") or {}).get("pending_obligations") or [])
    specs = [
        ObligationSpec(
            kind=str(snapshot["kind"]),
            source_generation=int(snapshot["source_generation"]),
            source_ref=str(snapshot["source_ref"]),
            payload=dict(snapshot.get("payload") or {}),
        )
        for snapshot in snapshots
    ]
    obligations = await persist_round_obligations(db, result=result, specs=specs)
    response_ref = str(result.seal_json["provider_response_ref"])
    if int(continuation_index) == 0:
        await mark_bound_inputs_applied(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            round_id=round_id,
            provider_response_ref=response_ref,
        )
    carries = list(
        (
            await db.execute(
                select(SessionCarryForward)
                .where(
                    SessionCarryForward.tenant_id == tenant_id,
                    SessionCarryForward.session_id == session_id,
                    SessionCarryForward.target_round_id == round_id,
                    SessionCarryForward.state == "round_bound",
                )
                .with_for_update()
            )
        ).scalars()
    )
    carry_drafts: list[SessionEventDraft] = []
    for carry in carries:
        carry_drafts.append(
            SessionEventDraft(
                item_id=carry.context_source_item_id,
                item_kind="carry_forward",
                lifecycle="consumed",
                scope=_scope(session_id, turn_id, run_id, round_id),
                actor={"type": "runtime"},
                payload={
                    "carry_forward_id": str(carry.id),
                    "provider_request_id": provider_request_id,
                    "model_request_snapshot_ref": carry.model_request_snapshot_ref,
                },
            )
        )
    if carry_drafts:
        carry_events = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=carry_drafts,
        )
        for carry, event in zip(carries, carry_events, strict=True):
            carry.state = "consumed"
            carry.consumed_event_id = event.id
            carry.claim_owner = None
            carry.claim_lease_expires_at = None
            carry.version = int(carry.version) + 1
    committed = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=result.id,
                item_kind="result_commit",
                lifecycle="round_committed",
                scope=_scope(session_id, turn_id, run_id, round_id),
                actor={"type": "runtime"},
                payload={
                    "provider_request_id": provider_request_id,
                    "provider_response_ref": response_ref,
                    "obligation_ids": [str(row.id) for row in obligations],
                    "continuation_verdict": (result.seal_json.get("continuation") or {}).get("verdict"),
                },
                result_id=result.id,
            )
        ],
    )
    result.state = "round_committed"
    result.round_committed_event_id = committed[0].id
    result.version = int(result.version) + 1
    return dict(result.seal_json or {})


async def commit_model_response(
    db: AsyncSession,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility wrapper; live runtime commits seal and round in two transactions."""

    seal = await seal_model_response(db, **kwargs)
    await commit_sealed_model_round(
        db,
        **{
            key: value
            for key, value in kwargs.items()
            if key
            in {
                "tenant_id",
                "agent_id",
                "session_id",
                "run_id",
                "turn_id",
                "round_index",
                "provider_request_id",
                "continuation_index",
            }
        },
    )
    return seal


async def fail_model_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
    provider_request_id: str,
    error_class: str,
    delivery_state: str = "unknown",
    retry_safe: bool,
    continuation_index: int = 0,
) -> None:
    round_id = _round_id(run_id, round_index, continuation_index)
    result = await db.scalar(
        select(SessionModelResult)
        .where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.round_id == round_id,
        )
        .with_for_update()
    )
    if (
        result is None
        or result.provider_request_id != provider_request_id
        or result.state not in {"prepared", "streaming"}
    ):
        return
    result.state = "failed" if retry_safe else "needs_reconciliation"
    result.seal_json = {
        "delivery_state": str(delivery_state),
        "error_class": str(error_class),
        "retry_safe": bool(retry_safe),
    }
    result.reconciliation_owner = None if retry_safe else "session_model_round:ambiguous_failure"
    result.version = int(result.version) + 1
    run_task: RuntimeTask | None = None
    if not retry_safe:
        run_task = await db.scalar(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == run_id,
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_agent_id == agent_id,
                RuntimeTask.parent_session_id == str(session_id),
            )
            .with_for_update()
        )
        if run_task is not None:
            recovery = {
                "reason": "ambiguous_provider_send",
                "provider_request_id": provider_request_id,
                "model_result_id": str(result.id),
                "round_id": round_id,
                "error_class": str(error_class),
                "delivery_state": str(delivery_state),
            }
            metadata = dict(run_task.metadata_json or {})
            metadata["session_v2_reconciliation"] = recovery
            run_task.status = "needs_reconciliation"
            run_task.result_summary = "Provider send outcome is ambiguous; operator reconciliation is required."
            run_task.metadata_json = metadata
            run_task.completed_at = datetime.now(timezone.utc)
            run_task.claim_version = int(run_task.claim_version or 0) + 1
        try:
            from app.services.session_fork_input import settle_fork_input_provider_delivery

            await settle_fork_input_provider_delivery(
                db,
                branch_run_id=run_id,
                provider_response_ref=f"provider-request:{provider_request_id}:delivery-unknown",
                delivery_state="unknown",
            )
        except ValueError as exc:
            if str(exc) != "fork_input_for_run_not_found":
                raise
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=result.id,
                item_kind="result_commit",
                lifecycle=result.state,
                scope=_scope(session_id, turn_id, run_id, round_id),
                actor={"type": "runtime"},
                payload={
                    "provider_request_id": provider_request_id,
                    "error_class": str(error_class),
                    "delivery_state": str(delivery_state),
                    "retry_safe": bool(retry_safe),
                },
                result_id=result.id,
            ),
            *(
                [
                    SessionEventDraft(
                        item_id=run_id,
                        item_kind="run",
                        lifecycle="needs_reconciliation",
                        scope=_run_scope(session_id, turn_id, run_id),
                        actor={"type": "runtime"},
                        payload={
                            "reason": "ambiguous_provider_send",
                            "provider_request_id": provider_request_id,
                            "model_result_id": str(result.id),
                            "round_id": round_id,
                            "error_class": str(error_class),
                            "delivery_state": str(delivery_state),
                        },
                    )
                ]
                if run_task is not None
                else []
            ),
        ],
    )


async def recover_sealed_model_rounds_once(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int = 50,
    run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Finish sealed Round registries without ever re-sending Provider bytes."""

    candidate_statement = (
        select(SessionModelResult)
        .where(SessionModelResult.state == "sealed")
        .order_by(SessionModelResult.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    if run_id is not None:
        candidate_statement = candidate_statement.where(SessionModelResult.run_id == run_id)
    candidates = list((await db.execute(candidate_statement)).scalars())
    committed = failed = 0
    for result in candidates:
        task = await db.get(RuntimeTask, result.run_id)
        if task is None or task.parent_agent_id is None:
            result.state = "needs_reconciliation"
            result.reconciliation_owner = f"{worker_id}:missing_runtime_task"
            result.version = int(result.version) + 1
            failed += 1
            continue
        try:
            await commit_sealed_model_round(
                db,
                tenant_id=result.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=result.session_id,
                run_id=result.run_id,
                turn_id=result.turn_id,
                round_index=int(result.round_id.split(":round:", 1)[1].split(":", 1)[0]),
                provider_request_id=result.provider_request_id,
                continuation_index=int(
                    result.round_id.rsplit(":output-continuation:", 1)[1]
                    if ":output-continuation:" in result.round_id
                    else 0
                ),
            )
            result.reconciliation_owner = worker_id
            committed += 1
        except (ModelRoundNeedsReconciliation, ValueError, RuntimeError):
            result.state = "needs_reconciliation"
            result.reconciliation_owner = worker_id
            result.version = int(result.version) + 1
            failed += 1
    return {"round_committed": committed, "needs_reconciliation": failed}


__all__ = [
    "ModelRoundNeedsReconciliation",
    "bind_round_inputs",
    "commit_sealed_model_round",
    "commit_model_response",
    "fail_model_request",
    "prepare_model_request",
    "recover_sealed_model_rounds_once",
    "seal_model_response",
]
