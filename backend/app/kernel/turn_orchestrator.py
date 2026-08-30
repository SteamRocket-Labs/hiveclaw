"""Owns one complete model/tool turn while the kernel facade retains reusable primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.kernel.contracts import ContextDependencyUnavailable, ProviderRequestNeedsReconciliation

if TYPE_CHECKING:
    from app.kernel.engine import (
        ContextPolicyV1,
        InvocationRequest,
        InvocationResult,
        LLMMessage,
        LoopGuardDecision,
        ToolExpansionResult,
    )


class _TurnTokenUsageLedger:
    """Track one invocation's delta without double-charging a resumed turn."""

    __slots__ = ("initial", "recorded")

    def __init__(self, initial: Any) -> None:
        try:
            self.initial = max(0, int(initial or 0))
        except (TypeError, ValueError):
            self.initial = 0
        self.recorded = 0

    async def record(
        self,
        record_token_usage: Any,
        maybe_await: Any,
        agent_id: Any,
        accumulated_tokens: int,
    ) -> None:
        invocation_tokens = max(accumulated_tokens - self.initial, 0)
        unrecorded_tokens = max(invocation_tokens - self.recorded, 0)
        if agent_id and unrecorded_tokens > 0:
            await maybe_await(record_token_usage(agent_id, unrecorded_tokens))
            self.recorded += unrecorded_tokens


class _RuntimeSpanRecorder:
    """Bind stable turn identity while allowing runtime config to resolve later."""

    __slots__ = ("deps", "record_runtime_span", "request", "root_span_id", "runtime_config")

    def __init__(self, deps: Any, request: Any, root_span_id: str, record_runtime_span: Any) -> None:
        self.deps = deps
        self.request = request
        self.root_span_id = root_span_id
        self.record_runtime_span = record_runtime_span
        self.runtime_config: Any = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        return await self.record_runtime_span(
            deps=self.deps,
            request=self.request,
            runtime_config=self.runtime_config,
            root_span_id=self.root_span_id,
            **kwargs,
        )


def _add_response_usage(
    deps: Any,
    response: Any,
    api_messages: list[Any],
    request: Any,
    accumulated_tokens: int,
    context_usage_anchor_tokens: int,
) -> tuple[int, int]:
    real_tokens = deps.extract_usage_tokens(response.usage)
    if real_tokens is not None:
        accumulated_tokens += real_tokens
    else:
        round_chars = sum(len(message.content or "") for message in api_messages if isinstance(message.content, str))
        accumulated_tokens += deps.estimate_tokens_from_chars(round_chars + len(response.content or ""))
    context_usage_anchor_tokens = max(context_usage_anchor_tokens, accumulated_tokens)
    if request.session_context is not None:
        request.session_context.metadata["usage_anchor_tokens"] = context_usage_anchor_tokens
    return accumulated_tokens, context_usage_anchor_tokens


def _turn_budget_blocks_tools(token_budget: int | None, tokens_used: int, tool_calls: list[Any] | None) -> bool:
    return bool(token_budget is not None and token_budget > 0 and tokens_used >= token_budget and tool_calls)


def _turn_token_budget_event(
    logical_round_index: int,
    tokens_used: int,
    token_budget: int,
    blocked_tool_call_count: int,
) -> dict[str, Any]:
    return {
        "type": "turn_token_budget_exhausted",
        "status": "blocked",
        "round": logical_round_index,
        "tokens_used": tokens_used,
        "token_budget": token_budget,
        "blocked_tool_call_count": blocked_tool_call_count,
        "retryable": True,
        "part": {
            "type": "event",
            "event_type": "turn_token_budget_exhausted",
            "title": "Turn budget reached",
            "text": "The turn stopped before the next tool action.",
            "status": "warning",
            "tokens_used": tokens_used,
            "token_budget": token_budget,
            "retryable": True,
        },
    }


def _build_turn_route_span_metadata(request: Any) -> dict[str, Any]:
    session_metadata = (
        request.session_context.metadata
        if request.session_context is not None and isinstance(request.session_context.metadata, dict)
        else {}
    )
    route = session_metadata.get("turn_route") if isinstance(session_metadata.get("turn_route"), dict) else {}
    return {
        "turn_route_reason": route.get("reason"),
        "routing_config_source": route.get("config_source"),
        "model_routing_locked": bool(route.get("model_routing_locked")),
        "selected_model_id": route.get("selected_model_id"),
    }


def _bind_system_prompt_suffix_sections(
    protected_sections: list[str],
    request: Any,
    session_ctx: Any,
    attachment_builder: Any,
) -> Any:
    def build() -> list[str]:
        return [
            *protected_sections,
            *attachment_builder(
                request.agent_id,
                session_ctx,
                request.recovery_manifest_result,
            ),
        ]

    return build


def _bind_work_ledger_view_provider(request: Any, *, enabled_key: str, logger: Any) -> Any:
    return lambda: _load_work_ledger_view(request, enabled_key=enabled_key, logger=logger)


def _provider_response_payload(response: Any) -> dict[str, Any]:
    return {
        "content": response.content or "",
        "reasoning_content": getattr(response, "reasoning_content", None),
        "reasoning_signature": getattr(response, "reasoning_signature", None),
        "tool_calls": list(response.tool_calls or []),
        "finish_reason": getattr(response, "finish_reason", None),
        "usage": dict(response.usage or {}),
        "model": getattr(response, "model", None),
    }


def _load_work_ledger_view(request: Any, *, enabled_key: str, logger: Any) -> dict[str, Any] | None:
    if not request.agent_id or request.session_context is None:
        return None
    metadata = getattr(request.session_context, "metadata", None)
    if not isinstance(metadata, dict) or not metadata.get(enabled_key):
        return None
    try:
        from app.services.agent_work_ledger import read_agent_work_ledger_view

        plan_state = getattr(request.session_context, "plan_mode", None)
        plan_id = metadata.get("plan_id") or getattr(plan_state, "plan_id", None)
        runtime_task_id = metadata.get("runtime_task_id") or metadata.get("task_id")
        session_id = request.memory_session_id or getattr(request.session_context, "session_id", None)
        return read_agent_work_ledger_view(
            agent_id=request.agent_id,
            plan_id=plan_id,
            runtime_task_id=runtime_task_id,
            session_id=None if plan_id or runtime_task_id else session_id,
        )
    except Exception as exc:
        logger.debug("[Kernel] Work Ledger reminder view failed: %s", exc)
        return None


def _render_work_ledger_snapshot(view_provider: Any, *, logger: Any) -> str:
    try:
        from app.services.agent_work_ledger import render_work_ledger_reminder_snapshot

        return render_work_ledger_reminder_snapshot(view_provider())
    except Exception as exc:
        logger.debug("[Kernel] Work Ledger reminder snapshot render failed: %s", exc)
        return ""


def _read_work_ledger_progress(view_provider: Any) -> dict[str, Any] | None:
    view = view_provider()
    if isinstance(view, dict) and isinstance(view.get("progress_ledger"), dict):
        return view["progress_ledger"]
    return None


