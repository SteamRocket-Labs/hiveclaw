"""Runtime service for governed tool execution."""

from __future__ import annotations

import asyncio
import inspect
import json as _json
import logging
import re
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from app.agents.coordination import CoordinationRuntime, coordination_runtime
from app.agents.coordination_gateway import CoordinationGateway, InProcessCoordinationGateway
from app.agents.coordination_wiring import gateway_scope
from app.services.action_preflight import (
    ActionPreflightInput,
    ActionPreflightResult,
    ActionPreflightService,
    BoundaryAxisLevel,
    CharterZone,
    PreflightDecision,
)
from app.services.decision_trace import DecisionTraceStore
from app.services.plan_mode_gate import PlanModeGate, get_plan_mode_gate
from app.services.privacy_layer import PrivacyLayer
from app.tools.governance import EventCallback, GovernanceDependencies, ToolGovernanceContext
from app.tools.plan_gate_registry import hard_gated_action_kind
from app.tools.result_envelope import render_tool_error
from app.tools.runtime import ToolExecutionContext, ToolExecutionRegistry, ToolExecutionRequest
from app.tools.backends import LocalToolRuntimeBackend, ToolRuntimeBackend


RuntimeResolver = Callable[..., Awaitable[ToolExecutionContext] | ToolExecutionContext]
GovernanceRunner = Callable[
    [ToolGovernanceContext, GovernanceDependencies],
    Awaitable[str | None] | str | None,
]
FallbackExecutor = Callable[[str, dict, ToolExecutionContext], Awaitable[str] | str]
ActivityLogger = Callable[..., Awaitable[None] | None]
EnsureRegistry = Callable[[], None]

_TOOL_ERROR_PAYLOAD_RE = re.compile(r"<tool_error>(.*?)</tool_error>", re.DOTALL)
_EXTERNAL_VISIBLE_TOOLS = frozenset(
    {
        "send_feishu_message",
        "send_web_message",
        "send_email",
        "reply_email",
        "plaza_create_post",
        "plaza_add_comment",
    }
)
_DELEGATED_USER_AUTHORIZED_TOOLS = frozenset(
    {
        "send_feishu_message",
    }
)
_COMPANY_CONFLICT_PATTERNS = (
    "bypass company policy",
    "share credentials",
    "expose pl3",
    "expose pl4",
)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _extract_tool_error_payload(result: str) -> dict[str, Any] | None:
    if not result or "<tool_error>" not in result:
        return None
    match = _TOOL_ERROR_PAYLOAD_RE.search(result)
    if not match:
        return None
    try:
        return _json.loads(match.group(1))
    except Exception:
        return None


