"""Typed, staged owner for the durable web-chat RuntimeTask lifecycle."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from app.kernel.contracts import ExecutionIdentityRef, TerminalReason
from app.runtime.invoker import AgentInvocationRequest
from app.runtime.runtime_phase import RunPhaseEmitter, RuntimePhase


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
    claim_mid_run_messages: Callable[..., Any]
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
    is_llm_error_message: Callable[..., bool]
    interactive_pause_summary: Callable[..., Any]
    cancel_events: dict[str, asyncio.Event]
    broadcast_run_context: Any
    user_visible_error: str
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
    terminal_tool_card_finalized: bool = False


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
    budget_run_id = getattr(state.runtime_task, "budget_run_id", None) or state.metadata.get("budget_run_id")
    if budget_run_id:
        context.metadata["budget_run_id"] = str(budget_run_id)
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
    response = (
        f"[LLM Error] {state.agent.name} has no LLM model configured. "
        "Please select a model in the agent's Settings tab."
    )
    await _finalize_pre_invocation_response(state, response, status="failed", reason="llm_model_missing")
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
        result_summary=response[:500],
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
        event = events.build_thinking(text) if kind == "thinking" else events.build_chunk(text, reset=reset)
        if text and not reset:
            persisted = await events.persist_stream_step(
                agent_id=state.agent.id,
                tenant_id=state.agent.tenant_id,
                user_id=state.actor_user_id,
                external_principal_id=state.actor_external_principal_id,
                session_id=state.session_id,
                run_uuid=state.run_uuid,
                event_type=kind,
                content=text,
                part=event.get("part") if isinstance(event.get("part"), dict) else None,
            )
            _apply_persisted_event(event, persisted, kind, text)
        await events.broadcast(state.agent.id, state.session_id, event)

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
        data = events.tool_step_contract(data, fallback_run_id=state.run_uuid)
        persisted = await events.persist_tool_call(
            agent_id=state.agent.id,
            user_id=state.actor_user_id,
            external_principal_id=state.actor_external_principal_id,
            session_id=state.session_id,
            data=data,
        )
        ws_event = events.build_tool_call(data)
        _apply_persisted_tool_event(ws_event, persisted, done=data.get("status") == "done")
        await events.broadcast(state.agent.id, state.session_id, ws_event)
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
        channel_prompt = None
        if pause == "awaiting_session_permission" and not state.ports.context.is_web_origin_turn(
            state.metadata, state.runtime_session_context
        ):
            channel_prompt = state.ports.context.channel_permission_prompt(data)
        await self.finalize_terminal_card(pause, channel_delivery_text=channel_prompt)
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
        finalized = await state.ports.terminal.finalize_without_assistant(
            run_uuid=state.run_uuid,
            agent_id=state.agent.id,
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
    suffix = _join_suffix(suffix, state.channel_delivery_suffix, state.ports.artifacts.prompt_suffix())
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
        mid_run_message_drain=lambda: state.ports.context.claim_mid_run_messages(state.run_uuid),
        disable_tools=state.disable_tools_for_turn,
        excluded_tool_names=state.excluded_tool_names_for_turn,
        emit_turn_stop=False,
    )


def _execution_identity_type(state: _WebChatRunState) -> str:
    if state.actor_external_principal_id is None:
        return "delegated_user"
    return "external_principal_bound" if state.actor_authority_bound else "external_principal"


async def _finalize_invocation_result(state: _WebChatRunState, result: Any) -> None:
    if result is None and state.interactive_pause_summary:
        await _finalize_interactive_pause(state, state.interactive_pause_summary)
        return
    response = result.content
    plan_error = (
        None
        if state.plan_mode_submitted or state.interactive_pause_summary
        else state.ports.terminal.plan_mode_terminal_error(state.runtime_session_context)
    )
    if plan_error:
        response = plan_error
        state.ports.terminal.clear_interactive_plan_mode(state.runtime_session_context)
    thinking = "".join(state.thinking_content) if state.thinking_content else None
    failed = bool(plan_error) or state.ports.runtime.is_llm_error_message(response)
    status = "killed" if state.cancel_event.is_set() else ("failed" if failed else "completed")
    state.terminal_phase_hint = state.ports.terminal.phase_for_status(status)
    metadata = {
        "cancelled_by_user": bool(state.cancel_event.is_set()),
        "terminal_reason": state.ports.terminal.terminal_reason(
            status=status,
            result_reason=getattr(result, "terminal_reason", None),
            cancelled_by_user=bool(state.cancel_event.is_set()),
            plan_mode_terminal_error=bool(plan_error),
            llm_error=state.ports.runtime.is_llm_error_message(response),
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
    finalized = await state.ports.terminal.finalize_without_assistant(
        run_uuid=state.run_uuid,
        agent_id=state.agent.id,
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


async def _handle_web_chat_failure(state: _WebChatRunState, exc: Exception) -> None:
    state.ports.runtime.logger.exception("[WebChatRun] Run {} failed", state.run_uuid.hex)
    cancelled = state.cancel_event.is_set()
    state.terminal_phase_hint = RuntimePhase.CANCELLED if cancelled else RuntimePhase.FAILED
    if cancelled:
        await _handle_cancelled_failure(state)
        return
    summary = f"Web chat run failed: {type(exc).__name__}"
    metadata = {"error": str(exc)[:500], "terminal_reason": TerminalReason.PROVIDER_ERROR.value}
    try:
        if state.stream_batcher is not None:
            await state.stream_batcher.flush()
        runtime_task, agent, user, *_ = await state.ports.context.load_runtime_context(state.run_uuid)
        session_id = str(runtime_task.parent_session_id)
        changes = _failed_file_change_kwargs(state)
        finalized = await state.ports.terminal.finalize_with_assistant(
            run_uuid=state.run_uuid,
            agent_id=agent.id,
            user_id=state.ports.context.actor_user_id(user),
            session_id=session_id,
            content=state.ports.runtime.user_visible_error,
            thinking=None,
            thinking_signature=None,
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
            {"type": "error", "content": state.ports.runtime.user_visible_error},
        )
    except Exception as terminal_exc:
        await _handle_terminal_persistence_failure(state, exc, terminal_exc)


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
        "error": str(terminal)[:500],
        "original_error": str(original)[:500],
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
