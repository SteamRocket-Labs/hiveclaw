"""CCPlus V1 runtime contract dataclasses.

These dataclasses are the stable shape that implementation packages refine.
They intentionally avoid DB or framework imports so tests, projections, and
runtime adapters can share them without coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only import (PEP 563 string annotations). Importing TerminalReason
    # at runtime would create a cycle once app.kernel.engine imports this module
    # (engine -> ccplus_contracts -> app.kernel.contracts -> app.kernel.__init__ -> engine).
    from app.kernel.contracts import TerminalReason


class TurnStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    BLOCKED_BY_HOOK = "blocked_by_hook"
    WAITING_FOR_CHILD = "waiting_for_child"
    WAITING_FOR_WORKFLOW = "waiting_for_workflow"
    COMPACTING = "compacting"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    DONT_ASK = "dontAsk"
    AUTO = "auto"
    BYPASS_PERMISSIONS = "bypassPermissions"


DEFAULT_CCPLUS_PERMISSION_MODE = PermissionMode.DEFAULT


class SandboxProfile(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS_LOCAL_ONLY = "full_access_local_only"
    EXTERNAL_SANDBOX = "external_sandbox"


DEFAULT_CCPLUS_WRITABLE_ROOTS: tuple[str, ...] = ("workspace/",)


@dataclass(frozen=True, slots=True)
class PermissionProfileV1:
    mode: PermissionMode = DEFAULT_CCPLUS_PERMISSION_MODE
    approval_policy: str = "granular"
    writable_roots: tuple[str, ...] = DEFAULT_CCPLUS_WRITABLE_ROOTS
    readable_roots: tuple[str, ...] = ()
    denied_reads: tuple[str, ...] = ()
    network_access: str = "governed"
    sandbox: SandboxProfile = SandboxProfile.WORKSPACE_WRITE
    request_permission_enabled: bool = True
    allowed_tools: tuple[str, ...] = ()
    denied_actions: tuple[str, ...] = ()
    capability_policy_snapshot: dict[str, Any] = field(default_factory=dict)
    default_decision: str = "escalate"


_PERMISSION_MODE_ALIASES: dict[str, PermissionMode] = {
    PermissionMode.DEFAULT.value: PermissionMode.DEFAULT,
    PermissionMode.PLAN.value: PermissionMode.PLAN,
    PermissionMode.ACCEPT_EDITS.value: PermissionMode.ACCEPT_EDITS,
    PermissionMode.DONT_ASK.value: PermissionMode.DONT_ASK,
    PermissionMode.AUTO.value: PermissionMode.AUTO,
    PermissionMode.BYPASS_PERMISSIONS.value: PermissionMode.BYPASS_PERMISSIONS,
    # Legacy Hive names kept for persisted RuntimeTask / ChatSession metadata.
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "dont_ask_low_risk": PermissionMode.DONT_ASK,
    "dont_ask": PermissionMode.DONT_ASK,
    "auto_review": PermissionMode.AUTO,
    "break_glass": PermissionMode.BYPASS_PERMISSIONS,
    "full_access": PermissionMode.BYPASS_PERMISSIONS,
    "bypass_permissions": PermissionMode.BYPASS_PERMISSIONS,
}


def normalize_permission_mode(value: Any) -> PermissionMode:
    """Normalize CC permission mode values plus legacy Hive aliases."""
    if isinstance(value, PermissionMode):
        return value
    text = str(value or "").strip()
    if not text:
        return PermissionMode.DEFAULT
    return _PERMISSION_MODE_ALIASES.get(text, PermissionMode.DEFAULT)


def tenant_permission_default_from_value(value: Any) -> str:
    """Return the only permission modes an organization may set by default."""
    raw = value.get("mode") if isinstance(value, dict) else None
    mode = normalize_permission_mode(raw).value if raw else PermissionMode.DEFAULT.value
    return mode if mode in {PermissionMode.DEFAULT.value, PermissionMode.AUTO.value} else PermissionMode.DEFAULT.value


@dataclass(frozen=True, slots=True)
class ContextPolicyV1:
    model_window: int
    history_limit: int | None = None
    output_reserve: int = 20_000
    tool_result_inline_limit: int = 50_000
    round_tool_result_budget: int = 200_000
    microcompact_threshold: float = 0.60
    autocompact_threshold: float = 0.75
    prompt_too_long_retries: int = 3
    compaction_prompt_version: str = "ccplus_v1"
    recovery_manifest_required: bool = True
    compaction_trace_required: bool = True
    autocompact_failure_breaker_limit: int = 3
    breaker_half_open_seconds: int = 600


def build_context_policy(model_window: int, *, overrides: dict[str, Any] | None = None) -> ContextPolicyV1:
    """Canonical ContextPolicyV1 builder.

    The live kernel constructs its context policy here so the contract — not
    scattered module constants — is the source of truth that governs context
    behavior (thresholds, budgets, retries, breaker). ``overrides`` lets a
    per-tenant/per-session metadata blob refine the policy without bypassing the
    contract; unknown keys and ``model_window`` overrides are ignored.
    """
    base = ContextPolicyV1(model_window=int(model_window or 0))
    if not overrides:
        return base
    valid = {f.name for f in dataclass_fields(base)} - {"model_window"}
    clean = {key: value for key, value in overrides.items() if key in valid}
    return replace(base, **clean) if clean else base


def build_permission_profile(overrides: dict[str, Any] | None = None) -> PermissionProfileV1:
    """Canonical PermissionProfileV1 builder.

    Runtime/session metadata may carry a permission profile override, but the
    live governance path must only accept fields declared by the contract. This
    keeps projection, runtime context, and governance on the same shape and
    prevents arbitrary metadata keys from becoming authority.
    """
    base = PermissionProfileV1()
    if not overrides:
        return base
    valid = {f.name for f in dataclass_fields(base)}
    clean = {key: value for key, value in overrides.items() if key in valid}
    if "mode" in clean:
        clean["mode"] = normalize_permission_mode(clean["mode"])
    return replace(base, **clean) if clean else base


@dataclass(frozen=True, slots=True)
class ToolSpecV1:
    name: str
    capability: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    read_only: bool = False
    destructive: bool = False
    concurrency_safe: bool = False
    defer_loading: bool = False
    always_load: bool = False
    permission_axes: tuple[str, ...] = ()
    sandbox_requirements: tuple[str, ...] = ()
    mcp_info: dict[str, Any] = field(default_factory=dict)
    result_budget: int | None = None
    timeout_seconds: float = 30.0
    risk_class: str = "standard"
    retry_policy: str = "none"
    idempotency_scope: str = "none"
    external_visible: bool = False
    delegated_user_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultV1:
    text: str
    structured_content: dict[str, Any] | None = None
    new_messages: tuple[dict[str, Any], ...] = ()
    context_modifier: dict[str, Any] | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    t0_refs: tuple[str, ...] = ()
    invocation_span_id: str | None = None
    mcp_meta: dict[str, Any] | None = None
    permission_request: dict[str, Any] | None = None
    terminal_signal: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallLifecycleV1:
    """Canonical per-call lifecycle ledger for a model-issued tool call."""

    session_id: str
    turn_id: str | None
    tool_call_id: str
    tool_name: str
    lifecycle_state: str = "created"
    original_arguments: dict[str, Any] = field(default_factory=dict)
    effective_arguments: dict[str, Any] = field(default_factory=dict)
    schema_ref: str | None = None
    capability: str | None = None
    taxonomy_layer: str | None = None
    governance_decisions: tuple[str, ...] = ()
    hook_refs: tuple[str, ...] = ()
    permission_request_id: str | None = None
    truth_evidence_refs: tuple[str, ...] = ()
    invocation_span_id: str | None = None
    t0_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedAssetRefV1:
    """Immutable identity of one AI asset revision consumed by a runtime.

    Native selectors such as a Skill display name are intentionally absent:
    the ref is minted only after the native runtime has resolved the concrete
    asset and the control plane has bound its active ConfigRevision.
    """

    asset_id: str
    asset_type: str
    native_key: str
    revision_id: str
    revision_version: int
    content_hash: str
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionFrameV1:
    """Execution-side frame after a tool call passes schema and governance."""

    tool_call_id: str
    tool_name: str
    executor: str
    sandbox_profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE
    input_hash: str | None = None
    output_hash: str | None = None
    artifact_refs: tuple[str, ...] = ()
    provider: str | None = None
    mcp_server_id: str | None = None
    workflow_run_id: str | None = None
    subagent_session_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    status: str = "pending"
    invocation_span_id: str | None = None
    t0_refs: tuple[str, ...] = ()
    resolved_asset_refs: tuple[ResolvedAssetRefV1, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingToolFrameV1:
    """Durable same-turn frame for an L3 permission pause."""

    permission_request_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    turn_id: str | None = None
    runtime_task_id: str | None = None
    origin_channel: str | None = None
    permission_profile: PermissionProfileV1 = field(default_factory=PermissionProfileV1)
    round_state: dict[str, Any] = field(default_factory=dict)
    knowledge_refs: tuple[str, ...] = ()
    hook_refs: tuple[str, ...] = ()
    t0_refs: tuple[str, ...] = ()
    created_at: str | None = None
    expires_at: str | None = None
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class PermissionCheckpointV1:
    """Auditable user or policy decision for a PendingToolFrameV1."""

    permission_request_id: str
    decision: str
    pending_frame: PendingToolFrameV1
    resolver_user_id: str | None = None
    resolved_at: str | None = None
    resolution_channel: str | None = None
    continuation_runtime_task_id: str | None = None
    denial_tool_result_ref: str | None = None
    invocation_span_id: str | None = None
    t0_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookLifecycleV1:
    """Trace record for one hook run."""

    hook_run_id: str
    event: str
    source: str
    trust_level: str
    lifecycle_state: str
    matcher_fields: dict[str, Any] = field(default_factory=dict)
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    timeout_ms: int | None = None
    decision: str = "observed"
    original_hash: str | None = None
    modified_hash: str | None = None
    result_hash: str | None = None
    additional_context_refs: tuple[str, ...] = ()
    output_rewrite_ref: str | None = None
    failure_policy: str = "fail_closed_if_blocking"
    span_ref: str | None = None
    t0_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompactionLifecycleV1:
    """Typed compact boundary spanning hooks, summary, replacement, and recovery."""

    compaction_id: str
    session_id: str
    trigger: str
    turn_id: str | None = None
    runtime_task_id: str | None = None
    before_message_count: int = 0
    after_message_count: int = 0
    before_token_estimate: int | None = None
    after_token_estimate: int | None = None
    summary_ref: str | None = None
    replacement_history_ref: str | None = None
    recovery_manifest_ref: str | None = None
    pre_hook_refs: tuple[str, ...] = ()
    post_hook_refs: tuple[str, ...] = ()
    context_status_refs: tuple[str, ...] = ()
    t0_checkpoint_refs: tuple[str, ...] = ()
    invocation_span_id: str | None = None
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class TruthEvidencePackV1:
    """Source-bound evidence pack for governance and answer citation."""

    evidence_id: str
    query: str
    source_refs: tuple[str, ...]
    citations: tuple[str, ...] = ()
    snippets: tuple[str, ...] = ()
    acl_scope: str = "tenant"
    digest: str | None = None
    provider: str = "knowledge_core"
    freshness: str | None = None
    confidence: float | None = None
    limitations: tuple[str, ...] = ()
    prompt_injection_stripped: bool = False
    tenant_id: str | None = None
    owner_id: str | None = None
    company_id: str | None = None
    trace_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnStateV1:
    session_id: str
    runtime_task_id: str | None = None
    turn_id: str | None = None
    status: TurnStatus = TurnStatus.ACCEPTED
    terminal_reason: TerminalReason | None = None
    accepted_prompt_event_id: str | None = None
    t0_event_refs: tuple[str, ...] = ()
    active_tool_call_ids: tuple[str, ...] = ()
    pending_steer_messages: tuple[dict[str, Any], ...] = ()
    permission_profile_snapshot: PermissionProfileV1 | None = None
    context_policy_snapshot: ContextPolicyV1 | None = None


@dataclass(frozen=True, slots=True)
class AgentSessionV1:
    session_id: str
    root_session_id: str | None = None
    parent_session_id: str | None = None
    session_kind: str = "web_chat"
    actor_type: str = "user"
    source: str = "web"
    workspace_roots: tuple[str, ...] = ()
    permission_profile: PermissionProfileV1 = field(default_factory=PermissionProfileV1)
    context_policy: ContextPolicyV1 | None = None
    active_turn: TurnStateV1 | None = None
    t0_segment_refs: tuple[str, ...] = ()
    runtime_task_refs: tuple[str, ...] = ()
    hook_event_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionNodeV1:
    session_id: str
    actor_type: str = "user"
    session_kind: str = "web_chat"
    source: str = "web"
    runtime_task_id: str | None = None
    parent_session_id: str | None = None
    root_session_id: str | None = None
    agent_id: str | None = None
    status: str | None = None
    transcript_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionEdgeV1:
    relation: str  # parent_child | delegated_to | team_member | workflow_leaf
    from_id: str
    to_id: str
    runtime_task_id: str | None = None
    task_type: str | None = None


@dataclass(frozen=True, slots=True)
class SessionGraphV1:
    root_session_id: str
    nodes: tuple[SessionNodeV1, ...] = ()
    edges: tuple[SessionEdgeV1, ...] = ()
    mailbox_events: tuple[dict[str, Any], ...] = ()
    task_notifications: tuple[dict[str, Any], ...] = ()
    transcript_refs_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtensionDescriptorV1:
    id: str
    type: str
    source: str
    trust_level: str = "tenant"
    owner_scope: str = "tenant"
    enabled_scope: str = "agent"
    exposed_tools: tuple[str, ...] = ()
    deferred_tools: tuple[str, ...] = ()
    hook_events: tuple[str, ...] = ()
    permission_requirements: tuple[str, ...] = ()
    install_review: dict[str, Any] = field(default_factory=dict)
    runtime_effects: tuple[str, ...] = ()
    audit_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionRegistryV1:
    extensions: tuple[ExtensionDescriptorV1, ...] = ()


@dataclass(frozen=True, slots=True)
class GovernanceCapabilityDescriptorV1:
    """Single-source taxonomy descriptor for runtime and product capability surfaces."""

    name: str
    layer: str
    tools: tuple[str, ...] = ()
    default_enabled: bool = True
    l2_visible: bool = False
    enterprise_toggleable: bool = False
    source: str = "core"
    notes: str = ""
    provider: str | None = None
    requires_local_bridge: bool = False
    aliases: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    audit_refs: tuple[str, ...] = ()
