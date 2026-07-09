"""Runtime service for governed tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json as _json
import logging
import re
import traceback
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agents.coordination import CoordinationRuntime, coordination_runtime
from app.agents.coordination_gateway import CoordinationGateway, InProcessCoordinationGateway
from app.agents.coordination_wiring import gateway_scope
from app.runtime.authorization_decision import build_authorization_decision_entry
from app.runtime.ccplus_contracts import ToolCallLifecycleV1, ToolExecutionFrameV1
from app.services.action_preflight import (
    ActionPreflightInput,
    ActionPreflightResult,
    ActionPreflightService,
    BoundaryAxisLevel,
    CharterZone,
    PreflightDecision,
)
from app.services.decision_trace import TenantScopedSqlDecisionTraceStore
from app.services.plan_mode_gate import PlanModeGate, get_plan_mode_gate
from app.services.privacy_layer import PrivacyLayer
from app.services.governance_capability_taxonomy import capability_descriptor_for_tool, is_l2_tool
from app.services.capability_group_policy_service import (
    get_agent_capability_group_policies,
    is_capability_group_enabled,
    policy_capability_group_names_for_tool,
)
from app.tools.governance import EventCallback, GovernanceDependencies, ToolGovernanceContext
from app.tools.plan_gate_registry import hard_gated_action_kind
from app.tools.result_envelope import ToolContentEnvelope, render_tool_error
from app.tools.runtime import ToolExecutionContext, ToolExecutionRegistry, ToolExecutionRequest
from app.tools.backends import LocalToolRuntimeBackend, ToolRuntimeBackend
from app.tools.validation import validate_tool_arguments


RuntimeResolver = Callable[..., Awaitable[ToolExecutionContext] | ToolExecutionContext]
GovernanceRunner = Callable[
    [ToolGovernanceContext, GovernanceDependencies],
    Awaitable[str | None] | str | None,
]
FallbackExecutor = Callable[[str, dict, ToolExecutionContext], Awaitable[str] | str]
ActivityLogger = Callable[..., Awaitable[None] | None]
EnsureRegistry = Callable[[], None]

TOOL_TIMEOUTS: dict[str, float] = {
    "execute_code": 120.0,
    "run_command": 120.0,
    "create_digital_employee": 120.0,
    # Long-running harness primitives wrap agent/workflow/DR execution and
    # must not fall back to the generic 30s timeout.
    "spawn_subagent": 180.0,
    "start_workflow": 180.0,
    # Synchronous A2A wraps the target's full LLM turn (including tools like
    # feishu_wiki_list / document reads) plus reply write-back. Keep this
    # above AGENT_MESSAGE_TIMEOUT_SECONDS so the wrapper does not preempt the
    # orchestrator's explicit timeout handling.
    "send_message_to_agent": 360.0,
    "web_fetch": 60.0,
    "web_search": 60.0,
    "advanced_web_search": 60.0,
    "advanced_web_fetch": 60.0,
    "anysearch_get_sub_domains": 60.0,
    "anysearch_search": 60.0,
    "anysearch_batch_search": 60.0,
    "anysearch_extract": 60.0,
    "exa_search": 60.0,
    "exa_fetch": 60.0,
    "tavily_search": 60.0,
    "tavily_extract": 60.0,
    "firecrawl_search": 60.0,
    "firecrawl_fetch": 60.0,
    "xcrawl_scrape": 60.0,
    "read_document": 60.0,
    "send_feishu_message": 45.0,
    "feishu_doc_read": 45.0,
    "feishu_url_resolve": 45.0,
    "feishu_url_read": 90.0,
    "feishu_drive_file_read": 90.0,
    "feishu_wiki_read": 45.0,
}

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


def _redact_args(arguments: Any) -> dict[str, Any]:
    """Shallow-redact obviously sensitive values before a tool's args enter a
    plan seed (defence-in-depth; trigger/task args rarely carry secrets). Also
    drops the confirmation-handshake keys, which are gate plumbing, not intent.
    """
    if not isinstance(arguments, dict):
        return {}
    redacted: dict[str, Any] = {}
    for k, v in arguments.items():
        if k in ("confirmed_plan_id", "confirmed_plan_version", "confirmed_plan_hash"):
            continue
        if any(s in str(k).lower() for s in ("secret", "token", "password", "credential", "api_key")):
            redacted[k] = "[redacted]"
        else:
            redacted[k] = v
    return redacted


def _plan_gate_action_artifact(tool_name: str, arguments: dict, action_kind: str) -> dict | None:
    """Build an exact action payload for confirmed-plan handoff checks.

    Workflow no longer uses PlanModeGate, so no current tool needs a separate
    action artifact. Keep this seam for legacy confirmed-plan callers without
    reintroducing risk-grade based Plan Mode routing.
    """
    _ = (tool_name, arguments, action_kind)
    return None


def _confirmation_required_payload(payload: dict | None) -> dict:
    """Return an external confirmation block that cannot activate Plan Mode."""
    source = dict(payload or {})
    source["status"] = "requires_confirmation"
    source["requires_confirmation"] = True
    source.pop("activate_interactive_plan", None)
    source.pop("interactive_plan_seed", None)
    source.setdefault("ok", False)
    source.setdefault(
        "summary",
        "Explicit user confirmation is required before this action can run.",
    )
    source.setdefault(
        "next_action",
        (
            "STOP and ask the user for explicit confirmation. Do not enter Plan Mode unless the user "
            "explicitly asks for Plan Mode or approves a Plan Mode entry request."
        ),
    )
    return source


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_runtime_context(
    runtime_resolver: Any,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str | None = None,
    permission_profile: Any | None = None,
    turn_id: str | None = None,
    runtime_task_id: str | None = None,
    budget_run_id: str | None = None,
    origin_channel: str | None = None,
    round_state: dict[str, Any] | None = None,
    t0_refs: tuple[str, ...] = (),
) -> ToolExecutionContext:
    kwargs: dict[str, Any] = {"agent_id": agent_id, "user_id": user_id}
    try:
        params = inspect.signature(runtime_resolver.resolve).parameters
        accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        params = {}
        accepts_kwargs = False
    if session_id is not None and (accepts_kwargs or "session_id" in params):
        kwargs["session_id"] = session_id
    if permission_profile is not None and (accepts_kwargs or "permission_profile" in params):
        kwargs["permission_profile"] = permission_profile
    if turn_id is not None and (accepts_kwargs or "turn_id" in params):
        kwargs["turn_id"] = turn_id
    if runtime_task_id is not None and (accepts_kwargs or "runtime_task_id" in params):
        kwargs["runtime_task_id"] = runtime_task_id
    if budget_run_id is not None and (accepts_kwargs or "budget_run_id" in params):
        kwargs["budget_run_id"] = budget_run_id
    if origin_channel is not None and (accepts_kwargs or "origin_channel" in params):
        kwargs["origin_channel"] = origin_channel
    if round_state is not None and (accepts_kwargs or "round_state" in params):
        kwargs["round_state"] = round_state
    if t0_refs and (accepts_kwargs or "t0_refs" in params):
        kwargs["t0_refs"] = t0_refs
    return await _maybe_await(runtime_resolver.resolve(**kwargs))


def _json_safe_runtime_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_safe_runtime_value(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_runtime_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_runtime_value(item) for key, item in value.items()}
    return value


def _permission_profile_payload(permission_profile: Any | None) -> dict[str, Any] | None:
    if permission_profile is None:
        return None
    if isinstance(permission_profile, dict):
        return _json_safe_runtime_value(permission_profile)
    if is_dataclass(permission_profile):
        return _json_safe_runtime_value(asdict(permission_profile))
    return None


def _inject_runtime_context_arguments(
    tool_name: str,
    arguments: dict,
    runtime_context: ToolExecutionContext,
) -> dict:
    """Thread runtime-owned session context into tools that must never rely on
    the model to restate it correctly.
    """
    if tool_name == "set_trigger":
        # B3: a `delivery=same_session` trigger must deliver into the live chat
        # session — supply its id from the runtime, never trust the model to
        # restate a session id. Only inject when same_session is requested so the
        # default path stays untouched.
        wants_same_session = (
            str((arguments.get("delivery") or (arguments.get("config") or {}).get("delivery") or "")).strip()
            == "same_session"
        )
        if not (wants_same_session and runtime_context.session_id and not arguments.get("source_session_id")):
            return arguments
        enriched = dict(arguments)
        enriched["source_session_id"] = runtime_context.session_id
        return enriched

    if tool_name not in {"delegate_to_agent", "send_message_to_agent"}:
        return arguments

    enriched = dict(arguments)
    if runtime_context.session_id and not enriched.get("parent_session_id"):
        enriched["parent_session_id"] = runtime_context.session_id
    if runtime_context.budget_run_id and not enriched.get("_budget_run_id"):
        enriched["_budget_run_id"] = runtime_context.budget_run_id

    profile_payload = _permission_profile_payload(runtime_context.permission_profile)
    if profile_payload and "_permission_profile" not in enriched:
        enriched["_permission_profile"] = profile_payload
    return enriched


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


def _validate_tool_arguments_block(tool_name: str, arguments: Any) -> str | None:
    errors = validate_tool_arguments(tool_name, arguments)
    if not errors:
        return None
    return render_tool_error(
        tool_name=tool_name,
        error_class="invalid_tool_arguments",
        message="; ".join(errors),
        provider="ccplus_validate_input",
        retryable=False,
        actionable_hint="Re-read the tool schema and rebuild the arguments object before retrying.",
    )


def _truth_evidence_payload_for_trace(pack: Any) -> dict[str, Any]:
    raw = asdict(pack) if is_dataclass(pack) else dict(pack) if isinstance(pack, dict) else {}
    payload: dict[str, Any] = {}
    for key in (
        "evidence_id",
        "query",
        "source_refs",
        "citations",
        "snippets",
        "digest",
        "provider",
        "freshness",
        "confidence",
        "limitations",
        "trace_refs",
    ):
        value = raw.get(key)
        if value in (None, "", (), [], {}):
            continue
        if isinstance(value, tuple):
            value = list(value)
        payload[key] = value
    return _json_safe_runtime_value(payload)


def _truth_evidence_trace_payload(evidence_packs: tuple[Any, ...]) -> tuple[list[str], list[dict[str, Any]]]:
    refs: list[str] = []
    payloads: list[dict[str, Any]] = []
    for pack in evidence_packs:
        payload = _truth_evidence_payload_for_trace(pack)
        evidence_id = str(payload.get("evidence_id") or "").strip()
        if evidence_id:
            refs.append(evidence_id)
        if payload:
            payloads.append(payload)
    return list(dict.fromkeys(refs)), payloads


def _runtime_hash(value: Any) -> str:
    payload = _json.dumps(_json_safe_runtime_value(value), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _new_runtime_tool_call_id() -> str:
    return f"runtime:{uuid.uuid4()}"


def _record_tool_lifecycle(
    runtime_context: ToolExecutionContext,
    *,
    tool_call_id: str,
    tool_name: str,
    state: str,
    original_arguments: dict,
    effective_arguments: dict,
    governance_decisions: tuple[str, ...] = (),
    permission_request_id: str | None = None,
    truth_evidence_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    descriptor = capability_descriptor_for_tool(tool_name)
    lifecycle = ToolCallLifecycleV1(
        session_id=str(runtime_context.session_id or ""),
        turn_id=runtime_context.turn_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        lifecycle_state=state,
        original_arguments=dict(original_arguments or {}),
        effective_arguments=dict(effective_arguments or {}),
        schema_ref=f"tool_schema:{tool_name}:parameters",
        capability=descriptor.name if descriptor else None,
        taxonomy_layer=descriptor.layer if descriptor else None,
        governance_decisions=governance_decisions,
        permission_request_id=permission_request_id,
        truth_evidence_refs=truth_evidence_refs,
        t0_refs=tuple(str(ref) for ref in (runtime_context.t0_refs or ()) if str(ref).strip()),
    )
    payload = _json_safe_runtime_value(asdict(lifecycle))
    runtime_context.tool_lifecycle_records.append(payload)
    return payload


def _record_tool_execution_frame(
    runtime_context: ToolExecutionContext,
    *,
    tool_call_id: str,
    tool_name: str,
    executor: str,
    arguments: dict,
    status: str,
    result: Any | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    frame = ToolExecutionFrameV1(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        executor=executor,
        input_hash=_runtime_hash(arguments or {}),
        output_hash=_runtime_hash(result) if result is not None else None,
        provider="tool_runtime_service",
        started_at=started_at or now,
        completed_at=now if status in {"completed", "failed", "blocked"} else None,
        status=status,
        t0_refs=tuple(str(ref) for ref in (runtime_context.t0_refs or ()) if str(ref).strip()),
    )
    payload = _json_safe_runtime_value(asdict(frame))
    runtime_context.tool_execution_frames.append(payload)
    return payload


def _latest_context_record(runtime_context: ToolExecutionContext, attr: str) -> dict[str, Any] | None:
    records = getattr(runtime_context, attr, None)
    if isinstance(records, list) and records and isinstance(records[-1], dict):
        return records[-1]
    return None


def _tool_trace_metadata(
    runtime_context: ToolExecutionContext,
    *,
    tool_call_id: str | None,
    source: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tenant_id": runtime_context.tenant_id,
        "user_id": str(runtime_context.user_id),
        "tool_call_id": tool_call_id,
        "entrypoint": source,
    }
    lifecycle = _latest_context_record(runtime_context, "tool_lifecycle_records")
    if lifecycle:
        metadata["tool_call_lifecycle"] = lifecycle
    frame = _latest_context_record(runtime_context, "tool_execution_frames")
    if frame:
        metadata["tool_execution_frame"] = frame
    return metadata


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
    decision_trace_store: Any | None = None
    coordination_runtime: CoordinationRuntime | None = None
    coordination_gateway: CoordinationGateway | None = None
    truth_search_service: Any | None = None
    capability_group_policy_loader: Callable[[ToolExecutionContext], Awaitable[dict[str, bool]] | dict[str, bool]] | None = None
    pack_policy_loader: Callable[[ToolExecutionContext], Awaitable[dict[str, bool]] | dict[str, bool]] | None = None
    preflight_enabled: bool = True
    # Confirmation gate. The gate is read-only and stateless; the session factory
    # opens a short-lived async session for the by-id plan lookup. Both are DI
    # seams so tests can inject fakes (the gate is otherwise the shared singleton).
    plan_mode_gate: PlanModeGate | None = None
    plan_mode_session_factory: Callable[[], Any] | None = None
    # Plan Mode service handle (the shared singleton handoffs register on). DI
    # seam kept for tests + future intake needs; blocked gated tools now return
    # ``requires_confirmation`` and never enter Plan Mode.
    plan_mode_service: Any | None = None

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = LocalToolRuntimeBackend()
        if self.preflight_service is None:
            self.preflight_service = ActionPreflightService()
        if self.decision_trace_store is None:
            self.decision_trace_store = TenantScopedSqlDecisionTraceStore()
        if self.coordination_runtime is None:
            self.coordination_runtime = coordination_runtime
        if self.coordination_gateway is None:
            self.coordination_gateway = InProcessCoordinationGateway(self.coordination_runtime)
        if self.truth_search_service is None:
            from app.services.truth_search_service import TruthSearchService

            self.truth_search_service = TruthSearchService()
        if self.plan_mode_gate is None:
            self.plan_mode_gate = get_plan_mode_gate()
        if self.plan_mode_session_factory is None:
            from app.database import async_session

            self.plan_mode_session_factory = async_session
        if self.plan_mode_service is None:
            from app.services.plan_mode_service import get_plan_mode_service

            self.plan_mode_service = get_plan_mode_service()

    async def _load_capability_group_policies(self, runtime_context: ToolExecutionContext) -> dict[str, bool]:
        policy_loader = self.capability_group_policy_loader or self.pack_policy_loader
        if policy_loader is not None:
            loaded = policy_loader(runtime_context)
            if inspect.isawaitable(loaded):
                loaded = await loaded
            return dict(loaded or {})
        if not runtime_context.tenant_id:
            return {}
        from app.database import tenant_scoped_session

        try:
            tenant_id = uuid.UUID(str(runtime_context.tenant_id))
        except (TypeError, ValueError):
            return {}
        async with tenant_scoped_session(tenant_id) as db:
            return await get_agent_capability_group_policies(db, tenant_id, runtime_context.agent_id)

    async def _l2_extension_policy_block(self, tool_name: str, runtime_context: ToolExecutionContext) -> str | None:
        if not is_l2_tool(tool_name):
            return None
        capability_group_names = policy_capability_group_names_for_tool(tool_name)
        if not capability_group_names:
            return None
        if not runtime_context.tenant_id:
            return render_tool_error(
                tool_name=tool_name,
                error_class="extension_disabled",
                message=f"{tool_name} is an L2 extension tool, but no tenant context was available for call-time policy enforcement.",
                provider="ccplus_l2_gate",
                retryable=False,
                actionable_hint="Retry from a normal agent session so tenant and agent extension policies can be checked.",
            )
        capability_group_policies = await self._load_capability_group_policies(runtime_context)
        disabled_capability_groups = tuple(
            capability_group_name
            for capability_group_name in capability_group_names
            if not is_capability_group_enabled(capability_group_policies, capability_group_name)
        )
        if not disabled_capability_groups:
            return None
        capability_group_list = ", ".join(disabled_capability_groups)
        return render_tool_error(
            tool_name=tool_name,
            error_class="extension_disabled",
            message=f"{tool_name} belongs to disabled L2 extension capability group(s): {capability_group_list}.",
            provider="ccplus_l2_gate",
            retryable=False,
            actionable_hint=(
                f"Enable the extension capability group(s) for this agent before calling {tool_name}: "
                f"{capability_group_list}."
            ),
        )

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
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
    ) -> str | ToolContentEnvelope:
        plan_mode_block = self._interactive_plan_mode_readonly_block(tool_name, arguments)
        if plan_mode_block:
            return plan_mode_block

        plan_block = await self._plan_mode_gate_block(
            tool_name,
            arguments,
            agent_id=agent_id,
            plan_mode_interactive_available=plan_mode_interactive_available,
            plan_mode_unattended_available=plan_mode_unattended_available,
        )
        if plan_block:
            return plan_block

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
        if workspace_override is not None:
            runtime_context.workspace = Path(workspace_override)
        effective_tool_call_id = tool_call_id or _new_runtime_tool_call_id()
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
                return "Blocked by hook: " + (hook_result.reason or "policy")
            if hook_result and hook_result.modified_args is not None:
                arguments = hook_result.modified_args
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
            return validation_block
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="validated",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
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
            return preflight_block
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="preflight",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
        )

        timeout_seconds = TOOL_TIMEOUTS.get(tool_name, 30.0)
        try:
            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="executing",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
            )
            result = await asyncio.wait_for(
                self.execute_with_context(tool_name, arguments, runtime_context),
                timeout=timeout_seconds,
            )
            # result may be a ToolContentEnvelope — use its text rendering for
            # logging / error extraction, but return the value untouched.
            result_text = str(result)
            tool_error_payload = _extract_tool_error_payload(result_text)
            if self.activity_logger:
                await _maybe_await(
                    self.activity_logger(
                        agent_id,
                        "tool_call",
                        f"Called tool {tool_name}: {result_text[:80]}",
                        tenant_id=runtime_context.tenant_id,
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
                            "tool_call_lifecycle": _latest_context_record(
                                runtime_context, "tool_lifecycle_records"
                            ),
                            "tool_execution_frame": _latest_context_record(
                                runtime_context, "tool_execution_frames"
                            ),
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
                            detail=tool_error_payload,
                        )
                    )
            if emit_runtime_hooks:
                _record_tool_lifecycle(
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    tool_name=tool_name,
                    state="completed",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                )
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
                    return rewrite if isinstance(rewrite, str) else _json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
            elif not emit_runtime_hooks:
                _record_tool_lifecycle(
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    tool_name=tool_name,
                    state="completed",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
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

    async def _emit_pre_tool_hook(
        self,
        tool_name: str,
        arguments: dict,
        runtime_context: ToolExecutionContext,
        *,
        tool_call_id: str | None,
        source: str,
    ) -> Any | None:
        from app.runtime.hooks import HookEvent, emit_hook

        return await emit_hook(
            HookEvent.PRE_TOOL_USE,
            agent_id=runtime_context.agent_id,
            session_id=runtime_context.session_id,
            tool_name=tool_name,
            tool_args=dict(arguments),
            source=source,
            metadata=_tool_trace_metadata(runtime_context, tool_call_id=tool_call_id, source=source),
        )

    async def _emit_post_tool_hook(
        self,
        tool_name: str,
        arguments: dict,
        result_text: str,
        runtime_context: ToolExecutionContext,
        *,
        tool_call_id: str | None,
        source: str,
    ) -> Any | None:
        from app.runtime.hooks import HookEvent, emit_hook

        return await emit_hook(
            HookEvent.POST_TOOL_USE,
            agent_id=runtime_context.agent_id,
            session_id=runtime_context.session_id,
            tool_name=tool_name,
            tool_args=dict(arguments),
            tool_result=result_text[:500],
            source=source,
            metadata=_tool_trace_metadata(runtime_context, tool_call_id=tool_call_id, source=source),
        )

    async def _emit_tool_failure_hook(
        self,
        tool_name: str,
        arguments: dict,
        error_text: str,
        runtime_context: ToolExecutionContext,
        *,
        tool_call_id: str | None,
        source: str,
    ) -> None:
        from app.runtime.hooks import HookEvent, emit_hook

        await emit_hook(
            HookEvent.POST_TOOL_FAILURE,
            agent_id=runtime_context.agent_id,
            session_id=runtime_context.session_id,
            tool_name=tool_name,
            tool_args=dict(arguments),
            error=error_text[:500],
            source=source,
            metadata=_tool_trace_metadata(runtime_context, tool_call_id=tool_call_id, source=source),
        )

    async def execute_direct(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> str | ToolContentEnvelope:
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
    ) -> str | ToolContentEnvelope:
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
    ) -> str | ToolContentEnvelope:
        _logger = logging.getLogger(__name__)

        plan_mode_block = self._interactive_plan_mode_readonly_block(tool_name, arguments)
        if plan_mode_block:
            return plan_mode_block

        # Plan Mode early-intercept (§9.2) fires here so both execute_direct and
        # execute_approved are covered — execute_approved must NOT be a bypass.
        plan_block = await self._plan_mode_gate_block(tool_name, arguments, agent_id=agent_id)
        if plan_block:
            return plan_block

        self.ensure_registry()

        resolved_user_id = user_id or agent_id
        _logger.info("[ToolService] %s: tool=%s agent=%s user=%s", log_label, tool_name, agent_id, resolved_user_id)

        runtime_context = await self.runtime_resolver.resolve(agent_id=agent_id, user_id=resolved_user_id)
        effective_tool_call_id = _new_runtime_tool_call_id()
        original_arguments = dict(arguments or {})
        _record_tool_lifecycle(
            runtime_context,
            tool_call_id=effective_tool_call_id,
            tool_name=tool_name,
            state="created",
            original_arguments=original_arguments,
            effective_arguments=dict(arguments or {}),
            governance_decisions=(log_label,),
        )
        try:
            hook_result = await self._emit_pre_tool_hook(
                tool_name,
                arguments,
                runtime_context,
                tool_call_id=effective_tool_call_id,
                source=log_label,
            )
            if hook_result and hook_result.block:
                _record_tool_lifecycle(
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    tool_name=tool_name,
                    state="blocked",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("pre_tool_hook_block", log_label),
                )
                return "Blocked by hook: " + (hook_result.reason or "policy")
            if hook_result and hook_result.modified_args is not None:
                arguments = hook_result.modified_args
            validation_block = _validate_tool_arguments_block(tool_name, arguments)
            if validation_block:
                _record_tool_lifecycle(
                    runtime_context,
                    tool_call_id=effective_tool_call_id,
                    tool_name=tool_name,
                    state="blocked",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("validate_input_block", log_label),
                )
                return validation_block
            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="validated",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
                governance_decisions=(log_label,),
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
                    governance_decisions=("l2_policy_block", log_label),
                )
                return l2_policy_block
            request = ToolExecutionRequest(
                tool_name=tool_name,
                arguments=arguments,
                context=runtime_context,
            )

            async def _execute_approved_request(inner_request: ToolExecutionRequest) -> str | ToolContentEnvelope:
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

            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="executing",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
                governance_decisions=(log_label,),
            )
            started_frame = _record_tool_execution_frame(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                executor=self.backend.name if self.backend else "unknown",
                arguments=arguments,
                status="executing",
            )
            result = await self.backend.execute(request, _execute_approved_request)
            _record_tool_execution_frame(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                executor=self.backend.name if self.backend else "unknown",
                arguments=arguments,
                status="completed",
                result=str(result),
                started_at=started_frame.get("started_at"),
            )
            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="completed",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
                governance_decisions=(log_label,),
            )
            # result may be a ToolContentEnvelope — text rendering for logging only.
            result_text = str(result)
            # Activity log for audit trail (mirrors execute() behavior)
            if self.activity_logger:
                try:
                    detail = {
                        "tool": tool_name,
                        "backend": self.backend.name if self.backend else "unknown",
                        "result": result_text[:300],
                        "tool_call_lifecycle": _latest_context_record(runtime_context, "tool_lifecycle_records"),
                        "tool_execution_frame": _latest_context_record(runtime_context, "tool_execution_frames"),
                        **activity_detail,
                    }
                    await _maybe_await(
                        self.activity_logger(
                            agent_id,
                            activity_type,
                            f"Approved-executed {tool_name}: {result_text[:80]}",
                            tenant_id=runtime_context.tenant_id,
                            detail=detail,
                        )
                    )
                except Exception as _log_err:
                    _logger.warning("[ToolService] Activity logging failed for %s: %s", log_label, _log_err)
            post_hook_result = await self._emit_post_tool_hook(
                tool_name,
                arguments,
                result_text,
                runtime_context,
                tool_call_id=effective_tool_call_id,
                source=log_label,
            )
            if post_hook_result and post_hook_result.output_rewrite is not None:
                rewrite = post_hook_result.output_rewrite
                return rewrite if isinstance(rewrite, str) else _json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
            return result
        except Exception as exc:
            _logger.error("[ToolService] %s failed: tool=%s agent=%s error=%s", log_label, tool_name, agent_id, exc)
            rendered = render_tool_error(
                tool_name=tool_name,
                error_class="tool_execution_error",
                message=f"{tool_name} failed with {type(exc).__name__}: {exc}",
                provider="runtime",
                retryable=False,
                actionable_hint="Check tool arguments and retry with a more targeted request.",
            )
            _record_tool_execution_frame(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                executor=self.backend.name if self.backend else "unknown",
                arguments=arguments,
                status="failed",
                result={"error": type(exc).__name__, "message": str(exc)[:500]},
            )
            _record_tool_lifecycle(
                runtime_context,
                tool_call_id=effective_tool_call_id,
                tool_name=tool_name,
                state="failed",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
                governance_decisions=("tool_execution_error", log_label),
            )
            await self._emit_tool_failure_hook(
                tool_name,
                arguments,
                rendered,
                runtime_context,
                tool_call_id=effective_tool_call_id,
                source=log_label,
            )
            return rendered

    async def execute_with_context(
        self,
        tool_name: str,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> str | ToolContentEnvelope:
        latest_lifecycle = _latest_context_record(context, "tool_lifecycle_records")
        terminal_states = {"completed", "failed", "blocked"}
        owns_lifecycle = not (
            latest_lifecycle
            and latest_lifecycle.get("tool_name") == tool_name
            and latest_lifecycle.get("lifecycle_state") not in terminal_states
        )
        tool_call_id = (
            _new_runtime_tool_call_id()
            if owns_lifecycle
            else str(latest_lifecycle.get("tool_call_id") or _new_runtime_tool_call_id())
        )
        original_arguments = dict(arguments or {})
        if owns_lifecycle:
            _record_tool_lifecycle(
                context,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state="created",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
            )

        plan_mode_block = self._interactive_plan_mode_readonly_block(tool_name, arguments)
        if plan_mode_block:
            if owns_lifecycle:
                _record_tool_lifecycle(
                    context,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    state="blocked",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("plan_mode_block",),
                )
            return plan_mode_block

        self.ensure_registry()
        validation_block = _validate_tool_arguments_block(tool_name, arguments)
        if validation_block:
            if owns_lifecycle:
                _record_tool_lifecycle(
                    context,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    state="blocked",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("validate_input_block",),
                )
            return validation_block
        if owns_lifecycle:
            _record_tool_lifecycle(
                context,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state="validated",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
            )
        l2_policy_block = await self._l2_extension_policy_block(tool_name, context)
        if l2_policy_block:
            if owns_lifecycle:
                _record_tool_lifecycle(
                    context,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    state="blocked",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("l2_policy_block",),
                )
            return l2_policy_block
        request = ToolExecutionRequest(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
        )

        async def _execute_request(inner_request: ToolExecutionRequest) -> str | ToolContentEnvelope:
            registry_result = await _maybe_await(self.registry.try_execute(inner_request))
            if registry_result is not None:
                return registry_result
            return await _maybe_await(
                self.fallback_executor(inner_request.tool_name, inner_request.arguments, inner_request.context)
            )

        started_frame = _record_tool_execution_frame(
            context,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            executor=self.backend.name if self.backend else "unknown",
            arguments=arguments,
            status="executing",
        )
        try:
            result = await self.backend.execute(request, _execute_request)
        except Exception as exc:
            _record_tool_execution_frame(
                context,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                executor=self.backend.name if self.backend else "unknown",
                arguments=arguments,
                status="failed",
                result={"error": type(exc).__name__, "message": str(exc)[:500]},
                started_at=started_frame.get("started_at"),
            )
            if owns_lifecycle:
                _record_tool_lifecycle(
                    context,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    state="failed",
                    original_arguments=original_arguments,
                    effective_arguments=dict(arguments or {}),
                    governance_decisions=("tool_execution_error",),
                )
            raise
        _record_tool_execution_frame(
            context,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            executor=self.backend.name if self.backend else "unknown",
            arguments=arguments,
            status="completed",
            result=str(result),
            started_at=started_frame.get("started_at"),
        )
        if owns_lifecycle:
            _record_tool_lifecycle(
                context,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state="completed",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
            )
        return result

    @staticmethod
    def _interactive_plan_mode_readonly_block(tool_name: str, arguments: dict | None = None) -> str | None:
        from app.services.plan_mode_runtime_context import (
            interactive_plan_mode_active,
            interactive_plan_mode_metadata,
        )
        from app.tools.plan_mode_policy import is_plan_mode_tool_allowed

        if not interactive_plan_mode_active():
            return None
        # Phase 4B: a provisioned exact plan-file path (mirrored onto the
        # ContextVar) lets write_file/edit_file/fs_write target only that file.
        plan_file_path = interactive_plan_mode_metadata().get("plan_file_path")
        if is_plan_mode_tool_allowed(tool_name, arguments, plan_file_path):
            return None
        return render_tool_error(
            tool_name=tool_name,
            error_class="plan_mode_readonly_violation",
            message=(
                "Interactive Plan Mode is active. Use only read-only exploration tools, then call "
                "exit_plan_mode to submit the plan for user confirmation. Do not execute or mutate state yet."
            ),
            provider="runtime",
            retryable=False,
            actionable_hint="Continue planning with read-only tools or call exit_plan_mode when the plan is ready.",
        )

    async def _plan_mode_gate_block(
        self,
        tool_name: str,
        arguments: dict,
        *,
        agent_id: uuid.UUID,
        plan_mode_interactive_available: bool = False,
        plan_mode_unattended_available: bool = False,
    ) -> str | None:
        """Return a confirmation-required JSON block when a gated action is blocked.

        This is the confirmation backstop, shared by every execution
        entrypoint (``execute`` / ``execute_direct`` / ``execute_approved``) so
        ``execute_approved`` cannot be used to bypass it. Only tools tagged with
        a real ``ACTION_KIND`` (``ToolMeta.plan_gate_action_kind``) are gated —
        untagged tools and ``bridge:self`` tools (which own their confirmation)
        short-circuit to ``None`` without touching the gate.

        A caller that has already confirmed a plan may thread
        ``confirmed_plan_id`` (and optionally ``confirmed_plan_version`` /
        ``confirmed_plan_hash``) through the tool arguments; they are forwarded
        to the gate so a confirmed handoff runs the tool.

        A block never activates Plan Mode. Plan Mode entry is explicit user/UI
        state only; this gate may only refuse execution and ask for confirmation.

        Returns ``None`` to proceed, or the JSON-serialised
        ``requires_confirmation`` envelope to short-circuit.
        """
        action_kind = hard_gated_action_kind(tool_name, arguments)
        if action_kind is None:
            return None

        confirmed_plan_id = arguments.get("confirmed_plan_id")
        plan_version = arguments.get("confirmed_plan_version")
        plan_hash = arguments.get("confirmed_plan_hash")
        action_artifact = _plan_gate_action_artifact(tool_name, arguments, action_kind)

        async with self.plan_mode_session_factory() as db:
            decision = await self.plan_mode_gate.check(
                db,
                agent_id=agent_id,
                action_kind=action_kind,
                confirmed_plan_id=confirmed_plan_id,
                plan_version=plan_version,
                plan_hash=plan_hash,
                action_artifact=action_artifact,
            )

        if not decision.needs_plan:
            return None

        _ = (plan_mode_interactive_available, plan_mode_unattended_available, action_artifact)
        payload = _confirmation_required_payload(decision.needs_plan_payload)
        payload["authorization_decision_entry"] = build_authorization_decision_entry(
            resource=f"tool:{tool_name}",
            action=action_kind,
            principal=str(agent_id),
            policy="plan_gate",
            result="requires_confirmation",
            reason=str(getattr(decision, "reason", "") or "requires_confirmation"),
            model_visible_message=payload.get("summary"),
            source="plan_gate",
        )
        return _json.dumps(payload, ensure_ascii=False, default=str)

    async def _preflight_tool_execution(
        self,
        tool_name: str,
        arguments: dict,
        runtime_context: ToolExecutionContext,
        *,
        trace_metadata_sink: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.preflight_enabled or self.preflight_service is None:
            return None

        truth_evidence = ()
        if self.truth_search_service is not None:
            truth_evidence = tuple(
                await _maybe_await(
                    self.truth_search_service.search(
                        f"{tool_name} {_json.dumps(arguments, ensure_ascii=False, default=str)}",
                        tenant_id=runtime_context.tenant_id,
                        agent_id=runtime_context.agent_id,
                        current_user_id=runtime_context.user_id,
                    )
                )
                or ()
            )
        preflight_input = _build_tool_preflight_input(
            tool_name,
            arguments,
            runtime_context=runtime_context,
            truth_evidence=truth_evidence,
        )
        preflight = self.preflight_service.evaluate(preflight_input)
        evidence_refs, evidence_payloads = _truth_evidence_trace_payload(truth_evidence)
        authorization_decision_entry = preflight.as_authorization_decision_entry(
            preflight_input,
            resource=f"tool:{tool_name}",
            principal=str(runtime_context.user_id) if runtime_context.user_id else None,
            company=str(runtime_context.tenant_id) if runtime_context.tenant_id else None,
            model_visible_message=(
                f"{tool_name} was not executed. reasons={','.join(preflight.reasons) or 'unspecified'}"
            ),
        )
        if trace_metadata_sink is not None:
            if evidence_refs:
                trace_metadata_sink["evidence_refs"] = evidence_refs
                trace_metadata_sink["truth_evidence_refs"] = evidence_refs
            if evidence_payloads:
                trace_metadata_sink["truth_evidence"] = evidence_payloads
            trace_metadata_sink["preflight"] = preflight.as_decision_trace_preflight()
            trace_metadata_sink["authorization_decision_entry"] = authorization_decision_entry
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
                        "evidence_refs": list(preflight.evidence_refs),
                    },
                )
            checkpoint_id = checkpoint.id

        decision_ref = ""
        if self.decision_trace_store is not None:
            preflight_trace = preflight.as_decision_trace_preflight()
            if checkpoint_id:
                preflight_trace["checkpoint_id"] = checkpoint_id
            decision = await _maybe_await(
                self.decision_trace_store.record_decision(
                    action=preflight_input.action,
                    chosen=preflight.decision.value,
                    reasoning="Tool runtime preflight blocked execution before registry/backend invocation.",
                    alternatives_considered=["execute tool immediately", "ask owner or escalate before execution"],
                    situational_factors=preflight.reasons,
                    charter_zone=preflight_input.charter_zone.value,
                    preflight=preflight_trace,
                    sensitivity=preflight_input.sensitivity.value,
                    tenant_id=str(runtime_context.tenant_id) if runtime_context.tenant_id else None,
                    agent_id=str(runtime_context.agent_id),
                    user_id=str(runtime_context.user_id),
                    session_id=runtime_context.session_id,
                    tool_name=tool_name,
                    checkpoint_id=checkpoint_id or None,
                )
            )
            decision_ref = f"decision/{decision.id}"

        await self._log_preflight_decision(tool_name, runtime_context, preflight)
        return _render_preflight_block(
            tool_name,
            preflight,
            checkpoint_id=checkpoint_id,
            decision_ref=decision_ref,
            authorization_decision_entry=authorization_decision_entry,
        )

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
                tenant_id=runtime_context.tenant_id,
                detail={
                    "tool": tool_name,
                    "decision": preflight.decision.value,
                    "reasons": preflight.reasons,
                    "requires_checkpoint": preflight.requires_checkpoint,
                    "requires_audit": preflight.requires_audit,
                    "escalation_target": preflight.escalation_target,
                    "evidence_refs": preflight.evidence_refs,
                },
            )
        )


def _build_tool_preflight_input(
    tool_name: str,
    arguments: dict,
    *,
    runtime_context: ToolExecutionContext | None = None,
    truth_evidence: tuple[Any, ...] = (),
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
            truth_evidence=truth_evidence,
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
        truth_evidence=truth_evidence,
    )


def _render_preflight_block(
    tool_name: str,
    preflight: ActionPreflightResult,
    *,
    checkpoint_id: str = "",
    decision_ref: str = "",
    authorization_decision_entry: dict[str, Any] | None = None,
) -> str:
    reason_text = ",".join(preflight.reasons) if preflight.reasons else "unspecified"
    suffix = ""
    if preflight.escalation_target:
        suffix = f" escalation_target={preflight.escalation_target}"
    if checkpoint_id:
        suffix += f" checkpoint={checkpoint_id}"
    if decision_ref:
        suffix += f" decision={decision_ref}"
    if preflight.evidence_refs:
        suffix += " evidence=" + ",".join(preflight.evidence_refs)
    rendered = f"[Preflight:{preflight.decision.value}] {tool_name} was not executed. reasons={reason_text}{suffix}"
    if authorization_decision_entry:
        rendered += "\n<authorization_decision>" + _json.dumps(
            authorization_decision_entry,
            ensure_ascii=False,
            default=str,
        ) + "</authorization_decision>"
    return rendered