async def _execute_committed_provider_round(
    *,
    self: Any,
    support: Any,
    request: Any,
    client: Any,
    active_model: Any,
    stream_messages: list[Any],
    tools_for_llm: list[dict[str, Any]],
    max_tokens: int,
    logical_round_index: int,
    emit_chunk: Any,
    emit_thinking: Any,
    record_span: Any,
    turn_route_span_metadata: Any,
    emit_event: Any,
    attempt_state: dict[str, Any],
    previous_result_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    """Own one Provider request/result fence without owning Agent semantics."""

    reasoning_kwargs = support.build_reasoning_kwargs(
        active_model,
        tools_enabled=bool(tools_for_llm),
    )
    request_temperature = support.resolve_temperature(active_model)
    provider = str(getattr(active_model, "provider", "") or "")
    model = str(getattr(active_model, "model", "") or "")
    provider_idempotency_supported = bool(getattr(client, "supports_request_idempotency", False))
    wire_request = {
        "messages": support._llm_messages_to_dicts(stream_messages),
        "tools": tools_for_llm,
        "temperature": request_temperature,
        "max_tokens": max_tokens,
        "reasoning": dict(reasoning_kwargs),
    }
    provider_request_id: str | None = None
    if request.model_request_prepare is not None:
        provider_request_id = str(
            await support._maybe_await(
                request.model_request_prepare(
                    round_index=logical_round_index,
                    messages=stream_messages,
                    tools=tools_for_llm or None,
                    provider=provider,
                    model=model,
                    wire_request=wire_request,
                    continuation_index=0,
                    provider_idempotency_supported=provider_idempotency_supported,
                    provider_idempotency_key_applied=False,
                )
            )
        )

    llm_started_ms = support.monotonic_ms()
    attempt_state.update(
        provider_request_id=provider_request_id,
        llm_started_ms=llm_started_ms,
    )
    response = await support._stream_with_cancel(
        client,
        cancel_event=request.cancel_event,
        messages=stream_messages,
        tools=tools_for_llm or None,
        temperature=request_temperature,
        max_tokens=max_tokens,
        on_chunk=emit_chunk,
        on_thinking=emit_thinking,
        **reasoning_kwargs,
    )
    base_physical_response = _provider_response_payload(response)
    response, continuation_receipts = await support._continue_after_output_cap(
        client=client,
        response=response,
        stream_messages=stream_messages,
        cancel_event=request.cancel_event,
        active_model=active_model,
        on_chunk=emit_chunk,
        on_thinking=emit_thinking,
        reasoning_kwargs=reasoning_kwargs,
        round_index=logical_round_index,
        model_request_prepare=request.model_request_prepare,
        provider=provider,
        model=model,
        provider_idempotency_supported=provider_idempotency_supported,
    )
    has_model_output = bool(response.tool_calls) or bool(isinstance(response.content, str) and response.content.strip())
    if not has_model_output:
        return {
            "response": response,
            "provider_request_id": provider_request_id,
            "llm_started_ms": llm_started_ms,
            "empty": True,
            "result_receipt": previous_result_receipt,
        }

    result_receipt = previous_result_receipt
    if request.model_response_commit is not None:
        for continuation_receipt in continuation_receipts:
            continuation_request_id = continuation_receipt.get("provider_request_id")
            if continuation_request_id is None:
                continue
            await support._maybe_await(
                request.model_response_commit(
                    round_index=logical_round_index,
                    continuation_index=int(continuation_receipt["continuation_index"]),
                    provider_request_id=str(continuation_request_id),
                    provider=provider,
                    model=model,
                    logical_round_complete=False,
                    response=dict(continuation_receipt["response"]),
                )
            )
        if provider_request_id is not None:
            logical_response = {
                **_provider_response_payload(response),
                "provider_call_ledger": [
                    {
                        "continuation_index": 0,
                        "provider_request_id": provider_request_id,
                        "response": base_physical_response,
                    },
                    *continuation_receipts,
                ],
            }
            committed_receipt = await support._maybe_await(
                request.model_response_commit(
                    round_index=logical_round_index,
                    continuation_index=0,
                    provider_request_id=provider_request_id,
                    provider=provider,
                    model=model,
                    logical_round_complete=True,
                    response=logical_response,
                )
            )
            if isinstance(committed_receipt, dict):
                result_receipt = committed_receipt

    provider_prompt_ledger = support.build_provider_prompt_ledger(
        messages=stream_messages,
        tools=tools_for_llm or None,
        provider=provider,
        model=model,
        round_index=logical_round_index,
        model_window_tokens=getattr(active_model, "max_input_tokens", None),
        cache_hints_applied=bool(self._deps.apply_cache_hints),
    )
    await record_span(
        span_type="generation",
        name="llm.stream",
        started_at_ms=llm_started_ms,
        metadata={
            **turn_route_span_metadata(),
            "provider": provider,
            "model": model,
            "round": logical_round_index,
            "tool_count": len(tools_for_llm),
            "tool_call_count": len(response.tool_calls or []),
            "usage": response.usage or {},
            "provider_prompt_ledger": provider_prompt_ledger,
        },
    )
    cache_metrics = support.extract_cache_metrics(
        response.usage,
        provider=provider or "unknown",
    )
    if response.usage:
        support.record_prompt_cache_metrics(cache_metrics)
    await emit_event(
        {
            "type": "session_context",
            "event_type": "provider_call_ledger",
            "provider_prompt_ledger": provider_prompt_ledger,
            "cache_metrics": cache_metrics.as_log_dict(),
            "tool_count": len(tools_for_llm),
            "tool_call_count": len(response.tool_calls or []),
            "visibility": "debug",
        }
    )
    output_tokens = support._usage_int(
        response.usage or {},
        "output_tokens",
        "completion_tokens",
        "candidatesTokenCount",
    )
    return {
        "response": response,
        "provider_request_id": provider_request_id,
        "llm_started_ms": llm_started_ms,
        "empty": False,
        "result_receipt": result_receipt,
        "provider_prompt_ledger": provider_prompt_ledger,
        "cache_metrics": cache_metrics,
        "output_tokens": output_tokens,
    }


async def _finalize_empty_provider_response(
    *,
    self: Any,
    support: Any,
    request: Any,
    response: Any,
    active_model: Any,
    logical_round_index: int,
    provider_request_id: str | None,
    llm_started_ms: int,
    accumulated_tokens: int,
    initial_turn_tokens_used: int,
    tools_for_llm: list[dict[str, Any]],
    record_span: Any,
    turn_route_span_metadata: Any,
) -> Any:
    if request.model_request_fail is not None and provider_request_id is not None:
        await support._maybe_await(
            request.model_request_fail(
                round_index=logical_round_index,
                provider_request_id=provider_request_id,
                error_class="provider_empty_response",
                delivery_state="response_received",
                retry_safe=True,
            )
        )
    empty_usage_tokens = self._deps.extract_usage_tokens(response.usage)
    if empty_usage_tokens:
        accumulated_tokens += empty_usage_tokens
    await record_span(
        span_type="generation",
        name="llm.stream",
        started_at_ms=llm_started_ms,
        metadata={
            **turn_route_span_metadata(),
            "provider": getattr(active_model, "provider", ""),
            "model": getattr(active_model, "model", ""),
            "round": logical_round_index,
            "status": "failed",
            "error_class": "provider_empty_response",
            "finish_reason": getattr(response, "finish_reason", None),
        },
    )
    invocation_tokens = max(accumulated_tokens - initial_turn_tokens_used, 0)
    if request.agent_id and invocation_tokens > 0:
        await support._maybe_await(self._deps.record_token_usage(request.agent_id, invocation_tokens))
    return support.InvocationResult(
        content="",
        tokens_used=accumulated_tokens,
        final_tools=tools_for_llm,
        parts=[],
        reasoning_signature=getattr(response, "reasoning_signature", None),
        terminal_reason=support.TerminalReason.PROVIDER_ERROR,
    )


def _classified_llm_error_result(
    build_error_result: Any,
    user_message: str,
    tokens_used: int,
    classification: Any,
    delivery_state: str,
) -> Any:
    """Preserve typed provider-failure facts at the turn result boundary."""

    return build_error_result(
        user_message,
        tokens_used=tokens_used,
        failure_code=classification.kind,
        failure_delivery_state=delivery_state,
        failure_requires_user_decision=classification.requires_user_decision,
    )


def _tool_result_views(result: Any, side_effects: Any) -> tuple[str, str]:
    """Keep durable raw evidence separate from the model-facing projection."""

    raw_evidence = (side_effects or {}).get("raw_tool_result")
    raw_result = raw_evidence if isinstance(raw_evidence, str) else str(result)
    return raw_result, str(result)


async def run_agent_turn(self, request: InvocationRequest, *, support: Any) -> InvocationResult:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    ExecutionIdentity = support.ExecutionIdentity
    InvocationResult = support.InvocationResult
    LLMError = support.LLMError
    LLMMessage = support.LLMMessage
    LoopGuard = support.LoopGuard
    ReminderScheduler = support.ReminderScheduler
    STREAM_RETRY_TOMBSTONE = support.STREAM_RETRY_TOMBSTONE
    SessionContext = support.SessionContext
    TerminalReason = support.TerminalReason
    ToolExpansionResult = support.ToolExpansionResult
    _KernelCancelledError = support._KernelCancelledError
    _MICROCOMPACT_CLEARED_MARKER = support._MICROCOMPACT_CLEARED_MARKER
    _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS = support._MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS
    _MICROCOMPACT_KEEP_RECENT = support._MICROCOMPACT_KEEP_RECENT
    _MIDLOOP_COMPACT_CHECK_INTERVAL = support._MIDLOOP_COMPACT_CHECK_INTERVAL
    _MIDLOOP_COMPACT_THRESHOLD = support._MIDLOOP_COMPACT_THRESHOLD
    _PARALLEL_SEMAPHORE_LIMIT = support._PARALLEL_SEMAPHORE_LIMIT
    _PTL_MAX_RETRIES = support._PTL_MAX_RETRIES
    _TOOL_RESULTS_AGGREGATE_BUDGET = support._TOOL_RESULTS_AGGREGATE_BUDGET
    _TOOL_RESULT_PREVIEW_LENGTH = support._TOOL_RESULT_PREVIEW_LENGTH
    _WORK_LEDGER_ENABLED_METADATA_KEY = support._WORK_LEDGER_ENABLED_METADATA_KEY
    _apply_mechanical_compaction_with_lifecycle_hooks = support._apply_mechanical_compaction_with_lifecycle_hooks
    _build_cancelled_result = support._build_cancelled_result
    _build_error_result = support._build_error_result
    _build_frozen_prompt_cache_key = support._build_frozen_prompt_cache_key
    _build_permissions_context = support._build_permissions_context
    _build_persisted_memory_messages = support._build_persisted_memory_messages
    _build_restoration_context = support._build_restoration_context
    _build_runtime_attachment_sections = support._build_runtime_attachment_sections
    _cached_prompt_prefix = support._cached_prompt_prefix
    _clone_api_messages = support._clone_api_messages
    _compress_messages_with_lifecycle_hooks = support._compress_messages_with_lifecycle_hooks
    _compute_microcompact_gap = support._compute_microcompact_gap
    _content_replacement_record = support._content_replacement_record
    _continue_after_output_cap = support._continue_after_output_cap
    _dicts_to_llm_messages = support._dicts_to_llm_messages
    _dynamic_suffix_notice = support._dynamic_suffix_notice
    _emit_runtime_hook = support._emit_runtime_hook
    _event_to_part = support._event_to_part
    _execute_recovered_pending_tool_frames = support._execute_recovered_pending_tool_frames
    _execute_tool_with_hooks = support._execute_tool_with_hooks
    _expand_concatenated_tool_calls = support._expand_concatenated_tool_calls
    _humanize_llm_error = support._humanize_llm_error
    _invalidate_prompt_prefix_cache = support._invalidate_prompt_prefix_cache
    _is_concurrency_safe_tool = support._is_concurrency_safe_tool
    is_read_only_tool = support.is_read_only_tool
    _is_prompt_too_long = support._is_prompt_too_long
    _is_terminal_tool_card_signal = support._is_terminal_tool_card_signal
    _latest_user_query = support._latest_user_query
    _llm_messages_to_dicts = support._llm_messages_to_dicts
    _load_and_hydrate_recovery_manifest = support._load_and_hydrate_recovery_manifest
    _maybe_await = support._maybe_await
    _maybe_evict_tool_result = support._maybe_evict_tool_result
    _merge_active_tool_groups = support._merge_active_tool_groups
    _mid_run_items_to_user_messages = support._mid_run_items_to_user_messages
    _persist_recovery_manifest_checkpoint = support._persist_recovery_manifest_checkpoint
    _prepare_ptl_round_group_fallback = support._prepare_ptl_round_group_fallback
    _record_runtime_span = support._record_runtime_span
    _registered_connector_source_items = support._registered_connector_source_items
    _resolve_eviction_threshold = support._resolve_eviction_threshold
    _restore_recovered_deferred_tool_schemas = support._restore_recovered_deferred_tool_schemas
    _sanitize_tool_calls_for_history = support._sanitize_tool_calls_for_history
    _schedule_runtime_hook = support._schedule_runtime_hook
    _should_buffer_stream_for_source_acl = support._should_buffer_stream_for_source_acl
    _should_expand_tools = support._should_expand_tools
    _split_system_prompt_for_api = support._split_system_prompt_for_api
    _store_prompt_prefix_cache = support._store_prompt_prefix_cache
    _stream_with_cancel = support._stream_with_cancel
    _tool_message_content = support._tool_message_content
    _tool_execution_evidence = support._tool_execution_evidence
    _tool_result_requests_user_clarification = support._tool_result_requests_user_clarification
    _tool_round_limit_message = support._tool_round_limit_message
    _turn_token_budget_message = support._turn_token_budget_message
    _usage_int = support._usage_int
    asyncio = support.asyncio
    build_context_policy = support.build_context_policy
    build_default_reminder_specs = support.build_default_reminder_specs
    build_done_event = support.build_done_event
    build_tool_call_event = support.build_tool_call_event
    clear_execution_identity = support.clear_execution_identity
    classify_llm_error = support.classify_llm_error
    get_execution_identity = support.get_execution_identity
    hashlib = support.hashlib
    json = support.json
    logger = support.logger
    monotonic_ms = support.monotonic_ms
    new_invocation_id = support.new_invocation_id
    prepare_session_context_for_request = support.prepare_session_context_for_request
    reset_invocation_id = support.reset_invocation_id
    set_execution_identity = support.set_execution_identity
    set_invocation_id = support.set_invocation_id
    should_surface_without_model_fallback = support.should_surface_without_model_fallback

    previous_identity = get_execution_identity()
    trace_token = None
    invocation_started_ms = monotonic_ms()
    invocation_id = ""
    invocation_span_id = f"invocation-{new_invocation_id()[:16]}"
    span_recorder = _RuntimeSpanRecorder(self._deps, request, invocation_span_id, _record_runtime_span)
    _record_span = span_recorder

    def _turn_route_span_metadata() -> dict[str, Any]:
        return _build_turn_route_span_metadata(request)

    if request.agent_id:
        metadata = request.session_context.metadata if request.session_context else {}
        invocation_id = str(metadata.get("trace_id") or new_invocation_id())
        trace_token = set_invocation_id(invocation_id)
        if request.session_context is not None:
            request.session_context.metadata["trace_id"] = invocation_id
    if request.execution_identity:
        set_execution_identity(
            ExecutionIdentity(
                identity_type=request.execution_identity.identity_type,
                identity_id=request.execution_identity.identity_id,
                label=request.execution_identity.label or request.execution_identity.identity_type,
            )
        )
    try:
        if request.model is None:
            return _build_error_result("[Error] No LLM model configured — unable to invoke agent.")

        runtime_config = await _maybe_await(self._deps.resolve_runtime_config(request.agent_id))
        span_recorder.runtime_config = runtime_config
        # P0-1b: invoker fallback paths set tenant_resolution_error instead
        # of silently returning tenant_id=None. Abort before any tool runs;
        # governance (P0-1a) is the second line of defence if a caller
        # bypasses the kernel entirely. Use getattr to stay compatible with
        # test doubles that mock RuntimeConfig as SimpleNamespace.
        if getattr(runtime_config, "tenant_resolution_error", None):
            logger.error(
                "[Kernel] Tenant resolution failed for agent %s: %s — aborting invocation",
                request.agent_id,
                runtime_config.tenant_resolution_error,
            )
            return _build_error_result(
                f"[Error] Cannot invoke agent — tenant resolution failed: "
                f"{runtime_config.tenant_resolution_error}. Please retry or contact admin.",
                terminal_reason=TerminalReason.TENANT_RESOLUTION_ERROR,
            )
        if runtime_config.quota_message:
            # Note: final_tools not included — not yet resolved at this point
            return _build_error_result(runtime_config.quota_message, terminal_reason=TerminalReason.QUOTA_DENIED)
        runtime_execution_mode = getattr(runtime_config, "execution_mode", None)
        if not request.invocation_scope and runtime_execution_mode:
            request.invocation_scope = runtime_execution_mode
        from app.runtime.recovery_manifest_store import (
            resolve_recovery_authority,
            unavailable_recovery_result,
        )

        recovery_resolution = resolve_recovery_authority(request, runtime_config)
        request.recovery_authority = recovery_resolution.frame
        if recovery_resolution.frame is not None and request.session_context is not None:
            try:
                request.recovery_manifest_result = _load_and_hydrate_recovery_manifest(
                    recovery_resolution.frame,
                    request.session_context,
                )
            except Exception as exc:
                logger.debug("[Kernel] early recovery manifest hydrate unavailable: %s", exc)
                request.recovery_manifest_result = unavailable_recovery_result(type(exc).__name__)
        else:
            request.recovery_manifest_result = unavailable_recovery_result(
                recovery_resolution.reason or "authority_unavailable"
            )

        try:
            resolved_memory_context = await _maybe_await(
                self._deps.resolve_memory_context(request, runtime_config.tenant_id)
            )
        except ContextDependencyUnavailable as exc:
            logger.error(
                "[Kernel] Required %s context unavailable: %s",
                exc.dependency,
                exc.code,
            )
            return _build_error_result(
                exc.user_message,
                terminal_reason=TerminalReason.MEMORY_UNAVAILABLE,
            )
        resolved_retrieval_context = ""
        if self._deps.resolve_retrieval_context:
            resolved_retrieval_context = await _maybe_await(
                self._deps.resolve_retrieval_context(request, runtime_config.tenant_id)
            )
        resolved_runtime_metadata_context = ""
        if self._deps.resolve_runtime_metadata_context:
            try:
                resolved_runtime_metadata_context = await _maybe_await(
                    self._deps.resolve_runtime_metadata_context(request, runtime_config.tenant_id)
                )
            except Exception as exc:  # noqa: BLE001 — runtime metadata is optional context
                logger.debug("[Kernel] Runtime metadata skipped for agent %s: %s", request.agent_id, exc)
        resolved_permissions_context = _build_permissions_context(request, runtime_config)
        current_user_name = await _maybe_await(self._deps.resolve_current_user_name(request.user_id))

        # Prompt cache: reuse frozen prefix from session if available
        from app.runtime.prompt_builder import (
            assemble_runtime_prompt,
            build_dynamic_prompt_suffix,
            build_frozen_context_dependency_manifest,
        )
        from app.runtime.turn_envelope import build_runtime_prompt_assembly_manifest

        session_ctx = request.session_context
        budget_profile = session_ctx.metadata.get("context_budget") if session_ctx else None
        latest_user_query = _latest_user_query(request.messages)
        available_deferred_tools: list[dict[str, Any]] = []
        if request.agent_id:
            try:
                from app.services.agent_tools import available_deferred_tool_candidates_for_agent
                from app.runtime.context import ensure_runtime_assembly_state

                available_deferred_tools = await available_deferred_tool_candidates_for_agent(request.agent_id)
                if session_ctx is not None:
                    ensure_runtime_assembly_state(session_ctx).record_deferred_tools(available_deferred_tools)
            except Exception as exc:
                logger.debug("[Kernel] available deferred tool list unavailable: %s", exc)
        # SA-09: rebuild the frozen prefix once per turn before considering
        # reuse. The rendered bytes are the only complete dependency
        # closure across workspace files and DB-backed company/channel/A2A
        # context. Provider prompt caching still benefits from an identical
        # prefix; this small in-process cache never serves unverified bytes.
        try:
            _fresh_prompt_prefix = await _maybe_await(
                self._deps.build_system_prompt(
                    request,
                    runtime_config.tenant_id,
                    resolved_memory_context,
                    current_user_name,
                )
            )
        except ContextDependencyUnavailable as exc:
            _invalidate_prompt_prefix_cache(session_ctx, reason=f"{exc.dependency}_context_unavailable")
            return _build_error_result(
                exc.user_message,
                terminal_reason=TerminalReason.MEMORY_UNAVAILABLE,
            )
        except Exception:
            _invalidate_prompt_prefix_cache(session_ctx, reason="frozen_context_rebuild_failed")
            raise
        _frozen_context_manifest = build_frozen_context_dependency_manifest(_fresh_prompt_prefix)
        if session_ctx is not None:
            session_ctx.metadata["frozen_context_dependency_manifest"] = _frozen_context_manifest
        _prompt_cache_key = _build_frozen_prompt_cache_key(
            request,
            runtime_config,
            current_user_name=current_user_name,
            rendered_prefix=_fresh_prompt_prefix,
        )
        _cached_prefix = _cached_prompt_prefix(session_ctx, _prompt_cache_key)
        _cache_valid = bool(_cached_prefix)

        # Resolve model context window for dynamic prompt budget
        _ctx_window = getattr(request.model, "max_input_tokens", None) if request.model else None

        # B-01 fix: detect coordinator mode early, include prompt in suffix BEFORE budget enforcement
        from app.runtime.coordinator import (
            is_coordinator_mode,
            is_strict_dispatcher_mode,
            get_coordinator_prompt,
            filter_tools_for_coordinator,
        )

        _is_coordinator = is_coordinator_mode(agent=runtime_config, request=request)
        _is_strict_dispatcher = is_strict_dispatcher_mode(agent=runtime_config, request=request)
        _system_prompt_suffix = request.system_prompt_suffix or ""
        _protected_system_prompt_suffixes = (
            [get_coordinator_prompt(dispatcher_only=_is_strict_dispatcher)] if _is_coordinator else []
        )

        _system_prompt_suffix_sections = _bind_system_prompt_suffix_sections(
            _protected_system_prompt_suffixes,
            request,
            session_ctx,
            _build_runtime_attachment_sections,
        )

        # P0.4 Observability: prompt cache hit/miss
        logger.info(
            "[Kernel] Prompt prefix cache %s (agent=%s)",
            "hit" if _cache_valid else "cold-build",
            request.agent_id,
            extra={"metric": "prompt_cache", "cache_hit": _cache_valid},
        )
        if session_ctx is not None:
            from app.runtime.decision_ledger import append_cache_decision_entry, build_cache_decision_entry

            append_cache_decision_entry(
                session_ctx,
                build_cache_decision_entry(
                    cache_surface="prompt_prefix",
                    cache_key=_prompt_cache_key,
                    decision="hit" if _cache_valid else "miss",
                    invalidation_reason=str(session_ctx.metadata.get("prompt_cache_invalidated_reason") or ""),
                    shared_with_parent=bool(getattr(request, "parent_session_id", None)),
                ),
            )

        dynamic_context_section_ledger: list[dict[str, Any]] = []
        if _cache_valid and _cached_prefix:
            # Session has a valid frozen prefix — only rebuild dynamic suffix
            frozen_prefix_for_manifest = _cached_prefix
            dynamic_suffix = build_dynamic_prompt_suffix(
                active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                available_deferred_tools=available_deferred_tools,
                memory_snapshot=resolved_memory_context,
                skill_catalog=request.skill_catalog,
                runtime_metadata_context=resolved_runtime_metadata_context,
                permissions_context=resolved_permissions_context,
                retrieval_context=resolved_retrieval_context,
                system_prompt_suffix=_system_prompt_suffix,
                system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                budget_profile=budget_profile,
                latest_user_query=latest_user_query,
                user_name=current_user_name or "",
                channel=session_ctx.channel if session_ctx else "",
                source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                agent_name=request.agent_name,
                context_section_ledger=dynamic_context_section_ledger,
            )
            combined_prompt = assemble_runtime_prompt(
                _cached_prefix,
                dynamic_suffix,
                context_window_tokens=_ctx_window,
                budget_profile=budget_profile,
            )
            system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(combined_prompt)
        else:
            # First call or changed rendered dependency manifest.
            prompt_prefix = _fresh_prompt_prefix
            frozen_prefix_for_manifest = prompt_prefix
            if session_ctx is not None:
                _store_prompt_prefix_cache(session_ctx, prompt_prefix, _prompt_cache_key)
                session_ctx._memory_hash = hashlib.sha256(resolved_memory_context.encode("utf-8")).hexdigest()[:16]
            dynamic_suffix = build_dynamic_prompt_suffix(
                active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                available_deferred_tools=available_deferred_tools,
                memory_snapshot=resolved_memory_context,
                skill_catalog=request.skill_catalog,
                runtime_metadata_context=resolved_runtime_metadata_context,
                permissions_context=resolved_permissions_context,
                retrieval_context=resolved_retrieval_context,
                system_prompt_suffix=_system_prompt_suffix,
                system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                budget_profile=budget_profile,
                latest_user_query=latest_user_query,
                user_name=current_user_name or "",
                channel=session_ctx.channel if session_ctx else "",
                source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                agent_name=request.agent_name,
                context_section_ledger=dynamic_context_section_ledger,
            )
            combined_prompt = assemble_runtime_prompt(
                prompt_prefix,
                dynamic_suffix,
                context_window_tokens=_ctx_window,
                budget_profile=budget_profile,
            )
            system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(combined_prompt)

        tools_for_llm = request.initial_tools
        if tools_for_llm is None:
            if request.agent_id:
                tools_for_llm = await _maybe_await(self._deps.get_tools(request.agent_id, request.core_tools_only))
            else:
                tools_for_llm = []

        # B-01/B-04 fix: Coordinator mode — filter tools (prompt already in budget via suffix)
        if _is_coordinator:
            tools_for_llm = filter_tools_for_coordinator(
                tools_for_llm,
                dispatcher_only=_is_strict_dispatcher,
            )
            logger.info(
                "[Kernel] Coordinator mode active for agent %s (dispatcher_only=%s)",
                request.agent_id,
                _is_strict_dispatcher,
            )

        tools_for_llm = await _restore_recovered_deferred_tool_schemas(
            request=request,
            tools_for_llm=tools_for_llm,
            resolve_tool_expansion=self._deps.resolve_tool_expansion,
        )

        if session_ctx is not None:
            from app.runtime.context import ensure_runtime_assembly_state
            from app.runtime.context_engine import record_prompt_manifest_context_artifacts

            dynamic_notice = _dynamic_suffix_notice(dynamic_prompt_suffix)
            hook_added_context = []
            if "## Hook Additional Context" in _system_prompt_suffix:
                hook_added_context.append("user_prompt_submit")
            prompt_manifest = build_runtime_prompt_assembly_manifest(
                turn_id=str(session_ctx.metadata.get("turn_id") or ""),
                session_id=str(session_ctx.session_id or request.memory_session_id or ""),
                frozen_prefix=frozen_prefix_for_manifest,
                dynamic_suffix=dynamic_prompt_suffix,
                provider_system_prompt=system_prompt,
                provider_dynamic_notice=dynamic_notice.content if dynamic_notice else "",
                context_budget=budget_profile,
                model_window=_ctx_window,
                tools_for_llm=tools_for_llm,
                active_tool_groups=session_ctx.active_tool_groups,
                available_deferred_tools=available_deferred_tools,
                memory_snapshot=resolved_memory_context,
                runtime_metadata_context=resolved_runtime_metadata_context,
                permissions_context=resolved_permissions_context,
                retrieval_context=resolved_retrieval_context,
                skill_catalog=request.skill_catalog,
                active_skill_names=session_ctx.active_skills,
                skill_ranking=list(ensure_runtime_assembly_state(session_ctx).skill_catalog_ranking),
                system_prompt_suffix=_system_prompt_suffix,
                system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                mcp_server_refs=list(session_ctx.metadata.get("mcp_server_refs") or []),
                hook_added_context=hook_added_context,
                available_agent_types=list(session_ctx.metadata.get("available_agent_types") or []),
                messages=request.messages,
                frozen_context_dependency_manifest=_frozen_context_manifest,
            )
            prompt_manifest["dynamic_context_section_ledger"] = {
                "schema": "hive.ccplus.dynamic_context_section_ledger.v1",
                "sections": list(dynamic_context_section_ledger),
            }
            record_prompt_manifest_context_artifacts(session_ctx, prompt_manifest)
            ensure_runtime_assembly_state(session_ctx).record_prompt_manifest(prompt_manifest)
            session_ctx.metadata["prompt_sections"] = list(prompt_manifest.get("prompt_sections") or [])
            session_ctx.metadata["active_tool_names"] = list(prompt_manifest.get("active_tool_names") or [])
            session_ctx.metadata["deferred_tool_names"] = list(prompt_manifest.get("available_deferred_tools") or [])
            existing_context_policy = (
                dict(session_ctx.metadata.get("context_policy") or {})
                if isinstance(session_ctx.metadata.get("context_policy"), dict)
                else {}
            )
            # The prompt manifest exposes the assembled prompt budget as a
            # read model; it must not erase runtime context-policy knobs such
            # as tool result budgets that are consumed before the next model
            # request.
            session_ctx.metadata["context_policy"] = {
                **existing_context_policy,
                **dict(prompt_manifest.get("context_budget") or {}),
            }

        async def _record_compaction_fact(fact: dict[str, Any]) -> None:
            await _record_span(
                span_type="compaction",
                name=str(fact.get("fact_type") or "compaction_fact"),
                started_at_ms=monotonic_ms(),
                invocation_id=invocation_id,
                metadata=fact,
            )

        from app.runtime.compaction_trace import CompactionTraceContext

        compaction_trace_context = CompactionTraceContext.enabled(
            thread_id=request.memory_session_id
            or (str(getattr(request.session_context, "session_id", "")) if request.session_context else ""),
            turn_id=invocation_id or new_invocation_id(),
            model=str(getattr(request.model, "model", "") or ""),
            provider_name=str(getattr(request.model, "provider", "") or ""),
            fact_recorder=_record_compaction_fact,
        )

        token_usage_ledger = _TurnTokenUsageLedger(request.initial_turn_tokens_used)
        initial_turn_tokens_used = token_usage_ledger.initial
        accumulated_tokens = initial_turn_tokens_used

        async def _record_new_token_usage() -> None:
            await token_usage_ledger.record(
                self._deps.record_token_usage,
                _maybe_await,
                request.agent_id,
                accumulated_tokens,
            )

        collected_parts: list[dict[str, Any]] = []
        streamed_chunks: list[str] = []
        delivered_chunk_count = 0
        streamed_thinking: list[str] = []
        _callback_failure_count: int = 0
        loop_guard = LoopGuard()
        # Runtime reminders (T-G1): one scheduler per invocation; texts are
        # transient (per-round stream clone only — never api_messages).
        reminder_scheduler = ReminderScheduler(build_default_reminder_specs())
        _round_tool_names: list[str] = []

        _work_ledger_view_provider = _bind_work_ledger_view_provider(
            request,
            enabled_key=_WORK_LEDGER_ENABLED_METADATA_KEY,
            logger=logger,
        )

        def _work_ledger_snapshot_provider() -> str:
            return _render_work_ledger_snapshot(_work_ledger_view_provider, logger=logger)

        def _work_ledger_progress_review_provider() -> dict[str, Any] | None:
            return _read_work_ledger_progress(_work_ledger_view_provider)

        async def _emit_event(event: dict[str, Any]) -> None:
            if request.on_event:
                try:
                    await _maybe_await(request.on_event(event))
                except Exception as _cb_exc:
                    logger.warning("[Kernel] on_event callback failed: %s", _cb_exc)
            part = _event_to_part(event)
            if part:
                collected_parts.append(part)

        async def _enforce_generated_source_permissions(final_content: str) -> tuple[str, bool]:
            source_items = _registered_connector_source_items(request)
            if not source_items:
                return final_content, True
            from app.services.connector_acl import (
                redact_forbidden_generated_source_fragments,
                record_generated_source_permission_check,
            )

            redacted_content, check = redact_forbidden_generated_source_fragments(
                final_content,
                source_items=source_items,
                tenant_id=runtime_config.tenant_id,
                current_user_id=request.user_id,
                agent_id=request.agent_id,
            )
            record_generated_source_permission_check(request.session_context, check)
            status = "allowed" if check.allowed else "blocked"
            await _record_span(
                span_type="permission",
                name="generated_source_permission_check",
                started_at_ms=monotonic_ms(),
                metadata={
                    "status": status,
                    "source_count": len(source_items),
                    "allowed_source_count": len(check.allowed_sources),
                    "forbidden_source_count": len(check.forbidden_sources),
                    "forbidden_sources": list(check.forbidden_sources),
                    "authorization_decision_entry": check.authorization_decision_entry,
                },
            )
            if check.allowed:
                return final_content, True
            await _emit_event(
                {
                    "type": "generated_source_permission_block",
                    "part": {
                        "type": "event",
                        "event_type": "generated_source_permission_block",
                        "title": "Source Permission Check",
                        "text": "Precisely redacted inaccessible connector fragments from the generated response.",
                        "status": "warning",
                        "forbidden_source_count": len(check.forbidden_sources),
                    },
                }
            )
            return redacted_content, False

        async def _flush_buffered_chunks() -> None:
            nonlocal delivered_chunk_count
            if request.on_chunk is None or delivered_chunk_count >= len(streamed_chunks):
                delivered_chunk_count = len(streamed_chunks)
                return
            for chunk in streamed_chunks[delivered_chunk_count:]:
                try:
                    await _maybe_await(request.on_chunk(chunk))
                except Exception as _cb_exc:
                    logger.warning("[Kernel] on_chunk buffered flush failed: %s", _cb_exc)
                    break
                delivered_chunk_count += 1

        async def _pause_for_user_clarification() -> InvocationResult:
            await _record_new_token_usage()
            await self._persist_before_exit(request, runtime_config, "", api_messages)
            return InvocationResult(
                content="",
                tokens_used=accumulated_tokens,
                final_tools=tools_for_llm,
                parts=collected_parts,
                terminal_reason=TerminalReason.CLARIFICATION_REQUIRED,
            )

        async def _end_turn_for_tool_terminal_signal(reason: str, content: str) -> InvocationResult:
            # D-08: a tool emitted ToolContentEnvelope.terminal_signal — end the
            # turn cleanly after the current round's tool results have been
            # appended, with a TURN_STOP terminal reason carrying the signal.
            await _record_new_token_usage()
            await self._persist_before_exit(request, runtime_config, content, api_messages)
            await _emit_event(
                {
                    "type": "tool_terminal_signal",
                    "part": {
                        "type": "event",
                        "event_type": "tool_terminal_signal",
                        "title": "Tool ended the turn",
                        "text": reason,
                        "status": "info",
                    },
                }
            )
            return InvocationResult(
                content=content,
                tokens_used=accumulated_tokens,
                final_tools=tools_for_llm,
                parts=collected_parts + build_done_event(content)["parts"],
                terminal_reason=TerminalReason.TURN_STOP,
            )

        async def _inject_loop_guard_warning(decision: LoopGuardDecision) -> None:
            # A4 warn-before-abort: give the model the diagnostic + one
            # self-correction chance (CC §12.2 soft-constraints-first).
            # T-G1: queued on the scheduler — injected transiently into the
            # next round's request, never persisted into api_messages.
            reminder_scheduler.enqueue(decision.message, source="loop_guard", ttl="next_collect", priority=95)
            await _emit_event(
                {
                    "type": "loop_guard",
                    "part": {
                        "type": "event",
                        "event_type": "loop_guard_warning",
                        "title": "Loop Guard Warning",
                        "text": decision.message,
                        "status": "warning",
                        **decision.trace_event,
                    },
                }
            )

        async def _emit_compaction_event(data: dict[str, Any]) -> None:
            await _emit_event({"type": "session_compact", **data})
            # System-level WAL: save compaction summary without touching user-authored workspace notes.
            if request.agent_id and data.get("summary"):
                try:
                    from app.config import get_settings as _gs
                    from pathlib import Path as _P

                    _header = "# Session Compaction Summary (auto-saved)\n\n"
                    _content = _header + data["summary"] + "\n"
                    # Write to both workspace roots so heartbeat can find it
                    for _root in [
                        _P(_gs().AGENT_DATA_DIR) / str(request.agent_id),
                        _P("/tmp/hive_workspaces") / str(request.agent_id),
                    ]:
                        if _root.exists():
                            _cfile = _root / "runtime_artifacts" / "compaction_summary.md"
                            _cfile.parent.mkdir(parents=True, exist_ok=True)
                            _cfile.write_text(_content, encoding="utf-8")
                            _legacy_cfile = _root / "workspace" / "compaction_summary.md"
                            _legacy_cfile.unlink(missing_ok=True)
                except Exception as _exc:
                    logger.warning("[Kernel] Auto-save compaction summary failed: %s", _exc)

            # P1-W3-9 — RecoveryManifest persistence.
            # Structured state is committed through the authority-bound
            # store. The next invocation consumes only its verified load
            # result; omitted bytes are exposed through an immutable governed
            # context-resource ref, never a raw workspace path.
            if request.agent_id and getattr(request, "session_context", None) is not None:
                try:
                    _persist_recovery_manifest_checkpoint(request, delete_if_empty=True)
                except Exception as _rec_exc:
                    logger.warning(
                        "[Kernel] Recovery manifest persistence failed (non-fatal): %s",
                        _rec_exc,
                    )

        async def _emit_context_decision_event(data: dict[str, Any]) -> None:
            event_type = str(data.get("event_type") or "context_window_status")
            runtime_decision_entry = data.get("runtime_decision_entry")
            if request.session_context is not None and isinstance(runtime_decision_entry, dict):
                from app.runtime.decision_ledger import append_runtime_decision_entry

                append_runtime_decision_entry(request.session_context, runtime_decision_entry)
            if (
                event_type == "compaction_lifecycle"
                and request.session_context is not None
                and isinstance(getattr(request.session_context, "metadata", None), dict)
                and isinstance(data.get("compaction_lifecycle"), dict)
            ):
                request.session_context.metadata.setdefault("compaction_lifecycle_records", []).append(
                    dict(data["compaction_lifecycle"])
                )
            title_by_type = {
                "context_window_status": "Context Window",
                "tool_result_budget_pass": "Tool Result Budget",
                "compaction_skipped": "Compaction Skipped",
                "compaction_started": "Compaction Started",
                "compaction_completed": "Compaction Completed",
                "compaction_lifecycle": "Compaction Lifecycle",
            }
            await _emit_event(
                {
                    "type": "session_context",
                    "visibility": "debug",
                    **data,
                    "part": {
                        "type": "event",
                        "event_type": event_type,
                        "title": title_by_type.get(event_type, "Session Context"),
                        "status": "info",
                        "visibility": "debug",
                        **data,
                    },
                }
            )

        def _context_policy_for_active_model() -> ContextPolicyV1:
            raw_policy = {}
            metadata = getattr(request.session_context, "metadata", None) if request.session_context else None
            if isinstance(metadata, dict) and isinstance(metadata.get("context_policy"), dict):
                raw_policy = metadata["context_policy"]
            model_window = int(
                getattr(active_model, "max_input_tokens", None)
                or raw_policy.get("model_window")
                or raw_policy.get("context_window_tokens")
                or 0
            )
            return build_context_policy(model_window, overrides=raw_policy)

        async def _prepare_api_messages_for_request() -> None:
            nonlocal api_messages
            if len(api_messages) <= 1:
                return

            system_message = api_messages[0]
            conversation_messages = api_messages[1:]
            policy = _context_policy_for_active_model()

            def _estimate_context_tokens(messages_for_estimate: list[LLMMessage]) -> int:
                chars = len(system_message.content or "") + sum(len(msg.content or "") for msg in messages_for_estimate)
                return self._deps.estimate_tokens_from_chars(chars)

            async def _compress_for_preflight(
                messages_as_dicts: list[dict[str, Any]], **kwargs: Any
            ) -> list[dict[str, Any]]:
                return await _compress_messages_with_lifecycle_hooks(
                    self._deps.maybe_compress_messages,
                    messages_as_dicts,
                    trace_context=compaction_trace_context,
                    tools=tools_for_llm,
                    instructions="request_preflight_context_compaction",
                    agent_id=request.agent_id,
                    session_id=request.memory_session_id,
                    trigger="request_preflight",
                    metadata={
                        "phase": "request_preflight_context_compaction",
                        "agent_name": request.agent_name,
                    },
                    **kwargs,
                )

            prepared = await prepare_session_context_for_request(
                messages=conversation_messages,
                policy=policy,
                estimate_tokens=_estimate_context_tokens,
                compress_messages=_compress_for_preflight,
                cumulative_run_tokens=context_usage_anchor_tokens,
                session_id=getattr(request.session_context, "session_id", None) if request.session_context else None,
                turn_id=str(getattr(request.session_context, "metadata", {}).get("turn_id") or "")
                if request.session_context
                else None,
                runtime_task_id=str(getattr(request.session_context, "metadata", {}).get("runtime_task_id") or "")
                if request.session_context
                else None,
                on_decision=_emit_context_decision_event,
                compress_kwargs={
                    "model_provider": active_model.provider,
                    "model_name": active_model.model,
                    "max_input_tokens_override": getattr(active_model, "max_input_tokens", None),
                    "tenant_id": runtime_config.tenant_id,
                    "usage_anchor_tokens": context_usage_anchor_tokens,
                    "agent_id": request.agent_id,
                    "user_id": request.user_id,
                    "on_compaction": _emit_compaction_event,
                },
                tool_result_exempt_names={"read_file", "list_files"},
            )
            if prepared.changed:
                api_messages = [system_message] + prepared.messages

        async def _emit_chunk(text: str) -> None:
            nonlocal _callback_failure_count, delivered_chunk_count
            if text == STREAM_RETRY_TOMBSTONE:
                streamed_chunks.clear()
                delivered_chunk_count = 0
                if request.on_event:
                    try:
                        await _maybe_await(request.on_event({"type": "stream_retry_tombstone"}))
                    except Exception as _cb_exc:
                        _callback_failure_count += 1
                        logger.warning(
                            "[Kernel] on_event callback failed for stream retry tombstone (%d): %s",
                            _callback_failure_count,
                            _cb_exc,
                        )
                return
            streamed_chunks.append(text)
            if _should_buffer_stream_for_source_acl(request):
                return
            if request.on_chunk:
                try:
                    await _maybe_await(request.on_chunk(text))
                    delivered_chunk_count = len(streamed_chunks)
                except Exception as _cb_exc:
                    _callback_failure_count += 1
                    logger.warning("[Kernel] on_chunk callback failed (%d): %s", _callback_failure_count, _cb_exc)
                    if _callback_failure_count == 3:
                        logger.error(
                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                            _callback_failure_count,
                        )

        async def _emit_thinking(text: str) -> None:
            nonlocal _callback_failure_count
            streamed_thinking.append(text)
            if request.on_thinking:
                try:
                    await _maybe_await(request.on_thinking(text))
                except Exception as _cb_exc:
                    _callback_failure_count += 1
                    logger.warning("[Kernel] on_thinking callback failed (%d): %s", _callback_failure_count, _cb_exc)
                    if _callback_failure_count == 3:
                        logger.error(
                            "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                            _callback_failure_count,
                        )

        context_usage_anchor_tokens = 0
        if request.session_context is not None:
            try:
                context_usage_anchor_tokens = int(request.session_context.metadata.get("usage_anchor_tokens") or 0)
            except (TypeError, ValueError):
                context_usage_anchor_tokens = 0

        messages = await _compress_messages_with_lifecycle_hooks(
            self._deps.maybe_compress_messages,
            request.messages,
            trace_context=compaction_trace_context,
            tools=tools_for_llm,
            instructions="initial_context_compaction",
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            trigger="initial",
            metadata={
                "phase": "initial_context_compaction",
                "agent_name": request.agent_name,
            },
            model_provider=request.model.provider,
            model_name=request.model.model,
            max_input_tokens_override=getattr(request.model, "max_input_tokens", None),
            tenant_id=runtime_config.tenant_id,
            on_compaction=_emit_compaction_event,
            usage_anchor_tokens=context_usage_anchor_tokens,
            user_id=request.user_id,
        )

        api_messages = [LLMMessage(role="system", content=system_prompt)]
        for msg in messages:
            api_messages.append(
                LLMMessage(
                    role=msg.get("role", "user"),
                    content=msg.get("content"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    reasoning_content=msg.get("reasoning_content"),
                    reasoning_signature=msg.get("reasoning_signature"),
                )
            )
        recovered_tool_results = await _execute_recovered_pending_tool_frames(
            execute_tool=self._deps.execute_tool,
            request=request,
            runtime_config=runtime_config,
            emit_event=_emit_event,
            tools_for_llm=tools_for_llm,
            api_messages=api_messages,
            record_span=_record_span,
        )
        if recovered_tool_results:
            api_messages.append(
                LLMMessage(
                    role="system",
                    content="## Recovered Tool Results\n" + recovered_tool_results,
                )
            )

        active_model = request.model
        fallback_model = request.fallback_model
        active_supports_vision = request.supports_vision
        # D-08: a tool may signal that the turn should end after the current
        # round via ToolContentEnvelope.terminal_signal. The reason string is
        # captured when a tool result carries it and consumed right after the
        # round's tool results are appended.
        _tool_terminal_signal: str | None = None
        _loop_guard_summary_pending = False
        _loop_guard_terminal_decision: LoopGuardDecision | None = None
        try:
            client = self._deps.create_client(active_model)
        except Exception as exc:
            return _build_error_result(
                f"[Error] Failed to create LLM client: {exc}",
                tokens_used=accumulated_tokens,
            )

        max_rounds = request.max_tool_rounds or runtime_config.max_tool_rounds
        max_tokens = self._deps.get_max_tokens(
            active_model.provider,
            active_model.model,
            request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
        )
        turn_token_budget = getattr(runtime_config, "turn_token_budget", None)
        last_model_result_receipt: dict[str, Any] | None = None
        # full_toolset tracks expanded tools after deferred-schema discovery.
        # Intentionally persists across rounds — discovered tool schemas stay active once loaded.
        full_toolset = None
        try:
            # A proven no-progress outcome reserves one final, tool-free model
            # round so the model—not the harness—authors the explanation.
            initial_round_index = max(0, int(request.initial_round_index or 0))
            remaining_rounds = max(0, max_rounds - initial_round_index)
            for round_i in range(remaining_rounds + 1):
                logical_round_index = initial_round_index + round_i + 1
                if round_i >= remaining_rounds and not _loop_guard_summary_pending:
                    break
                if request.cancel_event and request.cancel_event.is_set():
                    await _record_new_token_usage()
                    await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                    return _build_cancelled_result(
                        streamed_chunks,
                        streamed_thinking,
                        tokens_used=accumulated_tokens,
                        final_tools=tools_for_llm,
                        collected_parts=collected_parts,
                    )
                if request.round_input_bind is not None:
                    bound_round_inputs = _mid_run_items_to_user_messages(
                        await _maybe_await(request.round_input_bind(logical_round_index))
                    )
                    if bound_round_inputs:
                        api_messages.extend(bound_round_inputs)
                        await _emit_event(
                            {
                                "type": "session_round_inputs_bound",
                                "part": {
                                    "type": "event",
                                    "event_type": "session_round_inputs_bound",
                                    "title": "Durable session inputs bound",
                                    "round": logical_round_index,
                                    "count": len(bound_round_inputs),
                                },
                            }
                        )
                if request.mid_run_message_drain is not None:
                    try:
                        drained = _mid_run_items_to_user_messages(await _maybe_await(request.mid_run_message_drain()))
                    except Exception as drain_exc:
                        drained = []
                        logger.warning("[Kernel] mid-run message drain failed: %s", drain_exc)
                    if drained:
                        api_messages.extend(drained)
                        await _emit_event(
                            {
                                "type": "mid_run_user_messages_drained",
                                "part": {
                                    "type": "event",
                                    "event_type": "mid_run_user_messages_drained",
                                    "title": "User message queued during run",
                                    "round": round_i,
                                    "count": len(drained),
                                },
                            }
                        )
                # Runtime reminders (T-G1): the scheduler decides what this
                # round gets (plan FULL/SPARSE, work-ledger nudge, round
                # pressure, queued loop-guard warnings) under behavioral
                # throttling. The texts are TRANSIENT — appended to the
                # per-round stream clone below, never to api_messages — so
                # they cannot stack across rounds (M1) and never reach
                # memory persistence (M2). Feed the previous round's
                # tool-call names first so idle/cooldown clocks advance.
                if round_i > 0:
                    reminder_scheduler.observe(_round_tool_names)
                _round_tool_names = []
                _ctx_chars = sum(len(m.content or "") for m in api_messages)
                _transient_reminder_injections = reminder_scheduler.collect_with_metadata(
                    request.session_context,
                    {
                        "round_i": round_i,
                        "max_rounds": max_rounds,
                        "total_tool_calls": loop_guard.total_tool_calls,
                        "failed_tool_calls": loop_guard.failed_tool_calls,
                        "context_tokens": self._deps.estimate_tokens_from_chars(_ctx_chars),
                        "work_ledger_snapshot_provider": _work_ledger_snapshot_provider,
                        "work_ledger_progress_review_provider": _work_ledger_progress_review_provider,
                    },
                )
                _transient_reminders = [item.text for item in _transient_reminder_injections]
                _runtime_reminder_candidates: list[dict[str, Any]] = []
                if _transient_reminder_injections:
                    from app.runtime.runtime_reminder_candidate import append_runtime_reminder_candidate

                    for _reminder in _transient_reminder_injections:
                        _runtime_reminder_candidates.append(
                            append_runtime_reminder_candidate(
                                request.session_context,
                                source=_reminder.source,
                                text=_reminder.text,
                                ttl=_reminder.ttl,
                                priority=_reminder.priority,
                                consumed_at=f"round:{round_i}",
                            )
                        )
                if _transient_reminders:
                    # M6 observability: reminders no longer appear in the
                    # persisted transcript, so emit an event per injection.
                    await _emit_event(
                        {
                            "type": "reminder_injected",
                            "part": {
                                "type": "event",
                                "event_type": "reminder_injected",
                                "title": "Runtime Reminder",
                                "round": round_i,
                                "count": len(_transient_reminders),
                                "chars": sum(len(t) for t in _transient_reminders),
                                "context_candidates": _runtime_reminder_candidates,
                            },
                        }
                    )

                # Apply capability-driven cache hints.
                ptl_retries = 0
                while True:
                    await _prepare_api_messages_for_request()
                    stream_messages = _clone_api_messages(api_messages)
                    if _transient_reminders:
                        stream_messages = stream_messages + [
                            LLMMessage(role="system", content=text) for text in _transient_reminders
                        ]
                    dynamic_notice = _dynamic_suffix_notice(dynamic_prompt_suffix)
                    if dynamic_notice is not None:
                        stream_messages = stream_messages + [dynamic_notice]
                    if self._deps.apply_vision_transform:
                        stream_messages = self._deps.apply_vision_transform(
                            stream_messages,
                            active_supports_vision,
                        )
                    if self._deps.apply_cache_hints:
                        stream_messages = self._deps.apply_cache_hints(
                            stream_messages,
                            getattr(active_model, "provider", ""),
                            request.invocation_scope or "conversation",
                        )

                    provider_request_id: str | None = None
                    llm_started_ms = monotonic_ms()
                    provider_attempt_state: dict[str, Any] = {
                        "provider_request_id": provider_request_id,
                        "llm_started_ms": llm_started_ms,
                    }
                    try:
                        provider_round = await _execute_committed_provider_round(
                            self=self,
                            support=support,
                            request=request,
                            client=client,
                            active_model=active_model,
                            stream_messages=stream_messages,
                            tools_for_llm=tools_for_llm,
                            max_tokens=max_tokens,
                            logical_round_index=logical_round_index,
                            emit_chunk=_emit_chunk,
                            emit_thinking=_emit_thinking,
                            record_span=_record_span,
                            turn_route_span_metadata=_turn_route_span_metadata,
                            emit_event=_emit_event,
                            attempt_state=provider_attempt_state,
                            previous_result_receipt=last_model_result_receipt,
                        )
                        response = provider_round["response"]
                        provider_request_id = provider_round["provider_request_id"]
                        llm_started_ms = int(provider_round["llm_started_ms"])
                        if provider_round["empty"]:
                            return await _finalize_empty_provider_response(
                                self=self,
                                support=support,
                                request=request,
                                response=response,
                                active_model=active_model,
                                logical_round_index=logical_round_index,
                                provider_request_id=provider_request_id,
                                llm_started_ms=llm_started_ms,
                                accumulated_tokens=accumulated_tokens,
                                initial_turn_tokens_used=initial_turn_tokens_used,
                                tools_for_llm=tools_for_llm,
                                record_span=_record_span,
                                turn_route_span_metadata=_turn_route_span_metadata,
                            )
                        last_model_result_receipt = provider_round["result_receipt"]
                        provider_prompt_ledger = provider_round["provider_prompt_ledger"]
                        cache_metrics = provider_round["cache_metrics"]
                        cost_loop_decision = loop_guard.observe_provider_call_cost(
                            projected_input_tokens=int(provider_prompt_ledger.get("projected_input_tokens") or 0),
                            output_tokens=int(provider_round["output_tokens"] or 0),
                            cache_read_tokens=int(cache_metrics.cache_read_tokens or 0),
                            tool_schema_tokens=int(provider_prompt_ledger.get("tool_schema_tokens") or 0),
                        )
                        if cost_loop_decision:
                            await _inject_loop_guard_warning(cost_loop_decision)
                        break
                    except _KernelCancelledError:
                        provider_request_id = provider_attempt_state.get("provider_request_id")
                        llm_started_ms = int(provider_attempt_state["llm_started_ms"])
                        if request.model_request_fail is not None and provider_request_id is not None:
                            await _maybe_await(
                                request.model_request_fail(
                                    round_index=logical_round_index,
                                    provider_request_id=provider_request_id,
                                    error_class="cancelled",
                                    retry_safe=False,
                                )
                            )
                        await _record_span(
                            span_type="generation",
                            name="llm.stream",
                            started_at_ms=llm_started_ms,
                            metadata={
                                **_turn_route_span_metadata(),
                                "provider": getattr(active_model, "provider", ""),
                                "model": getattr(active_model, "model", ""),
                                "round": logical_round_index,
                                "status": "cancelled",
                            },
                        )
                        await _record_new_token_usage()
                        await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                        return _build_cancelled_result(
                            streamed_chunks,
                            streamed_thinking,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            collected_parts=collected_parts,
                        )
                    except LLMError as exc:
                        provider_request_id = provider_attempt_state.get("provider_request_id")
                        llm_started_ms = int(provider_attempt_state["llm_started_ms"])
                        error_classification = classify_llm_error(exc)
                        # Error classification is status-first (typed
                        # http_status, e.g. 402 owns the quota outcome) and
                        # only then text-based; it drives surface/fallback
                        # policy and fail evidence.  A replay hard outcome
                        # still requires the transport's authoritative typed
                        # delivery state, never natural-language text.
                        delivery_state = str(getattr(exc, "delivery_state", "unknown") or "unknown")
                        retry_safe = delivery_state == "rejected"
                        if request.model_request_fail is not None and provider_request_id is not None:
                            await _maybe_await(
                                request.model_request_fail(
                                    round_index=logical_round_index,
                                    provider_request_id=provider_request_id,
                                    error_class=error_classification.kind,
                                    delivery_state=delivery_state,
                                    retry_safe=retry_safe,
                                )
                            )
                        if provider_request_id is not None and not retry_safe:
                            raise ProviderRequestNeedsReconciliation(
                                provider_request_id=provider_request_id,
                                error_class=error_classification.kind,
                            ) from exc
                        await _record_span(
                            span_type="generation",
                            name="llm.stream",
                            started_at_ms=llm_started_ms,
                            metadata={
                                **_turn_route_span_metadata(),
                                "provider": getattr(active_model, "provider", ""),
                                "model": getattr(active_model, "model", ""),
                                "round": logical_round_index,
                                "status": "error",
                                "error_class": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        logger.error(
                            "[Kernel] LLMError provider=%s model=%s round=%s: %s",
                            getattr(active_model, "provider", "?"),
                            getattr(active_model, "model", "?"),
                            logical_round_index,
                            exc,
                        )
                        # ── PTL reactive retry: full compress first → mechanical round-group fallback ──
                        if _is_prompt_too_long(exc) and ptl_retries < _PTL_MAX_RETRIES:
                            if ptl_retries == 0 and len(api_messages) > 4:
                                ptl_retries += 1
                                _before_msgs = len(api_messages)
                                logger.warning(
                                    "[Kernel] PTL full compress first (attempt %d/%d)",
                                    ptl_retries,
                                    _PTL_MAX_RETRIES,
                                )
                                conv_dicts = _llm_messages_to_dicts(api_messages[1:])
                                _before_chars = sum(len(d.get("content", "") or "") for d in conv_dicts)
                                compressed = await _compress_messages_with_lifecycle_hooks(
                                    self._deps.maybe_compress_messages,
                                    conv_dicts,
                                    trace_context=compaction_trace_context,
                                    tools=tools_for_llm,
                                    instructions="prompt_too_long_full_compress_first",
                                    agent_id=request.agent_id,
                                    session_id=request.memory_session_id,
                                    trigger="prompt_too_long",
                                    metadata={
                                        "phase": "prompt_too_long_full_compress_first",
                                        "attempt": ptl_retries,
                                        "strategy": "full_compress_first",
                                        "agent_name": request.agent_name,
                                    },
                                    model_provider=active_model.provider,
                                    model_name=active_model.model,
                                    max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                                    tenant_id=runtime_config.tenant_id,
                                    compress_threshold=0.5,
                                    on_compaction=_emit_compaction_event,
                                    usage_anchor_tokens=context_usage_anchor_tokens,
                                    user_id=request.user_id,
                                )
                                _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
                                if _after_chars < _before_chars * 0.8:
                                    _ptl_dynamic = build_dynamic_prompt_suffix(
                                        active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                                        available_deferred_tools=available_deferred_tools,
                                        memory_snapshot=resolved_memory_context,
                                        skill_catalog=request.skill_catalog,
                                        runtime_metadata_context=resolved_runtime_metadata_context,
                                        permissions_context=resolved_permissions_context,
                                        retrieval_context=resolved_retrieval_context,
                                        system_prompt_suffix=_system_prompt_suffix,
                                        system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                        budget_profile=budget_profile,
                                        latest_user_query=latest_user_query,
                                        user_name=current_user_name or "",
                                        channel=session_ctx.channel if session_ctx else "",
                                        source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                                        agent_name=request.agent_name,
                                    )
                                    _ptl_prefix = (session_ctx.prompt_prefix if session_ctx else None) or prompt_prefix
                                    _ptl_combined = assemble_runtime_prompt(
                                        _ptl_prefix,
                                        _ptl_dynamic,
                                        context_window_tokens=_ctx_window,
                                        budget_profile=budget_profile,
                                    )
                                    _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(_ptl_combined)
                                    api_messages = [LLMMessage(role="system", content=_ptl_system)] + (
                                        _dicts_to_llm_messages(compressed)
                                    )
                                    await _emit_event(
                                        {
                                            "type": "session_compact",
                                            "summary": "Prompt too long; compressed conversation before retry.",
                                            "original_message_count": _before_msgs,
                                            "kept_message_count": len(api_messages),
                                            "reason": "prompt_too_long_retry",
                                            "strategy": "full_compress",
                                            "attempt": ptl_retries,
                                        }
                                    )
                                    logger.info(
                                        "[Kernel] PTL full compress first: %d→%d chars, %d→%d msgs (attempt %d/%d)",
                                        _before_chars,
                                        _after_chars,
                                        _before_msgs,
                                        len(api_messages),
                                        ptl_retries,
                                        _PTL_MAX_RETRIES,
                                        extra={
                                            "metric": "ptl_retry",
                                            "attempt": ptl_retries,
                                            "strategy": "full_compress",
                                        },
                                    )
                                    continue
                                logger.warning(
                                    "[Kernel] PTL full compression insufficient: %d→%d chars (%.0f%%), falling back to round-group drop",
                                    _before_chars,
                                    _after_chars,
                                    (_after_chars / max(_before_chars, 1)) * 100,
                                )
                            if len(api_messages) <= 4:
                                logger.warning(
                                    "[Kernel] PTL detected but only %d messages — skipping compression",
                                    len(api_messages),
                                )
                            else:
                                ptl_retries += 1
                                _before_msgs = len(api_messages)

                                if ptl_retries <= 2:
                                    # Attempt 1-2: drop 20% oldest round-groups.
                                    logger.warning(
                                        "[Kernel] PTL round-group drop (attempt %d/%d)",
                                        ptl_retries,
                                        _PTL_MAX_RETRIES,
                                    )
                                    if request.eviction_dir is None:
                                        raise RuntimeError(
                                            "PTL round-group fallback refused because no durable artifact directory is available"
                                        ) from exc
                                    _ptl_kept, _ptl_recovery_receipt = _prepare_ptl_round_group_fallback(
                                        api_messages[1:],
                                        drop_ratio=0.2,
                                        artifact_dir=request.eviction_dir / "compaction",
                                        session_id=request.memory_session_id,
                                        attempt=ptl_retries,
                                    )
                                    _truncated = _dicts_to_llm_messages(
                                        await _apply_mechanical_compaction_with_lifecycle_hooks(
                                            _llm_messages_to_dicts(api_messages[1:]),
                                            compact=lambda _items: _llm_messages_to_dicts(_ptl_kept),
                                            agent_id=request.agent_id,
                                            session_id=request.memory_session_id,
                                            trigger="prompt_too_long",
                                            metadata={
                                                "phase": "prompt_too_long_round_group_fallback",
                                                "attempt": ptl_retries,
                                                "strategy": "round_group",
                                                "agent_name": request.agent_name,
                                                "recovery_receipt": _ptl_recovery_receipt,
                                            },
                                        )
                                    )
                                    # Rebuild system prompt
                                    _ptl_dynamic = build_dynamic_prompt_suffix(
                                        active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                                        available_deferred_tools=available_deferred_tools,
                                        memory_snapshot=resolved_memory_context,
                                        skill_catalog=request.skill_catalog,
                                        runtime_metadata_context=resolved_runtime_metadata_context,
                                        permissions_context=resolved_permissions_context,
                                        retrieval_context=resolved_retrieval_context,
                                        system_prompt_suffix=_system_prompt_suffix,
                                        system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                        budget_profile=budget_profile,
                                        latest_user_query=latest_user_query,
                                        user_name=current_user_name or "",
                                        channel=session_ctx.channel if session_ctx else "",
                                        source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                                        agent_name=request.agent_name,
                                    )
                                    _ptl_prefix = (session_ctx.prompt_prefix if session_ctx else None) or prompt_prefix
                                    _ptl_combined = assemble_runtime_prompt(
                                        _ptl_prefix,
                                        _ptl_dynamic,
                                        context_window_tokens=_ctx_window,
                                        budget_profile=budget_profile,
                                    )
                                    _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(_ptl_combined)
                                    api_messages = [LLMMessage(role="system", content=_ptl_system)] + _truncated
                                    await _emit_event(
                                        {
                                            "type": "session_compact",
                                            "summary": "Prompt too long; dropped oldest round groups before retry.",
                                            "original_message_count": _before_msgs,
                                            "kept_message_count": len(api_messages),
                                            "reason": "prompt_too_long_retry",
                                            "strategy": "round_group",
                                            "attempt": ptl_retries,
                                            "recovery_receipt": _ptl_recovery_receipt,
                                        }
                                    )
                                    logger.info(
                                        "[Kernel] PTL round-group: %d→%d msgs (attempt %d/%d)",
                                        _before_msgs,
                                        len(api_messages),
                                        ptl_retries,
                                        _PTL_MAX_RETRIES,
                                        extra={
                                            "metric": "ptl_retry",
                                            "attempt": ptl_retries,
                                            "strategy": "round_group",
                                        },
                                    )
                                    continue
                                else:
                                    # Attempt 3: full compression fallback
                                    logger.warning(
                                        "[Kernel] PTL full compress fallback (attempt %d/%d)",
                                        ptl_retries,
                                        _PTL_MAX_RETRIES,
                                    )
                                    conv_dicts = _llm_messages_to_dicts(api_messages[1:])
                                    _before_chars = sum(len(d.get("content", "") or "") for d in conv_dicts)
                                    compressed = await _compress_messages_with_lifecycle_hooks(
                                        self._deps.maybe_compress_messages,
                                        conv_dicts,
                                        trace_context=compaction_trace_context,
                                        tools=tools_for_llm,
                                        instructions="prompt_too_long_full_compress_fallback",
                                        agent_id=request.agent_id,
                                        session_id=request.memory_session_id,
                                        trigger="prompt_too_long",
                                        metadata={
                                            "phase": "prompt_too_long_full_compress_fallback",
                                            "attempt": ptl_retries,
                                            "strategy": "full_compress_fallback",
                                            "agent_name": request.agent_name,
                                        },
                                        model_provider=active_model.provider,
                                        model_name=active_model.model,
                                        max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                                        tenant_id=runtime_config.tenant_id,
                                        compress_threshold=0.5,
                                        on_compaction=_emit_compaction_event,
                                        usage_anchor_tokens=context_usage_anchor_tokens,
                                        user_id=request.user_id,
                                    )
                                    _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
                                    if _after_chars < _before_chars * 0.8:
                                        _ptl_dynamic = build_dynamic_prompt_suffix(
                                            active_tool_groups=session_ctx.active_tool_groups if session_ctx else [],
                                            available_deferred_tools=available_deferred_tools,
                                            memory_snapshot=resolved_memory_context,
                                            skill_catalog=request.skill_catalog,
                                            runtime_metadata_context=resolved_runtime_metadata_context,
                                            permissions_context=resolved_permissions_context,
                                            retrieval_context=resolved_retrieval_context,
                                            system_prompt_suffix=_system_prompt_suffix,
                                            system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                            budget_profile=budget_profile,
                                            latest_user_query=latest_user_query,
                                            user_name=current_user_name or "",
                                            channel=session_ctx.channel if session_ctx else "",
                                            source=(getattr(session_ctx, "source", "") or "") if session_ctx else "",
                                            agent_name=request.agent_name,
                                        )
                                        _ptl_prefix = (
                                            session_ctx.prompt_prefix if session_ctx else None
                                        ) or prompt_prefix
                                        _ptl_combined = assemble_runtime_prompt(
                                            _ptl_prefix,
                                            _ptl_dynamic,
                                            context_window_tokens=_ctx_window,
                                            budget_profile=budget_profile,
                                        )
                                        _ptl_system, dynamic_prompt_suffix = _split_system_prompt_for_api(_ptl_combined)
                                        api_messages = [
                                            LLMMessage(role="system", content=_ptl_system)
                                        ] + _dicts_to_llm_messages(compressed)
                                        logger.info(
                                            "[Kernel] PTL full compress: %d→%d chars, %d→%d msgs (attempt %d/%d)",
                                            _before_chars,
                                            _after_chars,
                                            _before_msgs,
                                            len(api_messages),
                                            ptl_retries,
                                            _PTL_MAX_RETRIES,
                                            extra={
                                                "metric": "ptl_retry",
                                                "attempt": ptl_retries,
                                                "strategy": "full_compress",
                                            },
                                        )
                                        continue
                                    else:
                                        logger.warning(
                                            "[Kernel] PTL compression insufficient: %d→%d chars (%.0f%%), falling through",
                                            _before_chars,
                                            _after_chars,
                                            (_after_chars / _before_chars * 100) if _before_chars else 0,
                                        )

                        # ── Fallback model retry ──
                        if (
                            fallback_model is not None
                            and active_model is request.model
                            and not should_surface_without_model_fallback(exc)
                        ):
                            _fallback_reason = "prompt_too_long" if _is_prompt_too_long(exc) else "llm_error"
                            await _emit_event(
                                {
                                    "type": "runtime_fallback",
                                    "reason": _fallback_reason,
                                    "from_model": getattr(active_model, "model", None),
                                    "to_model": getattr(fallback_model, "model", None),
                                    "provider": getattr(fallback_model, "provider", None),
                                    "part": {
                                        "type": "event",
                                        "event_type": "runtime_fallback",
                                        "title": "Fallback Model Activated",
                                        "text": (
                                            f"Switched from {getattr(active_model, 'model', '?')} "
                                            f"to {getattr(fallback_model, 'model', '?')} after {_fallback_reason}."
                                        ),
                                        "status": "info",
                                        "reason": _fallback_reason,
                                        "from_model": getattr(active_model, "model", None),
                                        "to_model": getattr(fallback_model, "model", None),
                                        "provider": getattr(fallback_model, "provider", None),
                                    },
                                }
                            )
                            await client.close()
                            client = self._deps.create_client(fallback_model)
                            active_model = fallback_model
                            active_supports_vision = bool(
                                getattr(fallback_model, "supports_vision", active_supports_vision)
                            )
                            max_tokens = self._deps.get_max_tokens(
                                active_model.provider,
                                active_model.model,
                                request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
                            )
                            fallback_model = None
                            continue
                        await _record_new_token_usage()
                        user_msg = _humanize_llm_error(exc)
                        await self._persist_before_exit(request, runtime_config, f"[LLM Error] {exc}", api_messages)
                        return _classified_llm_error_result(
                            _build_error_result, user_msg, accumulated_tokens, error_classification, delivery_state
                        )
                    except Exception as exc:
                        logger.error(
                            "[Kernel] Unexpected error provider=%s model=%s round=%s: %s: %s",
                            getattr(active_model, "provider", "?"),
                            getattr(active_model, "model", "?"),
                            logical_round_index,
                            type(exc).__name__,
                            str(exc),
                        )
                        if (
                            fallback_model is not None
                            and active_model is request.model
                            and not should_surface_without_model_fallback(exc)
                        ):
                            await _emit_event(
                                {
                                    "type": "runtime_fallback",
                                    "reason": "unexpected_error",
                                    "from_model": getattr(active_model, "model", None),
                                    "to_model": getattr(fallback_model, "model", None),
                                    "provider": getattr(fallback_model, "provider", None),
                                    "part": {
                                        "type": "event",
                                        "event_type": "runtime_fallback",
                                        "title": "Fallback Model Activated",
                                        "text": (
                                            f"Switched from {getattr(active_model, 'model', '?')} "
                                            f"to {getattr(fallback_model, 'model', '?')} after unexpected_error."
                                        ),
                                        "status": "info",
                                        "reason": "unexpected_error",
                                        "from_model": getattr(active_model, "model", None),
                                        "to_model": getattr(fallback_model, "model", None),
                                        "provider": getattr(fallback_model, "provider", None),
                                    },
                                }
                            )
                            await client.close()
                            client = self._deps.create_client(fallback_model)
                            active_model = fallback_model
                            active_supports_vision = bool(
                                getattr(fallback_model, "supports_vision", active_supports_vision)
                            )
                            max_tokens = self._deps.get_max_tokens(
                                active_model.provider,
                                active_model.model,
                                request.max_output_tokens or getattr(active_model, "max_output_tokens", None),
                            )
                            fallback_model = None
                            continue
                        await _record_new_token_usage()
                        user_msg = _humanize_llm_error(exc)
                        await self._persist_before_exit(
                            request,
                            runtime_config,
                            f"[LLM call error] {type(exc).__name__}: {str(exc)}",
                            api_messages,
                        )
                        return _build_error_result(
                            user_msg,
                            tokens_used=accumulated_tokens,
                        )

                accumulated_tokens, context_usage_anchor_tokens = _add_response_usage(
                    self._deps,
                    response,
                    api_messages,
                    request,
                    accumulated_tokens,
                    context_usage_anchor_tokens,
                )

                if _turn_budget_blocks_tools(turn_token_budget, accumulated_tokens, response.tool_calls):
                    budget_msg = _turn_token_budget_message(
                        tokens_used=accumulated_tokens, token_budget=turn_token_budget
                    )
                    await _emit_event(
                        _turn_token_budget_event(
                            logical_round_index,
                            accumulated_tokens,
                            turn_token_budget,
                            len(response.tool_calls),
                        )
                    )
                    await _record_new_token_usage()
                    await self._persist_before_exit(request, runtime_config, budget_msg, api_messages)
                    result = _build_error_result(
                        budget_msg,
                        tokens_used=accumulated_tokens,
                        final_tools=tools_for_llm,
                        terminal_reason=TerminalReason.TOOL_BUDGET,
                    )
                    result.parts = collected_parts + result.parts
                    result.model_result_receipt = last_model_result_receipt
                    return result

                text_loop_decision = loop_guard.observe_assistant_text(response.content)
                if text_loop_decision:
                    await _inject_loop_guard_warning(text_loop_decision)

                if not response.tool_calls:
                    final_content = response.content
                    final_content, _source_permission_allowed = await _enforce_generated_source_permissions(
                        final_content
                    )
                    if _source_permission_allowed:
                        await _flush_buffered_chunks()
                    from app.runtime.hooks import HookEvent

                    _session_source = request.session_context.source if request.session_context else "runtime"
                    _stop_metadata = {
                        "tenant_id": str(runtime_config.tenant_id) if runtime_config.tenant_id else None,
                        "agent_name": request.agent_name or "Agent",
                        "turn_count": logical_round_index,
                        "execution_mode": getattr(runtime_config, "execution_mode", None) or request.invocation_scope,
                    }
                    if request.session_context is not None:
                        _stop_metadata.update(
                            {
                                "runtime_task_id": request.session_context.metadata.get("runtime_task_id")
                                or request.session_context.metadata.get("task_id"),
                            }
                        )
                    _stop_result = await _emit_runtime_hook(
                        HookEvent.STOP,
                        agent_id=request.agent_id,
                        session_id=request.memory_session_id,
                        source=_session_source,
                        messages=_llm_messages_to_dicts(api_messages[1:]),
                        last_assistant_message=final_content,
                        stop_hook_active=bool(
                            request.session_context.metadata.get("stop_hook_active")
                            if request.session_context is not None
                            else False
                        ),
                        metadata=_stop_metadata,
                    )
                    if _stop_result and _stop_result.prevent_continuation:
                        if request.session_context is not None:
                            request.session_context.metadata.pop("stop_hook_active", None)
                        await _emit_event(
                            {
                                "type": "stop_hook_prevented_continuation",
                                "reason": _stop_result.stop_reason or _stop_result.reason,
                            }
                        )
                    elif _stop_result and _stop_result.block:
                        _reason = _stop_result.reason or "Stop hook blocked stopping."
                        api_messages.append(
                            LLMMessage(
                                role="assistant",
                                content=final_content,
                                reasoning_content=response.reasoning_content,
                                reasoning_signature=getattr(response, "reasoning_signature", None),
                            )
                        )
                        api_messages.append(
                            LLMMessage(
                                role="user",
                                content=(
                                    "[Stop hook blocked stopping]\n"
                                    f"{_reason}\n\n"
                                    "Continue from where you left off and address the stop-hook requirement."
                                ),
                            )
                        )
                        if request.session_context is not None:
                            request.session_context.metadata["stop_hook_active"] = True
                        await _emit_event(
                            {
                                "type": "stop_hook_blocked",
                                "reason": _reason,
                                "part": {
                                    "type": "event",
                                    "event_type": "stop_hook_blocked",
                                    "title": "Stop Hook Blocked",
                                    "text": _reason,
                                    "status": "warning",
                                },
                            }
                        )
                        continue
                    elif request.session_context is not None:
                        request.session_context.metadata.pop("stop_hook_active", None)
                    # Subagent runs execute under the parent's agent_id but are
                    # clean specialists (standalone prompt): their INTERNAL
                    # transcript is not the parent's behavior. The conclusion
                    # reaches the parent's memory through the parent's own main
                    # session (the spawn tool result) — persisting/extracting the
                    # subagent session too would double-count it as tool noise.
                    _memory_isolated = _session_source == "subagent"
                    if request.agent_id and runtime_config.tenant_id and not _memory_isolated:
                        try:
                            await _maybe_await(
                                self._deps.persist_memory(
                                    agent_id=request.agent_id,
                                    session_id=request.memory_session_id,
                                    tenant_id=runtime_config.tenant_id,
                                    messages=_build_persisted_memory_messages(request, final_content, api_messages),
                                )
                            )
                        except Exception as exc:
                            logger.error("[Kernel] Failed to persist memory for agent %s: %s", request.agent_id, exc)
                    await _record_new_token_usage()

                    # ── RESPONSE_COMPLETE hook: fire-and-forget extraction trigger ──
                    # (skipped for heartbeat — SOP-driven distiller — and for
                    # subagent internals, per the isolation note above)
                    if _session_source != "heartbeat" and not _memory_isolated:
                        _schedule_runtime_hook(
                            HookEvent.RESPONSE_COMPLETE,
                            agent_id=request.agent_id,
                            session_id=request.memory_session_id,
                            messages=_llm_messages_to_dicts(api_messages[1:]),
                            source=_session_source,
                            metadata={
                                "last_response": final_content or "",
                                "turn_count": logical_round_index,
                                "tenant_id": str(runtime_config.tenant_id) if runtime_config.tenant_id else None,
                                "agent_name": request.agent_name or "Agent",
                                "skill_candidate_loop_enabled": runtime_config.skill_candidate_loop_enabled,
                            },
                        )

                    return InvocationResult(
                        content=final_content,
                        tokens_used=accumulated_tokens,
                        final_tools=tools_for_llm,
                        reasoning_signature=getattr(response, "reasoning_signature", None),
                        parts=collected_parts
                        + build_done_event(
                            final_content,
                            thinking=response.reasoning_content,
                        )["parts"],
                        model_result_receipt=last_model_result_receipt,
                    )

                # Tier 1-4: recover from DeepSeek-V4 style concatenated tool_call args
                # so every payload becomes its own executable tool_call before history is
                # frozen for the next round.
                expanded_tool_calls = _expand_concatenated_tool_calls(response.tool_calls)

                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=_sanitize_tool_calls_for_history(expanded_tool_calls),
                        reasoning_content=response.reasoning_content,
                        reasoning_signature=getattr(response, "reasoning_signature", None),
                    )
                )

                full_reasoning_content = response.reasoning_content or ""

                # Parse all tool calls upfront
                parsed_tool_calls: list[tuple[dict, str, dict]] = []
                for tc in expanded_tool_calls:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        logger.warning(
                            "[Kernel] Malformed tool arguments — returning error to LLM: tool=%s, raw=%s",
                            tool_name,
                            raw_args or "",
                        )
                        # Report parse error as tool result instead of silently using empty dict
                        _parse_err = (
                            f"[Argument Parse Error] Failed to parse JSON arguments for '{tool_name}'. "
                            f"Raw input: {raw_args or ''}. "
                            f"Please fix JSON syntax and retry."
                        )
                        _err_event = {"name": tool_name, "args": {}, "status": "done", "result": _parse_err}
                        api_messages.append(LLMMessage(role="tool", tool_call_id=tc["id"], content=_parse_err))
                        if request.on_tool_call:
                            try:
                                await _maybe_await(request.on_tool_call(_err_event))
                            except Exception as _cb_err:
                                logger.warning(
                                    "[Kernel] on_tool_call callback failed for parse error event: %s", _cb_err
                                )
                        collected_parts.append(build_tool_call_event(_err_event)["part"])
                        continue
                    parsed_tool_calls.append((tc, tool_name, args))

                # Per-round aggregate budget tracker.
                _round_tool_chars = 0
                for _tc, tool_name, args in parsed_tool_calls:
                    _round_tool_names.append(tool_name)
                    call_loop_decision = loop_guard.observe_tool_call(tool_name, args)
                    if call_loop_decision:
                        await _inject_loop_guard_warning(call_loop_decision)

                _round_side_effect_messages: list[dict[str, Any]] = []
                _session_permission_pause_pending = False
                if len(parsed_tool_calls) > 1 and any(
                    _is_concurrency_safe_tool(tool_name) for _tc, tool_name, _args in parsed_tool_calls
                ):
                    # --- Segmented parallel execution ---
                    # Parallel-safe tools run concurrently within their ordered
                    # segment. Non-safe tools wait for every previous call, so
                    # side-effecting tools still observe model order.
                    if request.cancel_event and request.cancel_event.is_set():
                        await _record_new_token_usage()
                        await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                        return _build_cancelled_result(
                            streamed_chunks,
                            streamed_thinking,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            collected_parts=collected_parts,
                        )

                    # 1. Emit all "running" events
                    for _tc, tool_name, args in parsed_tool_calls:
                        running_payload = {
                            "name": tool_name,
                            "args": args,
                            "status": "running",
                            "tool_call_id": _tc["id"],
                            "reasoning_content": full_reasoning_content,
                            "reasoning_signature": getattr(response, "reasoning_signature", None),
                        }
                        if request.on_tool_call:
                            try:
                                await _maybe_await(request.on_tool_call(running_payload))
                            except Exception as _cb_exc:
                                if _is_terminal_tool_card_signal(_cb_exc):
                                    raise
                                logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                _callback_failure_count += 1
                                if _callback_failure_count == 3:
                                    logger.error(
                                        "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                        _callback_failure_count,
                                    )

                    # 2. Execute tools with order barriers.
                    sem = asyncio.Semaphore(_PARALLEL_SEMAPHORE_LIMIT)
                    done_events = [asyncio.Event() for _ in parsed_tool_calls]
                    abort_later_siblings = asyncio.Event()
                    abort_reason = ""
                    unsafe_indices = [
                        idx
                        for idx, (_tc, t_name, _t_args) in enumerate(parsed_tool_calls)
                        if not _is_concurrency_safe_tool(t_name)
                    ]

                    async def _run_tool(
                        index: int, t_name: str, t_args: dict
                    ) -> tuple[str, dict[str, Any], bool, dict[str, Any] | None]:
                        nonlocal abort_reason
                        try:
                            if _is_concurrency_safe_tool(t_name):
                                for prev_unsafe in unsafe_indices:
                                    if prev_unsafe >= index:
                                        break
                                    await done_events[prev_unsafe].wait()
                            else:
                                for prev_index in range(index):
                                    await done_events[prev_index].wait()
                            if abort_later_siblings.is_set():
                                reason = abort_reason or "earlier unsafe tool failed"
                                provider_tool_use_id = parsed_tool_calls[index][0]["id"]
                                return (
                                    f"[Tool skipped] skipped because {reason}; this model batch was aborted.",
                                    t_args,
                                    False,
                                    {
                                        "tool_execution_evidence": _tool_execution_evidence(
                                            tool_name=t_name,
                                            tool_args=t_args,
                                            trace_metadata=None,
                                            machine_status="aborted",
                                            pre_effect_fence_ref=(f"kernel-parallel-abort:{provider_tool_use_id}"),
                                        )
                                    },
                                )
                            _call_side_effects: dict[str, Any] = {}
                            async with sem:
                                _r_str, _r_args, _r_exec = await _execute_tool_with_hooks(
                                    execute_tool=self._deps.execute_tool,
                                    request=request,
                                    runtime_config=runtime_config,
                                    tool_name=t_name,
                                    tool_args=t_args,
                                    tool_call_id=parsed_tool_calls[index][0]["id"],
                                    emit_event=_emit_event,
                                    tools_for_llm=tools_for_llm,
                                    api_messages=api_messages,
                                    record_span=_record_span,
                                    side_effect_sink=_call_side_effects,
                                )
                            if not _is_concurrency_safe_tool(t_name) and _r_exec is False:
                                abort_reason = f"earlier unsafe tool failed ({t_name})"
                                abort_later_siblings.set()
                            return _r_str, _r_args, _r_exec, (_call_side_effects or None)
                        finally:
                            done_events[index].set()

                    results = await asyncio.gather(
                        *[
                            _run_tool(index, t_name, t_args)
                            for index, (_tc, t_name, t_args) in enumerate(parsed_tool_calls)
                        ],
                        return_exceptions=True,
                    )
                    if any(isinstance(_r, _KernelCancelledError) for _r in results):
                        await _record_new_token_usage()
                        await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                        return _build_cancelled_result(
                            streamed_chunks,
                            streamed_thinking,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            collected_parts=collected_parts,
                        )
                    # Convert exceptions to error strings
                    for _i, _r in enumerate(results):
                        if isinstance(_r, BaseException):
                            _tn = parsed_tool_calls[_i][1]
                            logger.warning("[Kernel] Parallel tool %s failed: %s", _tn, _r)
                            _typed_failure = _tool_execution_evidence(
                                tool_name=_tn,
                                tool_args=parsed_tool_calls[_i][2],
                                trace_metadata=None,
                                machine_status="failed",
                            )
                            results[_i] = (
                                f"[Tool execution error] {type(_r).__name__}: {str(_r)}",
                                parsed_tool_calls[_i][2],
                                False,
                                {"tool_execution_evidence": _typed_failure},
                            )

                    # 3. Emit "done" events and append tool results in original order
                    for (tc, tool_name, _original_args), execution in zip(parsed_tool_calls, results):
                        result, effective_args, _executed, _side_effects = execution
                        _loop_proof = (_side_effects or {}).get("loop_guard_proof") or {}
                        _execution_evidence = (_side_effects or {}).get("tool_execution_evidence") or {}
                        _decision = _execution_evidence.get("tool_decision")
                        if isinstance(_decision, dict) and _decision.get("outcome") == "require_approval":
                            _session_permission_pause_pending = True
                        result_loop_decision = loop_guard.observe_tool_result(
                            tool_name,
                            effective_args,
                            str(result),
                            machine_outcome=str(_execution_evidence.get("status") or "") or None,
                            side_effect_free=is_read_only_tool(tool_name),
                            retry_exhausted=_loop_proof.get("retry_exhausted") is True,
                            progress_token=str(_loop_proof.get("progress_token") or "") or None,
                        )
                        if result_loop_decision:
                            if result_loop_decision.severity == "warn":
                                await _inject_loop_guard_warning(result_loop_decision)
                            else:
                                _loop_guard_terminal_decision = result_loop_decision
                        _raw_result, _model_result = _tool_result_views(result, _side_effects)
                        _replacement_reason = "result size threshold"
                        _content = _maybe_evict_tool_result(tool_name, tc["id"], _model_result, request.eviction_dir)
                        if _round_tool_chars + len(_content) > _TOOL_RESULTS_AGGREGATE_BUDGET:
                            _replacement_reason = "round aggregate budget"
                            logger.info(
                                "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                _round_tool_chars + len(_content),
                                _TOOL_RESULTS_AGGREGATE_BUDGET,
                                tool_name,
                            )
                            _content = _maybe_evict_tool_result(
                                tool_name,
                                tc["id"],
                                _model_result,
                                request.eviction_dir,
                                force=True,
                                reason="round aggregate budget",
                            )
                        _round_tool_chars += len(_content)
                        done_payload = {
                            "name": tool_name,
                            "args": effective_args,
                            "status": "done",
                            "result": _raw_result,
                            # D-04: the original streamed tool_call_id the model
                            # saw, so the web resume path can reuse it instead of
                            # synthesizing call_{msg.id}.
                            "tool_call_id": tc["id"],
                            "model_seen_result": _content,
                            "content_replacement": _content_replacement_record(
                                tool_name=tool_name,
                                tool_call_id=tc["id"],
                                raw_result=_raw_result,
                                inline_content=_content,
                                reason=_replacement_reason,
                            ),
                            "reasoning_content": full_reasoning_content,
                            "reasoning_signature": getattr(response, "reasoning_signature", None),
                        }
                        if _side_effects and _side_effects.get("artifacts"):
                            done_payload["artifacts"] = list(_side_effects["artifacts"])
                        if _side_effects and _side_effects.get("tool_execution_evidence"):
                            done_payload["tool_execution_evidence"] = dict(_side_effects["tool_execution_evidence"])
                        if request.on_tool_call:
                            try:
                                await _maybe_await(request.on_tool_call(done_payload))
                            except Exception as _cb_exc:
                                if _is_terminal_tool_card_signal(_cb_exc):
                                    raise
                                logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                _callback_failure_count += 1
                                if _callback_failure_count == 3:
                                    logger.error(
                                        "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                        _callback_failure_count,
                                    )
                        collected_parts.append(build_tool_call_event(done_payload)["part"])
                        api_messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tc["id"],
                                content=_tool_message_content(_content, result),
                            )
                        )
                        # D-08: capture the tool's side-effect channel on the REAL
                        # conversation. Inject new_messages only after every same-round
                        # tool result has been appended, so provider tool-result blocks
                        # remain contiguous.
                        if _side_effects:
                            _injected = _side_effects.get("new_messages") or []
                            if _injected:
                                _round_side_effect_messages.extend(_injected)
                            _ts = _side_effects.get("terminal_signal")
                            if isinstance(_ts, str) and _ts.strip():
                                _tool_terminal_signal = _ts
                        if _tool_result_requests_user_clarification(tool_name, str(result)):
                            if _round_side_effect_messages:
                                api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))
                            return await _pause_for_user_clarification()
                else:
                    # --- Sequential execution (original logic) ---
                    for tc, tool_name, args in parsed_tool_calls:
                        if request.cancel_event and request.cancel_event.is_set():
                            await _record_new_token_usage()
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )
                        running_payload = {
                            "name": tool_name,
                            "args": args,
                            "status": "running",
                            "tool_call_id": tc["id"],
                            "reasoning_content": full_reasoning_content,
                            "reasoning_signature": getattr(response, "reasoning_signature", None),
                        }
                        if request.on_tool_call:
                            try:
                                await _maybe_await(request.on_tool_call(running_payload))
                            except Exception as _cb_exc:
                                if _is_terminal_tool_card_signal(_cb_exc):
                                    raise
                                logger.warning("[Kernel] on_tool_call(running) callback failed: %s", _cb_exc)
                                _callback_failure_count += 1
                                if _callback_failure_count == 3:
                                    logger.error(
                                        "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                        _callback_failure_count,
                                    )

                        _side_effects: dict[str, Any] = {}
                        try:
                            if _session_permission_pause_pending:
                                result = json.dumps(
                                    {
                                        "status": "aborted",
                                        "reason_code": "earlier_tool_waiting_for_session_permission",
                                        "retryable": True,
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                                executed = False
                                _side_effects["tool_execution_evidence"] = _tool_execution_evidence(
                                    tool_name=tool_name,
                                    tool_args=args,
                                    trace_metadata=None,
                                    machine_status="aborted",
                                    retryable=True,
                                    pre_effect_fence_ref=f"session-permission-batch-abort:{tc['id']}",
                                )
                            else:
                                result, args, executed = await _execute_tool_with_hooks(
                                    execute_tool=self._deps.execute_tool,
                                    request=request,
                                    runtime_config=runtime_config,
                                    tool_name=tool_name,
                                    tool_args=args,
                                    tool_call_id=tc["id"],
                                    emit_event=_emit_event,
                                    tools_for_llm=tools_for_llm,
                                    api_messages=api_messages,
                                    record_span=_record_span,
                                    side_effect_sink=_side_effects,
                                )
                        except _KernelCancelledError:
                            await _record_new_token_usage()
                            await self._persist_before_exit(
                                request, runtime_config, "*[Generation stopped]*", api_messages
                            )
                            return _build_cancelled_result(
                                streamed_chunks,
                                streamed_thinking,
                                tokens_used=accumulated_tokens,
                                final_tools=tools_for_llm,
                                collected_parts=collected_parts,
                            )
                        _loop_proof = (_side_effects or {}).get("loop_guard_proof") or {}
                        _execution_evidence = (_side_effects or {}).get("tool_execution_evidence") or {}
                        _decision = _execution_evidence.get("tool_decision")
                        if isinstance(_decision, dict) and _decision.get("outcome") == "require_approval":
                            _session_permission_pause_pending = True
                        result_loop_decision = loop_guard.observe_tool_result(
                            tool_name,
                            args,
                            str(result),
                            machine_outcome=str(_execution_evidence.get("status") or "") or None,
                            side_effect_free=is_read_only_tool(tool_name),
                            retry_exhausted=_loop_proof.get("retry_exhausted") is True,
                            progress_token=str(_loop_proof.get("progress_token") or "") or None,
                        )
                        if result_loop_decision:
                            if result_loop_decision.severity == "warn":
                                await _inject_loop_guard_warning(result_loop_decision)
                            else:
                                _loop_guard_terminal_decision = result_loop_decision

                        if request.expand_tools and request.agent_id:
                            if executed and _should_expand_tools(tool_name, args):
                                expansion_payload: ToolExpansionResult | list[dict] | None = None
                                if self._deps.resolve_tool_expansion:
                                    expansion_payload = await _maybe_await(
                                        self._deps.resolve_tool_expansion(request, tool_name, args)
                                    )
                                if isinstance(expansion_payload, ToolExpansionResult):
                                    full_toolset = expansion_payload.tools
                                    session_context = request.session_context
                                    if session_context is None:
                                        session_context = request.session_context = SessionContext()
                                    new_tool_groups = _merge_active_tool_groups(
                                        session_context, expansion_payload.active_tool_groups
                                    )
                                    if new_tool_groups:
                                        # P1.10: Delayed loading metrics
                                        _new_tool_count = sum(
                                            len(p.get("tools", [])) for p in new_tool_groups if isinstance(p, dict)
                                        )
                                        _pack_names = [
                                            p.get("name", "?") for p in new_tool_groups if isinstance(p, dict)
                                        ]
                                        logger.info(
                                            "[Kernel] Tool expansion: +%d tools via %s (trigger: %s)",
                                            _new_tool_count,
                                            _pack_names,
                                            tool_name,
                                            extra={
                                                "metric": "tool_expansion",
                                                "trigger_tool": tool_name,
                                                "pack_names": _pack_names,
                                                "new_tool_count": _new_tool_count,
                                                "total_packs": len(session_context.active_tool_groups),
                                            },
                                        )
                                        event_payload = expansion_payload.event_payload or {
                                            "type": "tool_group_activation",
                                            "packs": new_tool_groups,
                                            "tool_groups": new_tool_groups,
                                            "message": "Activated runtime tool groups for this task.",
                                            "status": "info",
                                        }
                                        await _emit_event(event_payload)
                                        prompt_prefix = session_context.prompt_prefix
                                        if prompt_prefix is None:
                                            prompt_prefix = await _maybe_await(
                                                self._deps.build_system_prompt(
                                                    request,
                                                    runtime_config.tenant_id,
                                                    resolved_memory_context,
                                                    current_user_name,
                                                )
                                            )
                                            current_manifest = build_frozen_context_dependency_manifest(prompt_prefix)
                                            session_context.metadata["frozen_context_dependency_manifest"] = (
                                                current_manifest
                                            )
                                            from app.runtime.context import ensure_runtime_assembly_state

                                            assembly_state = ensure_runtime_assembly_state(session_context)
                                            refreshed_prompt_manifest = dict(assembly_state.prompt_assembly_manifest)
                                            refreshed_prompt_manifest["frozen_context_dependency_manifest"] = (
                                                current_manifest
                                            )
                                            refreshed_prompt_manifest["frozen_sections"] = [
                                                section["name"]
                                                for section in current_manifest.get("sections", [])
                                                if section.get("name")
                                            ]
                                            assembly_state.record_prompt_manifest(refreshed_prompt_manifest)
                                            _current_prompt_cache_key = _build_frozen_prompt_cache_key(
                                                request,
                                                runtime_config,
                                                current_user_name=current_user_name,
                                                rendered_prefix=prompt_prefix,
                                            )
                                            _store_prompt_prefix_cache(
                                                session_context,
                                                prompt_prefix,
                                                _current_prompt_cache_key,
                                            )
                                            session_context._memory_hash = hashlib.sha256(
                                                resolved_memory_context.encode("utf-8")
                                            ).hexdigest()[:16]
                                        combined_prompt = assemble_runtime_prompt(
                                            prompt_prefix,
                                            build_dynamic_prompt_suffix(
                                                active_tool_groups=session_context.active_tool_groups,
                                                available_deferred_tools=available_deferred_tools,
                                                memory_snapshot=resolved_memory_context,
                                                skill_catalog=request.skill_catalog,
                                                runtime_metadata_context=resolved_runtime_metadata_context,
                                                permissions_context=resolved_permissions_context,
                                                retrieval_context=resolved_retrieval_context,
                                                system_prompt_suffix=_system_prompt_suffix,
                                                system_prompt_suffix_sections=_system_prompt_suffix_sections(),
                                                budget_profile=budget_profile,
                                                latest_user_query=latest_user_query,
                                                user_name=current_user_name or "",
                                                channel=session_context.channel,
                                                source=getattr(session_context, "source", "") or "",
                                                agent_name=request.agent_name,
                                            ),
                                            context_window_tokens=_ctx_window,
                                            budget_profile=budget_profile,
                                        )
                                        system_prompt, dynamic_prompt_suffix = _split_system_prompt_for_api(
                                            combined_prompt
                                        )
                                        api_messages[0] = LLMMessage(role="system", content=system_prompt)
                                elif isinstance(expansion_payload, list):
                                    full_toolset = expansion_payload
                                if full_toolset is not None:
                                    # B-04 fix: re-filter expanded tools if coordinator mode active
                                    tools_for_llm = (
                                        filter_tools_for_coordinator(
                                            full_toolset,
                                            dispatcher_only=_is_strict_dispatcher,
                                        )
                                        if _is_coordinator
                                        else full_toolset
                                    )

                        _raw_result, _model_result = _tool_result_views(result, _side_effects)
                        _replacement_reason = "result size threshold"
                        _content = _maybe_evict_tool_result(tool_name, tc["id"], _model_result, request.eviction_dir)
                        if _round_tool_chars + len(_content) > _TOOL_RESULTS_AGGREGATE_BUDGET:
                            _replacement_reason = "round aggregate budget"
                            logger.info(
                                "[Kernel] Round aggregate budget exceeded (%d > %d), force-evicting %s",
                                _round_tool_chars + len(_content),
                                _TOOL_RESULTS_AGGREGATE_BUDGET,
                                tool_name,
                            )
                            _content = _maybe_evict_tool_result(
                                tool_name,
                                tc["id"],
                                _model_result,
                                request.eviction_dir,
                                force=True,
                                reason="round aggregate budget",
                            )
                        _round_tool_chars += len(_content)
                        done_payload = {
                            "name": tool_name,
                            "args": args,
                            "status": "done",
                            "result": _raw_result,
                            # D-04: the original streamed tool_call_id the model
                            # saw, so the web resume path can reuse it instead of
                            # synthesizing call_{msg.id}.
                            "tool_call_id": tc["id"],
                            "model_seen_result": _content,
                            "content_replacement": _content_replacement_record(
                                tool_name=tool_name,
                                tool_call_id=tc["id"],
                                raw_result=_raw_result,
                                inline_content=_content,
                                reason=_replacement_reason,
                            ),
                            "reasoning_content": full_reasoning_content,
                            "reasoning_signature": getattr(response, "reasoning_signature", None),
                        }
                        if _side_effects and _side_effects.get("artifacts"):
                            done_payload["artifacts"] = list(_side_effects["artifacts"])
                        if _side_effects and _side_effects.get("tool_execution_evidence"):
                            done_payload["tool_execution_evidence"] = dict(_side_effects["tool_execution_evidence"])
                        if request.on_tool_call:
                            try:
                                await _maybe_await(request.on_tool_call(done_payload))
                            except Exception as _cb_exc:
                                if _is_terminal_tool_card_signal(_cb_exc):
                                    raise
                                logger.warning("[Kernel] on_tool_call(done) callback failed: %s", _cb_exc)
                                _callback_failure_count += 1
                                if _callback_failure_count == 3:
                                    logger.error(
                                        "[Kernel] Multiple callback failures (%d) — client may be disconnected",
                                        _callback_failure_count,
                                    )
                        collected_parts.append(build_tool_call_event(done_payload)["part"])
                        api_messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tc["id"],
                                content=_tool_message_content(_content, result),
                            )
                        )
                        # D-08: capture the tool's side-effect channel on the REAL
                        # conversation. Inject new_messages only after every same-round
                        # tool result has been appended, so provider tool-result blocks
                        # remain contiguous.
                        if _side_effects:
                            _injected = _side_effects.get("new_messages") or []
                            if _injected:
                                _round_side_effect_messages.extend(_injected)
                            _ts = _side_effects.get("terminal_signal")
                            if isinstance(_ts, str) and _ts.strip():
                                _tool_terminal_signal = _ts
                        if _tool_result_requests_user_clarification(tool_name, str(result)):
                            if _round_side_effect_messages:
                                api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))
                            return await _pause_for_user_clarification()

                if _round_side_effect_messages:
                    api_messages.extend(_dicts_to_llm_messages(_round_side_effect_messages))

                if _session_permission_pause_pending:
                    return await _pause_for_user_clarification()

                if _loop_guard_terminal_decision is not None:
                    _loop_guard_summary_pending = True
                    _terminal_evidence = {
                        "schema": "hive.loop_guard_terminal_evidence.v1",
                        **_loop_guard_terminal_decision.trace_event,
                    }
                    api_messages.append(
                        LLMMessage(
                            role="system",
                            content=(
                                "<loop_guard_terminal_evidence>"
                                + json.dumps(_terminal_evidence, ensure_ascii=False, sort_keys=True)
                                + "</loop_guard_terminal_evidence>\n"
                                "The governed runtime has recorded that this side-effect-free retry path is exhausted "
                                "without state progress. Treat this as evidence, not as a platform-authored semantic "
                                "conclusion. Decide whether another available capability can make progress or whether "
                                "to answer now; preserve uncertainty and cite the most useful recovery action."
                            ),
                        )
                    )
                    await _emit_event(
                        {
                            "type": "loop_guard",
                            "part": {
                                "type": "event",
                                "event_type": "loop_guard_terminal_evidence",
                                "title": "Retry evidence available",
                                "text": "The model received typed non-progress evidence with its authorized tools intact.",
                                "status": "warning",
                                **_loop_guard_terminal_decision.trace_event,
                            },
                        }
                    )
                    _loop_guard_terminal_decision = None

                # ── D-08: tool-requested turn termination ──
                # A tool emitted ToolContentEnvelope.terminal_signal during this
                # round. The round's tool results are already appended; end the
                # turn now rather than looping into another model call.
                if _tool_terminal_signal is not None:
                    return await _end_turn_for_tool_terminal_signal(
                        _tool_terminal_signal,
                        response.content or "",
                    )

                # ── L1: Time-based microcompact — clear old tool results ──
                # Clear tool results older than 60min, always keep the 5 most recent.
                # P1-W2-3: At ≥60% context utilization the gap drops to 10min
                # so we shed aging tool results before sliding into the heavy
                # compaction zone at 75%.
                if logical_round_index % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0:
                    import time as _time_mod

                    _now = _time_mod.time()
                    # Collect all substantial tool results with their timestamps
                    _tool_entries: list[tuple[int, float, LLMMessage]] = []
                    for _mi, _msg in enumerate(api_messages):
                        if (
                            _msg.role == "tool"
                            and not str(_msg.content or "").startswith(_MICROCOMPACT_CLEARED_MARKER)
                            and len(_msg.content or "") > 500
                        ):
                            _tool_entries.append((_mi, _msg.created_at, _msg))

                    if _tool_entries:
                        # Sort by timestamp descending — keep the most recent N
                        _sorted_by_time = sorted(_tool_entries, key=lambda x: x[1], reverse=True)
                        _keep_indices = {x[0] for x in _sorted_by_time[:_MICROCOMPACT_KEEP_RECENT]}

                        # Pressure check: if context utilization is already
                        # ≥60% of the model window, the helper returns the
                        # aggressive 10min gap.
                        _model_window = getattr(active_model, "max_input_tokens", None) if active_model else None
                        _used_chars = sum(len(m.content or "") for m in api_messages)
                        _used_tokens = self._deps.estimate_tokens_from_chars(_used_chars)
                        _gap_seconds = _compute_microcompact_gap(_used_tokens, _model_window)

                        _mc_cleared = 0
                        for _mi, _ts, _msg in _tool_entries:
                            if _mi in _keep_indices:
                                continue
                            if (_now - _ts) < _gap_seconds:
                                continue
                            replacement = support._microcompact_artifact_replacement(str(_msg.content or ""))
                            if replacement is not None:
                                _msg.content = replacement
                                _mc_cleared += 1
                        if _mc_cleared:
                            logger.info(
                                "[Kernel] Microcompact: cleared %d old tool results (round %d, gap=%ds, kept=%d recent)",
                                _mc_cleared,
                                logical_round_index,
                                _gap_seconds,
                                _MICROCOMPACT_KEEP_RECENT,
                                extra={
                                    "metric": "microcompact",
                                    "cleared": _mc_cleared,
                                    "round": logical_round_index,
                                    "gap_seconds": _gap_seconds,
                                    "under_pressure": _gap_seconds == _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS,
                                },
                            )

                # ── L3: Mid-loop context compaction ──────────────────────────
                if logical_round_index % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0 and len(api_messages) > 6:
                    # Cancel check before potentially slow compression
                    if request.cancel_event and request.cancel_event.is_set():
                        await self._persist_before_exit(request, runtime_config, "*[Generation stopped]*", api_messages)
                        return _build_cancelled_result(
                            streamed_chunks,
                            streamed_thinking,
                            tokens_used=accumulated_tokens,
                            final_tools=tools_for_llm,
                            collected_parts=collected_parts,
                        )
                    # Note: system prompt tokens are NOT included in this compression
                    # because compress_threshold is relative to context_limit which
                    # already reserves space for the prompt via compute_history_limit.
                    conv_dicts = _llm_messages_to_dicts(api_messages[1:])

                    compressed = await _compress_messages_with_lifecycle_hooks(
                        self._deps.maybe_compress_messages,
                        conv_dicts,
                        trace_context=compaction_trace_context,
                        tools=tools_for_llm,
                        instructions="mid_loop_context_compaction",
                        agent_id=request.agent_id,
                        session_id=request.memory_session_id,
                        trigger="auto",
                        metadata={
                            "phase": "mid_loop_context_compaction",
                            "round": logical_round_index,
                            "agent_name": request.agent_name,
                            "important_files": list(getattr(request.session_context, "recent_files", []) or []),
                            "pending_work": list(getattr(request.session_context, "pending_items", []) or []),
                            "last_successful_step": "".join(streamed_chunks),
                        },
                        post_hook_async=True,
                        model_provider=active_model.provider,
                        model_name=active_model.model,
                        max_input_tokens_override=getattr(active_model, "max_input_tokens", None),
                        tenant_id=runtime_config.tenant_id,
                        compress_threshold=_MIDLOOP_COMPACT_THRESHOLD,
                        on_compaction=_emit_compaction_event,
                        usage_anchor_tokens=context_usage_anchor_tokens,
                        user_id=request.user_id,
                    )
                    if len(compressed) < len(conv_dicts):
                        # Post-compaction restoration: re-inject identity + work ledger
                        _restored = ""
                        if request.agent_id:
                            try:
                                _restored = _build_restoration_context(
                                    request.agent_id,
                                    session_context=request.session_context,
                                    recovery_result=request.recovery_manifest_result,
                                )
                            except Exception as _restore_err:
                                logger.debug("[Kernel] Post-compact restoration failed: %s", _restore_err)
                        restored_msgs = _dicts_to_llm_messages(compressed)
                        if _restored:
                            # Insert restoration context right after the summary, before recent messages
                            restored_msgs.insert(
                                1 if len(restored_msgs) > 1 else 0, LLMMessage(role="system", content=_restored)
                            )
                        api_messages = [api_messages[0]] + restored_msgs
                        # Compaction re-arm (T-G1, M8): fire-once reminders
                        # (plan FULL) re-send and all throttle clocks restart.
                        reminder_scheduler.reset()
                        # Preserve pre-compaction parts so clients get full event history (C-02)
                        # Mark them as pre-compaction to avoid duplicate persistence
                        logger.info(
                            "[Kernel] Mid-loop compaction: %d → %d messages (round %d)",
                            len(conv_dicts) + 1,
                            len(api_messages),
                            logical_round_index,
                            extra={
                                "metric": "compaction",
                                "before_msgs": len(conv_dicts) + 1,
                                "after_msgs": len(api_messages),
                                "round": logical_round_index,
                                "restored": bool(_restored),
                            },
                        )
                        # Persist compacted state so recovery doesn't lose progress
                        await self._persist_before_exit(
                            request,
                            runtime_config,
                            "[checkpoint] mid-loop compaction",
                            api_messages,
                        )

            await _record_new_token_usage()
            round_limit_msg = _tool_round_limit_message(max_rounds)
            await self._persist_before_exit(request, runtime_config, round_limit_msg, api_messages)
            return _build_error_result(
                round_limit_msg,
                tokens_used=accumulated_tokens,
                final_tools=tools_for_llm,
                terminal_reason=TerminalReason.TOOL_BUDGET,
            )
        finally:
            await client.close()
    finally:
        if request.execution_identity:
            if previous_identity:
                set_execution_identity(previous_identity)
            else:
                clear_execution_identity()
        if trace_token is not None:
            await _record_span(
                invocation_id=invocation_id,
                span_type="invocation",
                name="agent_kernel.handle",
                started_at_ms=invocation_started_ms,
                metadata={
                    "session_id": request.memory_session_id or getattr(request.session_context, "session_id", ""),
                    "source": getattr(request.session_context, "source", "") if request.session_context else "",
                    "agent_name": request.agent_name,
                },
            )
            reset_invocation_id(trace_token)
