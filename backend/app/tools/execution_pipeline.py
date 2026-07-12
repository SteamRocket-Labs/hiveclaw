"""Typed, staged owner for the governed tool execution lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.tools.service import ApprovalDecisionSet, EventCallback, ToolContentEnvelope


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    tool_name: str
    arguments: dict[str, Any]
    agent_id: Any
    user_id: Any
    execution_identity: Any | None = None
    tool_call_id: str | None = None
    event_callback: EventCallback | None = None
    delegation_token: Any | None = None
    session_id: str | None = None
    permission_profile: Any | None = None
    turn_id: str | None = None
    runtime_task_id: str | None = None
    budget_run_id: str | None = None
    origin_channel: str | None = None
    round_state: dict[str, Any] | None = None
    t0_refs: tuple[str, ...] = ()
    plan_mode_interactive_available: bool = False
    plan_mode_unattended_available: bool = False
    emit_runtime_hooks: bool = True
    trace_metadata_sink: dict[str, Any] | None = None
    workspace_override: Any | None = None
    approval_decision: ApprovalDecisionSet | None = None
    expected_asset_refs: tuple[Any, ...] | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionPorts:
    decision_outcome_type: Any
    extract_error_payload: Callable[..., Any]
    inject_runtime_arguments: Callable[..., Any]
    json: Any
    latest_context_record: Callable[..., Any]
    maybe_await: Callable[..., Any]
    new_tool_call_id: Callable[..., str]
    record_final_decision: Callable[..., Any]
    record_precontext_decision: Callable[..., Any]
    record_execution_frame: Callable[..., Any]
    record_lifecycle: Callable[..., Any]
    renew_runtime_lease: Callable[..., Any]
    resolve_runtime_context: Callable[..., Any]
    tool_result_failed: Callable[..., bool]
    validate_arguments: Callable[..., Any]
    hash_tool_input: Callable[..., str]
    render_tool_error: Callable[..., str]
    asyncio: Any
    inspect: Any
    traceback: Any
    path_type: Any


@dataclass(slots=True)
class _ToolExecutionState:
    service: Any
    request: ToolExecutionRequest
    ports: ToolExecutionPorts
    tool_call_id: str
    arguments: dict[str, Any]
    runtime_context: Any | None = None
    original_arguments: dict[str, Any] | None = None
    governance_context: Any | None = None


@dataclass(frozen=True, slots=True)
class _Stop:
    value: Any


async def run_tool_execution(
    service: Any,
    request: ToolExecutionRequest,
    ports: ToolExecutionPorts,
) -> str | ToolContentEnvelope:
    """Run one governed tool call through four explicit, testable stages."""
    state = _ToolExecutionState(
        service=service,
        request=request,
        ports=ports,
        tool_call_id=request.tool_call_id or ports.new_tool_call_id(),
        arguments=dict(request.arguments or {}),
    )
    for stage in (_prepare_runtime_context, _apply_hooks_and_assets, _apply_governance):
        stopped = await stage(state)
        if stopped is not None:
            return stopped.value
    return await _execute_tool(state)


def _approval_error(state: _ToolExecutionState, *, error_class: str, message: str, hint: str) -> _Stop:
    return _Stop(
        state.ports.render_tool_error(
            tool_name=state.request.tool_name,
            error_class=error_class,
            message=message,
            provider="approval_execution_kernel",
            retryable=False,
            actionable_hint=hint,
        )
    )


def _record_precontext_block(state: _ToolExecutionState, *, outcome: Any, reason: str) -> None:
    request = state.request
    state.ports.record_precontext_decision(
        trace_metadata_sink=request.trace_metadata_sink,
        agent_id=request.agent_id,
        user_id=request.user_id,
        permission_profile=request.permission_profile,
        tool_name=request.tool_name,
        arguments=state.arguments,
        outcome=outcome,
        reason_codes=(reason,),
        tool_call_id=state.tool_call_id,
        runtime_task_id=request.runtime_task_id,
        session_id=request.session_id,
    )


async def _prepare_runtime_context(state: _ToolExecutionState) -> _Stop | None:
    request, ports, service = state.request, state.ports, state.service
    approval = request.approval_decision
    if approval is not None and (
        approval.tool_name != request.tool_name
        or approval.input_hash != ports.hash_tool_input(request.tool_name, state.arguments)
    ):
        return _approval_error(
            state,
            error_class="approval_payload_mutation",
            message="The approved tool request no longer matches its immutable approval ticket.",
            hint="Submit a new approval request for the exact current action.",
        )
    plan_block = service._interactive_plan_mode_readonly_block(request.tool_name, state.arguments)
    if plan_block:
        _record_precontext_block(state, outcome=ports.decision_outcome_type.DENY, reason="plan_mode_readonly")
        return _Stop(plan_block)
    authorization: dict[str, Any] = {}
    plan_block = await service._plan_mode_gate_block(
        request.tool_name,
        state.arguments,
        agent_id=request.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        runtime_task_id=request.runtime_task_id,
        evidence_id=f"tool:{state.tool_call_id}",
        authorization_sink=authorization,
        plan_mode_interactive_available=request.plan_mode_interactive_available,
        plan_mode_unattended_available=request.plan_mode_unattended_available,
        consumed_plan_authorization=(approval.plan_authorization if approval is not None else None),
        approval_tenant_id=str(getattr(approval, "tenant_id", "")) if approval is not None else None,
    )
    if plan_block:
        _record_precontext_block(
            state,
            outcome=ports.decision_outcome_type.REQUIRE_APPROVAL,
            reason="plan_confirmation_required",
        )
        return _Stop(plan_block)
    if authorization:
        state.arguments["_plan_authorization"] = authorization
        if isinstance(request.trace_metadata_sink, dict):
            request.trace_metadata_sink["plan_authorization"] = dict(authorization)
    state.runtime_context = await ports.resolve_runtime_context(
        service.runtime_resolver,
        agent_id=request.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        permission_profile=request.permission_profile,
        turn_id=request.turn_id,
        runtime_task_id=request.runtime_task_id,
        budget_run_id=request.budget_run_id,
        origin_channel=request.origin_channel,
        round_state=request.round_state,
        t0_refs=request.t0_refs,
    )
    _configure_runtime_context(state)
    state.original_arguments = dict(state.arguments)
    _record_lifecycle(state, "created")
    state.arguments = ports.inject_runtime_arguments(request.tool_name, state.arguments, state.runtime_context)
    if approval is not None and approval.input_hash != ports.hash_tool_input(request.tool_name, state.arguments):
        _record_lifecycle(state, "blocked", "approval_payload_mutation")
        return _approval_error(
            state,
            error_class="approval_payload_mutation",
            message="Runtime context injection changed the immutable approved request.",
            hint="Submit a new approval request from the current runtime context.",
        )
    return None


def _configure_runtime_context(state: _ToolExecutionState) -> None:
    request, context = state.request, state.runtime_context
    context.delegation_token = request.delegation_token
    if request.execution_identity is not None:
        context.execution_identity = request.execution_identity
    context.approval_decision = request.approval_decision
    context.emit_runtime_hooks = request.emit_runtime_hooks
    context.plan_mode_interactive_available = request.plan_mode_interactive_available
    context.plan_mode_unattended_available = request.plan_mode_unattended_available
    if request.workspace_override is not None:
        context.workspace = state.ports.path_type(request.workspace_override)


def _record_lifecycle(state: _ToolExecutionState, status: str, *decisions: str) -> None:
    state.ports.record_lifecycle(
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        tool_name=state.request.tool_name,
        state=status,
        original_arguments=state.original_arguments or dict(state.arguments),
        effective_arguments=dict(state.arguments),
        governance_decisions=decisions,
    )


def _record_final_decision(
    state: _ToolExecutionState,
    *,
    outcome: Any,
    reasons: tuple[str, ...],
) -> None:
    state.ports.record_final_decision(
        trace_metadata_sink=state.request.trace_metadata_sink,
        runtime_context=state.runtime_context,
        tool_name=state.request.tool_name,
        arguments=state.arguments,
        outcome=outcome,
        reason_codes=reasons,
        tool_call_id=state.tool_call_id,
        governance_context=state.governance_context,
    )


async def _apply_hooks_and_assets(state: _ToolExecutionState) -> _Stop | None:
    request, ports, service = state.request, state.ports, state.service
    if request.emit_runtime_hooks:
        hook_result = await service._emit_pre_tool_hook(
            request.tool_name,
            state.arguments,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
        if hook_result and hook_result.block:
            _record_lifecycle(state, "blocked", "pre_tool_hook_block")
            _record_final_decision(
                state,
                outcome=ports.decision_outcome_type.DENY,
                reasons=("pre_tool_hook_block",),
            )
            if request.approval_decision is not None:
                return _approval_error(
                    state,
                    error_class="approval_hook_block",
                    message=(
                        "Approved execution was blocked by the current PRE_TOOL_USE hook: "
                        f"{hook_result.reason or 'policy'}"
                    ),
                    hint="Review the changed hook or submit a new approval request.",
                )
            return _Stop("Blocked by hook: " + (hook_result.reason or "policy"))
        if hook_result and hook_result.modified_args is not None:
            modified = dict(hook_result.modified_args)
            approval = request.approval_decision
            if approval is not None and approval.input_hash != ports.hash_tool_input(request.tool_name, modified):
                return _approval_error(
                    state,
                    error_class="approval_payload_mutation",
                    message="PRE_TOOL_USE cannot change an immutable approved request.",
                    hint="Submit a new approval request for the modified action.",
                )
            state.arguments = modified
    validation_block = ports.validate_arguments(request.tool_name, state.arguments)
    if validation_block:
        _record_lifecycle(state, "blocked", "validate_input_block")
        _record_final_decision(
            state,
            outcome=ports.decision_outcome_type.DENY,
            reasons=("invalid_tool_arguments",),
        )
        return _Stop(validation_block)
    _record_lifecycle(state, "validated")
    asset_block = await _resolve_assets(state)
    if asset_block is not None:
        return asset_block
    l2_block = await service._l2_extension_policy_block(request.tool_name, state.runtime_context)
    if l2_block:
        _record_lifecycle(state, "blocked", "l2_policy_block")
        _record_final_decision(
            state,
            outcome=ports.decision_outcome_type.DENY,
            reasons=("l2_policy_block",),
        )
        return _Stop(l2_block)
    return None


async def _resolve_assets(state: _ToolExecutionState) -> _Stop | None:
    from app.services.ai_asset_resolution import resolved_asset_refs_match, resolve_tool_asset_refs

    request, service = state.request, state.service
    resolver = service.asset_ref_resolver or resolve_tool_asset_refs
    refs = await state.ports.maybe_await(
        resolver(tool_name=request.tool_name, arguments=dict(state.arguments), context=state.runtime_context)
    )
    state.runtime_context.resolved_asset_refs = tuple(refs or ())
    if request.expected_asset_refs is None or resolved_asset_refs_match(
        request.expected_asset_refs,
        state.runtime_context.resolved_asset_refs,
    ):
        return None
    _record_lifecycle(state, "blocked", "approval_asset_revision_drift")
    return _approval_error(
        state,
        error_class="approval_asset_revision_drift",
        message="The approved AI asset revision is no longer the active resolved revision.",
        hint="Review the new asset revision and submit a new approval request.",
    )


async def _apply_governance(state: _ToolExecutionState) -> _Stop | None:
    request, ports, service = state.request, state.ports, state.service
    kwargs: dict[str, Any] = {
        "runtime_context": state.runtime_context,
        "tool_name": request.tool_name,
        "arguments": state.arguments,
        "delegation_token": request.delegation_token,
    }
    try:
        params = ports.inspect.signature(service.governance_resolver.build_context).parameters
        accepts_kwargs = any(param.kind == ports.inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        params, accepts_kwargs = {}, False
    if accepts_kwargs or "tool_call_id" in params:
        kwargs["tool_call_id"] = state.tool_call_id
    state.governance_context = await service.governance_resolver.build_context(**kwargs)
    dependencies = service.governance_resolver.build_dependencies()
    block = await ports.maybe_await(
        service.governance_runner(
            state.governance_context,
            dependencies,
            event_callback=request.event_callback,
        )
    )
    if block:
        _record_lifecycle(state, "blocked", "governance_block")
        outcome = (
            ports.decision_outcome_type.REQUIRE_APPROVAL
            if "approval_required" in str(block) or "session_permission_required" in str(block)
            else ports.decision_outcome_type.DENY
        )
        _record_final_decision(state, outcome=outcome, reasons=("governance_block",))
        return _Stop(block)
    _record_lifecycle(state, "governed")
    preflight_block = await service._preflight_tool_execution(
        request.tool_name,
        state.arguments,
        state.runtime_context,
        trace_metadata_sink=request.trace_metadata_sink,
    )
    if preflight_block:
        _record_lifecycle(state, "blocked", "preflight_block")
        outcome = (
            ports.decision_outcome_type.REQUIRE_APPROVAL
            if "ASK" in str(preflight_block) or "PREPARE_ONLY" in str(preflight_block)
            else ports.decision_outcome_type.DENY
        )
        _record_final_decision(state, outcome=outcome, reasons=("action_preflight_block",))
        return _Stop(preflight_block)
    _record_lifecycle(state, "preflight")
    _record_final_decision(
        state,
        outcome=ports.decision_outcome_type.ALLOW,
        reasons=("governance_allow", "preflight_allow"),
    )
    return None


async def _execute_tool(state: _ToolExecutionState) -> Any:
    from app.tools.registry import tool_execution_policy

    request, ports, service = state.request, state.ports, state.service
    timeout_seconds = tool_execution_policy(request.tool_name).timeout_seconds
    try:
        await ports.renew_runtime_lease()
        _record_lifecycle(state, "executing")
        result = await ports.asyncio.wait_for(
            service.execute_with_context(
                request.tool_name,
                state.arguments,
                state.runtime_context,
                trace_metadata_sink=request.trace_metadata_sink,
            ),
            timeout=timeout_seconds,
        )
        result_text = str(result)
        error_payload = ports.extract_error_payload(result_text)
        failed = ports.tool_result_failed(result)
        if failed and error_payload is None:
            error_payload = {"error_class": "legacy_tool_error", "message": result_text[:500], "retryable": False}
        _record_lifecycle(state, "failed" if failed else "completed")
        await _log_execution_result(state, result_text, error_payload)
        rewritten = await _run_result_hooks(state, result_text, failed)
        return rewritten.value if rewritten is not None else result
    except ports.asyncio.TimeoutError:
        return await _handle_execution_failure(state, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return await _handle_execution_failure(state, exception=exc)


async def _log_execution_result(
    state: _ToolExecutionState,
    result_text: str,
    error_payload: dict[str, Any] | None,
) -> None:
    service, request, ports = state.service, state.request, state.ports
    if not service.activity_logger:
        return
    approval = request.approval_decision
    approval_detail = (
        {
            "approved": True,
            "requested_by_user_id": str(approval.requested_by_user_id),
            "approved_by_user_id": str(approval.approved_by_user_id),
            "approval_id": str(approval.approval_id),
            "input_hash": approval.input_hash,
            "policy_snapshot_hash": approval.policy_snapshot_hash,
            "decision_id": approval.decision_id,
        }
        if approval is not None
        else {}
    )
    await ports.maybe_await(
        service.activity_logger(
            request.agent_id,
            "tool_call_approved" if approval is not None else "tool_call",
            f"{'Approved-executed' if approval is not None else 'Called tool'} {request.tool_name}: {result_text[:80]}",
            tenant_id=state.runtime_context.tenant_id,
            owner_user_id=state.runtime_context.user_id,
            root_session_id=state.runtime_context.session_id,
            detail={
                "tool": request.tool_name,
                "backend": service.backend.name if service.backend else "unknown",
                "args": {
                    key: (
                        ports.json.dumps(value, ensure_ascii=False, default=str)[:100]
                        if isinstance(value, (dict, list))
                        else str(value)[:100]
                    )
                    for key, value in state.arguments.items()
                },
                "result": result_text[:300],
                "tool_call_lifecycle": ports.latest_context_record(state.runtime_context, "tool_lifecycle_records"),
                "tool_execution_frame": ports.latest_context_record(state.runtime_context, "tool_execution_frames"),
                **approval_detail,
            },
        )
    )
    if error_payload:
        await ports.maybe_await(
            service.activity_logger(
                request.agent_id,
                "error",
                f"Tool {request.tool_name} failed: {error_payload.get('error_class', 'unknown')}",
                tenant_id=state.runtime_context.tenant_id,
                owner_user_id=state.runtime_context.user_id,
                root_session_id=state.runtime_context.session_id,
                detail=error_payload,
            )
        )


async def _run_result_hooks(state: _ToolExecutionState, result_text: str, failed: bool) -> _Stop | None:
    request, service, ports = state.request, state.service, state.ports
    if not request.emit_runtime_hooks:
        return None
    if failed:
        await service._emit_tool_failure_hook(
            request.tool_name,
            state.arguments,
            result_text,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
        return None
    hook_result = await service._emit_post_tool_hook(
        request.tool_name,
        state.arguments,
        result_text,
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        source="tool_runtime_service",
    )
    if hook_result and hook_result.output_rewrite is not None:
        rewrite = hook_result.output_rewrite
        return _Stop(
            rewrite if isinstance(rewrite, str) else ports.json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
        )
    return None


async def _handle_execution_failure(
    state: _ToolExecutionState,
    *,
    timeout_seconds: float | None = None,
    exception: Exception | None = None,
) -> str:
    request, ports, service = state.request, state.ports, state.service
    is_timeout = timeout_seconds is not None
    if exception is not None:
        ports.traceback.print_exc()
    error_class = "timeout" if is_timeout else "tool_execution_error"
    message = (
        f"{request.tool_name} exceeded the {int(timeout_seconds or 0)} second time limit."
        if is_timeout
        else f"{request.tool_name} failed with {type(exception).__name__}: {str(exception)[:500]}"
    )
    rendered = ports.render_tool_error(
        tool_name=request.tool_name,
        error_class=error_class,
        message=message,
        provider="runtime",
        retryable=is_timeout,
        actionable_hint=(
            "Try a simpler request, smaller input, or a more targeted operation."
            if is_timeout
            else "Check tool arguments and try again with simpler or better-scoped input."
        ),
    )
    _record_lifecycle(state, "failed", error_class)
    ports.record_execution_frame(
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        tool_name=request.tool_name,
        executor=service.backend.name if service.backend else "unknown",
        arguments=state.arguments,
        status="failed",
        result={"error": error_class if is_timeout else type(exception).__name__, "message": message[:500]},
        trace_metadata_sink=request.trace_metadata_sink,
    )
    if request.emit_runtime_hooks:
        await service._emit_tool_failure_hook(
            request.tool_name,
            state.arguments,
            rendered,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
    await _log_failure_activity(state, error_class=error_class, retryable=is_timeout, exception=exception)
    return rendered


async def _log_failure_activity(
    state: _ToolExecutionState,
    *,
    error_class: str,
    retryable: bool,
    exception: Exception | None,
) -> None:
    service, request, ports = state.service, state.request, state.ports
    if not service.activity_logger:
        return
    detail = {
        "tool_name": request.tool_name,
        "error_class": error_class,
        "retryable": retryable,
        "provider": "runtime",
    }
    if exception is not None:
        detail["exception_type"] = type(exception).__name__
    await ports.maybe_await(
        service.activity_logger(
            request.agent_id,
            "error",
            f"Tool {request.tool_name} {'timed out' if retryable else f'failed with {type(exception).__name__}'}",
            tenant_id=state.runtime_context.tenant_id,
            owner_user_id=state.runtime_context.user_id,
            root_session_id=state.runtime_context.session_id,
            detail=detail,
        )
    )
