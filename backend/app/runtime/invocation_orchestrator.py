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
    load_secret_boundary: Callable[..., Any]
    record_skill_usage: Callable[..., Any]
    logger: Any


@dataclass(slots=True)
class _InvocationState:
    request: AgentInvocationRequest
    ports: InvocationPorts
    kernel_request: Any
    route: dict[str, Any]
    skill_events: list[dict[str, Any]] = field(default_factory=list)
    exact_secret_boundary: Any = None
    secret_stream_redactor: Any = None
    secret_thinking_redactor: Any = None
    secret_aux_counts: dict[str, int] = field(default_factory=dict)
    secret_aux_refs: list[str] = field(default_factory=list)

    def blocked(self, prefix: str, reason: str | None = None) -> AgentInvocationResult:
        reason_redaction = self.exact_secret_boundary.redact_text(
            reason or "policy",
        )
        _record_state_redaction(self, "hook_block_reason", reason_redaction)
        return self.ports.result_type(
            content=f"{prefix}: {reason_redaction.text}",
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
            aux_surfaces, aux_refs = _secret_aux_evidence(state)
            await _emit_secret_egress_event(
                state,
                surfaces=aux_surfaces,
                source_refs=aux_refs,
            )
            return blocked
    return await _invoke_and_close(state)


async def _assemble_invocation_state(
    request: AgentInvocationRequest,
    ports: InvocationPorts,
) -> _InvocationState:
    skill_events: list[dict[str, Any]] = []
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
            ports.logger.warning(
                "[Invoker] model-route event callback failed: %s",
                type(exc).__name__,
            )
    execution_identity = _resolve_execution_identity(request, ports)
    skill_catalog = _build_skill_catalog(request, ports)
    exact_secret_boundary = _model_secret_boundary(route)
    if request.tenant_id is not None and request.agent_id is not None:
        try:
            tenant_secret_boundary = await ports.load_secret_boundary(
                tenant_id=request.tenant_id,
                agent_id=request.agent_id,
            )
        except Exception as exc:
            ports.logger.error(
                "[Invoker] credential authority unavailable for agent %s (%s)",
                request.agent_id,
                type(exc).__name__,
            )
            from app.kernel.contracts import ContextDependencyUnavailable

            raise ContextDependencyUnavailable(
                dependency="credential_authority",
                code="credential_authority_unavailable",
                user_message=("Credential authority is temporarily unavailable; the agent turn was not started."),
                retryable=True,
            ) from None
        from app.services.exact_secret_boundary import ExactSecretBoundary

        exact_secret_boundary = ExactSecretBoundary.combine(
            exact_secret_boundary,
            tenant_secret_boundary,
        )
    secret_aux_counts: dict[str, int] = {}
    secret_aux_refs: list[str] = []

    def record_payload_redaction(surface: str, redaction: Any) -> None:
        if redaction.redacted_count:
            secret_aux_counts[surface] = secret_aux_counts.get(surface, 0) + redaction.redacted_count
            for source_ref in redaction.matched_refs:
                if source_ref not in secret_aux_refs:
                    secret_aux_refs.append(source_ref)

    def redact_payload(surface: str, payload: Any) -> Any:
        redaction = exact_secret_boundary.redact_payload_with_evidence(payload)
        record_payload_redaction(surface, redaction)
        return redaction.value

    async def on_tool_call(data: dict[str, Any]) -> None:
        safe_data = redact_payload("tool_event", data)
        if isinstance(safe_data, dict) and safe_data.get("status") == "done":
            skill_events.append(
                {
                    "name": safe_data.get("name"),
                    "args": safe_data.get("args"),
                    "status": safe_data.get("status"),
                }
            )
        if request.on_tool_call is not None:
            await ports.maybe_await(request.on_tool_call(safe_data))

    async def on_event(data: dict[str, Any]) -> None:
        if isinstance(data, dict) and data.get("type") == "stream_retry_tombstone":
            for redactor in (
                secret_stream_redactor,
                secret_thinking_redactor,
            ):
                if redactor is not None:
                    redactor.reset_pending()
        safe_data = redact_payload("runtime_event", data)
        if request.on_event is not None:
            await ports.maybe_await(request.on_event(safe_data))

    async def model_response_commit(**payload: Any) -> Any:
        safe_payload = redact_payload("model_response_commit", payload)
        if request.model_response_commit is None:
            return None
        receipt = await ports.maybe_await(request.model_response_commit(**safe_payload))
        return redact_payload("model_response_receipt", receipt)

    async def mid_run_message_drain() -> Any:
        drained = await ports.maybe_await(request.mid_run_message_drain())
        return redact_payload("mid_run_input", drained)

    async def round_input_bind(*args: Any, **kwargs: Any) -> Any:
        bound = await ports.maybe_await(request.round_input_bind(*args, **kwargs))
        return redact_payload("round_input", bound)

    async def model_request_prepare(**payload: Any) -> Any:
        safe_payload = redact_payload("model_request_prepare", payload)
        return await ports.maybe_await(request.model_request_prepare(**safe_payload))

    secret_stream_redactor = None
    secret_thinking_redactor = None
    on_chunk = request.on_chunk
    if on_chunk is not None and not exact_secret_boundary.is_empty:
        from app.services.exact_secret_boundary import ExactSecretStreamRedactor

        secret_stream_redactor = ExactSecretStreamRedactor(exact_secret_boundary, on_chunk)
        on_chunk = secret_stream_redactor.feed
    on_thinking = request.on_thinking
    if on_thinking is not None and not exact_secret_boundary.is_empty:
        from app.services.exact_secret_boundary import ExactSecretStreamRedactor

        secret_thinking_redactor = ExactSecretStreamRedactor(
            exact_secret_boundary,
            on_thinking,
        )
        on_thinking = secret_thinking_redactor.feed
    kernel_request = _build_kernel_request(
        request,
        ports,
        route=route,
        execution_identity=execution_identity,
        skill_catalog=skill_catalog,
        on_tool_call=on_tool_call,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_event=on_event if request.on_event is not None else None,
        model_response_commit=(model_response_commit if request.model_response_commit is not None else None),
    )
    for surface, field_name in (
        ("model_input_messages", "messages"),
        ("model_input_memory_messages", "memory_messages"),
        ("model_input_memory_context", "memory_context"),
        ("model_input_agent_name", "agent_name"),
        ("model_input_role_description", "role_description"),
        ("model_input_system_prompt", "system_prompt_suffix"),
        ("model_input_standalone_prompt", "standalone_system_prompt"),
        ("model_input_skill_catalog", "skill_catalog"),
        ("model_input_initial_tools", "initial_tools"),
    ):
        setattr(
            kernel_request,
            field_name,
            redact_payload(surface, getattr(kernel_request, field_name)),
        )
    if request.mid_run_message_drain is not None:
        kernel_request.mid_run_message_drain = mid_run_message_drain
    if request.round_input_bind is not None:
        kernel_request.round_input_bind = round_input_bind
    if request.model_request_prepare is not None:
        kernel_request.model_request_prepare = model_request_prepare
    return _InvocationState(
        request=request,
        ports=ports,
        kernel_request=kernel_request,
        route=route,
        skill_events=skill_events,
        exact_secret_boundary=exact_secret_boundary,
        secret_stream_redactor=secret_stream_redactor,
        secret_thinking_redactor=secret_thinking_redactor,
        secret_aux_counts=secret_aux_counts,
        secret_aux_refs=secret_aux_refs,
    )


def _model_secret_boundary(route: dict[str, Any]):
    from app.services.exact_secret_boundary import ExactSecretBoundary

    pairs: list[tuple[str, str]] = []
    for lane, model in (("selected", route.get("model")), ("fallback", route.get("fallback_model"))):
        if model is None:
            continue
        value = str(getattr(model, "api_key", "") or "")
        # One-character/local test sentinels are not usable provider
        # credentials and would make exact substring redaction corrupt normal
        # prose (for example api_key="k" rewriting "ok"). Persisted runtime
        # credentials are still protected by the credential-store boundary.
        if len(value) < 8:
            continue
        model_id = getattr(model, "id", None) or getattr(model, "model", lane)
        pairs.append((f"llm-model://{model_id}/{lane}/api_key", value))
    return ExactSecretBoundary.from_pairs(pairs)


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
    on_chunk: Callable[..., Any] | None,
    on_thinking: Callable[..., Any] | None,
    on_event: Callable[..., Any] | None,
    model_response_commit: Callable[..., Any] | None,
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
        on_chunk=on_chunk,
        on_tool_call=on_tool_call,
        on_thinking=on_thinking,
        on_event=on_event,
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
        round_input_bind=request.round_input_bind,
        model_request_prepare=request.model_request_prepare,
        model_response_commit=model_response_commit,
        model_request_fail=request.model_request_fail,
        initial_round_index=max(0, int(request.initial_round_index or 0)),
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
        redaction = state.exact_secret_boundary.redact_text(hook_context)
        _record_state_redaction(state, "hook_context", redaction)
        state.kernel_request.system_prompt_suffix = "\n\n".join(
            part for part in (state.kernel_request.system_prompt_suffix, redaction.text) if part
        )


async def _run_setup_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.SETUP,
            evidence_mode="independent",
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
        state.ports.logger.error(
            "[Invoker] required SETUP hook runtime failed closed: %s",
            type(exc).__name__,
        )
        return state.blocked("Blocked by required setup hook failure", type(exc).__name__)
    return None


async def _run_prompt_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.USER_PROMPT_SUBMIT,
            evidence_mode="independent",
            agent_id=request.agent_id,
            session_id=session_id,
            prompt=state.ports.latest_user_prompt(state.kernel_request.messages),
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
        state.ports.logger.error(
            "[Invoker] required USER_PROMPT_SUBMIT hook runtime failed closed: %s",
            type(exc).__name__,
        )
        return state.blocked("Blocked by required prompt hook failure", type(exc).__name__)
    return None


async def _run_session_start_hook(state: _InvocationState) -> AgentInvocationResult | None:
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    source, _session_id, metadata = _hook_identity(state)
    try:
        result = await emit_hook(
            HookEvent.SESSION_START,
            evidence_mode="independent",
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
        state.ports.logger.error(
            "[Invoker] required SESSION_START hook runtime failed closed: %s",
            type(exc).__name__,
        )
        return state.blocked("Blocked by required session start hook failure", type(exc).__name__)
    return None


def _model_name(model: Any) -> str | None:
    return getattr(model, "model", str(model)) if model else None


def _apply_session_start_result(state: _InvocationState, result: Any, metadata: dict[str, Any]) -> None:
    if result.initial_user_message:
        redaction = state.exact_secret_boundary.redact_text(str(result.initial_user_message).strip())
        _record_state_redaction(state, "session_start_input", redaction)
        message = redaction.text
        if message:
            state.kernel_request.messages = [
                {"role": "user", "content": message, "source": "session_start_hook"},
                *state.kernel_request.messages,
            ]
    if result.additional_contexts:
        _append_hook_context(state, result.additional_contexts)
    if result.watch_paths and state.request.session_context is not None:
        existing = list(metadata.get("hook_watch_paths") or [])
        redaction = state.exact_secret_boundary.redact_payload_with_evidence(result.watch_paths)
        _record_state_redaction(state, "hook_watch_paths", redaction)
        metadata["hook_watch_paths"] = list(dict.fromkeys((*existing, *redaction.value)))


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
            type(exc).__name__,
        )


async def _invoke_and_close(state: _InvocationState) -> AgentInvocationResult:
    try:
        result = await state.ports.resolve_kernel(state.request).handle(state.kernel_request)
    except Exception as exc:
        await _finish_secret_streams(state)
        error_redaction = state.exact_secret_boundary.redact_text(f"{type(exc).__name__}: {exc}")
        _record_skill_usage(state, "failed", error_redaction.text)
        stream_surfaces, stream_refs = _secret_stream_evidence(state)
        aux_surfaces, aux_refs = _secret_aux_evidence(state)
        await _emit_secret_egress_event(
            state,
            surfaces={
                **stream_surfaces,
                **aux_surfaces,
                "error": error_redaction.redacted_count,
            },
            source_refs=tuple(
                dict.fromkeys(
                    [
                        *stream_refs,
                        *aux_refs,
                        *error_redaction.matched_refs,
                    ]
                )
            ),
        )
        if error_redaction.redacted_count:
            from app.services.exact_secret_boundary import ExactSecretEgressError

            raise ExactSecretEgressError(
                error_redaction.text,
                source_refs=error_redaction.matched_refs,
            ) from None
        raise
    await _finish_secret_streams(state)
    redaction = state.exact_secret_boundary.redact_text(str(result.content or ""))
    result.content = redaction.text
    parts_redaction = state.exact_secret_boundary.redact_payload_with_evidence(result.parts)
    result.parts = parts_redaction.value
    _record_skill_usage(state, "completed", str(result.content or ""))
    await _emit_close_hooks(state, result)
    stream_surfaces, stream_refs = _secret_stream_evidence(state)
    aux_surfaces, aux_refs = _secret_aux_evidence(state)
    source_refs = tuple(
        dict.fromkeys(
            [
                *stream_refs,
                *aux_refs,
                *redaction.matched_refs,
                *parts_redaction.matched_refs,
            ]
        )
    )
    await _emit_secret_egress_event(
        state,
        surfaces={
            **stream_surfaces,
            **aux_surfaces,
            "content": redaction.redacted_count,
            "parts": parts_redaction.redacted_count,
        },
        source_refs=source_refs,
    )
    return state.ports.result_type(
        content=result.content,
        tokens_used=result.tokens_used,
        final_tools=result.final_tools,
        parts=result.parts,
        reasoning_signature=getattr(result, "reasoning_signature", None),
        terminal_reason=getattr(result, "terminal_reason", state.ports.terminal_reason_type.TURN_STOP),
    )


def _secret_stream_evidence(
    state: _InvocationState,
) -> tuple[dict[str, int], tuple[str, ...]]:
    surfaces = {
        "stream": (state.secret_stream_redactor.redacted_count if state.secret_stream_redactor is not None else 0),
        "thinking": (
            state.secret_thinking_redactor.redacted_count if state.secret_thinking_redactor is not None else 0
        ),
    }
    source_refs: list[str] = []
    for redactor in (
        state.secret_stream_redactor,
        state.secret_thinking_redactor,
    ):
        if redactor is None:
            continue
        for source_ref in redactor.matched_refs:
            if source_ref not in source_refs:
                source_refs.append(source_ref)
    return surfaces, tuple(source_refs)


def _secret_aux_evidence(
    state: _InvocationState,
) -> tuple[dict[str, int], tuple[str, ...]]:
    return dict(state.secret_aux_counts), tuple(state.secret_aux_refs)


def _record_state_redaction(
    state: _InvocationState,
    surface: str,
    redaction: Any,
) -> None:
    if not redaction.redacted_count:
        return
    state.secret_aux_counts[surface] = state.secret_aux_counts.get(surface, 0) + redaction.redacted_count
    for source_ref in redaction.matched_refs:
        if source_ref not in state.secret_aux_refs:
            state.secret_aux_refs.append(source_ref)


async def _emit_secret_egress_event(
    state: _InvocationState,
    *,
    surfaces: dict[str, int],
    source_refs: tuple[str, ...],
) -> None:
    redacted_count = sum(max(0, int(count)) for count in surfaces.values())
    if not redacted_count:
        return
    event = {
        "type": "secret_egress_redacted",
        "status": "redacted",
        "code": "exact_unauthorized_secret_bytes",
        "redacted_count": redacted_count,
        "surfaces": surfaces,
        "source_refs": list(source_refs),
    }
    if state.request.session_context is not None:
        receipts = state.request.session_context.metadata.setdefault(
            "secret_boundary_receipts",
            [],
        )
        if isinstance(receipts, list):
            receipts.append(event)
    state.ports.logger.warning(
        "[Invoker] exact-secret boundary redacted %s occurrence(s) on %s",
        redacted_count,
        ",".join(surface for surface, count in surfaces.items() if count),
    )
    if state.request.on_event is not None:
        try:
            await state.ports.maybe_await(state.request.on_event(event))
        except Exception as exc:
            state.ports.logger.warning(
                "[Invoker] secret-egress evidence callback failed: %s",
                type(exc).__name__,
            )


async def _finish_secret_streams(state: _InvocationState) -> None:
    for surface, redactor in (
        ("stream", state.secret_stream_redactor),
        ("thinking", state.secret_thinking_redactor),
    ):
        if redactor is None:
            continue
        try:
            await redactor.finish()
        except Exception as exc:
            state.ports.logger.warning(
                "[Invoker] exact-secret %s flush callback failed: %s",
                surface,
                type(exc).__name__,
            )


async def _emit_close_hooks(state: _InvocationState, result: Any) -> None:
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.hooks import HookEvent, emit_hook

    request = state.request
    completed_messages = [
        *state.kernel_request.messages,
        {"role": "assistant", "content": result.content},
    ]
    source, _session_id, metadata = _hook_identity(state)
    hook_metadata = {
        "agent_name": state.kernel_request.agent_name,
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
    metadata_redaction = state.exact_secret_boundary.redact_payload_with_evidence(hook_metadata)
    _record_state_redaction(state, "close_hook_metadata", metadata_redaction)
    hook_metadata = metadata_redaction.value
    try:
        await emit_hook(
            HookEvent.SESSION_END,
            evidence_mode="independent",
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=source,
            messages=completed_messages,
            metadata=hook_metadata,
        )
        if request.emit_turn_stop:
            await emit_hook(
                HookEvent.TURN_STOP,
                evidence_mode="independent",
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                source=source,
                messages=completed_messages,
                metadata=hook_metadata,
            )
    except Exception as exc:
        state.ports.logger.debug(
            "[Invoker] response/session close hooks failed (non-fatal): %s",
            type(exc).__name__,
        )
