"""Durable Session V2 model-round input binding and provider receipts."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import (
    SessionCarryForward,
    SessionModelResult,
    SessionToolInvocation,
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


# Rounds sealed with this version are charged by the keyed per-round ledger
# (see kernel turn accounting and token_tracker.record_token_usage).
TOKEN_ACCOUNTING_VERSION = 2


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


def _root_round_index_from_id(round_id: str) -> int:
    try:
        return int(str(round_id).split(":round:", 1)[1].split(":", 1)[0])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("session_round_index_invalid") from exc


async def committed_final_round_at_frontier(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    round_index: int,
) -> bool:
    """True when this committed round is a no-tool final at the run's frontier.

    Durable facts only — never the RuntimeTask receipt, whose persistence the
    caller cannot guarantee: the round row itself is the authority.  A
    committed round with no sealed tool calls and no later committed logical
    root is a final whose kernel post-commit stages / outer terminal
    settlement never provably completed (a terminally settled run is never
    re-dispatched), so re-entering it must replay the immutable seal rather
    than issue a replacement provider generation.
    """

    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_settlement import TERMINAL_SETTLEMENT_STATUSES

    task = await db.scalar(
        select(RuntimeTask).where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == tenant_id,
        )
    )
    if task is not None and str(getattr(task, "status", "") or "") in TERMINAL_SETTLEMENT_STATUSES:
        return False
    result = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.round_id == _round_id(run_id, round_index, 0),
        )
    )
    if result is None or result.state not in {"sealed", "round_committed"} or not result.seal_json:
        return False
    if list((result.seal_json.get("response") or {}).get("tool_calls") or []):
        return False
    later_roots = list(
        (
            await db.execute(
                select(SessionModelResult.round_id).where(
                    SessionModelResult.tenant_id == tenant_id,
                    SessionModelResult.run_id == run_id,
                    SessionModelResult.state == "round_committed",
                    SessionModelResult.round_id.not_like("%:output-continuation:%"),
                )
            )
        ).scalars()
    )
    return not any(_root_round_index_from_id(round_id) > round_index for round_id in later_roots)


async def bind_round_inputs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    turn_id: str,
    round_index: int,
    bind_new_inputs: bool = True,
) -> list[dict[str, Any]]:
    """Bind admitted inputs FIFO and return only durable rows for this exact round.

    ``bind_new_inputs=False`` is the committed-final sealed-replay lane: the
    replayed round never re-sends the provider request, so a queued steer
    input bound here could never reach the model in this turn.  Already-bound
    rows are still returned (they authored the sealed round); new inputs stay
    queued for the turn's natural next-turn fallback or a genuine Stop-hook
    continuation round.
    """

    round_id = _round_id(run_id, round_index)
    result_id = _result_id(run_id, round_id)
    ref = _snapshot_ref(result_id)
    if bind_new_inputs and await committed_final_round_at_frontier(
        db, tenant_id=tenant_id, run_id=run_id, round_index=int(round_index)
    ):
        # A committed-final sealed replay never re-sends the provider request,
        # so newly queued inputs must not bind into it (they could never
        # reach the model in this turn); they stay queued for the turn's
        # natural next-turn fallback or a genuine Stop-hook continuation
        # round, which binds them for real.
        bind_new_inputs = False
    if bind_new_inputs:
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
    from app.services.session_user_question import answer_projections

    return [*input_parts_to_runtime_messages(rows, answer_projections=await answer_projections(db, rows)), *carry_messages]


def _unsettled_effect_states() -> tuple[str, ...]:
    """Invocation states in which a round may own an executed or in-flight effect.

    Reuses the authoritative ``UNRESOLVED_TOOL_EFFECT_STATES`` (effect outcome
    genuinely unknown) plus the deterministic pre-effect and committed-effect
    fences.  Only a cleanly ``failed`` invocation is excluded: it settled
    typed-failed without producing the effect.
    """
    from app.services.session_tool_runtime import UNRESOLVED_TOOL_EFFECT_STATES

    return ("prepared_not_started", "effect_committed", *sorted(UNRESOLVED_TOOL_EFFECT_STATES))


async def _round_has_unsettled_effects(db: AsyncSession, result: SessionModelResult) -> bool:
    """True when a round may already own an executed or in-flight tool effect."""

    count = await db.scalar(
        select(func.count())
        .select_from(SessionToolInvocation)
        .where(
            SessionToolInvocation.tenant_id == result.tenant_id,
            SessionToolInvocation.run_id == result.run_id,
            SessionToolInvocation.provider_request_id == result.provider_request_id,
            SessionToolInvocation.effect_state.in_(_unsettled_effect_states()),
        )
    )
    return bool(count)


async def _round_has_unknown_effects(db: AsyncSession, result: SessionModelResult) -> bool:
    """True when a round owns an invocation whose effect outcome is unknown.

    Only the authoritative ``UNRESOLVED_TOOL_EFFECT_STATES`` qualify: a
    ``prepared_not_started`` invocation is deterministic pending work whose
    effect never started, so re-entering its round is safe and required.
    """
    from app.services.session_tool_runtime import UNRESOLVED_TOOL_EFFECT_STATES

    count = await db.scalar(
        select(func.count())
        .select_from(SessionToolInvocation)
        .where(
            SessionToolInvocation.tenant_id == result.tenant_id,
            SessionToolInvocation.run_id == result.run_id,
            SessionToolInvocation.provider_request_id == result.provider_request_id,
            SessionToolInvocation.effect_state.in_(sorted(UNRESOLVED_TOOL_EFFECT_STATES)),
        )
    )
    return bool(count)


def _committed_request_content_matches(result: SessionModelResult, snapshot: dict[str, Any]) -> bool:
    """Compare only the provider request content of a committed snapshot.

    Assembly-plan ids, fence generations and bound-input bookkeeping differ on
    re-entry by construction; the bytes that reach the provider are the exact
    ``provider``/``model``/``messages``/``tools`` quadruple plus its sampling
    parameters.  Message dicts are compared after dropping null-valued
    optional fields: a fresh assembly and a recovered-history assembly may
    differ only in whether an absent ``tool_calls``/``reasoning_content`` key
    is serialized as ``null``, which never changes provider-visible content.
    """

    def _normalized_message(message: Any) -> Any:
        if not isinstance(message, dict):
            return message
        return {key: value for key, value in message.items() if value is not None}

    def _normalized_messages(messages: Any) -> Any:
        if not isinstance(messages, list):
            return messages
        return [_normalized_message(message) for message in messages]

    stored = dict(result.model_request_snapshot_json or {})
    return all(
        _canonical(_normalized_messages(stored.get(key)) if key == "messages" else stored.get(key))
        == _canonical(_normalized_messages(snapshot.get(key)) if key == "messages" else snapshot.get(key))
        for key in ("provider", "model", "messages", "tools", "temperature", "max_tokens", "continuation_index")
    )


def split_context_message(message, sections):
    """Keep compiler-known input items separate before capturing model bindings."""
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.llm_client import LLMMessage

    origin = message.context_origin
    remainder = message.content
    sources, unbound = list(origin.sources), list(origin.unbound)
    parts = []
    for section in sections:
        text = section["content"]
        if not text:
            continue
        start = remainder.index(text)
        remainder = remainder[:start] + remainder[start + len(text):]
        refs, unknown = section["sources"], section["unbound"]
        parts.append(LLMMessage(role=message.role, content=text, context_origin=ContextMessageOrigin.capture(
            kind=origin.kind, role=message.role, content=text, sources=refs, unbound=unknown,
            sections=[{key: section[key] for key in ("name", "source_ref") if key in section}]
                if section.get("name") else [])))
        sources = [ref for ref in sources if ref not in refs]
        unbound = [reason for reason in unbound if reason not in unknown]
    if remainder.strip():
        parts.insert(0, LLMMessage(role=message.role, content=remainder, context_origin=ContextMessageOrigin.capture(
            kind=origin.kind, role=message.role, content=remainder, sources=sources, unbound=unbound)))
    return parts


def _withhold_model_source(messages, wire_request, input_bindings, source, code, indexes=None):
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.llm_client import LLMMessage

    note = "有一项上下文暂时无法核实，已略过，请使用其余内容继续，需要时向用户询问。"
    if indexes is None:
        indexes = [entry["message_index"] for entry in input_bindings["messages"]
            if source in entry.get("dependencies", [])]
    if not indexes:
        # Delegated instructions can inherit dependencies without inheriting their bodies.
        indexes = [len(messages)]
        messages.append(LLMMessage(role="system", content=note))
        wire_request["messages"].append({"role": "system", "content": note})
    wire_messages = wire_request["messages"]
    attached = [ref for entry in input_bindings["messages"] for ref in entry.get("dependencies", [])]
    unattached = [ref for ref in input_bindings["dependencies"] if ref not in attached]
    indexes = set(indexes)
    # Providers may reuse a call ID in later rounds. Pair within its originating
    # assistant message so withholding one result leaves earlier rounds intact.
    pairs, active_calls = {}, {}
    for i, message in enumerate(wire_messages):
        if message.get("role") == "tool":
            if message.get("tool_call_id") in active_calls:
                pairs[i] = active_calls[message["tool_call_id"]]
        elif message.get("role") == "assistant":
            active_calls = {call["id"]: i for call in message.get("tool_calls") or [] if call.get("id")}
        elif message.get("role") == "user":
            active_calls = {}
    partial = {}
    for i, message in enumerate(wire_messages):
        calls = message.get("tool_calls") or []
        result_ids = {wire_messages[j].get("tool_call_id") for j in indexes if pairs.get(j) == i}
        if i not in indexes and any(call.get("id") in result_ids for call in calls if call.get("id")):
            remaining = [call for call in calls if call.get("id") not in result_ids]
            if remaining:
                partial[i] = {"role": "assistant", "content": note, "tool_calls": remaining}
            else:
                indexes.add(i)
    for i in list(indexes):
        if any(not call.get("id") for call in wire_messages[i].get("tool_calls") or []):
            for j in range(i + 1, len(wire_messages)):
                if wire_messages[j].get("role") != "tool":
                    break
                indexes.add(j)
    indexes.update(index for index, origin in pairs.items() if origin in indexes)
    for index, replacement in {**partial, **{i: {"role": "system", "content": note} for i in indexes}}.items():
        origin = (messages[index].get("context_origin") if isinstance(messages[index], dict)
            else getattr(messages[index], "context_origin", None))
        if isinstance(origin, ContextMessageOrigin) and any(section.get("name") == "skill_catalog" for section in origin.sections):
            replacement["content"] = "技能目录内容暂时无法核实，已略过；请使用其余内容继续，需要时向用户询问。"
        # Replacing the complete payload also removes arguments and private
        # reasoning; unaffected sibling calls keep their own paired results.
        wire_messages[index] = replacement
        # Dict messages may also be the caller's provider wire payload.
        if replacement["role"] == "system" and not isinstance(messages[index], dict):
            replacement = {**replacement, "context_origin": ContextMessageOrigin.capture(
                kind="source_withholding_notice", role="system", content=replacement["content"],
                sources=(), unbound=())}
        messages[index] = dict(replacement) if isinstance(messages[index], dict) else LLMMessage(**replacement)
    for entry in input_bindings["messages"]:
        if entry["message_index"] in indexes:
            entry.update(status="withheld", code=code, dependencies=[], dependency_status="resolved")
        elif entry["message_index"] in partial:
            entry.update(code=code, message_hash=_sha256(partial[entry["message_index"]]))
    retained = [*(ref for ref in unattached if ref != source),
        *(ref for entry in input_bindings["messages"] for ref in entry.get("dependencies", []))]
    input_bindings["dependencies"][:] = [ref for ref in input_bindings["dependencies"] if ref in retained]


async def _validate_or_withhold_model_sources(db, *, tenant_id, agent_id, session_id, run_id,
    input_bindings, messages, wire_request):
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies, group_source_read
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.delegation_context_sources import PERMISSION_INPUT_KEYS
    from app.services.shared_group_authority import validate_shared_group_model_inputs
    from app.services.trigger_read_sources import validate_native_trigger_model_sources
    from app.services.team_task_context import validate_model_sources
    from app.services.explicit_memory_authority import preload_memory_receipts, validate_memory_model_sources

    task = await db.get(RuntimeTask, run_id)
    # A selected team goal defines the work, rather than an optional source.
    await validate_model_sources(db, task=task, dependencies=input_bindings["dependencies"], effect="model_request")
    # Compiler-bound platform constants have resolved, empty dependencies;
    # unbound data has no such proof, even when sent with the system role.
    unbound = [entry["message_index"] for entry in input_bindings["messages"]
        if entry.get("dependency_status") != "resolved"]
    if unbound:
        permission_unbound = {index for index in unbound
            if isinstance(origin := getattr(messages[index], "context_origin", None), ContextMessageOrigin)
            and any(section.get("name") in PERMISSION_INPUT_KEYS for section in origin.sections)
        }
        _withhold_model_source(messages, wire_request, input_bindings, None, "model_input_source_unbound", unbound)
        for entry in input_bindings["messages"]:
            if entry["message_index"] in permission_unbound:
                entry["code"] = "session_permission_frame_unbound"
        input_bindings["status"] = "resolved"
        input_bindings["dependency_coverage"]["complete"] = True
    group_principal = await validate_shared_group_model_inputs(db, tenant_id=tenant_id, agent_id=agent_id,
        session_id=session_id, run_id=run_id, input_bindings={**input_bindings, "dependencies": []})
    from app.services.delegation_input_origin import delegated_instruction_memory_reader

    memory_principal = (AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=task.root_user_id,
        session_id=str(session_id), runtime_task_id=str(task.id), purpose="interactive_session") if task else None)
    principals = {group_principal: group_principal} if group_principal is not None else {}
    memory_principal = principals.setdefault(memory_principal, memory_principal)
    memory_readers = {}
    for entry in input_bindings["messages"]:
        if not any(ref["source_kind"] == "explicit_memory" for ref in entry.get("dependencies", [])):
            continue
        reader = await delegated_instruction_memory_reader(db, task=task, agent_id=agent_id,
            session_id=session_id, source_ref=entry.get("source_ref", ""))
        if reader is not None:
            memory_readers[entry["message_index"]] = principals.setdefault(reader, reader)
    # Reuse source walks and rows; resolve reader authority on each validation.
    with group_source_read(batch_inputs=True, cache_authority=False) as scope:
        await preload_memory_receipts(db, tenant_id, input_bindings["dependencies"])
        # Each dependency is checked once; an arbitrary retry cap must not turn the
        # fourth unavailable item into a whole-turn failure.
        for source in list(input_bindings["dependencies"]) or [None]:
            if source is not None and source not in input_bindings["dependencies"]:
                continue
            dependencies = [source] if source is not None else []
            for rows in scope.inputs.values():
                for key in list(rows):
                    if isinstance(key, tuple) and key[0] == "platform_skill_sources":
                        del rows[key]
            source_key = tuple(source[key] for key in ("source_kind", "source_id", "source_version")) if source else None
            try:
                if group_principal is not None:
                    scope.verified = {key: value for key, value in scope.verified.items() if key[-1] != source_key}
                    await _validate_input_dependencies(db, tenant_id, dependencies, runtime_request=group_principal)
                await validate_native_trigger_model_sources(db, task=task, dependencies=dependencies, effect="model_request")
                if source is not None and source["source_kind"] == "explicit_memory" and memory_readers:
                    readers, inherited_indexes = {}, []
                    for entry in input_bindings["messages"]:
                        if source in entry.get("dependencies", []):
                            reader = memory_readers.get(entry["message_index"])
                            if reader is None and entry.get("source_ref", "").startswith((
                                "session-model-result:", "session-tool-result:")):
                                inherited_indexes.append(entry["message_index"])
                            else:
                                readers.setdefault(reader or memory_principal, []).append(entry["message_index"])
                    # Own history can reuse only this exact source's successful
                    # parent-reader check for an instruction in this request.
                    inherited_verified = False
                    for reader, indexes in [*readers.items(), (None, inherited_indexes)]:
                        if not indexes or (reader is None and inherited_verified):
                            continue
                        reader = reader or memory_principal
                        try:
                            scope.verified = {key: value for key, value in scope.verified.items() if key[-1] != source_key}
                            await validate_memory_model_sources(db, task=task, agent_id=agent_id,
                                session_id=session_id, dependencies=dependencies, principal=reader)
                        except (ExecutionAuthorityUnavailable, ContentInputReceiptError) as exc:
                            _withhold_model_source(messages, wire_request, input_bindings, source, exc.code, indexes)
                        else:
                            inherited_verified = inherited_verified or reader in memory_readers.values()
                else:
                    scope.verified = {key: value for key, value in scope.verified.items() if key[-1] != source_key}
                    await validate_memory_model_sources(db, task=task, agent_id=agent_id,
                        session_id=session_id, dependencies=dependencies, principal=memory_principal)
            except (ExecutionAuthorityUnavailable, ContentInputReceiptError) as exc:
                if source is None or exc.code == "group_model_sources_unavailable":
                    raise
                if group_principal is not None and not any(source in entry.get("dependencies", [])
                    for entry in input_bindings["messages"]):
                    raise ContentInputReceiptError("group_model_sources_unavailable",
                        "这次对话暂时无法确认访问范围，请重试。", 503) from exc
                _withhold_model_source(messages, wire_request, input_bindings, source, exc.code)

    # Defer non-tool rows within a group, but never cross into the next group.
    wire_messages = wire_request["messages"]
    if len(messages) != len(wire_messages):
        return
    order, notices, pending = [], [], set()
    for index, message in enumerate(wire_messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            order.extend(notices)
            notices = []
            pending = {call.get("id") for call in message["tool_calls"]}
            order.append(index)
            continue
        if pending and message.get("role") != "tool":
            notices.append(index)
            continue
        order.append(index)
        if message.get("role") == "tool":
            pending.discard(message.get("tool_call_id"))
        if not pending:
            order.extend(notices)
            notices = []
    order.extend(notices)
    if order != list(range(len(wire_messages))):
        messages[:] = [messages[index] for index in order]
        wire_messages[:] = [wire_messages[index] for index in order]
        positions = {old: new for new, old in enumerate(order)}
        for entry in input_bindings["messages"]:
            entry["message_index"] = positions[entry["message_index"]]
        input_bindings["messages"].sort(key=lambda entry: entry["message_index"])


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
    resume_committed_round: bool = False,
) -> str:
    """Persist the exact post-transform provider request before any bytes are sent."""

    from app.services.chat_transcript import lock_transcript_session

    # Next-round planning locks RuntimeTask and flushes FK-bearing rows. Take
    # the transcript advisory first, as completion input/event writers do.
    await lock_transcript_session(db, session_id=session_id)
    round_id = _round_id(run_id, round_index, continuation_index)
    result_id = _result_id(run_id, round_id)
    from app.services.session_model_inputs import build_model_input_bindings, capture_context_message_origins, message_snapshot

    if not isinstance(messages, list):
        messages = list(messages)
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
    canonical_wire_request = _canonical(
        wire_request
        or {
            "messages": [message_snapshot(message) for message in messages],
            "tools": _canonical(tools or []),
            "temperature": None,
            "max_tokens": None,
            "reasoning": {},
        }
    )
    result = await db.scalar(
        select(SessionModelResult)
        .where(SessionModelResult.id == result_id, SessionModelResult.tenant_id == tenant_id)
        .with_for_update()
    )
    request_lane = f"round:{int(round_index)}"
    if int(continuation_index) > 0:
        request_lane += f":output-continuation:{int(continuation_index)}"
    if result is not None and (
        result.state in {"sealed", "round_committed"}
        or (
            result.state == "needs_reconciliation"
            and (result.seal_json is not None or result.round_committed_event_id is not None)
        )
    ):
        from app.services.runtime_terminal_settlement import TERMINAL_SETTLEMENT_STATUSES

        task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == run_id, RuntimeTask.tenant_id == tenant_id))
        if task is not None and str(task.status) in TERMINAL_SETTLEMENT_STATUSES:
            # A terminal run has no recovery lane to re-arm, even when its
            # request bytes match. Preserve its committed evidence unchanged.
            raise ModelRoundNeedsReconciliation("model_round_result_already_committed")
        # Durable provider evidence is immutable.  A repeat prepare carrying
        # the exact request content is an idempotent re-arm: the reclaimed
        # worker re-enters this round and the seal/commit callbacks return the
        # existing committed seal.  Every other repeat or stale prepare is a
        # typed rejection that leaves the committed facts byte-identical.
        content_snapshot = {
            "provider": provider,
            "model": model,
            "messages": list(canonical_wire_request.get("messages") or []),
            "tools": _canonical(canonical_wire_request.get("tools") or []),
            "temperature": canonical_wire_request.get("temperature"),
            "max_tokens": canonical_wire_request.get("max_tokens"),
            "continuation_index": int(continuation_index),
        }
        if (
            result.state in {"sealed", "round_committed"}
            and (
                resume_committed_round
                or (
                    int(continuation_index) == 0
                    and await committed_final_round_at_frontier(
                        db, tenant_id=tenant_id, run_id=run_id, round_index=int(round_index)
                    )
                )
                or _committed_request_content_matches(result, content_snapshot)
            )
            and not await _round_has_unknown_effects(db, result)
        ):
            # ``resume_committed_round`` is set only by the restart loader's
            # authoritative receipt for the exact pending round: the sealed
            # response is replayed (no provider bytes are re-sent), so the
            # regenerated prompt's transient dynamic-suffix drift (timestamps,
            # restart annotations) cannot diverge a request that is never
            # issued.  The committed-final replay lane is additionally
            # re-armed from DURABLE ROUND FACTS (a committed no-tool final at
            # the run's frontier), so the re-arm holds regardless of the
            # caller's in-memory metadata assembly.  Every other repeat
            # prepare still requires an exact provider-content match.
            #
            # Re-arming hands the caller this round's durable identity and
            # with it the path to the round's Stop boundary and its keyed
            # charge — a CONSEQUENTIAL lane, so a worker whose lease was
            # superseded (still alive, task already reclaimed) is rejected
            # here instead of proceeding alongside the new owner.  Callers
            # without a RuntimeTask row (kernel-only harness) keep the
            # existing behavior: there is no claim to protect.
            if task is not None and str(attempt_owner) != "direct":
                # ``claim_version`` is the fencing token (the claim service
                # increments it on EVERY claim); the worker name is
                # informational.  A stale worker — one whose lease was
                # superseded by a new claim — carries an older fencing suffix.
                # Owners that do not carry the ``{worker}:{version}:{attempt}``
                # fencing suffix cannot be checked and keep legacy behavior.
                suffix = str(attempt_owner).rsplit(":", 2)
                if (
                    len(suffix) == 3
                    and suffix[1].isdigit()
                    and suffix[2].isdigit()
                    and f"{suffix[1]}:{suffix[2]}" != f"{task.claim_version}:{task.attempt_count}"
                ):
                    raise ModelRoundNeedsReconciliation("model_round_claim_superseded")
            return result.provider_request_id
        raise ModelRoundNeedsReconciliation("model_round_result_already_committed")
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
                # Physical continuation rows also reach round_committed, but
                # only a logical root owns the round's obligation registry.
                SessionModelResult.round_id.not_like("%:output-continuation:%"),
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
    if result is not None and result.state == "failed" and bool((result.seal_json or {}).get("retry_safe")):
        candidate_provider_request_id = f"hive:{run_id}:{request_lane}:attempt:{int(result.version) + 1}"
    elif result is not None:
        candidate_provider_request_id = result.provider_request_id
    else:
        candidate_provider_request_id = f"hive:{run_id}:{request_lane}:attempt:1"
    await capture_context_message_origins(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        run_id=run_id, result_id=result_id, messages=messages)
    input_bindings = await build_model_input_bindings(db, tenant_id=tenant_id, agent_id=agent_id,
        session_id=session_id, messages=messages, wire_messages=list(canonical_wire_request.get("messages") or []))
    await _validate_or_withhold_model_sources(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        input_bindings=input_bindings,
        messages=messages,
        wire_request=canonical_wire_request,
    )
    snapshot = {
        "provider": provider,
        "model": model,
        # The exact post-transform semantic request passed to the provider
        # adapter.  Top-level mirrors remain for compatibility readers.
        "wire_request": canonical_wire_request,
        "input_bindings": input_bindings,
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
            elif not result.seal_json and not await _round_has_unsettled_effects(db, result):
                # Lease takeover after a worker restart: the interrupted send
                # produced no durable response and settled no tool effect, so
                # only the model generation itself may be reissued — always on
                # a fresh internal attempt lane, so a late callback from the
                # dead owner (seal/stream/fail) can no longer match this row.
                result.version = int(result.version) + 1
                result.provider_request_id = f"hive:{run_id}:{request_lane}:attempt:{int(result.version)}"
                snapshot["request_fence"]["hive_provider_request_id"] = result.provider_request_id
                snapshot_hash = _sha256(snapshot)
                result.model_request_hash = snapshot_hash
                result.model_request_snapshot_json = snapshot
                result.state = "prepared"
                result.reconciliation_owner = attempt_owner
                emit_prepared_event = True
            else:
                result.state = "needs_reconciliation"
                result.reconciliation_owner = f"session_model_round:ambiguous_owner:{attempt_owner}"
                result.version = int(result.version) + 1
                raise ModelRoundNeedsReconciliation("model_round_provider_send_is_ambiguous")
        elif (
            result.state in {"prepared", "streaming"}
            and not result.seal_json
            and not await _round_has_unsettled_effects(db, result)
        ):
            # Restart recovery: a worker died between prepare/seal.  With no
            # sealed response and no settled or in-flight tool effect, this
            # round never produced a consequential fact, so the reclaimed
            # worker may retake it on a fresh attempt lane.  Provider request
            # ids are internal (no provider-side idempotency exists), so a new
            # lane costs nothing and fences the dead owner's late callbacks;
            # the exact snapshot request_fence always carries the actual id.
            result.version = int(result.version) + 1
            result.provider_request_id = f"hive:{run_id}:{request_lane}:attempt:{int(result.version)}"
            snapshot["request_fence"]["hive_provider_request_id"] = result.provider_request_id
            snapshot_hash = _sha256(snapshot)
            result.state = "prepared"
            result.model_request_hash = snapshot_hash
            result.model_request_snapshot_json = snapshot
            result.bound_input_ids_json = [str(row.id) for row in rows]
            result.seal_json = None
            result.reconciliation_owner = attempt_owner
            emit_prepared_event = True
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


async def resume_committed_round_receipt(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    round_index: int,
    provider_request_id: str,
    continuation_index: int = 0,
) -> dict[str, Any] | None:
    """Return the authoritative sealed response of an already-committed round.

    A committed logical round whose tools never ran — or only partly ran — is
    recovered by replaying its immutable seal, never by reissuing the provider
    generation.  The receipt carries the sealed model-authored response plus
    the durable tool result of every call that already settled, so a mixed
    batch resumes from canonical evidence instead of a fabricated CAS failure.
    ``None`` means this round is a genuinely unsealed generation that may use
    the normal fresh fenced attempt path.
    """

    round_id = _round_id(run_id, round_index, continuation_index)
    result = await db.scalar(
        select(SessionModelResult).where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.session_id == session_id,
            SessionModelResult.run_id == run_id,
            SessionModelResult.round_id == round_id,
        )
    )
    if result is None or result.provider_request_id != str(provider_request_id):
        return None
    if result.state not in {"sealed", "round_committed"} or not result.seal_json:
        return None
    response = dict(result.seal_json.get("response") or {})
    settled_tool_results: dict[str, str] = {}
    for call in response.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "").strip()
        if not call_id:
            continue
        invocation = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == tenant_id,
                SessionToolInvocation.run_id == run_id,
                SessionToolInvocation.provider_request_id == result.provider_request_id,
                SessionToolInvocation.provider_tool_use_id == call_id,
            )
        )
        if invocation is None or invocation.result_event_id is None:
            continue
        result_event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        if result_event is None or result_event.item_kind != "tool_result":
            continue
        payload = dict((result_event.metadata_json or {}).get("v2_payload") or {})
        settled_tool_results[call_id] = str(payload.get("content") or "")
    return {
        "schema": "hive.session_committed_round_resume.v1",
        "provider_request_id": result.provider_request_id,
        "sealed_round_resume": {
            "response": response,
            "settled_tool_results": settled_tool_results,
        },
        # The replayed round's own durable seal is the exact model-result
        # receipt: a replayed committed final must settle terminally against
        # its own committed result, never against the previous round's.
        "model_result_receipt": dict(result.seal_json or {}),
    }


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

    from app.services.chat_transcript import lock_transcript_session

    await lock_transcript_session(db, session_id=session_id)
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

    from app.services.chat_transcript import lock_transcript_session

    await lock_transcript_session(db, session_id=session_id)
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
    content_draft_start = 0
    content_draft_end = 0
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
        content_draft_start = len(drafts)
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
        content_draft_end = len(drafts)
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
    content_events = sealed_events[content_draft_start:content_draft_end]
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
        "live_visible_event_ids": [str(event.id) for event in content_events],
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
        # Accounting provenance: rounds sealed by this code version carry an
        # explicit token-accounting version.  Post-deploy, every round is
        # charged under its own durable idempotency key at the kernel usage
        # fold, so a missing keyed charge for a version-2 round is a
        # crash-before-accounting/suppressed-failure window the restart
        # settlement may close.  PRE-deploy rounds (sealed without this
        # marker) were charged through the legacy UNKEYED ledger whose events
        # carry no round identity: whether such a round was already charged
        # cannot be established from exact evidence, so recovery must keep
        # that ambiguity explicit instead of re-debiting or claiming settled.
        "token_accounting_version": TOKEN_ACCOUNTING_VERSION,
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

    from app.services.chat_transcript import lock_transcript_session

    await lock_transcript_session(db, session_id=session_id)
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


def provider_request_has_effect(wire: dict[str, Any]) -> bool:
    provider_request = {**wire, **dict(wire.get("reasoning") or {})}
    tools = provider_request.get("tools", [])
    return bool(provider_request.get("mcp_servers")) or not isinstance(tools, list) or any(
        not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
    )


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
) -> str | None:
    from app.services.chat_transcript import lock_transcript_session
    from app.services.runtime_task_fence import assert_runtime_task_fence, clear_runtime_task_fence, reset_runtime_task_fence

    # Choose cancellation and provider failure under the same advisory → row
    # locks as the terminal write, including failures without a sealable round.
    await lock_transcript_session(db, session_id=session_id)
    token = clear_runtime_task_fence()
    try:
        run_task = await db.scalar(select(RuntimeTask).where(
            RuntimeTask.id == run_id, RuntimeTask.tenant_id == tenant_id,
            or_(and_(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.parent_session_id == str(session_id)),
                and_(RuntimeTask.task_type.in_(("delegation", "business_task")), RuntimeTask.child_agent_id == agent_id,
                    func.replace(func.lower(RuntimeTask.child_session_id), "-", "") == session_id.hex)),
        ).with_for_update().execution_options(populate_existing=True))
    finally:
        reset_runtime_task_fence(token)
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
    provider_effect = provider_request_has_effect(dict((result.model_request_snapshot_json or {}).get("wire_request") or {}))
    if error_class == "cancelled" or (run_task is not None and
        (run_task.metadata_json or {}).get("cancel_state") in {"cancelling", "cancelled"}):
        error_class = "cancelled"
        retry_safe = False
    cancelled = error_class == "cancelled" and not provider_effect
    result.state = "failed" if retry_safe or cancelled else "needs_reconciliation"
    result.seal_json = {
        "delivery_state": str(delivery_state),
        "error_class": str(error_class),
        "retry_safe": bool(retry_safe),
    }
    result.reconciliation_owner = None if retry_safe or cancelled else "session_model_round:ambiguous_failure"
    result.version = int(result.version) + 1
    failure = {
        "reason": "user_cancel" if cancelled else "ambiguous_provider_send",
        "provider_request_id": provider_request_id,
        "model_result_id": str(result.id),
        "round_id": round_id,
        "error_class": str(error_class),
        "delivery_state": str(delivery_state),
    }
    if not retry_safe:
        from app.services.runtime_terminal_settlement import TERMINAL_SETTLEMENT_STATUSES

        if run_task is not None and run_task.status in TERMINAL_SETTLEMENT_STATUSES:
            result.state = "needs_reconciliation" if provider_effect else "failed"
            result.reconciliation_owner = "session_model_round:ambiguous_failure" if provider_effect else None
            return result.state
        if run_task is not None:
            assert_runtime_task_fence(run_task)
            metadata = dict(run_task.metadata_json or {})
            if cancelled:
                metadata["terminal_reason"] = "user_cancel"
            else:
                metadata["session_v2_reconciliation"] = failure
            run_task.status = "killed" if cancelled else "needs_reconciliation"
            run_task.result_summary = "Generation stopped." if cancelled else "Provider send outcome is ambiguous; operator reconciliation is required."
            run_task.metadata_json = metadata
            run_task.completed_at = datetime.now(timezone.utc)
            run_task.claim_version = int(run_task.claim_version or 0) + 1
    else:
        run_task = None
    if not retry_safe and not cancelled:
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
    drafts = [SessionEventDraft(
        item_id=result.id, item_kind="result_commit", lifecycle=result.state,
        scope=_scope(session_id, turn_id, run_id, round_id), actor={"type": "runtime"},
        payload=dict(result.seal_json), result_id=result.id,
    )]
    if run_task is not None:
        drafts.append(SessionEventDraft(
            item_id=run_id, item_kind="run", lifecycle="cancelled" if cancelled else "needs_reconciliation",
            scope=_run_scope(session_id, turn_id, run_id), actor={"type": "runtime"}, payload=failure,
        ))
    await append_session_events(db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, drafts=drafts)
    if run_task is not None:
        from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

        await settle_and_enqueue_runtime_task_terminal(
            db,
            run_task,
            terminal_source="session_model_round:cancelled" if cancelled else "session_model_round:ambiguous_provider_send",
            root_reason_code="user_cancel" if cancelled else "ambiguous_provider_send_terminal",
        )
    return result.state


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
        agent_id = (task.child_agent_id if task.task_type == "delegation" else task.parent_agent_id) if task else None
        if agent_id is None:
            result.state = "needs_reconciliation"
            result.reconciliation_owner = f"{worker_id}:missing_runtime_task"
            result.version = int(result.version) + 1
            failed += 1
            continue
        try:
            await commit_sealed_model_round(
                db,
                tenant_id=result.tenant_id,
                agent_id=agent_id,
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
    "committed_final_round_at_frontier",
    "commit_sealed_model_round",
    "commit_model_response",
    "fail_model_request",
    "prepare_model_request",
    "recover_sealed_model_rounds_once",
    "resume_committed_round_receipt",
    "seal_model_response",
]
