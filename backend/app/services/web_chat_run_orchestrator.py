"""Typed, staged owner for the durable web-chat RuntimeTask lifecycle."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.database import PostgresTextContractError
from app.kernel.contracts import ExecutionIdentityRef, ProviderRequestNeedsReconciliation, TerminalReason
from app.runtime.invoker import AgentInvocationRequest
from app.runtime.runtime_phase import RunPhaseEmitter, RuntimePhase
from app.services.llm_client import LLMError
from app.services.runtime_budget_failover import runtime_budget_model_notice, runtime_budget_payload


@dataclass(frozen=True, slots=True)
class RuntimeExceptionFailure:
    terminal_reason: str


def _runtime_exception_failure(exc: Exception) -> RuntimeExceptionFailure:
    """Classify runtime failures from authoritative exception types, never message text."""
    if isinstance(exc, (SQLAlchemyError, PostgresTextContractError)):
        return RuntimeExceptionFailure(terminal_reason=TerminalReason.PERSISTENCE_ERROR.value)
    if isinstance(exc, LLMError):
        return RuntimeExceptionFailure(terminal_reason=TerminalReason.PROVIDER_ERROR.value)
    return RuntimeExceptionFailure(terminal_reason=TerminalReason.TURN_ABORT.value)


@dataclass(frozen=True, slots=True)
class WebChatContextPorts:
    run_id: Callable[..., uuid.UUID]
    load_runtime_context: Callable[..., Any]
    conversation_from_history: Callable[..., list[dict[str, Any]]]
    merge_permission_metadata: Callable[..., dict[str, Any]]
    actor_user_id: Callable[..., uuid.UUID | None]
    actor_external_principal_id: Callable[..., uuid.UUID | None]
    actor_authority_bound: Callable[..., bool]
    broker: Any
    sync_permission_metadata: Callable[..., Any]
    channel_delivery_suffix: Callable[..., str]
    clear_stale_plan_mode: Callable[..., Any]
    maybe_enter_plan_mode: Callable[..., Any]
    claim_pending_reply_suffix: Callable[..., Any]
    runtime_excluded_tools: Callable[..., Any]
    active_channel_delivery_target: Callable[..., Any]
    is_web_origin_turn: Callable[..., bool]
    channel_permission_prompt: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WebChatEventPorts:
    broadcast: Callable[..., Any]
    persist_stream_step: Callable[..., Any]
    persist_runtime_event: Callable[..., Any]
    persist_tool_call: Callable[..., Any]
    should_persist_runtime_event: Callable[..., bool]
    runtime_action_from_tool_result: Callable[..., Any]
    tool_step_contract: Callable[..., dict[str, Any]]
    build_chunk: Callable[..., dict[str, Any]]
    build_done: Callable[..., dict[str, Any]]
    build_session_native: Callable[..., dict[str, Any]]
    build_thinking: Callable[..., dict[str, Any]]
    build_tool_call: Callable[..., dict[str, Any]]
    session_native_types: Any
    stream_retry_tombstone: str
    stream_batcher_type: Any
    terminal_signal_type: Any


@dataclass(frozen=True, slots=True)
class WebChatTerminalPorts:
    finalize_with_assistant: Callable[..., Any]
    finalize_without_assistant: Callable[..., Any]
    emit_terminal_hook: Callable[..., Any]
    update_runtime_task: Callable[..., Any]
    phase_for_pause: Callable[..., RuntimePhase]
    phase_for_status: Callable[..., RuntimePhase]
    terminal_reason: Callable[..., str]
    resume_queued_handoffs: Callable[..., Any]
    clear_interactive_plan_mode: Callable[..., Any]
    plan_mode_terminal_error: Callable[..., Any]
    final_marker_conflict: Callable[..., bool]


@dataclass(frozen=True, slots=True)
class WebChatArtifactPorts:
    declared_paths: Callable[..., list[str]]
    artifact_paths: Callable[..., list[str]]
    rejected_paths: Callable[..., list[str]]
    file_change_paths: Callable[..., list[str]]
    file_change_states: Callable[..., dict[str, Any]]
    file_change_lineage: Callable[..., dict[str, Any]]
    prompt_suffix: Callable[..., str]
    prompt_metadata: Callable[..., dict[str, Any]]
    result_title: Callable[..., str]


@dataclass(frozen=True, slots=True)
class WebChatRuntimePorts:
    invoke_agent: Callable[..., Any]
    tenant_scoped_session: Callable[..., Any]
    plan_mode_core: Any
    interactive_pause_summary: Callable[..., Any]
    cancel_events: dict[str, asyncio.Event]
    broadcast_run_context: Any
    logger: Any


@dataclass(frozen=True, slots=True)
class WebChatRunPorts:
    """All mutable facade dependencies captured explicitly for one run."""

    context: WebChatContextPorts
    events: WebChatEventPorts
    terminal: WebChatTerminalPorts
    artifacts: WebChatArtifactPorts
    runtime: WebChatRuntimePorts


@dataclass(slots=True)
class _WebChatRunState:
    run_uuid: uuid.UUID
    ports: WebChatRunPorts
    cancel_event: asyncio.Event
    run_key: str
    broadcast_token: Any
    streamed_chunks: list[str] = field(default_factory=list)
    thinking_content: list[str] = field(default_factory=list)
    stream_batcher: Any | None = None
    runtime_task: Any | None = None
    agent: Any | None = None
    user: Any | None = None
    llm_model: Any | None = None
    fallback_model: Any | None = None
    history_messages: list[Any] = field(default_factory=list)
    session: Any | None = None
    session_id: str | None = None
    conversation: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    runtime_session_context: Any | None = None
    actor_user_id: uuid.UUID | None = None
    actor_external_principal_id: uuid.UUID | None = None
    actor_authority_bound: bool = False
    phase_emitter: RunPhaseEmitter | None = None
    terminal_phase_hint: RuntimePhase | None = None
    summary_turn_mode: bool = False
    internal_runtime_context_turn: bool = False
    channel_delivery_suffix: str = ""
    active_plan_mode_metadata: Any = None
    disable_tools_for_turn: bool = False
    excluded_tool_names_for_turn: Any = None
    plan_mode_submitted: bool = False
    interactive_pause_summary: str | None = None
    interactive_pause_channel_text: str | None = None
    terminal_tool_card_finalized: bool = False
    active_provider_request_id: str | None = None


async def run_web_chat_task(
    run_id: str | uuid.UUID,
    *,
    cancel_event: asyncio.Event | None = None,
    ports: WebChatRunPorts,
) -> None:
    """Run one durable web-chat task through explicit lifecycle stages."""
    run_uuid = ports.context.run_id(run_id)
    run_key = run_uuid.hex
    state = _WebChatRunState(
        run_uuid=run_uuid,
        ports=ports,
        cancel_event=cancel_event or ports.runtime.cancel_events.setdefault(run_key, asyncio.Event()),
        run_key=run_key,
        broadcast_token=ports.runtime.broadcast_run_context.set(run_key),
    )
    try:
        await _run_web_chat_stages(state)
    except Exception as exc:
        await _handle_web_chat_failure(state, exc)
    finally:
        await _cleanup_web_chat_run(state)


async def _run_web_chat_stages(state: _WebChatRunState) -> None:
    await _load_web_chat_context(state)
    await _configure_runtime_session(state)
    if await _handle_budget_unavailable_before_model(state):
        return
    if await _handle_pre_invocation_terminal(state):
        return
    callbacks = _WebChatCallbacks(state)
    pending_suffix = await _prepare_pending_suffix(state)
    result = await _invoke_model(state, callbacks, pending_suffix)
    if state.terminal_tool_card_finalized:
        return
    await callbacks.flush()
    await _finalize_invocation_result(state, result)


async def _load_web_chat_context(state: _WebChatRunState) -> None:
    loaded = await state.ports.context.load_runtime_context(state.run_uuid)
    if len(loaded) == 6:
        (*head, history) = loaded
        runtime_task, agent, user, model, fallback = head
        session = None
    else:
        runtime_task, agent, user, model, fallback, history, session = loaded
    state.runtime_task = runtime_task
    state.agent = agent
    state.user = user
    state.llm_model = model
    state.fallback_model = fallback
    state.history_messages = history
    state.session = session
    state.session_id = str(runtime_task.parent_session_id)
    state.summary_turn_mode = bool(
        (runtime_task.metadata_json or {}).get("budget_summary_turn")
        if isinstance(runtime_task.metadata_json, dict)
        else False
    )
    reclaimed = bool(
        (runtime_task.metadata_json or {}).get("reclaimed_expired_claim")
        if isinstance(runtime_task.metadata_json, dict)
        else False
    )
    initial_phase = (
        RuntimePhase.SUMMARIZING
        if state.summary_turn_mode
        else (RuntimePhase.RESUMING if reclaimed else RuntimePhase.STARTING)
    )
    state.phase_emitter = RunPhaseEmitter(
        lambda event: state.ports.events.broadcast(agent.id, state.session_id, event),
        run_id=state.run_key,
    )
    await state.phase_emitter.transition(initial_phase)
    state.conversation = state.ports.context.conversation_from_history(history)
    state.prompt = runtime_task.prompt or ""
    state.metadata = state.ports.context.merge_permission_metadata(
        runtime_metadata=runtime_task.metadata_json if isinstance(runtime_task.metadata_json, dict) else {},
        session_metadata=getattr(session, "transcript_metadata_json", None) if session is not None else None,
    )
    state.actor_user_id = state.ports.context.actor_user_id(user)
    state.actor_external_principal_id = state.ports.context.actor_external_principal_id(user)
    state.actor_authority_bound = state.ports.context.actor_authority_bound(user)
    if state.actor_external_principal_id is not None and not state.actor_authority_bound:
        state.metadata["disable_tools"] = True
        state.metadata["tool_policy"] = "disabled_for_unbound_external_principal"
    mailbox_role = str(state.metadata.get("runtime_mailbox_role") or "").strip().lower()
    state.internal_runtime_context_turn = bool(state.metadata.get("task_notification")) or mailbox_role == "system"
    if (
        state.metadata.get("latest_user_prompt_overrides_history")
        and state.prompt
        and not state.internal_runtime_context_turn
    ):
        _replace_latest_user_prompt(state.conversation, state.prompt)


def _replace_latest_user_prompt(conversation: list[dict[str, Any]], prompt: str) -> None:
    for message in reversed(conversation):
        if message.get("role") == "user":
            message["content"] = prompt
            return
    conversation.append({"role": "user", "content": prompt})


async def _configure_runtime_session(state: _WebChatRunState) -> None:
    context = await state.ports.context.broker.get_or_create_runtime_session(str(state.agent.id), state.session_id)
    state.runtime_session_context = context
    if hasattr(context, "begin_turn"):
        context.begin_turn()
    context.source = str(state.metadata.get("source") or context.source or "web")
    context.channel = str(state.metadata.get("channel") or context.channel or "web")
    context.metadata["tenant_id"] = str(state.agent.tenant_id) if state.agent.tenant_id else None
    context.metadata["runtime_task_id"] = state.run_uuid.hex
    root_runtime_task_id = (
        getattr(state.runtime_task, "root_runtime_task_id", None)
        or state.metadata.get("root_runtime_task_id")
        or state.run_uuid
    )
    root_session_id = (
        getattr(state.runtime_task, "root_session_id", None)
        or state.metadata.get("root_session_id")
        or state.session_id
    )
    context.metadata["root_runtime_task_id"] = str(root_runtime_task_id)
    context.metadata["root_session_id"] = str(root_session_id)
    runtime_budget = state.metadata.get("runtime_budget")
    if isinstance(runtime_budget, dict):
        context.metadata["runtime_budget"] = dict(runtime_budget)
    else:
        context.metadata.pop("runtime_budget", None)
    base_transcript_sequence = state.metadata.get("initial_user_message_t0_sequence")
    if isinstance(base_transcript_sequence, int) and not isinstance(base_transcript_sequence, bool):
        context.metadata["base_transcript_sequence"] = base_transcript_sequence
    budget_run_id = getattr(state.runtime_task, "budget_run_id", None) or state.metadata.get("budget_run_id")
    if budget_run_id:
        context.metadata["budget_run_id"] = str(budget_run_id)
    else:
        context.metadata.pop("budget_run_id", None)
    if state.summary_turn_mode:
        context.metadata["budget_summary_turn"] = True
    else:
        context.metadata.pop("budget_summary_turn", None)
    context.metadata["request_id"] = str(state.run_uuid)
    context.metadata["turn_id"] = str(state.metadata.get("turn_id") or f"turn-{state.run_uuid.hex}")
    context.metadata["intent_id"] = str(
        state.metadata.get("intent_id") or state.metadata.get("request_id") or f"intent-{state.run_uuid.hex}"
    )
    context.metadata["trace_id"] = (
        getattr(state.runtime_task, "trace_id", None)
        or state.metadata.get("trace_id")
        or f"{context.source or 'web'}-chat:{state.run_uuid.hex}"
    )
    _copy_optional_runtime_metadata(state)
    state.ports.context.sync_permission_metadata(context, state.metadata)
    state.channel_delivery_suffix = state.ports.context.channel_delivery_suffix(state.metadata, context)
    state.ports.context.clear_stale_plan_mode(
        context,
        plan_mode_requested=bool(state.metadata.get("plan_mode_requested")),
        history_messages=state.history_messages,
    )


async def _handle_budget_unavailable_before_model(state: _WebChatRunState) -> bool:
    """Fail closed for autonomous/legacy work before any LLM or tool effect."""

    payload = runtime_budget_payload(state.metadata)
    if not payload or payload.get("status") != "unavailable" or payload.get("interactive") is not False:
        return False
    reason = str(payload.get("reason") or "runtime_budget_service_unavailable")
    await state.ports.terminal.finalize_without_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        session_id=state.session_id,
        status="failed",
        result_summary="Runtime budget admission unavailable; autonomous execution did not start.",
        metadata_json={
            "runtime_budget": payload,
            "terminal_reason": TerminalReason.TOOL_BUDGET.value,
            "effect_started": False,
        },
    )
    await state.ports.events.broadcast(
        state.agent.id,
        state.session_id,
        {
            "type": "runtime_budget_unavailable",
            "status": "unavailable",
            "reason": reason,
            "retryable": True,
            "effect_started": False,
        },
    )
    if state.phase_emitter is not None:
        await state.phase_emitter.transition(
            RuntimePhase.FAILED,
            detail={"reason": reason, "retryable": True},
        )
    return True


def _copy_optional_runtime_metadata(state: _WebChatRunState) -> None:
    context = state.runtime_session_context
    for key in ("parent_trace_id", "tool_policy"):
        if state.metadata.get(key):
            context.metadata[key] = state.metadata[key]
    if state.metadata.get("side_session"):
        context.metadata["side_session"] = True
        context.metadata["side_session_kind"] = state.metadata.get("side_session_kind") or "btw"


async def _handle_pre_invocation_terminal(state: _WebChatRunState) -> bool:
    plan_response = None
    if not state.internal_runtime_context_turn:
        plan_response = await state.ports.context.maybe_enter_plan_mode(
            agent_id=state.agent.id,
            user_id=getattr(state.user, "id", None),
            session_id=state.session_id,
            content=state.prompt,
            classification_content=str(state.metadata.get("display_content") or state.prompt),
            plan_mode_requested=bool(state.metadata.get("plan_mode_requested")),
            runtime_session_context=state.runtime_session_context,
        )
    if plan_response is not None:
        await _finalize_pre_invocation_response(state, plan_response, status="completed", reason="invoke_complete")
        return True
    if state.llm_model is not None:
        return False
    state.terminal_phase_hint = RuntimePhase.FAILED
    finalized = await state.ports.terminal.finalize_without_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        session_id=state.session_id,
        status="failed",
        result_summary="llm_model_missing",
        metadata_json={
            "terminal_reason": TerminalReason.PROVIDER_ERROR.value,
            "error_code": "llm_model_missing",
            "retryable": False,
        },
        **_file_change_kwargs(state),
    )
    if finalized:
        await state.ports.events.broadcast(
            state.agent.id,
            state.session_id,
            {
                "type": "runtime_failure",
                "status": "unavailable",
                "reason": "llm_model_missing",
                "retryable": False,
            },
        )
    return True


async def _finalize_pre_invocation_response(
    state: _WebChatRunState,
    response: str,
    *,
    status: str,
    reason: str,
) -> None:
    finalized = await state.ports.terminal.finalize_with_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        user_id=state.actor_user_id,
        session_id=state.session_id,
        content=response,
        thinking=None,
        status=status,
        result_summary=response,
    )
    if not finalized:
        return
    await state.ports.terminal.emit_terminal_hook(
        agent_id=state.agent.id,
        session_id=state.session_id,
        run_uuid=state.run_uuid,
        runtime_metadata=state.metadata,
        status=status,
        reason=reason,
        source=state.runtime_session_context.source,
    )
    await state.ports.events.broadcast(state.agent.id, state.session_id, state.ports.events.build_done(response))


@dataclass(slots=True)
class _WebChatCallbacks:
    state: _WebChatRunState

    def __post_init__(self) -> None:
        self.state.stream_batcher = self.state.ports.events.stream_batcher_type(self.send_stream_event)

    async def flush(self) -> None:
        if self.state.stream_batcher is not None:
            await self.state.stream_batcher.flush()

    async def send_stream_event(self, kind: str, text: str, *, reset: bool = False) -> None:
        state, events = self.state, self.state.ports.events
        if not state.active_provider_request_id:
            raise RuntimeError("model_stream_arrived_without_prepared_provider_request")
        phase = "reasoning_private" if kind == "thinking" else "unknown"
        persisted = await events.persist_stream_step(
            agent_id=state.agent.id,
            tenant_id=state.agent.tenant_id,
            user_id=state.actor_user_id,
            external_principal_id=state.actor_external_principal_id,
            session_id=state.session_id,
            run_uuid=state.run_uuid,
            provider_request_id=state.active_provider_request_id,
            phase=phase,
            lifecycle="snapshot" if reset else "delta",
            event_type=kind,
            content=text,
            part=None,
        )
        # No committed envelope means there is nothing truthful to publish.
        if not persisted:
            return
        audience = str((persisted.get("visibility") or {}).get("audience") or "")
        if audience not in {"direct_user", "participants"}:
            return
        await events.broadcast(state.agent.id, state.session_id, persisted)

    async def stream(self, text: str) -> None:
        state = self.state
        if text == state.ports.events.stream_retry_tombstone:
            state.streamed_chunks.clear()
            await state.stream_batcher.reset_chunk()
            return
        if state.phase_emitter is not None and not state.summary_turn_mode:
            await state.phase_emitter.transition(RuntimePhase.RESPONDING)
        state.streamed_chunks.append(text)
        await state.stream_batcher.emit_chunk(text)

    async def thinking(self, text: str) -> None:
        state = self.state
        if state.phase_emitter is not None and not state.summary_turn_mode:
            await state.phase_emitter.transition(RuntimePhase.THINKING)
        state.thinking_content.append(text)
        await state.stream_batcher.emit_thinking(text)

    async def runtime_event(self, data: dict[str, Any]) -> None:
        state, events = self.state, self.state.ports.events
        if data.get("type") == "stream_retry_tombstone":
            state.streamed_chunks.clear()
            await state.stream_batcher.reset_chunk()
            return
        event_type = str(data.get("type") or data.get("event_type") or "")
        await _transition_for_runtime_event(state, event_type, data)
        payload = events.build_session_native(data) if event_type in events.session_native_types else data
        await self.flush()
        if events.should_persist_runtime_event(data):
            await events.persist_runtime_event(
                agent_id=state.agent.id,
                user_id=state.actor_user_id,
                external_principal_id=state.actor_external_principal_id,
                session_id=state.session_id,
                data=data,
            )
        await events.broadcast(state.agent.id, state.session_id, payload)

    async def tool_call(self, data: dict[str, Any]) -> None:
        state, events = self.state, self.state.ports.events
        if state.terminal_tool_card_finalized:
            return
        await _transition_for_tool_event(state, data)
        await self.flush()
        data = dict(data)
        data["provider_request_id"] = state.active_provider_request_id
        data = events.tool_step_contract(data, fallback_run_id=state.run_uuid)
        persisted_envelopes = await events.persist_tool_call(
            agent_id=state.agent.id,
            user_id=state.actor_user_id,
            external_principal_id=state.actor_external_principal_id,
            session_id=state.session_id,
            data=data,
        )
        for envelope in persisted_envelopes or []:
            audience = str((envelope.get("visibility") or {}).get("audience") or "")
            if audience in {"direct_user", "participants"}:
                await events.broadcast(state.agent.id, state.session_id, envelope)
        if data.get("status") != "done":
            return
        await self._consume_completed_tool(data)

    async def _consume_completed_tool(self, data: dict[str, Any]) -> None:
        state, events = self.state, self.state.ports.events
        runtime_action = events.runtime_action_from_tool_result(data)
        if runtime_action:
            await events.persist_runtime_event(
                agent_id=state.agent.id,
                user_id=state.actor_user_id,
                external_principal_id=state.actor_external_principal_id,
                session_id=state.session_id,
                data=runtime_action,
            )
            await events.broadcast(
                state.agent.id,
                state.session_id,
                events.build_session_native(runtime_action),
            )
        if _tool_result_exits_plan_mode(data):
            state.plan_mode_submitted = True
        pause = state.ports.runtime.interactive_pause_summary(data)
        if not pause:
            return
        state.interactive_pause_summary = pause
        if pause == "awaiting_session_permission":
            # The kernel must finish persisting every matching result in the
            # current Provider tool batch before the Run may suspend. Raising
            # here used to strand already-executed parallel siblings.
            if not state.ports.context.is_web_origin_turn(
                state.metadata,
                state.runtime_session_context,
            ):
                state.interactive_pause_channel_text = state.ports.context.channel_permission_prompt(data)
            return
        await self.finalize_terminal_card(pause)
        raise events.terminal_signal_type(pause)

    async def finalize_terminal_card(
        self,
        summary: str,
        *,
        channel_delivery_text: str | None = None,
    ) -> bool:
        state = self.state
        if state.terminal_tool_card_finalized:
            return True
        cancelled = bool(state.cancel_event.is_set())
        state.terminal_phase_hint = state.ports.terminal.phase_for_pause(summary, cancelled=cancelled)
        metadata = _interactive_pause_metadata(state, summary)
        if summary == "awaiting_session_permission" and not cancelled:
            # Approval is an open Tool/Run/Turn obligation, not a completed
            # turn. Release the worker lease without inventing a terminal
            # outcome; the same RuntimeTask becomes resumable after the typed
            # permission response and unique matching tool result commit.
            await state.ports.terminal.update_runtime_task(
                state.run_uuid,
                status="suspended",
                result_summary=summary,
                metadata_json=metadata,
                channel_delivery_text=channel_delivery_text or state.interactive_pause_channel_text,
            )
            state.terminal_tool_card_finalized = True
            await self.flush()
            return True
        finalized = await state.ports.terminal.finalize_without_assistant(
            run_uuid=state.run_uuid,
            agent_id=state.agent.id,
            session_id=state.session_id,
            status="killed" if cancelled else "completed",
            result_summary=summary,
            metadata_json=metadata,
            **_file_change_kwargs(state),
            channel_delivery_text=channel_delivery_text,
        )
        state.terminal_tool_card_finalized = finalized or state.terminal_tool_card_finalized
        if finalized:
            await _emit_interactive_terminal_hook(state, summary, metadata, reason="terminal_tool_card")
            await self.flush()
            await state.ports.events.broadcast(state.agent.id, state.session_id, state.ports.events.build_done(""))
        return state.terminal_tool_card_finalized


def _apply_persisted_event(event: dict[str, Any], persisted: Any, kind: str, text: str) -> None:
    if not persisted:
        return
    parts = persisted.transcript_event.parts_json or []
    event.update(
        {
            "transcript_event_id": str(persisted.event_id),
            "sequence": persisted.sequence,
            "event_type": kind,
            "role": "assistant",
            "content": persisted.transcript_event.content or text,
            "message_id": str(persisted.message_id) if persisted.message_id else None,
            "parts": parts or None,
            "metadata": persisted.transcript_event.metadata_json or {},
        }
    )


def _apply_persisted_tool_event(event: dict[str, Any], persisted: Any, *, done: bool) -> None:
    if not persisted:
        return
    parts = persisted.transcript_event.parts_json or []
    event.update(
        {
            "transcript_event_id": str(persisted.event_id),
            "sequence": persisted.sequence,
            "event_type": "tool_result" if done else "tool_call",
            "role": "tool_call",
            "content": persisted.transcript_event.content or "",
            "message_id": str(persisted.message_id) if persisted.message_id else None,
            "parts": parts or None,
            "artifacts": [part for part in parts if part.get("type") == "artifact"] or None,
            "metadata": persisted.transcript_event.metadata_json or {},
        }
    )


async def _transition_for_runtime_event(state: _WebChatRunState, event_type: str, data: dict[str, Any]) -> None:
    if state.phase_emitter is None or state.summary_turn_mode:
        return
    if event_type == "compaction_started":
        await state.phase_emitter.transition(RuntimePhase.COMPACTING)
    elif event_type in {"compaction_completed", "compaction_skipped"}:
        await state.phase_emitter.transition(RuntimePhase.THINKING)
    elif event_type == "permission" and str(data.get("status") or "") == "session_permission_required":
        await state.phase_emitter.transition(RuntimePhase.AWAITING_APPROVAL)


async def _transition_for_tool_event(state: _WebChatRunState, data: dict[str, Any]) -> None:
    if state.phase_emitter is None or state.summary_turn_mode:
        return
    if data.get("status") != "done":
        await state.phase_emitter.transition(
            RuntimePhase.TOOL_RUNNING,
            detail={"tool_name": str(data.get("name") or "")},
        )
    else:
        await state.phase_emitter.transition(RuntimePhase.THINKING)


def _tool_result_exits_plan_mode(data: dict[str, Any]) -> bool:
    if data.get("name") != "exit_plan_mode" or data.get("status") != "done":
        return False
    raw = data.get("result")
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw or "{}"))
        except Exception:
            payload = {}
    return isinstance(payload, dict) and payload.get("status") in {"needs_plan", "planning_failed"}


async def _prepare_pending_suffix(state: _WebChatRunState) -> str:
    suffix = ""
    try:
        async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
            suffix = await state.ports.context.claim_pending_reply_suffix(
                db,
                agent_id=state.agent.id,
                session_id=state.session_id,
            )
    except Exception as exc:
        state.ports.runtime.logger.warning("[WebChatRun] Pending reply injection failed (non-fatal): {}", exc)
    if state.internal_runtime_context_turn and state.prompt:
        suffix = _join_suffix(
            suffix,
            f"Runtime continuation context (system generated, not a user message):\n{state.prompt}",
        )
    restart = state.metadata.get("restart_resume_context")
    if isinstance(restart, dict) and str(restart.get("resume_prompt") or "").strip():
        suffix = _join_suffix(
            suffix,
            "Restart recovery context: this run was active before the worker restarted. "
            "Use the following durable resume context to continue from the saved artifacts instead of "
            f"starting over.\n{str(restart.get('resume_prompt')).strip()}",
        )
    suffix = _join_suffix(
        suffix,
        state.channel_delivery_suffix,
        state.ports.artifacts.prompt_suffix(),
        runtime_budget_model_notice(state.metadata),
    )
    trusted_decline = await _bind_trusted_decline(state)
    if trusted_decline:
        suffix = _join_suffix(
            suffix,
            "Plan Mode governance: the runtime verified that the user declined the immediately preceding "
            "Plan Mode recommendation. If you create or update a scheduled/monitoring trigger as a direct "
            "follow-up, call the trigger tool normally. Do not add opt-out fields to tool arguments, and do "
            "not use this opt-out for long tasks, delegation, or other high-risk actions.",
        )
    _configure_turn_policy(state, trusted_decline)
    return suffix


def _join_suffix(*parts: str | None) -> str:
    return "\n\n".join(part for part in parts if part)


async def _bind_trusted_decline(state: _WebChatRunState) -> dict[str, Any] | None:
    if state.internal_runtime_context_turn:
        return None
    core = state.ports.runtime.plan_mode_core
    decline = core.trusted_decline_metadata(
        content=str(state.metadata.get("display_content") or state.prompt),
        messages=state.history_messages,
        explicit=bool(state.metadata.get("plan_mode_requested")),
    )
    if not decline or state.actor_user_id is None:
        return None
    try:
        from app.services.plan_mode_recommendation_service import decline_latest_recommendation_for_user

        async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
            recommendation = await decline_latest_recommendation_for_user(
                db,
                agent_id=state.agent.id,
                user_id=state.actor_user_id,
                session_id=state.session_id,
            )
            if recommendation is None:
                return None
            decline["recommendation_id"] = str(recommendation.id)
            await db.commit()
            return decline
    except Exception as exc:
        state.ports.runtime.logger.warning(
            "[WebChatRun] Plan recommendation decline binding failed (non-fatal): {}", exc
        )
        return None


def _configure_turn_policy(state: _WebChatRunState, trusted_decline: dict[str, Any] | None) -> None:
    context = state.runtime_session_context
    key = state.ports.runtime.plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY
    if trusted_decline:
        context.metadata[key] = trusted_decline
    else:
        context.metadata.pop(key, None)
    state.active_plan_mode_metadata = context.metadata.get("plan_mode")
    state.disable_tools_for_turn = bool(
        state.metadata.get("disable_tools") or state.metadata.get("tool_policy") == "disabled_by_default"
    )
    state.excluded_tool_names_for_turn = state.ports.context.runtime_excluded_tools(
        state.metadata,
        context,
        prompt=state.prompt,
    )


async def _invoke_model(state: _WebChatRunState, callbacks: _WebChatCallbacks, suffix: str) -> Any:
    plan_token = None
    channel_token = None
    try:
        if isinstance(state.active_plan_mode_metadata, dict) and state.active_plan_mode_metadata.get("active"):
            from app.services.plan_mode_runtime_context import set_interactive_plan_mode

            plan_token = set_interactive_plan_mode(state.active_plan_mode_metadata)
        target = state.ports.context.active_channel_delivery_target(
            metadata=state.metadata,
            runtime_session_context=state.runtime_session_context,
            session=state.session,
            prompt=state.prompt,
        )
        if target:
            from app.services.channel_delivery_service import channel_delivery_target

            channel_token = channel_delivery_target.set(target)
        try:
            return await state.ports.runtime.invoke_agent(_agent_invocation_request(state, callbacks, suffix))
        except state.ports.events.terminal_signal_type as signal:
            state.interactive_pause_summary = signal.summary
            return None
    finally:
        if channel_token is not None:
            from app.services.channel_delivery_service import channel_delivery_target

            channel_delivery_target.reset(channel_token)
        if plan_token is not None:
            from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

            reset_interactive_plan_mode(plan_token)
        if state.plan_mode_submitted:
            state.ports.terminal.clear_interactive_plan_mode(state.runtime_session_context)
        key = state.ports.runtime.plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY
        state.runtime_session_context.metadata.pop(key, None)


def _agent_invocation_request(
    state: _WebChatRunState,
    callbacks: _WebChatCallbacks,
    suffix: str,
) -> AgentInvocationRequest:
    return AgentInvocationRequest(
        model=state.llm_model,
        fallback_model=state.fallback_model,
        messages=state.conversation,
        agent_name=state.agent.name,
        role_description=state.agent.role_description or "",
        tenant_id=state.agent.tenant_id,
        agent_id=state.agent.id,
        user_id=state.actor_user_id,
        execution_identity=ExecutionIdentityRef(
            identity_type=_execution_identity_type(state),
            identity_id=state.actor_external_principal_id or state.actor_user_id,
            label=(
                f"{state.user.display_name or state.user.username} via {state.runtime_session_context.channel or 'web'}"
            ),
        ),
        on_chunk=callbacks.stream,
        on_tool_call=callbacks.tool_call,
        on_thinking=callbacks.thinking,
        on_event=callbacks.runtime_event,
        supports_vision=getattr(state.llm_model, "supports_vision", False),
        memory_session_id=state.session_id,
        memory_messages=state.conversation,
        cancel_event=state.cancel_event,
        session_context=state.runtime_session_context,
        system_prompt_suffix=suffix,
        round_input_bind=lambda round_index: _bind_session_round_inputs(state, round_index),
        model_request_prepare=lambda **payload: _prepare_session_model_request(state, **payload),
        model_response_commit=lambda **payload: _commit_session_model_response(state, **payload),
        model_request_fail=lambda **payload: _fail_session_model_request(state, **payload),
        initial_round_index=max(0, int(state.metadata.get("session_resume_round_index") or 0)),
        initial_turn_tokens_used=max(0, int(state.metadata.get("session_resume_tokens_used") or 0)),
        disable_tools=state.disable_tools_for_turn,
        excluded_tool_names=state.excluded_tool_names_for_turn,
        model_routing_locked=bool(state.metadata.get("model_routing_locked")),
        emit_turn_stop=False,
    )


def _session_turn_id(state: _WebChatRunState) -> str:
    return str(state.metadata.get("turn_id") or f"turn-{state.run_uuid.hex}")


async def _bind_session_round_inputs(state: _WebChatRunState, round_index: int) -> list[dict[str, Any]]:
    from app.services.session_model_round import bind_round_inputs

    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        messages = await bind_round_inputs(
            db,
            tenant_id=state.agent.tenant_id,
            agent_id=state.agent.id,
            session_id=uuid.UUID(str(state.session_id)),
            run_id=state.run_uuid,
            turn_id=_session_turn_id(state),
            round_index=round_index,
        )
        await db.commit()
        return messages


async def _prepare_session_model_request(state: _WebChatRunState, **payload: Any) -> str:
    from app.services.session_model_round import ModelRoundNeedsReconciliation, prepare_model_request

    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        try:
            round_index = int(payload["round_index"])
            logical_round_id = f"{state.run_uuid}:round:{round_index}"
            logical_root_result_id = uuid.uuid5(
                state.run_uuid,
                f"session-model-result:{logical_round_id}",
            )
            provider_request_id = await prepare_model_request(
                db,
                tenant_id=state.agent.tenant_id,
                agent_id=state.agent.id,
                session_id=uuid.UUID(str(state.session_id)),
                run_id=state.run_uuid,
                turn_id=_session_turn_id(state),
                round_index=round_index,
                messages=payload["messages"],
                tools=payload.get("tools"),
                provider=str(payload.get("provider") or ""),
                model=str(payload.get("model") or ""),
                wire_request=dict(payload.get("wire_request") or {}),
                continuation_index=int(payload.get("continuation_index") or 0),
                logical_root_result_id=payload.get("logical_root_result_id") or logical_root_result_id,
                provider_idempotency_supported=bool(payload.get("provider_idempotency_supported")),
                provider_idempotency_key_applied=bool(payload.get("provider_idempotency_key_applied")),
                attempt_owner=(
                    f"{getattr(state.runtime_task, 'claimed_by', None) or 'unclaimed'}:"
                    f"{getattr(state.runtime_task, 'claim_version', 0)}:"
                    f"{getattr(state.runtime_task, 'attempt_count', 0)}"
                ),
            )
        except ModelRoundNeedsReconciliation:
            await db.commit()
            raise
        await db.commit()
        state.active_provider_request_id = provider_request_id
        return provider_request_id


async def _commit_session_model_response(state: _WebChatRunState, **payload: Any) -> dict[str, Any]:
    from app.models.session_v2 import SessionEventOutbox
    from app.services.session_model_round import (
        ModelRoundNeedsReconciliation,
        commit_sealed_model_round,
        seal_model_response,
    )

    live_visible_envelopes: list[dict[str, Any]] = []
    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        try:
            seal = await seal_model_response(
                db,
                tenant_id=state.agent.tenant_id,
                agent_id=state.agent.id,
                session_id=uuid.UUID(str(state.session_id)),
                run_id=state.run_uuid,
                turn_id=_session_turn_id(state),
                round_index=int(payload["round_index"]),
                provider_request_id=str(payload["provider_request_id"]),
                response=dict(payload.get("response") or {}),
                continuation_index=int(payload.get("continuation_index") or 0),
                logical_round_complete=bool(payload.get("logical_round_complete", True)),
                pending_obligations=list(payload.get("pending_obligations") or []),
            )
        except ModelRoundNeedsReconciliation:
            await db.commit()
            raise
        visible_event_ids = [uuid.UUID(str(event_id)) for event_id in seal.get("live_visible_event_ids", [])]
        if visible_event_ids:
            result = await db.execute(
                select(SessionEventOutbox.envelope_json)
                .where(SessionEventOutbox.event_id.in_(visible_event_ids))
                .order_by(SessionEventOutbox.sequence)
            )
            live_visible_envelopes = [dict(envelope or {}) for envelope in result.scalars()]
        await db.commit()
    # The result seal is deliberately durable before the round registry.  A
    # crash between these transactions is recovered without re-sending the
    # Provider request.
    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        try:
            await commit_sealed_model_round(
                db,
                tenant_id=state.agent.tenant_id,
                agent_id=state.agent.id,
                session_id=uuid.UUID(str(state.session_id)),
                run_id=state.run_uuid,
                turn_id=_session_turn_id(state),
                round_index=int(payload["round_index"]),
                provider_request_id=str(payload["provider_request_id"]),
                continuation_index=int(payload.get("continuation_index") or 0),
            )
        except ModelRoundNeedsReconciliation:
            await db.commit()
            raise
        await db.commit()
    # The durable outbox remains the recovery authority. This immediate replay
    # gives an attached Session socket the same committed commentary without a
    # refresh; duplicate delivery is harmless because event_id is stable.
    for envelope in live_visible_envelopes:
        try:
            await state.ports.events.broadcast(state.agent.id, state.session_id, envelope)
        except Exception as exc:
            state.ports.runtime.logger.warning(
                "[WebChatRun] Immediate Session event broadcast failed; durable outbox will recover event {}: {}",
                envelope.get("event_id"),
                exc,
            )
    return seal


async def _fail_session_model_request(state: _WebChatRunState, **payload: Any) -> None:
    from app.services.session_model_round import fail_model_request

    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        await fail_model_request(
            db,
            tenant_id=state.agent.tenant_id,
            agent_id=state.agent.id,
            session_id=uuid.UUID(str(state.session_id)),
            run_id=state.run_uuid,
            turn_id=_session_turn_id(state),
            round_index=int(payload["round_index"]),
            provider_request_id=str(payload["provider_request_id"]),
            error_class=str(payload.get("error_class") or "provider_error"),
            delivery_state=str(payload.get("delivery_state") or "unknown"),
            retry_safe=bool(payload.get("retry_safe")),
            continuation_index=int(payload.get("continuation_index") or 0),
        )
        await db.commit()


def _execution_identity_type(state: _WebChatRunState) -> str:
    if state.actor_external_principal_id is None:
        return "delegated_user"
    return "external_principal_bound" if state.actor_authority_bound else "external_principal"


async def _finalize_invocation_result(state: _WebChatRunState, result: Any) -> None:
    if state.interactive_pause_summary:
        await _finalize_interactive_pause(state, state.interactive_pause_summary)
        return
    response = result.content
    plan_error = (
        None
        if state.plan_mode_submitted or state.interactive_pause_summary
        else state.ports.terminal.plan_mode_terminal_error(state.runtime_session_context)
    )
    if plan_error:
        state.ports.terminal.clear_interactive_plan_mode(state.runtime_session_context)
    # Terminal status is a typed runtime fact.  Natural-language response
    # bytes are never scanned to decide success or failure.
    terminal_reason = getattr(result, "terminal_reason", TerminalReason.TURN_STOP)
    terminal_reason_value = getattr(terminal_reason, "value", str(terminal_reason))
    failed = bool(plan_error) or terminal_reason_value not in {
        TerminalReason.TURN_STOP.value,
        TerminalReason.CLARIFICATION_REQUIRED.value,
    }
    thinking = None
    status = "killed" if state.cancel_event.is_set() else ("failed" if failed else "completed")
    state.terminal_phase_hint = state.ports.terminal.phase_for_status(status)
    metadata = {
        "cancelled_by_user": bool(state.cancel_event.is_set()),
        "terminal_reason": state.ports.terminal.terminal_reason(
            status=status,
            result_reason=getattr(result, "terminal_reason", None),
            cancelled_by_user=bool(state.cancel_event.is_set()),
            plan_mode_terminal_error=bool(plan_error),
            llm_error=terminal_reason_value == TerminalReason.PROVIDER_ERROR.value,
        ),
        "turn_tokens_used": int(getattr(result, "tokens_used", 0) or 0),
        **state.ports.artifacts.prompt_metadata(state.runtime_session_context),
    }
    if plan_error:
        metadata["interactive_pause"] = "plan_mode_missing_terminal_tool"
    if state.interactive_pause_summary and not str(response or "").strip():
        metadata["interactive_pause"] = state.interactive_pause_summary
        await _finalize_empty_interactive_response(state, status, metadata)
        return
    await _finalize_assistant_response(state, result, response, thinking, status, metadata)


def _interactive_pause_metadata(state: _WebChatRunState, summary: str) -> dict[str, Any]:
    cancelled = bool(state.cancel_event.is_set())
    return {
        "cancelled_by_user": cancelled,
        "interactive_pause": summary,
        "terminal_reason": state.ports.terminal.terminal_reason(
            status="killed" if cancelled else "completed",
            cancelled_by_user=cancelled,
        ),
    }


def _file_change_kwargs(state: _WebChatRunState) -> dict[str, Any]:
    return {
        "file_change_paths": state.ports.artifacts.file_change_paths(state.runtime_session_context),
        "file_change_states": state.ports.artifacts.file_change_states(state.runtime_session_context),
        "file_change_lineage": state.ports.artifacts.file_change_lineage(state.runtime_session_context),
    }


async def _finalize_interactive_pause(state: _WebChatRunState, summary: str) -> None:
    cancelled = bool(state.cancel_event.is_set())
    state.terminal_phase_hint = state.ports.terminal.phase_for_pause(summary, cancelled=cancelled)
    metadata = _interactive_pause_metadata(state, summary)
    if summary == "awaiting_session_permission" and not cancelled:
        await state.ports.terminal.update_runtime_task(
            state.run_uuid,
            status="suspended",
            result_summary=summary,
            metadata_json=metadata,
            channel_delivery_text=state.interactive_pause_channel_text,
        )
        return
    finalized = await state.ports.terminal.finalize_without_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        session_id=state.session_id,
        status="killed" if cancelled else "completed",
        result_summary=summary,
        metadata_json=metadata,
        **_file_change_kwargs(state),
    )
    if not finalized:
        return
    await _emit_interactive_terminal_hook(state, summary, metadata, reason="interactive_pause")
    await state.ports.events.broadcast(state.agent.id, state.session_id, state.ports.events.build_done(""))


async def _emit_interactive_terminal_hook(
    state: _WebChatRunState,
    summary: str,
    metadata: dict[str, Any],
    *,
    reason: str,
) -> None:
    await state.ports.terminal.emit_terminal_hook(
        agent_id=state.agent.id,
        session_id=state.session_id,
        run_uuid=state.run_uuid,
        runtime_metadata=state.metadata,
        status="killed" if state.cancel_event.is_set() else "completed",
        reason=reason,
        source=state.runtime_session_context.source,
        extra_metadata=metadata,
    )


async def _finalize_empty_interactive_response(
    state: _WebChatRunState,
    status: str,
    metadata: dict[str, Any],
) -> None:
    summary = state.interactive_pause_summary or "interactive_pause"
    finalized = await state.ports.terminal.finalize_without_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
        session_id=state.session_id,
        status=status,
        result_summary=summary,
        metadata_json=metadata,
        **_file_change_kwargs(state),
    )
    if not finalized:
        return
    await state.ports.terminal.emit_terminal_hook(
        agent_id=state.agent.id,
        session_id=state.session_id,
        run_uuid=state.run_uuid,
        runtime_metadata=state.metadata,
        status=status,
        reason="interactive_pause",
        source=state.runtime_session_context.source,
        extra_metadata=metadata,
    )
    await state.ports.events.broadcast(state.agent.id, state.session_id, state.ports.events.build_done(""))


async def _finalize_assistant_response(
    state: _WebChatRunState,
    result: Any,
    response: str,
    thinking: str | None,
    status: str,
    metadata: dict[str, Any],
) -> None:
    artifacts = state.ports.artifacts
    model_receipt = getattr(result, "model_result_receipt", None)
    if status == "completed" and isinstance(model_receipt, dict) and model_receipt.get("result_id"):
        committed = await _commit_canonical_terminal_outcome(state, model_receipt, response)
        if not committed:
            return
        # Hooks, metrics, ChatMessage projections and transport delivery are
        # sidecars after the canonical outcome transaction.  Their failure may
        # be retried but can never rewrite the model-authored outcome.
        try:
            await state.ports.terminal.emit_terminal_hook(
                agent_id=state.agent.id,
                session_id=state.session_id,
                run_uuid=state.run_uuid,
                runtime_metadata=state.metadata,
                status=status,
                reason="invoke_complete",
                source=state.runtime_session_context.source,
                extra_metadata=metadata,
            )
        except Exception as exc:
            state.ports.runtime.logger.warning(
                "[WebChatRun] terminal sidecar hook failed after committed outcome run={}: {}",
                state.run_uuid.hex,
                exc,
            )
        try:
            await state.ports.events.broadcast(
                state.agent.id,
                state.session_id,
                state.ports.events.build_done(response, artifacts=metadata.get("artifacts")),
            )
        except Exception as exc:
            state.ports.runtime.logger.warning(
                "[WebChatRun] terminal delivery failed after committed outcome run={}: {}",
                state.run_uuid.hex,
                exc,
            )
        return
    if status != "completed":
        finalized = await state.ports.terminal.finalize_without_assistant(
            run_uuid=state.run_uuid,
            agent_id=state.agent.id,
            session_id=state.session_id,
            status=status,
            result_summary=str(metadata.get("terminal_reason") or status),
            metadata_json=metadata,
            **_file_change_kwargs(state),
        )
        if finalized:
            await state.ports.events.broadcast(
                state.agent.id,
                state.session_id,
                {
                    "type": "runtime_failure",
                    "status": status,
                    "reason": metadata.get("terminal_reason"),
                    "retryable": status != "killed",
                },
            )
        return
    try:
        finalized = await state.ports.terminal.finalize_with_assistant(
            run_uuid=state.run_uuid,
            agent_id=state.agent.id,
            user_id=state.actor_user_id,
            session_id=state.session_id,
            content=response,
            thinking=thinking,
            thinking_signature=getattr(result, "reasoning_signature", None),
            status=status,
            result_summary=artifacts.result_title(response),
            metadata_json=metadata,
            artifact_paths=artifacts.artifact_paths(state.runtime_session_context, response),
            declared_artifact_paths=artifacts.declared_paths(response),
            rejected_artifact_paths=artifacts.rejected_paths(state.runtime_session_context, response),
            **_file_change_kwargs(state),
        )
    except IntegrityError as exc:
        if state.ports.terminal.final_marker_conflict(exc):
            state.ports.runtime.logger.info(
                "[WebChatRun] Terminal assistant finalization lost idempotency race for run {}",
                state.run_uuid.hex,
            )
            return
        raise
    if not finalized:
        return
    await state.ports.terminal.emit_terminal_hook(
        agent_id=state.agent.id,
        session_id=state.session_id,
        run_uuid=state.run_uuid,
        runtime_metadata=state.metadata,
        status=status,
        reason="invoke_complete",
        source=state.runtime_session_context.source,
        extra_metadata=metadata,
    )
    await state.ports.events.broadcast(
        state.agent.id,
        state.session_id,
        state.ports.events.build_done(response, thinking=thinking, artifacts=metadata.get("artifacts")),
    )


async def _commit_canonical_terminal_outcome(
    state: _WebChatRunState,
    model_receipt: dict[str, Any],
    response: str,
) -> bool:
    from app.services.session_terminal_outcome import (
        TerminalOutcomeIneligible,
        TerminalOutcomeNeedsReconciliation,
        commit_terminal_outcome,
        prepare_and_seal_run_outcome,
    )

    result_id = uuid.UUID(str(model_receipt["result_id"]))
    try:
        await _record_canonical_terminal_artifact_selection(state, response)
        async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
            outcome = await prepare_and_seal_run_outcome(
                db,
                tenant_id=state.agent.tenant_id,
                agent_id=state.agent.id,
                session_id=uuid.UUID(str(state.session_id)),
                turn_id=_session_turn_id(state),
                run_id=state.run_uuid,
                terminal_result_id=result_id,
            )
            outcome_id = outcome.id
            await db.commit()
        # A separately durable outcome seal is the recovery fence for the
        # all-or-nothing Run/Turn/final transaction below.
        async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
            await commit_terminal_outcome(
                db,
                tenant_id=state.agent.tenant_id,
                agent_id=state.agent.id,
                session_id=uuid.UUID(str(state.session_id)),
                run_id=state.run_uuid,
                outcome_id=outcome_id,
            )
            await db.commit()
        return True
    except TerminalOutcomeIneligible as exc:
        state.ports.runtime.logger.info(
            "[WebChatRun] terminal candidate held for continuation run={}: {}",
            state.run_uuid.hex,
            exc,
        )
        await state.ports.events.broadcast(
            state.agent.id,
            state.session_id,
            {
                "type": "result.commit_pending",
                "run_id": str(state.run_uuid),
                "result_id": str(result_id),
                "reason": "terminal_ineligible",
            },
        )
        return False
    except TerminalOutcomeNeedsReconciliation as exc:
        await state.ports.terminal.update_runtime_task(
            state.run_uuid,
            status="needs_reconciliation",
            result_summary="Canonical terminal outcome requires reconciliation.",
            metadata_json={
                "session_v2_reconciliation": {
                    "reason": "terminal_outcome_commit",
                    "result_id": str(result_id),
                    "error_class": type(exc).__name__,
                }
            },
        )
        return False


async def _record_canonical_terminal_artifact_selection(
    state: _WebChatRunState,
    response: str,
) -> None:
    """Bind model-declared, current-turn artifacts before the outcome seal."""

    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_artifact_delivery import artifact_part_from_model
    from app.services.session_terminal_outcome import TerminalOutcomeNeedsReconciliation

    selected_paths = state.ports.artifacts.artifact_paths(state.runtime_session_context, response)
    declared_paths = state.ports.artifacts.declared_paths(response)
    rejected_paths = state.ports.artifacts.rejected_paths(state.runtime_session_context, response)
    session_id = uuid.UUID(str(state.session_id))
    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        task = await db.scalar(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == state.run_uuid,
                RuntimeTask.tenant_id == state.agent.tenant_id,
                RuntimeTask.parent_agent_id == state.agent.id,
                RuntimeTask.parent_session_id == str(session_id),
            )
            .with_for_update()
        )
        if task is None:
            raise TerminalOutcomeNeedsReconciliation("canonical terminal artifact selection has no RuntimeTask")
        rows: list[ChatArtifact] = []
        if selected_paths:
            artifact_statement = select(ChatArtifact).where(
                ChatArtifact.tenant_id == state.agent.tenant_id,
                ChatArtifact.agent_id == state.agent.id,
                ChatArtifact.session_id == session_id,
                ChatArtifact.runtime_task_id == state.run_uuid,
                ChatArtifact.authority_state == "owned",
                ChatArtifact.path.in_(selected_paths),
            )
            if task.root_user_id is not None:
                artifact_statement = artifact_statement.where(ChatArtifact.owner_user_id == task.root_user_id)
            try:
                root_session_id = uuid.UUID(str(task.root_session_id)) if task.root_session_id else session_id
            except (TypeError, ValueError, AttributeError):
                root_session_id = session_id
            artifact_statement = artifact_statement.where(ChatArtifact.root_session_id == root_session_id)
            rows = list(
                (
                    await db.execute(
                        artifact_statement.order_by(
                            ChatArtifact.created_at.desc(), ChatArtifact.id.desc()
                        ).with_for_update()
                    )
                ).scalars()
            )
        newest_by_path: dict[str, ChatArtifact] = {}
        for row in rows:
            newest_by_path.setdefault(row.path, row)
        selected = [newest_by_path[path] for path in selected_paths if path in newest_by_path]
        missing_paths = [path for path in selected_paths if path not in newest_by_path]
        artifact_parts = [artifact_part_from_model(row) for row in selected]
        metadata = dict(task.metadata_json or {})
        metadata.update(
            {
                "declared_artifact_paths": declared_paths,
                "rejected_artifact_paths": list(dict.fromkeys([*rejected_paths, *missing_paths])),
                "artifact_ids": [part["artifact_id"] for part in artifact_parts],
                "artifact_paths": [part["path"] for part in artifact_parts],
                "artifacts": artifact_parts,
                "artifact_attachment_policy": "model_declared_current_turn_writes_only",
            }
        )
        task.metadata_json = metadata
        await db.commit()


async def _handle_web_chat_failure(state: _WebChatRunState, exc: Exception) -> None:
    state.ports.runtime.logger.exception("[WebChatRun] Run {} failed", state.run_uuid.hex)
    cancelled = state.cancel_event.is_set()
    state.terminal_phase_hint = RuntimePhase.CANCELLED if cancelled else RuntimePhase.FAILED
    if cancelled:
        await _handle_cancelled_failure(state)
        return
    if isinstance(exc, ProviderRequestNeedsReconciliation):
        await _handle_provider_reconciliation_required(state, exc)
        return
    summary = f"Web chat run failed: {type(exc).__name__}"
    failure = _runtime_exception_failure(exc)
    metadata = {"error": str(exc), "terminal_reason": failure.terminal_reason}
    try:
        if state.stream_batcher is not None:
            await state.stream_batcher.flush()
        runtime_task, agent, user, *_ = await state.ports.context.load_runtime_context(state.run_uuid)
        session_id = str(runtime_task.parent_session_id)
        changes = _failed_file_change_kwargs(state)
        finalized = await state.ports.terminal.finalize_without_assistant(
            run_uuid=state.run_uuid,
            agent_id=agent.id,
            session_id=session_id,
            status="failed",
            result_summary=summary,
            metadata_json=metadata,
            **changes,
        )
        if not finalized:
            await state.ports.terminal.update_runtime_task(
                state.run_uuid, status="failed", result_summary=summary, metadata_json=metadata
            )
        await state.ports.terminal.emit_terminal_hook(
            agent_id=agent.id,
            session_id=session_id,
            run_uuid=state.run_uuid,
            runtime_metadata=state.metadata or None,
            status="failed",
            reason="runtime_exception",
            extra_metadata=metadata,
        )
        await state.ports.events.broadcast(
            agent.id,
            session_id,
            {
                "type": "runtime_failure",
                "status": "failed",
                "reason": failure.terminal_reason,
                "retryable": True,
            },
        )
    except Exception as terminal_exc:
        await _handle_terminal_persistence_failure(state, exc, terminal_exc)


_PROVIDER_SEND_RECONCILIATION_REASON = "ambiguous_provider_send"


async def _committed_provider_send_reconciliation(
    state: _WebChatRunState,
    *,
    provider_request_id: str,
) -> dict[str, Any] | None:
    """Read the durable ambiguous-send settlement without a fenced entity load.

    ``fail_model_request`` owns the canonical terminal write for an ambiguous
    provider send and bumps claim_version once committed, so reloading the ORM
    entity from this worker would trip the stale fence. The typed columns are
    read directly and matched against the exact reconciliation code.
    """

    from app.models.runtime_task import RuntimeTask

    if state.agent is None or not state.session_id:
        return None
    async with state.ports.runtime.tenant_scoped_session(state.agent.tenant_id) as db:
        row = (
            await db.execute(
                select(
                    RuntimeTask.status,
                    RuntimeTask.metadata_json,
                ).where(
                    RuntimeTask.id == state.run_uuid,
                    RuntimeTask.tenant_id == state.agent.tenant_id,
                    RuntimeTask.parent_agent_id == state.agent.id,
                    RuntimeTask.parent_session_id == state.session_id,
                )
            )
        ).first()
    if row is None:
        return None
    status, metadata = row
    recovery = metadata.get("session_v2_reconciliation") if isinstance(metadata, dict) else None
    if not isinstance(recovery, dict):
        return None
    if str(status or "") != "needs_reconciliation":
        return None
    if str(recovery.get("reason") or "") != _PROVIDER_SEND_RECONCILIATION_REASON:
        return None
    if str(recovery.get("provider_request_id") or "") != str(provider_request_id):
        return None
    return dict(recovery)


async def _handle_provider_reconciliation_required(
    state: _WebChatRunState,
    exc: ProviderRequestNeedsReconciliation,
) -> None:
    """Fence an ambiguous provider send without inventing an assistant answer."""

    committed = await _committed_provider_send_reconciliation(
        state,
        provider_request_id=exc.provider_request_id,
    )
    if committed is None:
        # No durable ambiguous-send settlement exists yet: this terminal
        # update is the canonical settlement for the run.
        metadata = {
            "terminal_reason": "provider_send_ambiguous",
            "session_v2_reconciliation": {
                "reason": "ambiguous_provider_send",
                "provider_request_id": exc.provider_request_id,
                "error_class": exc.error_class,
            },
        }
        await state.ports.terminal.update_runtime_task(
            state.run_uuid,
            status="needs_reconciliation",
            result_summary="Provider send outcome is ambiguous; operator reconciliation is required.",
            metadata_json=metadata,
        )
    if state.agent is None or not state.session_id:
        return
    await state.ports.events.broadcast(
        state.agent.id,
        state.session_id,
        {
            "type": "runtime_reconciliation_required",
            "run_id": str(state.run_uuid),
            "provider_request_id": exc.provider_request_id,
            "error_class": exc.error_class,
            "retryable": False,
        },
    )


async def _handle_cancelled_failure(state: _WebChatRunState) -> None:
    await state.ports.terminal.update_runtime_task(
        state.run_uuid,
        status="killed",
        result_summary="Generation stopped by user.",
        metadata_json={"cancelled_by_user": True},
    )
    if state.agent is None or not state.session_id:
        return
    await state.ports.terminal.emit_terminal_hook(
        agent_id=state.agent.id,
        session_id=state.session_id,
        run_uuid=state.run_uuid,
        runtime_metadata=state.metadata or None,
        status="killed",
        reason="user_cancelled",
        extra_metadata={"cancelled_by_user": True, "terminal_reason": TerminalReason.USER_CANCEL.value},
    )


def _failed_file_change_kwargs(state: _WebChatRunState) -> dict[str, Any]:
    if state.runtime_session_context is None:
        return {}
    paths = state.ports.artifacts.file_change_paths(state.runtime_session_context)
    return _file_change_kwargs(state) if paths else {}


async def _handle_terminal_persistence_failure(
    state: _WebChatRunState,
    original: Exception,
    terminal: Exception,
) -> None:
    state.ports.runtime.logger.warning(
        "[WebChatRun] Failed to persist visible terminal error for {}: {}",
        state.run_uuid.hex,
        terminal,
    )
    metadata = {
        "error": str(terminal),
        "original_error": str(original),
        "terminal_reason": TerminalReason.PERSISTENCE_ERROR.value,
        "persistence_error": True,
    }
    await state.ports.terminal.update_runtime_task(
        state.run_uuid,
        status="failed",
        result_summary=f"Web chat persistence failed: {type(terminal).__name__}",
        metadata_json=metadata,
    )


async def _cleanup_web_chat_run(state: _WebChatRunState) -> None:
    if state.phase_emitter is not None:
        settled = state.terminal_phase_hint or (
            RuntimePhase.CANCELLED if state.cancel_event.is_set() else RuntimePhase.DONE
        )
        await state.phase_emitter.transition(settled)
    state.ports.runtime.broadcast_run_context.reset(state.broadcast_token)
    state.ports.runtime.cancel_events.pop(state.run_key, None)
    if state.agent is None or not state.session_id:
        return
    try:
        await state.ports.terminal.resume_queued_handoffs(
            agent_id=state.agent.id,
            session_id=state.session_id,
            completed_run_id=state.run_key,
        )
    except Exception as exc:
        state.ports.runtime.logger.warning(
            "[WebChatRun] queued Plan Mode handoff cleanup failed: run_id={} error={}",
            state.run_key,
            exc,
        )