@dataclass(slots=True)
class ToolRuntimeService:
    runtime_resolver: Any
    governance_resolver: Any
    registry: ToolExecutionRegistry
    ensure_registry: EnsureRegistry
    governance_runner: Callable[..., Awaitable[str | None] | str | None]
    fallback_executor: FallbackExecutor
    direct_fallback_executor: FallbackExecutor
    activity_logger: ActivityLogger | None = None
    backend: ToolRuntimeBackend | None = None
    preflight_service: ActionPreflightService | None = None
    decision_trace_store: DecisionTraceStore | None = None
    coordination_runtime: CoordinationRuntime | None = None
    coordination_gateway: CoordinationGateway | None = None
    preflight_enabled: bool = True
    # Plan Mode early-intercept gate (docs/plan-mode-design.md §9.2). The gate is
    # read-only and stateless; the session factory opens a short-lived async
    # session for the by-id plan lookup. Both are DI seams so tests can inject
    # fakes (the gate is otherwise the shared singleton).
    plan_mode_gate: PlanModeGate | None = None
    plan_mode_session_factory: Callable[[], Any] | None = None
    # Plan Mode intake service (§9.2 "intercept-then-create"). When a tagged tool
    # is blocked with no confirmed plan, the gate seeds a confirmable awaiting
    # PlanRequest from the tool's own args via ``ensure_awaiting_plan`` and embeds
    # plan_id/json/version/hash into the needs_plan payload. DI seam for tests;
    # otherwise the shared singleton (the one handoffs register on).
    plan_mode_service: Any | None = None

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = LocalToolRuntimeBackend()
        if self.preflight_service is None:
            self.preflight_service = ActionPreflightService()
        if self.decision_trace_store is None:
            self.decision_trace_store = DecisionTraceStore()
        if self.coordination_runtime is None:
            self.coordination_runtime = coordination_runtime
        if self.coordination_gateway is None:
            self.coordination_gateway = InProcessCoordinationGateway(self.coordination_runtime)
        if self.plan_mode_gate is None:
            self.plan_mode_gate = get_plan_mode_gate()
        if self.plan_mode_session_factory is None:
            from app.database import async_session

            self.plan_mode_session_factory = async_session
        if self.plan_mode_service is None:
            from app.services.plan_mode_service import get_plan_mode_service

            self.plan_mode_service = get_plan_mode_service()

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        event_callback: EventCallback | None = None,
        delegation_token: Any | None = None,
    ) -> str:
        plan_block = await self._plan_mode_gate_block(tool_name, arguments, agent_id=agent_id)
        if plan_block:
            return plan_block

        runtime_context = await self.runtime_resolver.resolve(agent_id=agent_id, user_id=user_id)
        governance_context = await self.governance_resolver.build_context(
            runtime_context=runtime_context,
            tool_name=tool_name,
            arguments=arguments,
            delegation_token=delegation_token,
        )
        governance_dependencies = self.governance_resolver.build_dependencies()
        governance_block = await _maybe_await(
            self.governance_runner(
                governance_context,
                governance_dependencies,
                event_callback=event_callback,
            )
        )
        if governance_block:
            return governance_block

        preflight_block = await self._preflight_tool_execution(tool_name, arguments, runtime_context)
        if preflight_block:
            return preflight_block

        _TOOL_TIMEOUTS: dict[str, float] = {
            "execute_code": 120.0,
            "run_command": 120.0,
            "create_digital_employee": 120.0,
            "web_fetch": 60.0,
            "web_search": 60.0,
            "firecrawl_fetch": 60.0,
            "xcrawl_scrape": 60.0,
            "read_document": 60.0,
            "send_feishu_message": 45.0,
            "feishu_doc_read": 45.0,
            "feishu_wiki_read": 45.0,
        }
        timeout_seconds = _TOOL_TIMEOUTS.get(tool_name, 30.0)
        try:
            result = await asyncio.wait_for(
                self.execute_with_context(tool_name, arguments, runtime_context),
                timeout=timeout_seconds,
            )
            tool_error_payload = _extract_tool_error_payload(result)
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "tool_call",
                        f"Called tool {tool_name}: {result[:80]}",
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
                            "result": result[:300],
                        },
                    )
                )
                if tool_error_payload:
                    await _maybe_await(
                        self.activity_logger(
                            agent_id,
                            "error",
                            f"Tool {tool_name} failed: {tool_error_payload.get('error_class', 'unknown')}",
                            detail=tool_error_payload,
                        )
                    )
            return result
        except asyncio.TimeoutError:
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "error",
                        f"Tool {tool_name} timed out",
                        detail={
                            "tool_name": tool_name,
                            "error_class": "timeout",
                            "retryable": True,
                            "provider": "runtime",
                        },
                    )
                )
            return render_tool_error(
                tool_name=tool_name,
                error_class="timeout",
                message=f"{tool_name} exceeded the {int(timeout_seconds)} second time limit.",
                provider="runtime",
                retryable=True,
                actionable_hint="Try a simpler request, smaller input, or a more targeted operation.",
            )
        except Exception as exc:
            traceback.print_exc()
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "error",
                        f"Tool {tool_name} failed with {type(exc).__name__}",
                        detail={
                            "tool_name": tool_name,
                            "error_class": "tool_execution_error",
                            "retryable": False,
                            "provider": "runtime",
                            "exception_type": type(exc).__name__,
                        },
                    )
                )
            return render_tool_error(
                tool_name=tool_name,
                error_class="tool_execution_error",
                message=f"{tool_name} failed with {type(exc).__name__}: {str(exc)[:500]}",
                provider="runtime",
                retryable=False,
                actionable_hint="Check tool arguments and try again with simpler or better-scoped input.",
            )

    async def execute_direct(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> str:
        """Execute a tool after approval, with basic validation.

        Governance is intentionally skipped (approval already granted), but
        we validate the tool exists and log the execution for audit.
        """
        return await self._execute_without_governance(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=user_id,
            activity_type="tool_call_direct",
            activity_detail={"approved": True},
            log_label="execute_direct",
        )

    async def execute_approved(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        approved_by_user_id: uuid.UUID | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> str:
        """Execute a tool after a recorded approval decision.

        This is the public post-approval entrypoint. It skips governance
        preflight because the approval decision is the governance result, but
        keeps execution inside ToolRuntimeService for validation and audit.
        """
        detail = {
            "approved": True,
            "approved_by_user_id": str(approved_by_user_id) if approved_by_user_id else None,
            "approval_id": str(approval_id) if approval_id else None,
        }
        return await self._execute_without_governance(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=approved_by_user_id,
            activity_type="tool_call_approved",
            activity_detail=detail,
            log_label="execute_approved",
        )

    async def _execute_without_governance(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        activity_type: str,
        activity_detail: dict[str, Any],
        log_label: str,
    ) -> str:
        _logger = logging.getLogger(__name__)

        # Plan Mode early-intercept (§9.2) fires here so both execute_direct and
        # execute_approved are covered — execute_approved must NOT be a bypass.
        plan_block = await self._plan_mode_gate_block(tool_name, arguments, agent_id=agent_id)
        if plan_block:
            return plan_block

        self.ensure_registry()

        resolved_user_id = user_id or agent_id
        _logger.info("[ToolService] %s: tool=%s agent=%s user=%s", log_label, tool_name, agent_id, resolved_user_id)

        runtime_context = await self.runtime_resolver.resolve(agent_id=agent_id, user_id=resolved_user_id)
        try:
            request = ToolExecutionRequest(
                tool_name=tool_name,
                arguments=arguments,
                context=runtime_context,
            )

            async def _execute_approved_request(inner_request: ToolExecutionRequest) -> str:
                direct_result = await _maybe_await(self.registry.try_execute(inner_request))
                if direct_result is not None:
                    return direct_result
                return await _maybe_await(
                    self.direct_fallback_executor(
                        inner_request.tool_name,
                        inner_request.arguments,
                        inner_request.context,
                    )
                )

            result = await self.backend.execute(request, _execute_approved_request)
            # Activity log for audit trail (mirrors execute() behavior)
            if self.activity_logger and tool_name not in ("list_files", "read_file", "read_document"):
                try:
                    detail = {
                        "tool": tool_name,
                        "backend": self.backend.name if self.backend else "unknown",
                        "result": result[:300],
                        **activity_detail,
                    }
                    await _maybe_await(
                        self.activity_logger(
                            agent_id,
                            activity_type,
                            f"Approved-executed {tool_name}: {result[:80]}",
                            detail=detail,
                        )
                    )
                except Exception as _log_err:
                    _logger.warning("[ToolService] Activity logging failed for %s: %s", log_label, _log_err)
            return result
        except Exception as exc:
            _logger.error("[ToolService] %s failed: tool=%s agent=%s error=%s", log_label, tool_name, agent_id, exc)
            return render_tool_error(
                tool_name=tool_name,
                error_class="tool_execution_error",
                message=f"{tool_name} failed with {type(exc).__name__}: {exc}",
                provider="runtime",
                retryable=False,
                actionable_hint="Check tool arguments and retry with a more targeted request.",
            )

    async def execute_with_context(
        self,
        tool_name: str,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> str:
        self.ensure_registry()
        request = ToolExecutionRequest(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
        )

        async def _execute_request(inner_request: ToolExecutionRequest) -> str:
            registry_result = await _maybe_await(self.registry.try_execute(inner_request))
            if registry_result is not None:
                return registry_result
            return await _maybe_await(
                self.fallback_executor(inner_request.tool_name, inner_request.arguments, inner_request.context)
            )

        return await self.backend.execute(request, _execute_request)

    async def _plan_mode_gate_block(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
    ) -> str | None:
        """Return a ``needs_plan`` JSON block when Plan Mode forbids ``tool_name``.

        This is the §9.2 early-intercept tool gate, shared by every execution
        entrypoint (``execute`` / ``execute_direct`` / ``execute_approved``) so
        ``execute_approved`` cannot be used to bypass it. Only tools tagged with
        a real ``ACTION_KIND`` (``ToolMeta.plan_gate_action_kind``) are gated —
        untagged tools and ``bridge:self`` tools (which own their confirmation)
        short-circuit to ``None`` without touching the gate.

        A caller that has already confirmed a plan may thread
        ``confirmed_plan_id`` (and optionally ``confirmed_plan_version`` /
        ``confirmed_plan_hash``) through the tool arguments; they are forwarded
        to the gate so a confirmed handoff runs the tool.

        When the gate blocks **and** no confirmed plan was claimed, this seeds a
        confirmable awaiting :class:`AgentPlanRequest` from the tool's own
        arguments (§9.2 "intercept-then-create") and embeds
        ``plan_id`` / ``plan_json`` / ``plan_version`` / ``plan_hash`` into the
        envelope, so the agent/UI can drive the user to confirm a concrete plan.

        Returns ``None`` to proceed, or the JSON-serialised ``needs_plan``
        envelope (mirroring the deep_research contract) to short-circuit.
        """
        action_kind = hard_gated_action_kind(tool_name, arguments)
        if action_kind is None:
            return None

        confirmed_plan_id = arguments.get("confirmed_plan_id")
        plan_version = arguments.get("confirmed_plan_version")
        plan_hash = arguments.get("confirmed_plan_hash")

        async with self.plan_mode_session_factory() as db:
            decision = await self.plan_mode_gate.check(
                db,
                agent_id=agent_id,
                action_kind=action_kind,
                confirmed_plan_id=confirmed_plan_id,
                plan_version=plan_version,
                plan_hash=plan_hash,
            )

        if not decision.needs_plan:
            return None

        payload = dict(decision.needs_plan_payload or {})
        # Only materialise a fresh plan for a genuine "no confirmed plan" block.
        # A failed *handoff* (the caller claimed a plan that didn't validate) must
        # not spawn a new plan — surface the gate's reason as-is.
        if confirmed_plan_id is None:
            payload = await self._attach_intercepted_plan(
                payload,
                agent_id=agent_id,
                action_kind=action_kind,
                tool_name=tool_name,
                arguments=arguments,
            )
        return _json.dumps(payload, ensure_ascii=False, default=str)

    async def _attach_intercepted_plan(
        self,
        payload: dict,
        *,
        agent_id: uuid.UUID,
        action_kind: str,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """Seed an awaiting plan and merge its identity into ``payload`` (§9.2).

        Fail-closed by construction: any error materialising the plan is logged
        and swallowed, returning the bare ``needs_plan`` envelope unchanged. The
        tool is *already* blocked at this point — a plan-creation failure must
        never downgrade that block into an execution.
        """
        if self.plan_mode_service is None:
            return payload

        # Strip the confirmation-handshake keys before mirroring args into the
        # plan: they are gate plumbing, not part of the planned action.
        action_args = {
            k: v
            for k, v in arguments.items()
            if k not in ("confirmed_plan_id", "confirmed_plan_version", "confirmed_plan_hash")
        }
        try:
            plan = await self.plan_mode_service.ensure_awaiting_plan(
                agent_id=agent_id,
                action_kind=action_kind,
                tool_name=tool_name,
                arguments=action_args,
                source="tool_runtime",
            )
        except Exception as exc:  # noqa: BLE001 — logged, then fail-closed to the bare block
            logging.getLogger(__name__).warning(
                "[ToolService] plan intercept failed for %s: %s", tool_name, exc
            )
            return payload

        if plan is None:
            return payload

        merged = dict(payload)
        merged["plan_id"] = str(plan.id)
        merged["plan_version"] = plan.plan_version
        if plan.plan_hash:
            merged["plan_hash"] = plan.plan_hash
        if plan.plan_json:
            merged["plan_json"] = plan.plan_json
        return merged

    async def _preflight_tool_execution(
        self,
        tool_name: str,
        arguments: dict,
        runtime_context: ToolExecutionContext,
    ) -> str | None:
        if not self.preflight_enabled or self.preflight_service is None:
            return None

        preflight_input = _build_tool_preflight_input(tool_name, arguments, runtime_context=runtime_context)
        preflight = self.preflight_service.evaluate(preflight_input)
        if preflight.decision == PreflightDecision.DO:
            if preflight.requires_audit:
                await self._log_preflight_decision(tool_name, runtime_context, preflight)
            return None

        checkpoint_id = ""
        if preflight.requires_checkpoint:
            tenant_id = getattr(runtime_context, "tenant_id", None)
            async with gateway_scope(self.coordination_gateway, tenant_id=tenant_id) as gateway:
                checkpoint = await gateway.create_checkpoint(
                    action=preflight_input.action,
                    approver_id=str(runtime_context.user_id),
                    escalation_chain=[preflight.escalation_target or "company_admin"],
                    deadline_at=datetime.now(UTC) + timedelta(minutes=30),
                    metadata={
                        "tool_name": tool_name,
                        "agent_id": str(runtime_context.agent_id),
                        "decision": preflight.decision.value,
                    },
                )
            checkpoint_id = checkpoint.id

        if self.decision_trace_store is not None:
            preflight_trace = preflight.as_decision_trace_preflight()
            if checkpoint_id:
                preflight_trace["checkpoint_id"] = checkpoint_id
            self.decision_trace_store.record_decision(
                action=preflight_input.action,
                chosen=preflight.decision.value,
                reasoning="Tool runtime preflight blocked execution before registry/backend invocation.",
                alternatives_considered=["execute tool immediately", "ask owner or escalate before execution"],
                situational_factors=preflight.reasons,
                charter_zone=preflight_input.charter_zone.value,
                preflight=preflight_trace,
                sensitivity=preflight_input.sensitivity.value,
            )

        await self._log_preflight_decision(tool_name, runtime_context, preflight)
        return _render_preflight_block(tool_name, preflight, checkpoint_id=checkpoint_id)

    async def _log_preflight_decision(
        self,
        tool_name: str,
        runtime_context: ToolExecutionContext,
        preflight: ActionPreflightResult,
    ) -> None:
        if not self.activity_logger:
            return
        await _maybe_await(
            self.activity_logger(
                runtime_context.agent_id,
                "action_preflight",
                f"Preflight {preflight.decision.value} for {tool_name}",
                detail={
                    "tool": tool_name,
                    "decision": preflight.decision.value,
                    "reasons": preflight.reasons,
                    "requires_checkpoint": preflight.requires_checkpoint,
                    "requires_audit": preflight.requires_audit,
                    "escalation_target": preflight.escalation_target,
                },
            )
        )


def _build_tool_preflight_input(
    tool_name: str,
    arguments: dict,
    *,
    runtime_context: ToolExecutionContext | None = None,
) -> ActionPreflightInput:
    args_text = _json.dumps(arguments, ensure_ascii=False, default=str)
    privacy = PrivacyLayer().classify_and_mask(args_text)
    sensitivity = privacy.sensitivity
    lower_action = f"{tool_name} {args_text}".lower()
    company_conflict = any(pattern in lower_action for pattern in _COMPANY_CONFLICT_PATTERNS)
    execution_identity = getattr(runtime_context, "execution_identity", None) if runtime_context is not None else None
    explicit_user_authorized = (
        tool_name in _DELEGATED_USER_AUTHORIZED_TOOLS
        and getattr(execution_identity, "identity_type", None) == "delegated_user"
    )

    if tool_name in _EXTERNAL_VISIBLE_TOOLS:
        return ActionPreflightInput(
            action=f"send external message via {tool_name}",
            reversibility=BoundaryAxisLevel.MEDIUM,
            representativeness=BoundaryAxisLevel.HIGH,
            judgment_density=BoundaryAxisLevel.HIGH,
            visibility=BoundaryAxisLevel.HIGH,
            domain_specialization=BoundaryAxisLevel.MEDIUM,
            charter_zone=CharterZone.CONFIRM_FIRST,
            sensitivity=sensitivity,
            company_boundary_conflict=company_conflict,
            explicit_user_authorized=explicit_user_authorized,
        )

    return ActionPreflightInput(
        action=f"execute local tool {tool_name}",
        reversibility=BoundaryAxisLevel.LOW,
        representativeness=BoundaryAxisLevel.LOW,
        judgment_density=BoundaryAxisLevel.LOW,
        visibility=BoundaryAxisLevel.LOW,
        domain_specialization=BoundaryAxisLevel.LOW,
        charter_zone=CharterZone.FULL_AUTHORITY,
        sensitivity=sensitivity,
        company_boundary_conflict=company_conflict,
    )


def _render_preflight_block(tool_name: str, preflight: ActionPreflightResult, *, checkpoint_id: str = "") -> str:
    reason_text = ",".join(preflight.reasons) if preflight.reasons else "unspecified"
    suffix = ""
    if preflight.escalation_target:
        suffix = f" escalation_target={preflight.escalation_target}"
    if checkpoint_id:
        suffix += f" checkpoint={checkpoint_id}"
    return f"[Preflight:{preflight.decision.value}] {tool_name} was not executed. reasons={reason_text}{suffix}"
