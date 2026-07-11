"""Owns the single governed tool execution pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.service import (
        ApprovalDecisionSet,
        EventCallback,
        Path,
        ToolContentEnvelope,
        uuid,
    )


async def run_tool_execution(
    self,
    tool_name: str,
    arguments: dict,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    execution_identity: Any | None = None,
    tool_call_id: str | None = None,
    event_callback: EventCallback | None = None,
    delegation_token: Any | None = None,
    session_id: str | None = None,
    permission_profile: Any | None = None,
    turn_id: str | None = None,
    runtime_task_id: str | None = None,
    budget_run_id: str | None = None,
    origin_channel: str | None = None,
    round_state: dict[str, Any] | None = None,
    t0_refs: tuple[str, ...] = (),
    plan_mode_interactive_available: bool = False,
    plan_mode_unattended_available: bool = False,
    emit_runtime_hooks: bool = True,
    trace_metadata_sink: dict[str, Any] | None = None,
    workspace_override: Path | str | None = None,
    _approval_decision: ApprovalDecisionSet | None = None,
    _expected_asset_refs: tuple[Any, ...] | None = None,
    support: Any,
) -> str | ToolContentEnvelope:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    ToolDecisionOutcome = support.ToolDecisionOutcome
    _extract_tool_error_payload = support._extract_tool_error_payload
    _inject_runtime_context_arguments = support._inject_runtime_context_arguments
    _json = support._json
    _latest_context_record = support._latest_context_record
    _maybe_await = support._maybe_await
    _new_runtime_tool_call_id = support._new_runtime_tool_call_id
    _record_final_tool_decision = support._record_final_tool_decision
    _record_precontext_tool_decision = support._record_precontext_tool_decision
    _record_tool_execution_frame = support._record_tool_execution_frame
    _record_tool_lifecycle = support._record_tool_lifecycle
    _renew_runtime_task_lease_before_execution = support._renew_runtime_task_lease_before_execution
    _resolve_runtime_context = support._resolve_runtime_context
    _tool_result_failed = support._tool_result_failed
    _validate_tool_arguments_block = support._validate_tool_arguments_block
    asyncio = support.asyncio
    hash_tool_input = support.hash_tool_input
    inspect = support.inspect
    render_tool_error = support.render_tool_error
    traceback = support.traceback
    Path = support.Path

    effective_tool_call_id = tool_call_id or _new_runtime_tool_call_id()
    if _approval_decision is not None:
        if _approval_decision.tool_name != tool_name or _approval_decision.input_hash != hash_tool_input(
            tool_name, dict(arguments or {})
        ):
            return render_tool_error(
                tool_name=tool_name,
                error_class="approval_payload_mutation",
                message="The approved tool request no longer matches its immutable approval ticket.",
                provider="approval_execution_kernel",
                retryable=False,
                actionable_hint="Submit a new approval request for the exact current action.",
            )
    plan_mode_block = self._interactive_plan_mode_readonly_block(tool_name, arguments)
    if plan_mode_block:
        _record_precontext_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            agent_id=agent_id,
            user_id=user_id,
            permission_profile=permission_profile,
            tool_name=tool_name,
            arguments=arguments,
            outcome=ToolDecisionOutcome.DENY,
            reason_codes=("plan_mode_readonly",),
            tool_call_id=effective_tool_call_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
        )
        return plan_mode_block

    plan_authorization: dict[str, Any] = {}
    plan_block = await self._plan_mode_gate_block(
        tool_name,
        arguments,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        evidence_id=f"tool:{effective_tool_call_id}",
        authorization_sink=plan_authorization,
        plan_mode_interactive_available=plan_mode_interactive_available,
        plan_mode_unattended_available=plan_mode_unattended_available,
        consumed_plan_authorization=(_approval_decision.plan_authorization if _approval_decision is not None else None),
        approval_tenant_id=(
            str(getattr(_approval_decision, "tenant_id", "")) if _approval_decision is not None else None
        ),
    )
    if plan_block:
        _record_precontext_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            agent_id=agent_id,
            user_id=user_id,
            permission_profile=permission_profile,
            tool_name=tool_name,
            arguments=arguments,
            outcome=ToolDecisionOutcome.REQUIRE_APPROVAL,
            reason_codes=("plan_confirmation_required",),
            tool_call_id=effective_tool_call_id,
            runtime_task_id=runtime_task_id,
            session_id=session_id,
        )
        return plan_block
    if plan_authorization:
        arguments = {**dict(arguments or {}), "_plan_authorization": plan_authorization}
        if isinstance(trace_metadata_sink, dict):
            trace_metadata_sink["plan_authorization"] = dict(plan_authorization)

    runtime_context = await _resolve_runtime_context(
        self.runtime_resolver,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        permission_profile=permission_profile,
        turn_id=turn_id,
        runtime_task_id=runtime_task_id,
        budget_run_id=budget_run_id,
        origin_channel=origin_channel,
        round_state=round_state,
        t0_refs=t0_refs,
    )
    runtime_context.delegation_token = delegation_token
    if execution_identity is not None:
        runtime_context.execution_identity = execution_identity
    runtime_context.approval_decision = _approval_decision
    runtime_context.emit_runtime_hooks = emit_runtime_hooks
    runtime_context.plan_mode_interactive_available = plan_mode_interactive_available
    runtime_context.plan_mode_unattended_available = plan_mode_unattended_available
    if workspace_override is not None:
        runtime_context.workspace = Path(workspace_override)
    original_arguments = dict(arguments or {})
    _record_tool_lifecycle(
        runtime_context,
        tool_call_id=effective_tool_call_id,
        tool_name=tool_name,
        state="created",
        original_arguments=original_arguments,
        effective_arguments=dict(arguments or {}),
    )
    arguments = _inject_runtime_context_arguments(tool_name, arguments, runtime_context)
    if _approval_decision is not None and _approval_decision.input_hash != hash_tool_input(
        tool_name, dict(arguments or {})
    ):
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("approval_payload_mutation",),
        )
        return render_tool_error(
            tool_name=tool_name,
            error_class="approval_payload_mutation",
            message="Runtime context injection changed the immutable approved request.",
            provider="approval_execution_kernel",
            retryable=False,
            actionable_hint="Submit a new approval request from the current runtime context.",
        )
    if emit_runtime_hooks:
        hook_result = await self._emit_pre_tool_hook(
            tool_name,
            arguments,
            runtime_context,
            tool_call_id=effective_tool_call_id,
            source="tool_runtime_service",
        )
        if hook_result and hook_result.block:
            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="blocked",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
                governance_decisions=("pre_tool_hook_block",),
            )
            _record_final_tool_decision(
                trace_metadata_sink=trace_metadata_sink,
                runtime_context=runtime_context,
                tool_name=tool_name,
                arguments=arguments,
                outcome=ToolDecisionOutcome.DENY,
                reason_codes=("pre_tool_hook_block",),
                tool_call_id=effective_tool_call_id,
            )
            if _approval_decision is not None:
                return render_tool_error(
                    tool_name=tool_name,
                    error_class="approval_hook_block",
                    message=(
                        "Approved execution was blocked by the current PRE_TOOL_USE hook: "
                        f"{hook_result.reason or 'policy'}"
                    ),
                    provider="approval_execution_kernel",
                    retryable=False,
                    actionable_hint="Review the changed hook or submit a new approval request.",
                )
            return "Blocked by hook: " + (hook_result.reason or "policy")
        if hook_result and hook_result.modified_args is not None:
            modified_arguments = dict(hook_result.modified_args)
            if _approval_decision is not None and _approval_decision.input_hash != hash_tool_input(
                tool_name, modified_arguments
            ):
                return render_tool_error(
                    tool_name=tool_name,
                    error_class="approval_payload_mutation",
                    message="PRE_TOOL_USE cannot change an immutable approved request.",
                    provider="approval_execution_kernel",
                    retryable=False,
                    actionable_hint="Submit a new approval request for the modified action.",
                )
            arguments = modified_arguments
    validation_block = _validate_tool_arguments_block(tool_name, arguments)
    if validation_block:
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("validate_input_block",),
        )
        _record_final_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            outcome=ToolDecisionOutcome.DENY,
            reason_codes=("invalid_tool_arguments",),
            tool_call_id=effective_tool_call_id,
        )
        return validation_block
    _record_tool_lifecycle(
        runtime_context,
        tool_call_id=effective_tool_call_id,
        tool_name=tool_name,
        state="validated",
        original_arguments=original_arguments,
        effective_arguments=dict(arguments or {}),
    )
    from app.services.ai_asset_resolution import resolved_asset_refs_match, resolve_tool_asset_refs

    resolver = self.asset_ref_resolver or resolve_tool_asset_refs
    resolved_refs = await _maybe_await(
        resolver(tool_name=tool_name, arguments=dict(arguments or {}), context=runtime_context)
    )
    runtime_context.resolved_asset_refs = tuple(resolved_refs or ())
    if _expected_asset_refs is not None and not resolved_asset_refs_match(
        tuple(_expected_asset_refs), runtime_context.resolved_asset_refs
    ):
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("approval_asset_revision_drift",),
        )
        return render_tool_error(
            tool_name=tool_name,
            error_class="approval_asset_revision_drift",
            message="The approved AI asset revision is no longer the active resolved revision.",
            provider="approval_execution_kernel",
            retryable=False,
            actionable_hint="Review the new asset revision and submit a new approval request.",
        )
    l2_policy_block = await self._l2_extension_policy_block(tool_name, runtime_context)
    if l2_policy_block:
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("l2_policy_block",),
        )
        _record_final_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            outcome=ToolDecisionOutcome.DENY,
            reason_codes=("l2_policy_block",),
            tool_call_id=effective_tool_call_id,
        )
        return l2_policy_block
    governance_kwargs: dict[str, Any] = {
        "runtime_context": runtime_context,
        "tool_name": tool_name,
        "arguments": arguments,
        "delegation_token": delegation_token,
    }
    try:
        params = inspect.signature(self.governance_resolver.build_context).parameters
        accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        params = {}
        accepts_kwargs = False
    if effective_tool_call_id is not None and (accepts_kwargs or "tool_call_id" in params):
        governance_kwargs["tool_call_id"] = effective_tool_call_id
    governance_context = await self.governance_resolver.build_context(**governance_kwargs)
    governance_dependencies = self.governance_resolver.build_dependencies()
    governance_block = await _maybe_await(
        self.governance_runner(
            governance_context,
            governance_dependencies,
            event_callback=event_callback,
        )
    )
    if governance_block:
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("governance_block",),
        )
        governance_outcome = (
            ToolDecisionOutcome.REQUIRE_APPROVAL
            if "approval_required" in str(governance_block) or "session_permission_required" in str(governance_block)
            else ToolDecisionOutcome.DENY
        )
        _record_final_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            outcome=governance_outcome,
            reason_codes=("governance_block",),
            tool_call_id=effective_tool_call_id,
            governance_context=governance_context,
        )
        return governance_block
    _record_tool_lifecycle(
        runtime_context,
        tool_call_id=effective_tool_call_id,
        tool_name=tool_name,
        state="governed",
        original_arguments=original_arguments,
        effective_arguments=dict(arguments or {}),
    )

    preflight_block = await self._preflight_tool_execution(
        tool_name,
        arguments,
        runtime_context,
        trace_metadata_sink=trace_metadata_sink,
    )
    if preflight_block:
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="blocked",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("preflight_block",),
        )
        preflight_outcome = (
            ToolDecisionOutcome.REQUIRE_APPROVAL
            if "ASK" in str(preflight_block) or "PREPARE_ONLY" in str(preflight_block)
            else ToolDecisionOutcome.DENY
        )
        _record_final_tool_decision(
            trace_metadata_sink=trace_metadata_sink,
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            outcome=preflight_outcome,
            reason_codes=("action_preflight_block",),
            tool_call_id=effective_tool_call_id,
            governance_context=governance_context,
        )
        return preflight_block
    _record_tool_lifecycle(
        runtime_context,
        tool_call_id=effective_tool_call_id,
        tool_name=tool_name,
        state="preflight",
        original_arguments=original_arguments,
        effective_arguments=dict(arguments or {}),
    )

    _record_final_tool_decision(
        trace_metadata_sink=trace_metadata_sink,
        runtime_context=runtime_context,
        tool_name=tool_name,
        arguments=arguments,
        outcome=ToolDecisionOutcome.ALLOW,
        reason_codes=("governance_allow", "preflight_allow"),
        tool_call_id=effective_tool_call_id,
        governance_context=governance_context,
    )
    from app.tools.registry import tool_execution_policy

    timeout_seconds = tool_execution_policy(tool_name).timeout_seconds
    try:
        await _renew_runtime_task_lease_before_execution()
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="executing",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
        )
        result = await asyncio.wait_for(
            self.execute_with_context(
                tool_name,
                arguments,
                runtime_context,
                trace_metadata_sink=trace_metadata_sink,
            ),
            timeout=timeout_seconds,
        )
        # result may be a ToolContentEnvelope — use its text rendering for
        # logging / error extraction, but return the value untouched.
        result_text = str(result)
        tool_error_payload = _extract_tool_error_payload(result_text)
        tool_failed = _tool_result_failed(result)
        if tool_failed and tool_error_payload is None:
            tool_error_payload = {
                "error_class": "legacy_tool_error",
                "message": result_text[:500],
                "retryable": False,
            }
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="failed" if tool_failed else "completed",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
        )
        if self.activity_logger:
            approval_detail = (
                {
                    "approved": True,
                    "requested_by_user_id": str(_approval_decision.requested_by_user_id),
                    "approved_by_user_id": str(_approval_decision.approved_by_user_id),
                    "approval_id": str(_approval_decision.approval_id),
                    "input_hash": _approval_decision.input_hash,
                    "policy_snapshot_hash": _approval_decision.policy_snapshot_hash,
                    "decision_id": _approval_decision.decision_id,
                }
                if _approval_decision is not None
                else {}
            )
            await _maybe_await(
                self.activity_logger(
                    agent_id,
                    "tool_call_approved" if _approval_decision is not None else "tool_call",
                    (
                        f"Approved-executed {tool_name}: {result_text[:80]}"
                        if _approval_decision is not None
                        else f"Called tool {tool_name}: {result_text[:80]}"
                    ),
                    tenant_id=runtime_context.tenant_id,
                    owner_user_id=runtime_context.user_id,
                    root_session_id=runtime_context.session_id,
                    detail={
                        "tool": tool_name,
                        "backend": self.backend.name if self.backend else "unknown",
                        "args": {
                            k: (
                                _json.dumps(v, ensure_ascii=False, default=str)[:100]
                                if isinstance(v, (dict, list))
                                else str(v)[:100]
                            )
                            for k, v in arguments.items()
                        },
                        "result": result_text[:300],
                        "tool_call_lifecycle": _latest_context_record(runtime_context, "tool_lifecycle_records"),
                        "tool_execution_frame": _latest_context_record(runtime_context, "tool_execution_frames"),
                        **approval_detail,
                    },
                )
            )
            if tool_error_payload:
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "error",
                        f"Tool {tool_name} failed: {tool_error_payload.get('error_class', 'unknown')}",
                        tenant_id=runtime_context.tenant_id,
                        owner_user_id=runtime_context.user_id,
                        root_session_id=runtime_context.session_id,
                        detail=tool_error_payload,
                    )
                )
        if emit_runtime_hooks:
            if tool_failed:
                await self._emit_tool_failure_hook(
                    tool_name,
                    arguments,
                    result_text,
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    source="tool_runtime_service",
                )
            else:
                post_hook_result = await self._emit_post_tool_hook(
                    tool_name,
                    arguments,
                    result_text,
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    source="tool_runtime_service",
                )
                if post_hook_result and post_hook_result.output_rewrite is not None:
                    rewrite = post_hook_result.output_rewrite
                    return (
                        rewrite
                        if isinstance(rewrite, str)
                        else _json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
                    )
        return result
    except asyncio.TimeoutError:
        rendered = render_tool_error(
            tool_name=tool_name,
            error_class="timeout",
            message=f"{tool_name} exceeded the {int(timeout_seconds)} second time limit.",
            provider="runtime",
            retryable=True,
            actionable_hint="Try a simpler request, smaller input, or a more targeted operation.",
        )
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="failed",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("timeout",),
        )
        _record_tool_execution_frame(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            executor=self.backend.name if self.backend else "unknown",
            arguments=arguments,
            status="failed",
            result={"error": "timeout", "message": rendered[:500]},
            trace_metadata_sink=trace_metadata_sink,
        )
        if emit_runtime_hooks:
            await self._emit_tool_failure_hook(
                tool_name,
                arguments,
                rendered,
                runtime_context,
                tool_call_id=effective_tool_call_id,
                source="tool_runtime_service",
            )
        if self.activity_logger:
            await _maybe_await(
                self.activity_logger(
                    agent_id,
                    "error",
                    f"Tool {tool_name} timed out",
                    tenant_id=runtime_context.tenant_id,
                    owner_user_id=runtime_context.user_id,
                    root_session_id=runtime_context.session_id,
                    detail={
                        "tool_name": tool_name,
                        "error_class": "timeout",
                        "retryable": True,
                        "provider": "runtime",
                    },
                )
            )
        return rendered
    except Exception as exc:
        traceback.print_exc()
        rendered = render_tool_error(
            tool_name=tool_name,
            error_class="tool_execution_error",
            message=f"{tool_name} failed with {type(exc).__name__}: {str(exc)[:500]}",
            provider="runtime",
            retryable=False,
            actionable_hint="Check tool arguments and try again with simpler or better-scoped input.",
        )
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="failed",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=("tool_execution_error",),
        )
        _record_tool_execution_frame(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            executor=self.backend.name if self.backend else "unknown",
            arguments=arguments,
            status="failed",
            result={"error": type(exc).__name__, "message": str(exc)[:500]},
            trace_metadata_sink=trace_metadata_sink,
        )
        if emit_runtime_hooks:
            await self._emit_tool_failure_hook(
                tool_name,
                arguments,
                rendered,
                runtime_context,
                tool_call_id=effective_tool_call_id,
                source="tool_runtime_service",
            )
        if self.activity_logger:
            await _maybe_await(
                self.activity_logger(
                    agent_id,
                    "error",
                    f"Tool {tool_name} failed with {type(exc).__name__}",
                    tenant_id=runtime_context.tenant_id,
                    owner_user_id=runtime_context.user_id,
                    root_session_id=runtime_context.session_id,
                    detail={
                        "tool_name": tool_name,
                        "error_class": "tool_execution_error",
                        "retryable": False,
                        "provider": "runtime",
                        "exception_type": type(exc).__name__,
                    },
                )
            )
        return rendered
