"""Runtime service for governed tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json as _json
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
from app.core.execution_context import ExecutionPrincipal
from app.runtime.decision_ledger import build_authorization_decision_entry
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
from app.services.approval_ticket import ApprovalDecisionSet, hash_tool_input
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
from app.tools.decision import ToolDecisionOutcome, build_tool_decision
from app.tools.validation import validate_tool_arguments


RuntimeResolver = Callable[..., Awaitable[ToolExecutionContext] | ToolExecutionContext]
GovernanceRunner = Callable[
    [ToolGovernanceContext, GovernanceDependencies],
    Awaitable[str | None] | str | None,
]
FallbackExecutor = Callable[[str, dict, ToolExecutionContext], Awaitable[str] | str]
ActivityLogger = Callable[..., Awaitable[None] | None]
EnsureRegistry = Callable[[], None]

_TOOL_ERROR_PAYLOAD_RE = re.compile(r"<tool_error>(.*?)</tool_error>", re.DOTALL)
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

    Sensitive values are represented by one-way hashes: the lease still changes
    when a credential-bearing value changes, but approval storage never receives
    the secret itself.
    """

    def safe(value: Any, *, key: str = "") -> Any:
        if any(marker in key.lower() for marker in ("secret", "token", "password", "credential", "api_key")):
            digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            return {"sha256": digest}
        if isinstance(value, dict):
            return {str(k): safe(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(item, key=key) for item in value]
        return value

    return {
        "tool_name": str(tool_name),
        "action_kind": str(action_kind),
        "arguments": safe({key: value for key, value in dict(arguments or {}).items() if key != "_plan_authorization"}),
    }


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


async def _renew_runtime_task_lease_before_execution() -> None:
    """Fence every tool side effect against the worker's current claim."""
    from app.config import get_settings
    from app.services.runtime_task_fence import renew_current_runtime_task_lease

    await renew_current_runtime_task_lease(lease_seconds=float(get_settings().RUNTIME_TASK_CLAIM_LEASE_SECONDS))


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
        if not wants_same_session:
            return arguments
        enriched = dict(arguments)
        if runtime_context.session_id:
            enriched["source_session_id"] = runtime_context.session_id
        else:
            enriched.pop("source_session_id", None)
        return enriched

    if tool_name == "schedule_wakeup":
        # B2: a self-pace wakeup is always same-session by definition — the
        # runtime supplies the live session id, never the model.
        enriched = dict(arguments)
        if runtime_context.session_id:
            enriched["source_session_id"] = runtime_context.session_id
        else:
            enriched.pop("source_session_id", None)
        return enriched

    if tool_name not in {"delegate_to_agent", "send_message_to_agent"}:
        return arguments

    enriched = dict(arguments)
    if runtime_context.session_id:
        enriched["parent_session_id"] = runtime_context.session_id
    if runtime_context.budget_run_id:
        enriched["_budget_run_id"] = runtime_context.budget_run_id
    if runtime_context.turn_id:
        enriched["_turn_id"] = runtime_context.turn_id
    if runtime_context.runtime_task_id:
        enriched["_runtime_task_id"] = runtime_context.runtime_task_id
    enriched["_requester_user_id"] = str(runtime_context.user_id)
    if not runtime_context.tenant_id:
        raise ValueError("A2A tool execution requires a tenant-bound runtime context")
    principal = ExecutionPrincipal(
        tenant_id=runtime_context.tenant_id,
        source_agent_id=runtime_context.agent_id,
        requester_user_id=runtime_context.user_id,
        root_session_id=runtime_context.session_id,
        root_runtime_task_id=runtime_context.runtime_task_id,
        origin="agent_tool",
    )
    # Reserved authority evidence is always overwritten with runtime-owned
    # values; model-authored arguments can never choose their own requester.
    enriched["_execution_principal"] = principal.to_evidence()

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


def _tool_result_failed(result: Any) -> bool:
    rendered = str(result or "")
    if _extract_tool_error_payload(rendered) is not None or rendered.lstrip().startswith("❌"):
        return True
    try:
        payload = _json.loads(rendered)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and (
        payload.get("ok") is False
        or str(payload.get("status") or "").lower() in {"error", "failed", "rejected", "blocked"}
    )


def _capture_workspace_mutation_evidence(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: str | ToolContentEnvelope,
    workspace: Path,
    before_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    """Capture exact post-tool file states while the workspace lock is held."""
    from app.services.chat_artifact_delivery import tool_session_write_paths
    from app.services.session_workspace_snapshot import workspace_path_state

    artifacts = getattr(result, "artifacts", ()) if isinstance(result, ToolContentEnvelope) else ()
    # A rejected path-bearing write did not establish ownership. Structured
    # execution artifacts remain valid even when a command exits non-zero.
    rendered_result = str(result)
    failed = _tool_result_failed(rendered_result)
    paths = tool_session_write_paths(
        "" if failed else tool_name,
        {} if failed else arguments,
        artifacts=list(artifacts or ()),
    )
    states: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    lineage: list[dict[str, Any]] = []
    before_states = dict(before_states or {})
    artifact_by_path = {
        str(item.get("path") or ""): item for item in artifacts or () if isinstance(item, dict) and item.get("path")
    }
    for path in paths:
        normalized_path = str(path or "").replace("\\", "/").strip()
        if not normalized_path.startswith("workspace/"):
            errors[normalized_path] = "outside_rewind_workspace"
            continue
        try:
            state = workspace_path_state(workspace=Path(workspace).resolve() / "workspace", path=normalized_path)
            states[str(state["path"])] = state
            artifact = artifact_by_path.get(normalized_path)
            before_state = artifact.get("before_state") if isinstance(artifact, dict) else None
            if not isinstance(before_state, dict):
                before_state = before_states.get(str(state["path"]))
            before_verifiable = (
                isinstance(before_state, dict)
                and before_state.get("path") == state["path"]
                and (
                    before_state.get("exists") is False
                    or (
                        before_state.get("exists") is True
                        and isinstance(before_state.get("sha256"), str)
                        and len(str(before_state.get("sha256"))) == 64
                    )
                )
            )
            if before_verifiable:
                lineage.append(
                    {
                        "path": state["path"],
                        "before_state": dict(before_state),
                        "after_state": dict(state),
                    }
                )
            else:
                errors[str(state["path"])] = "missing_exact_pre_write_state"
        except Exception as exc:  # noqa: BLE001 - evidence failure must be explicit and fail closed at rewind.
            errors[str(path)] = f"{type(exc).__name__}: {exc}"
    return states, errors, lineage


def _capture_workspace_pre_mutation_states(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    workspace: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Capture argument-addressable pre-write states under the same lock."""
    from app.services.chat_artifact_delivery import tool_session_write_paths
    from app.services.session_workspace_snapshot import workspace_path_state

    states: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for raw_path in tool_session_write_paths(tool_name, arguments):
        path = str(raw_path or "").replace("\\", "/").strip()
        if not path.startswith("workspace/"):
            errors[path] = "outside_rewind_workspace"
            continue
        try:
            state = workspace_path_state(workspace=Path(workspace).resolve() / "workspace", path=path)
            states[str(state["path"])] = state
        except Exception as exc:  # noqa: BLE001 - evidence failure is retained for fail-closed rewind.
            errors[path] = f"{type(exc).__name__}: {exc}"
    return states, errors


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
    trace_metadata_sink: dict[str, Any] | None = None,
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
        resolved_asset_refs=tuple(runtime_context.resolved_asset_refs or ()),
    )
    payload = _json_safe_runtime_value(asdict(frame))
    runtime_context.tool_execution_frames.append(payload)
    if trace_metadata_sink is not None:
        trace_metadata_sink["tool_execution_frame"] = payload
        trace_metadata_sink["resolved_asset_refs"] = payload.get("resolved_asset_refs", [])
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


def _record_final_tool_decision(
    *,
    trace_metadata_sink: dict[str, Any] | None,
    runtime_context: ToolExecutionContext,
    tool_name: str,
    arguments: dict[str, Any],
    outcome: ToolDecisionOutcome,
    reason_codes: tuple[str, ...],
    tool_call_id: str,
    governance_context: ToolGovernanceContext | None = None,
) -> dict[str, Any]:
    profile = runtime_context.permission_profile
    if is_dataclass(profile):
        policy_snapshot = asdict(profile)
    elif isinstance(profile, dict):
        policy_snapshot = dict(profile)
    else:
        policy_snapshot = {"mode": "default"}
    if governance_context is not None:
        guard_policy_snapshot = getattr(governance_context, "guard_policy_snapshot", None)
        guard_policy_verdict = getattr(governance_context, "guard_policy_verdict", None)
        if guard_policy_snapshot is not None:
            policy_snapshot["guard_policy"] = dict(guard_policy_snapshot)
        if guard_policy_verdict is not None:
            policy_snapshot["guard_policy_verdict"] = dict(guard_policy_verdict)
    from app.tools.registry import tool_spec_v1

    spec = tool_spec_v1(tool_name)
    capability_snapshot = {
        "descriptor": asdict(spec) if spec is not None else {"tool_name": tool_name},
        "resolved_asset_refs": [
            _json_safe_runtime_value(asdict(ref)) for ref in tuple(runtime_context.resolved_asset_refs or ())
        ],
    }
    live_capability_snapshot = getattr(governance_context, "capability_snapshot", None)
    if live_capability_snapshot is not None:
        capability_snapshot["live_policy"] = dict(live_capability_snapshot)
    decision = build_tool_decision(
        decision_id=getattr(governance_context, "decision_id", None),
        tenant_id=runtime_context.tenant_id,
        agent_id=runtime_context.agent_id,
        actor_user_id=runtime_context.user_id,
        tool_name=tool_name,
        arguments=arguments,
        policy_snapshot=policy_snapshot,
        capability_snapshot=capability_snapshot,
        outcome=outcome,
        reason_codes=reason_codes,
        delegated_by=getattr(
            getattr(governance_context, "delegation_token", None),
            "parent_agent_id",
            None,
        ),
        approval_id=getattr(governance_context, "approval_id", None),
        runtime_task_id=runtime_context.runtime_task_id,
        session_id=runtime_context.session_id,
        trace_id=(runtime_context.round_state or {}).get("trace_id"),
        idempotency_key=f"tool-call:{tool_call_id}",
    )
    payload = decision.to_dict()
    if trace_metadata_sink is not None:
        trace_metadata_sink.update(
            {
                "tool_decision": payload,
                "decision_id": decision.decision_id,
                "input_hash": decision.input_hash,
                "policy_snapshot_hash": decision.policy_snapshot_hash,
                "capability_snapshot_hash": decision.capability_snapshot_hash,
                "idempotency_key": decision.idempotency_key,
                "authority_policy_snapshot": policy_snapshot,
                "authority_capability_snapshot": capability_snapshot,
            }
        )
    return payload


def _record_precontext_tool_decision(
    *,
    trace_metadata_sink: dict[str, Any] | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    permission_profile: Any,
    tool_name: str,
    arguments: dict[str, Any],
    outcome: ToolDecisionOutcome,
    reason_codes: tuple[str, ...],
    tool_call_id: str,
    runtime_task_id: str | None,
    session_id: str | None,
) -> None:
    if trace_metadata_sink is None:
        return
    if is_dataclass(permission_profile):
        policy_snapshot = asdict(permission_profile)
    elif isinstance(permission_profile, dict):
        policy_snapshot = dict(permission_profile)
    else:
        policy_snapshot = {"mode": "default"}
    from app.tools.registry import tool_spec_v1

    spec = tool_spec_v1(tool_name)
    decision = build_tool_decision(
        decision_id=f"decision:{tool_call_id}",
        tenant_id=None,
        agent_id=agent_id,
        actor_user_id=user_id,
        tool_name=tool_name,
        arguments=arguments,
        policy_snapshot=policy_snapshot,
        capability_snapshot=asdict(spec) if spec is not None else {"tool_name": tool_name},
        outcome=outcome,
        reason_codes=reason_codes,
        runtime_task_id=runtime_task_id,
        session_id=session_id,
        idempotency_key=f"tool-call:{tool_call_id}",
    )
    trace_metadata_sink.update(
        {
            "tool_decision": decision.to_dict(),
            "decision_id": decision.decision_id,
            "input_hash": decision.input_hash,
            "policy_snapshot_hash": decision.policy_snapshot_hash,
            "capability_snapshot_hash": decision.capability_snapshot_hash,
            "idempotency_key": decision.idempotency_key,
        }
    )


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
    capability_group_policy_loader: (
        Callable[[ToolExecutionContext], Awaitable[dict[str, bool]] | dict[str, bool]] | None
    ) = None
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
    approval_ticket_consumer: Any | None = None
    approval_ticket_completer: Any | None = None
    asset_ref_resolver: Callable[..., Any] | None = None
    asset_usage_recorder: Callable[..., Any] | None = None

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
        if self.plan_mode_gate is None:
            self.plan_mode_gate = get_plan_mode_gate()
        if self.plan_mode_session_factory is None:
            from app.database import async_session

            self.plan_mode_session_factory = async_session
        if self.plan_mode_service is None:
            from app.services.plan_mode_service import get_plan_mode_service

            self.plan_mode_service = get_plan_mode_service()
        if self.approval_ticket_consumer is None or self.approval_ticket_completer is None:
            from app.services.approval_ticket import complete_approval_ticket, consume_approval_ticket

            self.approval_ticket_consumer = self.approval_ticket_consumer or consume_approval_ticket
            self.approval_ticket_completer = self.approval_ticket_completer or complete_approval_ticket
        if self.asset_ref_resolver is None or self.asset_usage_recorder is None:
            from app.services.ai_asset_resolution import record_tool_asset_usage, resolve_tool_asset_refs

            self.asset_ref_resolver = self.asset_ref_resolver or resolve_tool_asset_refs
            self.asset_usage_recorder = self.asset_usage_recorder or record_tool_asset_usage

    @staticmethod
    async def _emit_loaded_instruction_hook(
        *,
        tool_name: str,
        arguments: dict,
        context: ToolExecutionContext,
        tool_call_id: str,
        result: str | ToolContentEnvelope,
    ) -> None:
        if tool_name != "load_skill" or _tool_result_failed(result):
            return
        skill_name = str(arguments.get("name") or "").strip()
        if not skill_name:
            return
        from app.runtime.hooks import HookEvent, emit_hook

        result_text = str(result)
        await emit_hook(
            HookEvent.INSTRUCTIONS_LOADED,
            agent_id=context.agent_id,
            session_id=context.session_id,
            source="load_skill",
            metadata={
                "tenant_id": context.tenant_id,
                "user_id": str(context.user_id),
                "runtime_task_id": context.runtime_task_id,
                "tool_call_id": tool_call_id,
                "instruction_uri": f"agent://{context.agent_id}/skills/{skill_name}",
                "instruction_scope": "skill",
                "load_reason": "load_skill",
                "content_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
                "content_chars": len(result_text),
            },
        )

    async def _record_resolved_asset_usage_for_tool(
        self,
        *,
        tool_name: str,
        context: ToolExecutionContext,
        tool_call_id: str,
        result: str | ToolContentEnvelope,
    ) -> None:
        if not context.resolved_asset_refs or _tool_result_failed(result):
            return
        from app.services.ai_asset_resolution import record_tool_asset_usage

        recorder = self.asset_usage_recorder or record_tool_asset_usage
        await _maybe_await(
            recorder(
                asset_refs=tuple(context.resolved_asset_refs),
                context=context,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        )

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
    ) -> str | ToolContentEnvelope:
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
            consumed_plan_authorization=(
                _approval_decision.plan_authorization if _approval_decision is not None else None
            ),
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
                if "approval_required" in str(governance_block)
                or "session_permission_required" in str(governance_block)
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

    async def execute_approved(
        self,
        *,
        approval_id: uuid.UUID,
        expected_agent_id: uuid.UUID | None = None,
        approved_by_user_id: uuid.UUID | None = None,
    ) -> str | ToolContentEnvelope:
        """Consume one immutable approval ticket and execute its exact request.

        No tool name or arguments are accepted here: callers cannot approve one
        payload and replace it at execution time.
        """
        ticket = await _maybe_await(
            self.approval_ticket_consumer(
                approval_id=approval_id,
                expected_agent_id=expected_agent_id,
                expected_user_id=approved_by_user_id,
            )
        )
        from app.services.approval_ticket import restore_approval_execution_envelope

        envelope = restore_approval_execution_envelope(
            ticket.execution_envelope,
            expected_hash=ticket.execution_envelope_hash,
        )
        if (
            envelope.tenant_id != ticket.tenant_id
            or envelope.agent_id != ticket.agent_id
            or envelope.requester_user_id != ticket.requested_by_user_id
        ):
            raise RuntimeError("approval_execution_envelope_authority_mismatch")
        decision = ApprovalDecisionSet(
            approval_id=ticket.approval_id,
            tenant_id=ticket.tenant_id,
            action_type=ticket.action_type,
            tool_name=ticket.tool_name,
            input_hash=ticket.input_hash,
            policy_snapshot_hash=ticket.policy_snapshot_hash,
            envelope_hash=ticket.execution_envelope_hash,
            decision_id=ticket.decision_id,
            requested_by_user_id=ticket.requested_by_user_id,
            approved_by_user_id=ticket.approved_by_user_id,
            plan_authorization=(
                dict(ticket.arguments.get("_plan_authorization"))
                if isinstance(ticket.arguments.get("_plan_authorization"), dict)
                else None
            ),
        )
        trace_metadata: dict[str, Any] = {}
        receipt = {
            "tool_name": ticket.tool_name,
            "requested_by_user_id": str(ticket.requested_by_user_id),
            "approved_by_user_id": str(ticket.approved_by_user_id),
            "input_hash": ticket.input_hash,
            "policy_snapshot_hash": ticket.policy_snapshot_hash,
            "idempotency_key": ticket.idempotency_key,
            "decision_id": ticket.decision_id,
            "execution_envelope_hash": ticket.execution_envelope_hash,
        }
        try:
            result = await self.execute(
                ticket.tool_name,
                ticket.arguments,
                agent_id=ticket.agent_id,
                user_id=ticket.requested_by_user_id,
                execution_identity=envelope.execution_identity,
                tool_call_id=envelope.tool_call_id,
                delegation_token=envelope.delegation_token,
                session_id=envelope.session_id,
                permission_profile=envelope.permission_profile,
                turn_id=envelope.turn_id,
                runtime_task_id=envelope.runtime_task_id,
                budget_run_id=envelope.budget_run_id,
                origin_channel=envelope.origin_channel,
                round_state=dict(envelope.round_state),
                t0_refs=tuple(envelope.t0_refs),
                plan_mode_interactive_available=envelope.plan_mode_interactive_available,
                plan_mode_unattended_available=envelope.plan_mode_unattended_available,
                emit_runtime_hooks=envelope.emit_runtime_hooks,
                trace_metadata_sink=trace_metadata,
                workspace_override=envelope.workspace,
                _approval_decision=decision,
                _expected_asset_refs=tuple(envelope.resolved_asset_refs),
            )
        except Exception as exc:
            await _maybe_await(
                self.approval_ticket_completer(
                    approval_id=ticket.approval_id,
                    tenant_id=ticket.tenant_id,
                    status="failed",
                    result=f"{type(exc).__name__}: {exc}",
                    receipt={
                        **receipt,
                        "status": "failed",
                        "error_class": type(exc).__name__,
                    },
                )
            )
            raise
        result_text = str(result)
        tool_error = _extract_tool_error_payload(result_text)
        execution_status = "failed" if tool_error else "succeeded"
        await _maybe_await(
            self.approval_ticket_completer(
                approval_id=ticket.approval_id,
                tenant_id=ticket.tenant_id,
                status=execution_status,
                result=result_text,
                receipt={
                    **receipt,
                    "status": execution_status,
                    "tool_decision": trace_metadata.get("tool_decision"),
                    "runtime_evidence": trace_metadata,
                    **({"error": tool_error} if tool_error else {}),
                },
            )
        )
        return result

    async def execute_with_context(
        self,
        tool_name: str,
        arguments: dict,
        context: ToolExecutionContext,
        *,
        trace_metadata_sink: dict[str, Any] | None = None,
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

        if not context.resolved_asset_refs:
            from app.services.ai_asset_resolution import resolve_tool_asset_refs

            resolver = self.asset_ref_resolver or resolve_tool_asset_refs
            context.resolved_asset_refs = tuple(
                await _maybe_await(resolver(tool_name=tool_name, arguments=dict(arguments or {}), context=context))
                or ()
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
            trace_metadata_sink=trace_metadata_sink,
        )
        try:
            from app.services.session_workspace_snapshot import async_agent_workspace_lock
            from app.tools.registry import is_workspace_mutating_tool

            workspace_mutating = context.agent_id is not None and is_workspace_mutating_tool(tool_name)

            async def _execute_backend() -> str | ToolContentEnvelope:
                before_states: dict[str, dict[str, Any]] = {}
                before_errors: dict[str, str] = {}
                if workspace_mutating and trace_metadata_sink is not None:
                    before_states, before_errors = _capture_workspace_pre_mutation_states(
                        tool_name=tool_name,
                        arguments=arguments,
                        workspace=context.workspace,
                    )
                value = await self.backend.execute(request, _execute_request)
                if context.workspace_authority_scope is not None:
                    from app.services.workspace_resource_authority import record_workspace_tool_mutations

                    await record_workspace_tool_mutations(
                        context=context,
                        tool_name=tool_name,
                        arguments=arguments,
                        result=value,
                    )
                if workspace_mutating and trace_metadata_sink is not None:
                    states, errors, lineage = _capture_workspace_mutation_evidence(
                        tool_name=tool_name,
                        arguments=arguments,
                        result=value,
                        workspace=context.workspace,
                        before_states=before_states,
                    )
                    trace_metadata_sink["workspace_mutation_evidence_captured"] = True
                    trace_metadata_sink["workspace_mutation_states"] = states
                    trace_metadata_sink["workspace_mutation_state_errors"] = {**before_errors, **errors}
                    trace_metadata_sink["workspace_mutation_lineage"] = lineage
                return value

            if workspace_mutating:
                async with async_agent_workspace_lock(context.agent_id):
                    result = await _execute_backend()
            else:
                result = await _execute_backend()
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
                trace_metadata_sink=trace_metadata_sink,
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
        result_failed = _tool_result_failed(result)
        _record_tool_execution_frame(
            context,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            executor=self.backend.name if self.backend else "unknown",
            arguments=arguments,
            status="failed" if result_failed else "completed",
            result=str(result),
            started_at=started_frame.get("started_at"),
            trace_metadata_sink=trace_metadata_sink,
        )
        if owns_lifecycle:
            _record_tool_lifecycle(
                context,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state="failed" if result_failed else "completed",
                original_arguments=original_arguments,
                effective_arguments=dict(arguments or {}),
            )
        await self._emit_loaded_instruction_hook(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
            tool_call_id=tool_call_id,
            result=result,
        )
        await self._record_resolved_asset_usage_for_tool(
            tool_name=tool_name,
            context=context,
            tool_call_id=tool_call_id,
            result=result,
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
        user_id: uuid.UUID,
        session_id: str | None,
        runtime_task_id: str | None,
        evidence_id: str,
        authorization_sink: dict[str, Any] | None = None,
        plan_mode_interactive_available: bool = False,
        plan_mode_unattended_available: bool = False,
        consumed_plan_authorization: dict[str, Any] | None = None,
        approval_tenant_id: str | None = None,
    ) -> str | None:
        """Return a confirmation-required JSON block when a gated action is blocked.

        This is the confirmation backstop, shared by every execution
        entrypoint (normal execution or immutable-ticket approved execution) so
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

        if consumed_plan_authorization is not None:
            from app.services.plan_authorization_lease import (
                PlanAuthorizationLeaseError,
                canonical_action_artifact,
                canonical_json,
                verify_consumed_plan_authorization_lease,
            )

            expected_runtime_task_id = (
                str(runtime_task_id) if runtime_task_id is not None and session_id is None else None
            )
            live_bindings = {
                "target_ref": f"tool:{tool_name}",
                "requester_user_id": str(user_id),
                "session_id": str(session_id) if session_id is not None else None,
                "runtime_task_id": expected_runtime_task_id,
                "evidence_id": str(evidence_id),
            }
            mismatch = next(
                (
                    key
                    for key, expected in live_bindings.items()
                    if (
                        str(consumed_plan_authorization.get(key))
                        if consumed_plan_authorization.get(key) is not None
                        else None
                    )
                    != (str(expected) if expected is not None else None)
                ),
                None,
            )
            try:
                if mismatch is not None:
                    raise PlanAuthorizationLeaseError(
                        "evidence_mismatch",
                        f"approved plan authorization {mismatch} no longer matches the runtime",
                    )
                if not approval_tenant_id or not confirmed_plan_id:
                    raise PlanAuthorizationLeaseError(
                        "authority_missing",
                        "approved plan authorization is missing tenant or plan authority",
                    )
                lease = await verify_consumed_plan_authorization_lease(
                    tenant_id=approval_tenant_id,
                    agent_id=agent_id,
                    plan_id=confirmed_plan_id,
                    evidence=consumed_plan_authorization,
                    session_factory=self.plan_mode_session_factory,
                )
                canonical_artifact = canonical_action_artifact(action_artifact)
                live_args_hash = hashlib.sha256(canonical_json(canonical_artifact).encode("utf-8")).hexdigest()
                if (
                    lease.binding.action_kind != action_kind
                    or lease.binding.target_ref != f"tool:{tool_name}"
                    or lease.binding.canonical_args_hash != live_args_hash
                ):
                    raise PlanAuthorizationLeaseError(
                        "action_mismatch",
                        "approved plan authorization no longer matches this exact tool action",
                    )
            except PlanAuthorizationLeaseError as exc:
                return render_tool_error(
                    tool_name=tool_name,
                    error_class=f"approval_plan_authorization_{exc.code}",
                    message=exc.message,
                    provider="approval_execution_kernel",
                    retryable=False,
                    actionable_hint="Submit a new approval request from a newly confirmed plan.",
                )
            if authorization_sink is not None:
                authorization_sink.update(dict(consumed_plan_authorization))
            return None

        async with self.plan_mode_session_factory() as db:
            decision = await self.plan_mode_gate.check(
                db,
                agent_id=agent_id,
                requester_user_id=user_id,
                session_id=session_id,
                runtime_task_id=runtime_task_id,
                action_kind=action_kind,
                target_ref=f"tool:{tool_name}",
                confirmed_plan_id=confirmed_plan_id,
                plan_version=plan_version,
                plan_hash=plan_hash,
                action_artifact=action_artifact,
                evidence_id=evidence_id,
            )
            if getattr(decision, "authorization_lease_id", None):
                # This session is owned only by the tool gate; without an
                # explicit commit the row-locked lease consumption would be
                # rolled back when the context closes and could be replayed.
                await db.commit()

        if not decision.needs_plan:
            if getattr(decision, "authorization_lease_id", None) and authorization_sink is not None:
                authorization_sink.update(
                    {
                        "schema": "hive.plan_authorization_evidence.v1",
                        "lease_id": str(decision.authorization_lease_id),
                        "canonical_args_hash": str(decision.canonical_args_hash or ""),
                        "target_ref": str(decision.target_ref or f"tool:{tool_name}"),
                        "requester_user_id": str(user_id),
                        "session_id": str(session_id) if session_id is not None else None,
                        "runtime_task_id": (
                            str(runtime_task_id) if runtime_task_id is not None and session_id is None else None
                        ),
                        "evidence_id": str(evidence_id),
                    }
                )
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

        preflight_input = _build_tool_preflight_input(
            tool_name,
            arguments,
            runtime_context=runtime_context,
        )
        preflight = self.preflight_service.evaluate(preflight_input)
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
                owner_user_id=runtime_context.user_id,
                root_session_id=runtime_context.session_id,
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
) -> ActionPreflightInput:
    args_text = _json.dumps(arguments, ensure_ascii=False, default=str)
    privacy = PrivacyLayer().classify_and_mask(args_text)
    sensitivity = privacy.sensitivity
    lower_action = f"{tool_name} {args_text}".lower()
    company_conflict = any(pattern in lower_action for pattern in _COMPANY_CONFLICT_PATTERNS)
    execution_identity = getattr(runtime_context, "execution_identity", None) if runtime_context is not None else None
    from app.tools.registry import tool_execution_policy

    execution_policy = tool_execution_policy(tool_name)
    explicit_user_authorized = bool(
        execution_policy.delegated_user_authorized
        and getattr(execution_identity, "identity_type", None) in {"delegated_user", "external_principal_bound"}
    )

    if execution_policy.external_visible:
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
        rendered += (
            "\n<authorization_decision>"
            + _json.dumps(
                authorization_decision_entry,
                ensure_ascii=False,
                default=str,
            )
            + "</authorization_decision>"
        )
    return rendered
