"""Owns one invocation assembly and hook handoff."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.runtime.invoker import (
        AgentInvocationRequest,
        AgentInvocationResult,
    )


async def run_agent_invocation(request: AgentInvocationRequest, *, support: Any) -> AgentInvocationResult:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    AgentInvocationResult = support.AgentInvocationResult
    ExecutionIdentityRef = support.ExecutionIdentityRef
    InvocationRequest = support.InvocationRequest
    TerminalReason = support.TerminalReason
    _enforce_invocation_quota = support._enforce_invocation_quota
    _ensure_turn_metadata = support._ensure_turn_metadata
    _format_hook_additional_contexts = support._format_hook_additional_contexts
    _latest_user_prompt = support._latest_user_prompt
    _maybe_await = support._maybe_await
    _normalize_invocation_session_context = support._normalize_invocation_session_context
    _resolve_agent_smart_model_routing = support._resolve_agent_smart_model_routing
    _resolve_context_budget = support._resolve_context_budget
    _resolve_effective_turn_route = support._resolve_effective_turn_route
    _resolve_eviction_dir = support._resolve_eviction_dir
    _resolve_kernel_for_request = support._resolve_kernel_for_request
    _skill_catalog_ranking_inputs = support._skill_catalog_ranking_inputs
    build_skill_catalog_section_for_agent = support.build_skill_catalog_section_for_agent
    get_combined_openai_tools = support.get_combined_openai_tools
    logger = support.logger
    logging = support.logging
    record_skill_runtime_usage_for_invocation = support.record_skill_runtime_usage_for_invocation

    _normalize_invocation_session_context(request)
    quota_result = await _enforce_invocation_quota(request)
    if quota_result is not None:
        return quota_result

    skill_runtime_tool_events: list[dict[str, Any]] = []

    async def _on_tool_call_with_skill_runtime_usage(data: dict) -> None:
        if isinstance(data, dict) and data.get("status") == "done":
            skill_runtime_tool_events.append(
                {
                    "name": data.get("name"),
                    "args": data.get("args"),
                    "status": data.get("status"),
                }
            )
        if request.on_tool_call is not None:
            await _maybe_await(request.on_tool_call(data))

    def _record_skill_runtime_usage(terminal_status: str, assistant_text: str) -> None:
        if request.agent_id is None or not skill_runtime_tool_events:
            return
        try:
            record_skill_runtime_usage_for_invocation(
                agent_id=request.agent_id,
                session_context=request.session_context,
                tool_events=skill_runtime_tool_events,
                terminal_status=terminal_status,
                assistant_text=assistant_text,
                note=assistant_text[:500],
            )
        except Exception as exc:  # noqa: BLE001 - telemetry cannot fail the user-facing invocation
            logger.warning(
                "[Invoker] skill runtime usage telemetry failed: agent_id=%s session_id=%s error=%s",
                request.agent_id,
                request.session_context.session_id if request.session_context else None,
                exc,
            )

    routing_config = request.smart_model_routing
    if routing_config is None and request.agent_id is not None and request.fallback_model is not None:
        routing_config = await _resolve_agent_smart_model_routing(request.agent_id)

    effective_turn_route = _resolve_effective_turn_route(request, routing_config=routing_config)
    effective_model = effective_turn_route["model"]
    effective_fallback_model = effective_turn_route["fallback_model"]
    effective_supports_vision = effective_turn_route["supports_vision"]
    turn_route_metadata = effective_turn_route["metadata"]

    execution_identity = request.execution_identity
    if execution_identity is None:
        try:
            from app.core.execution_context import get_execution_identity

            current_identity = get_execution_identity()
            if current_identity:
                execution_identity = ExecutionIdentityRef(
                    identity_type=current_identity.identity_type,
                    identity_id=current_identity.identity_id,
                    label=current_identity.label,
                )
        except Exception:
            execution_identity = None

    # Step 9 (CC parity): load the skill catalog once and thread it through the
    # dynamic suffix (InvocationRequest.skill_catalog → kernel → dynamic suffix),
    # NOT the frozen prefix. Standalone subagents carry no host catalog.
    skill_catalog_text = ""
    skill_catalog_ranking: list[dict[str, Any]] = []
    if request.agent_id is not None and not (request.standalone_system_prompt or "").strip():
        ranking_inputs = _skill_catalog_ranking_inputs(request)
        skill_catalog_text = build_skill_catalog_section_for_agent(
            request.agent_id,
            budget_profile=_resolve_context_budget(request),
            scenario_text=ranking_inputs["scenario_text"],
            session_id=(
                getattr(request.session_context, "session_id", None)
                if request.session_context is not None
                else request.memory_session_id
            ),
            active_skill_names=ranking_inputs["active_skill_names"],
            path_triggered_skill_names=ranking_inputs["path_triggered_skill_names"],
            ranking_manifest=skill_catalog_ranking,
        )
        if request.session_context is not None:
            from app.runtime.context import ensure_runtime_assembly_state

            ensure_runtime_assembly_state(request.session_context).record_skill_catalog_ranking(
                ranking=skill_catalog_ranking,
                inputs={
                    "scenario_text_present": bool(ranking_inputs["scenario_text"]),
                    "active_skill_names": list(ranking_inputs["active_skill_names"]),
                    "path_triggered_skill_names": list(ranking_inputs["path_triggered_skill_names"]),
                },
            )

    kernel_request = InvocationRequest(
        model=effective_model,
        fallback_model=effective_fallback_model,
        messages=request.messages,
        agent_name=request.agent_name,
        role_description=request.role_description,
        agent_id=request.agent_id,
        user_id=request.user_id,
        execution_identity=execution_identity,
        on_chunk=request.on_chunk,
        on_tool_call=_on_tool_call_with_skill_runtime_usage,
        on_thinking=request.on_thinking,
        on_event=request.on_event,
        supports_vision=effective_supports_vision,
        memory_context=request.memory_context,
        memory_session_id=request.memory_session_id,
        memory_messages=request.memory_messages,
        session_context=request.session_context,
        system_prompt_suffix=request.system_prompt_suffix,
        standalone_system_prompt=request.standalone_system_prompt,
        skill_catalog=skill_catalog_text,
        tool_executor=request.tool_executor,
        mid_run_message_drain=request.mid_run_message_drain,
        cancel_event=request.cancel_event,
        initial_tools=request.initial_tools or (get_combined_openai_tools() if request.agent_id is None else None),
        core_tools_only=request.core_tools_only,
        allowed_tool_names=request.allowed_tool_names,
        excluded_tool_names=request.excluded_tool_names,
        expand_tools=request.expand_tools,
        max_tool_rounds=request.max_tool_rounds,
        max_output_tokens=request.max_output_tokens,
        eviction_dir=_resolve_eviction_dir(request.agent_id),
        invocation_scope=request.invocation_scope,
        delegation_token=request.delegation_token,
    )

    # ── SETUP hook ──
    # Cloud runtimes do not create a local checkout per turn, but they still
    # have a real, blockable setup boundary: tenant/session identity is pinned,
    # the virtual workspace is selected, and model context can be extended
    # before the accepted prompt enters the loop.
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        setup_metadata = _ensure_turn_metadata(request)
        setup_result = await emit_hook(
            HookEvent.SETUP,
            agent_id=request.agent_id,
            session_id=request.memory_session_id
            or (request.session_context.session_id if request.session_context else None),
            source=_session_source,
            metadata={
                "tenant_id": setup_metadata.get("tenant_id"),
                "user_id": str(request.user_id) if request.user_id else None,
                "runtime_task_id": setup_metadata.get("runtime_task_id") or setup_metadata.get("task_id"),
                "request_id": setup_metadata.get("request_id"),
                "trace_id": setup_metadata.get("trace_id"),
                "turn_id": setup_metadata.get("turn_id"),
                "intent_id": setup_metadata.get("intent_id"),
                "setup_trigger": "invocation",
                "cloud_workspace_uri": (
                    f"agent://{request.agent_id}/sessions/"
                    f"{request.memory_session_id or (request.session_context.session_id if request.session_context else 'runtime')}"
                    "/workspace"
                ),
            },
        )
        if setup_result and setup_result.block:
            return AgentInvocationResult(
                content=f"Blocked by setup hook: {setup_result.reason or 'policy'}",
                tokens_used=0,
                final_tools=[],
                parts=[],
                terminal_reason=TerminalReason.HOOK_STOPPED,
            )
        if setup_result and setup_result.additional_contexts:
            hook_context = _format_hook_additional_contexts(setup_result.additional_contexts)
            if hook_context:
                kernel_request.system_prompt_suffix = "\n\n".join(
                    part for part in (kernel_request.system_prompt_suffix, hook_context) if part
                )
    except Exception as setup_error:
        logger.warning("[Invoker] SETUP hook failed (continuing): %s", setup_error)

    # ── USER_PROMPT_SUBMIT hook ──
    # Entry points are responsible for durable DB/T0 append before invoking the
    # runtime. This hook is the shared post-append, pre-model lifecycle boundary.
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
        prompt_text = _latest_user_prompt(request.messages)
        prompt_result = await emit_hook(
            HookEvent.USER_PROMPT_SUBMIT,
            agent_id=request.agent_id,
            session_id=request.memory_session_id
            or (request.session_context.session_id if request.session_context else None),
            prompt=prompt_text,
            source=_session_source,
            metadata={
                "tenant_id": session_metadata.get("tenant_id"),
                "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
                "request_id": session_metadata.get("request_id"),
                "trace_id": session_metadata.get("trace_id"),
                "turn_id": session_metadata.get("turn_id"),
                "intent_id": session_metadata.get("intent_id"),
                "agent_name": request.agent_name,
                "execution_mode": request.invocation_scope,
            },
        )
        if prompt_result and prompt_result.block:
            return AgentInvocationResult(
                content=f"Blocked by prompt hook: {prompt_result.reason or 'policy'}",
                tokens_used=0,
                final_tools=[],
                parts=[],
                terminal_reason=TerminalReason.HOOK_STOPPED,
            )
        if prompt_result and prompt_result.additional_contexts:
            hook_context = _format_hook_additional_contexts(prompt_result.additional_contexts)
            if hook_context:
                kernel_request.system_prompt_suffix = "\n\n".join(
                    part for part in (kernel_request.system_prompt_suffix, hook_context) if part
                )
    except Exception as _prompt_err:
        logging.getLogger(__name__).debug("[Invoker] USER_PROMPT_SUBMIT hook failed (non-fatal): %s", _prompt_err)

    # ── SESSION_START hook ──
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
        session_start_result = await emit_hook(
            HookEvent.SESSION_START,
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=_session_source,
            metadata={
                "model": getattr(effective_model, "model", str(effective_model)) if effective_model else None,
                "fallback_model": getattr(effective_fallback_model, "model", str(effective_fallback_model))
                if effective_fallback_model
                else None,
                "turn_route_reason": turn_route_metadata["reason"],
                "execution_mode": request.invocation_scope,
                "tenant_id": session_metadata.get("tenant_id"),
                "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
                "request_id": session_metadata.get("request_id"),
                "trace_id": session_metadata.get("trace_id"),
                "turn_id": session_metadata.get("turn_id"),
                "intent_id": session_metadata.get("intent_id"),
            },
        )
        if session_start_result:
            if session_start_result.initial_user_message:
                initial_message = str(session_start_result.initial_user_message).strip()
                if initial_message:
                    kernel_request.messages = [
                        {"role": "user", "content": initial_message, "source": "session_start_hook"},
                        *kernel_request.messages,
                    ]
            if session_start_result.additional_contexts:
                hook_context = _format_hook_additional_contexts(session_start_result.additional_contexts)
                if hook_context:
                    kernel_request.system_prompt_suffix = "\n\n".join(
                        part for part in (kernel_request.system_prompt_suffix, hook_context) if part
                    )
            if session_start_result.watch_paths and request.session_context is not None:
                existing_watch_paths = list(session_metadata.get("hook_watch_paths") or [])
                session_metadata["hook_watch_paths"] = list(
                    dict.fromkeys((*existing_watch_paths, *session_start_result.watch_paths))
                )
    except Exception as _start_err:
        logging.getLogger(__name__).debug("[Invoker] SESSION_START hook failed (non-fatal): %s", _start_err)

    try:
        result = await _resolve_kernel_for_request(request).handle(kernel_request)
    except Exception as exc:
        _record_skill_runtime_usage("failed", f"{type(exc).__name__}: {exc}")
        raise

    _record_skill_runtime_usage("completed", str(result.content or ""))
    completed_messages = [*request.messages, {"role": "assistant", "content": result.content}]
    try:
        from app.runtime.context import ensure_runtime_assembly_state
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
        _hook_metadata = {
            "agent_name": request.agent_name,
            "tenant_id": session_metadata.get("tenant_id"),
            "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
            "request_id": session_metadata.get("request_id"),
            "trace_id": session_metadata.get("trace_id"),
            "turn_id": session_metadata.get("turn_id"),
            "intent_id": session_metadata.get("intent_id"),
            "turn_count": len(completed_messages),
            "reason": "invoke_return",
            "checkpoint_kind": "user_turn_stop",
            "important_files": list(getattr(request.session_context, "recent_files", []) or [])
            if request.session_context
            else [],
            "pending_work": list(getattr(request.session_context, "pending_items", []) or [])
            if request.session_context
            else [],
            "last_successful_step": result.content[:300],
            "activation_events": list(ensure_runtime_assembly_state(request.session_context).activation_events)
            if request.session_context
            else [],
        }
        await emit_hook(
            HookEvent.SESSION_END,
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=_session_source,
            messages=completed_messages,
            metadata=_hook_metadata,
        )
        if request.emit_turn_stop:
            await emit_hook(
                HookEvent.TURN_STOP,
                agent_id=request.agent_id,
                session_id=request.memory_session_id,
                source=_session_source,
                messages=completed_messages,
                metadata=_hook_metadata,
            )
    except Exception as _close_err:
        logging.getLogger(__name__).debug("[Invoker] response/session close hooks failed (non-fatal): %s", _close_err)
    return AgentInvocationResult(
        content=result.content,
        tokens_used=result.tokens_used,
        final_tools=result.final_tools,
        parts=result.parts,
        reasoning_signature=getattr(result, "reasoning_signature", None),
        terminal_reason=getattr(result, "terminal_reason", TerminalReason.TURN_STOP),
    )
