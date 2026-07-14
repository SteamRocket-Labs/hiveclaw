"""Typed orchestration for one agent invocation lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.runtime.invoker import AgentInvocationRequest, AgentInvocationResult


@dataclass(frozen=True, slots=True)
class InvocationPorts:
    """Explicit runtime dependencies captured by the invoker facade per call."""

    result_type: Any
    execution_identity_type: Any
    kernel_request_type: Any
    terminal_reason_type: Any
    enforce_quota: Callable[..., Any]
    ensure_turn_metadata: Callable[..., Any]
    format_hook_contexts: Callable[..., str]
    latest_user_prompt: Callable[..., str]
    maybe_await: Callable[..., Any]
    normalize_session_context: Callable[..., Any]
    resolve_smart_routing: Callable[..., Any]
    resolve_context_budget: Callable[..., Any]
    resolve_turn_route: Callable[..., Any]
    resolve_eviction_dir: Callable[..., Any]
    resolve_kernel: Callable[..., Any]
    skill_ranking_inputs: Callable[..., Any]
    build_skill_catalog: Callable[..., str]
    combined_tools: Callable[..., Any]
    record_skill_usage: Callable[..., Any]
    logger: Any


@dataclass(slots=True)
class _InvocationState:
    request: AgentInvocationRequest
    ports: InvocationPorts
    kernel_request: Any
    route: dict[str, Any]
    skill_events: list[dict[str, Any]] = field(default_factory=list)

    def blocked(self, prefix: str, reason: str | None = None) -> AgentInvocationResult:
        return self.ports.result_type(
            content=f"{prefix}: {reason or 'policy'}",
            tokens_used=0,
            final_tools=[],
            parts=[],
            terminal_reason=self.ports.terminal_reason_type.HOOK_STOPPED,
        )


async def run_agent_invocation(
    request: AgentInvocationRequest,
    *,
    ports: InvocationPorts,
) -> AgentInvocationResult:
    """Run the single public invocation owner through explicit lifecycle stages."""
    ports.normalize_session_context(request)
    quota_result = await ports.enforce_quota(request)
    if quota_result is not None:
        return quota_result
    state = await _assemble_invocation_state(request, ports)
    for hook_stage in (_run_setup_hook, _run_prompt_hook, _run_session_start_hook):
        blocked = await hook_stage(state)
        if blocked is not None:
            return blocked
    return await _invoke_and_close(state)


async def _assemble_invocation_state(
    request: AgentInvocationRequest,
    ports: InvocationPorts,
) -> _InvocationState:
    skill_events: list[dict[str, Any]] = []

    async def on_tool_call(data: dict[str, Any]) -> None:
        if isinstance(data, dict) and data.get("status") == "done":
            skill_events.append({"name": data.get("name"), "args": data.get("args"), "status": data.get("status")})
        if request.on_tool_call is not None:
            await ports.maybe_await(request.on_tool_call(data))

    routing_config = request.smart_model_routing
    if routing_config is None and request.agent_id is not None and request.fallback_model is not None:
        routing_config = await ports.resolve_smart_routing(request.agent_id)
    route = ports.resolve_turn_route(request, routing_config=routing_config)
    if request.on_event is not None:
        try:
            await ports.maybe_await(
                request.on_event(
                    {
                        "type": "session_context",
                        "event_type": "model_route",
                        **route["metadata"],
                    }
                )
            )
        except Exception as exc:
            ports.logger.warning("[Invoker] model-route event callback failed: %s", exc)
    execution_identity = _resolve_execution_identity(request, ports)
    skill_catalog = _build_skill_catalog(request, ports)
    kernel_request = _build_kernel_request(
        request,
        ports,
        route=route,
        execution_identity=execution_identity,
        skill_catalog=skill_catalog,
        on_tool_call=on_tool_call,
    )
    return _InvocationState(
        request=request,
        ports=ports,
        kernel_request=kernel_request,
        route=route,
        skill_events=skill_events,
    )


def _resolve_execution_identity(request: AgentInvocationRequest, ports: InvocationPorts) -> Any:
    if request.execution_identity is not None:
        return request.execution_identity
    try:
        from app.core.execution_context import get_execution_identity

        current_identity = get_execution_identity()
        if current_identity:
            return ports.execution_identity_type(
                identity_type=current_identity.identity_type,
                identity_id=current_identity.identity_id,
                label=current_identity.label,
            )
    except Exception:
        return None
    return None


def _build_skill_catalog(request: AgentInvocationRequest, ports: InvocationPorts) -> str:
    if request.agent_id is None or (request.standalone_system_prompt or "").strip():
        return ""
    ranking_inputs = ports.skill_ranking_inputs(request)
    ranking: list[dict[str, Any]] = []
    catalog = ports.build_skill_catalog(
        request.agent_id,
        budget_profile=ports.resolve_context_budget(request),
        scenario_text=ranking_inputs["scenario_text"],
        session_id=(
            getattr(request.session_context, "session_id", None)
            if request.session_context is not None
            else request.memory_session_id
        ),
        active_skill_names=ranking_inputs["active_skill_names"],
        path_triggered_skill_names=ranking_inputs["path_triggered_skill_names"],
        ranking_manifest=ranking,
    )
    if request.session_context is not None:
        from app.runtime.context import ensure_runtime_assembly_state

        ensure_runtime_assembly_state(request.session_context).record_skill_catalog_ranking(
            ranking=ranking,
            inputs={
                "scenario_text_present": bool(ranking_inputs["scenario_text"]),
                "active_skill_names": list(ranking_inputs["active_skill_names"]),
                "path_triggered_skill_names": list(ranking_inputs["path_triggered_skill_names"]),
            },
        )
    return catalog


def _build_kernel_request(
    request: AgentInvocationRequest,
    ports: InvocationPorts,
    *,
    route: dict[str, Any],
    execution_identity: Any,
    skill_catalog: str,
    on_tool_call: Callable[..., Any],
) -> Any:
    return ports.kernel_request_type(
        model=route["model"],
        fallback_model=route["fallback_model"],
        messages=request.messages,
        agent_name=request.agent_name,
        role_description=request.role_description,
        agent_id=request.agent_id,
        user_id=request.user_id,
        execution_identity=execution_identity,
        on_chunk=request.on_chunk,
        on_tool_call=on_tool_call,
        on_thinking=request.on_thinking,
        on_event=request.on_event,
        supports_vision=route["supports_vision"],
        memory_context=request.memory_context,
        memory_session_id=request.memory_session_id,
        memory_messages=request.memory_messages,
        session_context=request.session_context,
        system_prompt_suffix=request.system_prompt_suffix,
        standalone_system_prompt=request.standalone_system_prompt,
        skill_catalog=skill_catalog,
        tool_executor=request.tool_executor,
        mid_run_message_drain=request.mid_run_message_drain,
        cancel_event=request.cancel_event,
        initial_tools=request.initial_tools or (ports.combined_tools() if request.agent_id is None else None),
        core_tools_only=request.core_tools_only,
        allowed_tool_names=request.allowed_tool_names,
        excluded_tool_names=request.excluded_tool_names,
        expand_tools=request.expand_tools,
        max_tool_rounds=request.max_tool_rounds,
        max_output_tokens=request.max_output_tokens,
        eviction_dir=ports.resolve_eviction_dir(request.agent_id),
        invocation_scope=request.invocation_scope,
        delegation_token=request.delegation_token,
    )


def _hook_identity(state: _InvocationState) -> tuple[str, str | None, dict[str, Any]]:
    request = state.request
    source = request.session_context.source if request.session_context else "runtime"
    session_id = request.memory_session_id or (request.session_context.session_id if request.session_context else None)
    return source, session_id, state.ports.ensure_turn_metadata(request)


def _append_hook_context(state: _InvocationState, contexts: Any) -> None:
    hook_context = state.ports.format_hook_contexts(contexts)
    if hook_context:
        state.kernel_request.system_prompt_suffix = "\n\n".join(
            part for part in (state.kernel_request.system_prompt_suffix, hook_context) if part
        )


async def _run_setup_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.SETUP,
            agent_id=request.agent_id,
            session_id=session_id,
            source=source,
            metadata={
                "tenant_id": metadata.get("tenant_id"),
                "user_id": str(request.user_id) if request.user_id else None,
                "runtime_task_id": metadata.get("runtime_task_id") or metadata.get("task_id"),
                "request_id": metadata.get("request_id"),
                "trace_id": metadata.get("trace_id"),
                "turn_id": metadata.get("turn_id"),
                "intent_id": metadata.get("intent_id"),
                "setup_trigger": "invocation",
                "cloud_workspace_uri": (f"agent://{request.agent_id}/sessions/{session_id or 'runtime'}/workspace"),
            },
        )
        if result and result.block:
            return state.blocked("Blocked by setup hook", result.reason)
        if result and result.additional_contexts:
            _append_hook_context(state, result.additional_contexts)
    except Exception as exc:
        state.ports.logger.error("[Invoker] required SETUP hook runtime failed closed: %s", exc)
        return state.blocked("Blocked by required setup hook failure", type(exc).__name__)
    return None


async def _run_prompt_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.USER_PROMPT_SUBMIT,
            agent_id=request.agent_id,
            session_id=session_id,
            prompt=state.ports.latest_user_prompt(request.messages),
            source=source,
            metadata={
                "tenant_id": metadata.get("tenant_id"),
                "runtime_task_id": metadata.get("runtime_task_id") or metadata.get("task_id"),
                "request_id": metadata.get("request_id"),
                "trace_id": metadata.get("trace_id"),
                "turn_id": metadata.get("turn_id"),
                "intent_id": metadata.get("intent_id"),
                "agent_name": request.agent_name,
                "execution_mode": request.invocation_scope,
            },
        )
        if result and result.block:
            return state.blocked("Blocked by prompt hook", result.reason)
        if result and result.additional_contexts:
            _append_hook_context(state, result.additional_contexts)
    except Exception as exc:
        state.ports.logger.error("[Invoker] required USER_PROMPT_SUBMIT hook runtime failed closed: %s", exc)
        return state.blocked("Blocked by required prompt hook failure", type(exc).__name__)
    return None


async def _run_session_start_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, _session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.SESSION_START,
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=source,
            metadata={
                "model": _model_name(state.route["model"]),
                "fallback_model": _model_name(state.route["fallback_model"]),
                "turn_route_reason": state.route["metadata"]["reason"],
                "execution_mode": request.invocation_scope,
                "tenant_id": metadata.get("tenant_id"),
                "runtime_task_id": metadata.get("runtime_task_id") or metadata.get("task_id"),
                "request_id": metadata.get("request_id"),
                "trace_id": metadata.get("trace_id"),
                "turn_id": metadata.get("turn_id"),
                "intent_id": metadata.get("intent_id"),
            },
        )
        if result and result.block:
            return state.blocked("Blocked by session start hook", result.reason)
        if result:
            _apply_session_start_result(state, result, metadata)
    except Exception as exc:
        state.ports.logger.error("[Invoker] required SESSION_START hook runtime failed closed: %s", exc)
        return state.blocked("Blocked by required session start hook failure", type(exc).__name__)
    return None


def _model_name(model: Any) -> str | None:
    return getattr(model, "model", str(model)) if model else None


def _apply_session_start_result(state: _InvocationState, result: Any, metadata: dict[str, Any]) -> None:
    if result.initial_user_message:
        message = str(result.initial_user_message).strip()
        if message:
            state.kernel_request.messages = [
                {"role": "user", "content": message, "source": "session_start_hook"},
                *state.kernel_request.messages,
            ]
    if result.additional_contexts:
        _append_hook_context(state, result.additional_contexts)
    if result.watch_paths and state.request.session_context is not None:
        existing = list(metadata.get("hook_watch_paths") or [])
        metadata["hook_watch_paths"] = list(dict.fromkeys((*existing, *result.watch_paths)))


def _record_skill_usage(state: _InvocationState, terminal_status: str, assistant_text: str) -> None:
    request = state.request
    if request.agent_id is None or not state.skill_events:
        return
    try:
        state.ports.record_skill_usage(
            agent_id=request.agent_id,
            session_context=request.session_context,
            tool_events=state.skill_events,
            terminal_status=terminal_status,
            assistant_text=assistant_text,
            note=assistant_text,
        )
    except Exception as exc:
        state.ports.logger.warning(
            "[Invoker] skill runtime usage telemetry failed: agent_id=%s session_id=%s error=%s",
            request.agent_id,
            request.session_context.session_id if request.session_context else None,
            exc,
        )


async def _invoke_and_close(state: _InvocationState) -> AgentInvocationResult:
    try:
        result = await state.ports.resolve_kernel(state.request).handle(state.kernel_request)
    except Exception as exc:
        _record_skill_usage(state, "failed", f"{type(exc).__name__}: {exc}")
        raise
    _record_skill_usage(state, "completed", str(result.content or ""))
    await _emit_close_hooks(state, result)
    return state.ports.result_type(
        content=result.content,
        tokens_used=result.tokens_used,
        final_tools=result.final_tools,
        parts=result.parts,
        reasoning_signature=getattr(result, "reasoning_signature", None),
        terminal_reason=getattr(result, "terminal_reason", state.ports.terminal_reason_type.TURN_STOP),
    )


async def _emit_close_hooks(state: _InvocationState, result: Any) -> None:
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    completed_messages = [*request.messages, {"role": "assistant", "content": result.content}]
    source, _session_id, metadata = _hook_identity(state)
    hook_metadata = {
        "agent_name": request.agent_name,
        "tenant_id": metadata.get("tenant_id"),
        "runtime_task_id": metadata.get("runtime_task_id") or metadata.get("task_id"),
        "request_id": metadata.get("request_id"),
        "trace_id": metadata.get("trace_id"),
        "turn_id": metadata.get("turn_id"),
        "intent_id": metadata.get("intent_id"),
        "turn_count": len(completed_messages),
        "reason": "invoke_return",
        "checkpoint_kind": "user_turn_stop",
        "important_files": list(getattr(request.session_context, "recent_files", []) or [])
        if request.session_context
        else [],
        "pending_work": list(getattr(request.session_context, "pending_items", []) or [])
        if request.session_context
        else [],
        "last_successful_step": result.content,
        "activation_events": list(ensure_runtime_assembly_state(request.session_context).activation_events)
        if request.session_context
        else [],
    }
    try:
        await emit_hook(
            HookEvent.SESSION_END,
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=source,
            messages=completed_messages,
            metadata=hook_metadata,
        )
        if request.emit_turn_stop:
            await emit_hook(
                HookEvent.TURN_STOP,
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                source=source,
                messages=completed_messages,
                metadata=hook_metadata,
            )
    except Exception as exc:
        state.ports.logger.debug("[Invoker] response/session close hooks failed (non-fatal): %s", exc)
