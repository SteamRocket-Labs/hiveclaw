"""Explicit multi-agent orchestration helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from app.agents.coordination_gateway import CoordinationGateway
from app.agents.coordination_wiring import gateway_scope
from app.agents.delegation_token import DEFAULT_DELEGATION_TTL_SECONDS, issue_delegation_token
from app.agents.tool_policies import DELEGATED_WORKER_BASE_EXCLUDED_TOOLS
from app.kernel.contracts import ExecutionIdentityRef
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.agent_tools import CORE_TOOL_NAMES
from app.services.capability_gate import CAPABILITY_MAP
from app.services.runtime_task_service import (
    build_restart_reconciliation_metadata,
    build_restart_replay_contract,
    build_restart_replay_journal_entry,
    create_runtime_task_record,
    get_runtime_task_record,
    list_active_runtime_task_records,
    merge_restart_replay_journal,
    update_runtime_task_record,
)

logger = logging.getLogger(__name__)

AGENT_MESSAGE_TIMEOUT_SECONDS = 300.0
ASYNC_DELEGATION_TIMEOUT_SECONDS = 600.0
ASYNC_DELEGATION_CANCEL_GRACE_SECONDS = 180.0
MAX_DELEGATION_TIMEOUT_SECONDS = 3600.0

ToolExecutor = Callable[..., Awaitable[str] | str]
_DELEGATION_BASE_EXCLUDED_TOOLS = DELEGATED_WORKER_BASE_EXCLUDED_TOOLS
_DELEGATION_MEMORY_WRITE_TOOLS = (
    "save_memory",
    "update_memory",
    "retire_memory",
    "submit_t3_consolidation_pitch",
    "submit_t3_memory_gate_review",
    "submit_t3_revised_patch",
)


@dataclass(slots=True, frozen=True)
class DelegationToolProfile:
    name: str
    core_tools_only: bool
    allowed_tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]
    tool_policy: str
    tool_rule: str
    memory_policy: str
    memory_rule: str


_DELEGATION_TOOL_PROFILES: dict[str, DelegationToolProfile] = {
    "worker_safe": DelegationToolProfile(
        name="worker_safe",
        core_tools_only=True,
        allowed_tools=(),
        excluded_tools=_DELEGATION_BASE_EXCLUDED_TOOLS
        + ("save_skill", "search_memory", "load_memory")
        + _DELEGATION_MEMORY_WRITE_TOOLS,
        tool_policy="worker_safe",
        tool_rule=(
            "Your tool surface is worker-safe: do the delegated work, but do not schedule triggers, "
            "manage async tasks, or send deliverables directly to channels."
        ),
        memory_policy="isolated_no_long_term_memory",
        memory_rule=(
            "Do NOT read or write long-term memory. "
            "Long-term memory tools are disabled for this worker session. "
            "Skill creation/update is also disabled — only the parent agent manages skills."
        ),
    ),
    "memory_readonly": DelegationToolProfile(
        name="memory_readonly",
        core_tools_only=True,
        allowed_tools=(),
        excluded_tools=_DELEGATION_BASE_EXCLUDED_TOOLS + ("save_skill",) + _DELEGATION_MEMORY_WRITE_TOOLS,
        tool_policy="worker_memory_readonly",
        tool_rule=(
            "Your tool surface is worker-safe, and you may use read-only recall tools when they materially help the delegated task."
        ),
        memory_policy="read_only_long_term_memory",
        memory_rule=(
            "You MAY read long-term memory when it materially helps the delegated task. "
            "Writing memory and creating/updating skills are disabled for this worker session."
        ),
    ),
    "review_readonly": DelegationToolProfile(
        name="review_readonly",
        core_tools_only=False,
        allowed_tools=(
            "list_files",
            "read_file",
            "glob_search",
            "grep_search",
            "load_skill",
            "tool_search",
            "search_memory",
            "load_memory",
            "get_current_time",
        ),
        excluded_tools=(),
        tool_policy="worker_review_readonly",
        tool_rule=(
            "Your tool surface is review-only: read files, search, and recall. "
            "Do NOT edit files, run commands, execute code, or perform external actions."
        ),
        memory_policy="read_only_long_term_memory",
        memory_rule=(
            "You can read long-term memory to reconstruct context. Writing memory and skill creation are disabled."
        ),
    ),
    "research_readonly": DelegationToolProfile(
        name="research_readonly",
        core_tools_only=False,
        allowed_tools=(
            "list_files",
            "read_file",
            "glob_search",
            "grep_search",
            "load_skill",
            "tool_search",
            "search_memory",
            "load_memory",
            "get_current_time",
            "web_fetch",
            "web_search",
            "firecrawl_fetch",
            "xcrawl_scrape",
        ),
        excluded_tools=(),
        tool_policy="worker_research_readonly",
        tool_rule=(
            "Your tool surface is research-only. You MAY browse and retrieve external sources, but do NOT edit files, "
            "run commands, execute code, or perform external-facing actions."
        ),
        memory_policy="read_only_long_term_memory",
        memory_rule=(
            "You can read long-term memory to orient the research task. Writing memory and skill creation are disabled."
        ),
    ),
    "agent_message": DelegationToolProfile(
        name="agent_message",
        core_tools_only=False,
        allowed_tools=(),
        excluded_tools=_DELEGATION_BASE_EXCLUDED_TOOLS + ("save_skill",) + _DELEGATION_MEMORY_WRITE_TOOLS,
        tool_policy="peer_agent_tool_surface",
        tool_rule=(
            "This is a peer agent request. Use your own assigned/read-authorized tools when needed, "
            "but do not delegate recursively, manage triggers/workflows, or send deliverables directly to channels."
        ),
        memory_policy="peer_read_only_memory",
        memory_rule=(
            "You MAY read long-term memory when it materially helps answer the peer request. "
            "Writing memory and creating/updating skills are disabled for this peer session."
        ),
    ),
}

_RESTART_REPLAY_SAFE_TOOL_PROFILES: frozenset[str] = frozenset(
    {
        "review_readonly",
        "research_readonly",
    }
)


def _resolve_delegation_tool_profile(name: str | None) -> DelegationToolProfile:
    profile_name = "agent_message" if name == "peer_agent" else name
    return _DELEGATION_TOOL_PROFILES.get(profile_name or "worker_safe", _DELEGATION_TOOL_PROFILES["worker_safe"])


def _delegation_profile_restart_replay_safe(profile_name: str | None) -> bool:
    profile = _resolve_delegation_tool_profile(profile_name)
    return profile.name in _RESTART_REPLAY_SAFE_TOOL_PROFILES


def _delegation_profile_tool_names(profile: DelegationToolProfile) -> set[str]:
    tool_names = set(profile.allowed_tools)
    if profile.core_tools_only:
        tool_names |= CORE_TOOL_NAMES
    tool_names.difference_update(profile.excluded_tools)
    return tool_names


def _delegation_capability_grants(profile: DelegationToolProfile) -> frozenset[str]:
    return frozenset(
        CAPABILITY_MAP[tool_name]
        for tool_name in sorted(_delegation_profile_tool_names(profile))
        if tool_name in CAPABILITY_MAP
    )


def _issue_delegation_token_for_request(
    request: AgentDelegationRequest,
    profile: DelegationToolProfile,
) -> Any | None:
    child_agent_id = _maybe_uuid(getattr(request.target, "id", None))
    if child_agent_id is None:
        logger.warning("[Orchestrator] Cannot issue delegation token: target agent has no valid UUID id")
        return None
    parent_agent_id = _maybe_uuid(request.parent_agent_id) or request.owner_id
    ttl_seconds = max(DEFAULT_DELEGATION_TTL_SECONDS, float(request.policy.timeout_seconds) + 30.0)
    granted_capabilities = None if profile.name == "agent_message" else _delegation_capability_grants(profile)
    return issue_delegation_token(
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        granted_capabilities=granted_capabilities,
        ttl_seconds=ttl_seconds,
    )


def _build_delegated_worker_prompt(profile: DelegationToolProfile, parent_name: str | None = None) -> str:
    """Build a lightweight framing context for the delegated worker session.

    F-1 (dispatch symmetry): the heavy ``<return_format>`` / ``<good_return_examples>``
    / ``<bad_return_examples>`` template was removed — freezing the return shape
    is an L1 violation (it boxes in the model's thinking product).  What remains
    is the legitimate L2 harness context: isolation facts the worker needs to know,
    tool/memory policy, and one descriptive framing line naming the delegating peer.
    """
    framing = (
        f"你正在作为同事处理 {parent_name} 委派的工作；完成后把结论与证据返回给委派方。"
        if parent_name
        else "你正在处理委派方交来的工作；完成后把结论与证据返回给委派方。"
    )
    return (
        "<isolation_contract>\n"
        "- The delegated task is the ONLY authoritative context you have.\n"
        "- The parent agent's conversation history is NOT available to you.\n"
        "- Do not assume shared state with the parent beyond what the task says.\n"
        "- Do not leak information about the parent's other tasks or sessions.\n"
        "- Delegation tools are disabled in worker sessions — do not try to spawn\n"
        "  nested workers. If the task truly exceeds your scope, say so explicitly\n"
        "  and let the parent re-scope.\n"
        "</isolation_contract>\n\n"
        "<tool_policy>\n"
        f"- {profile.tool_rule}\n"
        f"- {profile.memory_rule}\n"
        "</tool_policy>\n\n"
        f"{framing}"
    )


_DELEGATED_WORKER_PROMPT_SUFFIX = _build_delegated_worker_prompt(_DELEGATION_TOOL_PROFILES["worker_safe"])


# P0-3a: per-trace visited-agent tracking for cycle detection.
# `max_depth` alone cannot stop A→B→A→B style loops if a child uses the
# messaging tool to bounce work back to a previously-active agent (the
# delegation-tool blacklist doesn't cover messaging). The set is keyed by
# trace_id and populated/cleaned via a try/finally in `_delegate` so that
# concurrent traces don't interfere and successful chains free their
# entries deterministically.
_visited_agents_by_trace: dict[str, set[str]] = {}


@dataclass(slots=True)
class OrchestrationPolicy:
    max_depth: int = 2
    timeout_seconds: float = 30.0
    tool_profile: str = "worker_safe"


@dataclass(slots=True)
class AsyncDelegationState:
    task: asyncio.Task["AgentDelegationResult"]
    parent_agent_id: uuid.UUID | None
    child_agent_name: str | None
    child_session_id: str
    trace_id: str
    started_at: datetime


# ── Async delegation registry (in-process, per-worker) ──────────────
_async_tasks: dict[str, AsyncDelegationState] = {}
_MAX_TRACKED_TASKS = 200


def _maybe_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        logger.debug("[orchestrator] _maybe_uuid could not coerce %r: %s", value, exc)
        return None


def _coerce_aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _deferred_cancellation_result(
    task_id: str,
    *,
    elapsed_seconds: float,
    min_runtime_seconds: float,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "running",
        "result": (
            "Task is still running; cancellation was deferred because the worker "
            f"has only run for {elapsed_seconds:.1f}s of the {min_runtime_seconds:.1f}s grace window. "
            "Use check_async_task to inspect progress, or retry cancel_async_task with force=true "
            "only if the task is truly runaway or no longer needed."
        ),
        "timed_out": False,
        "cancellation_deferred": True,
        "elapsed_seconds": elapsed_seconds,
        "min_runtime_seconds": min_runtime_seconds,
    }


def _capture_execution_identity_ref() -> ExecutionIdentityRef | None:
    try:
        from app.core.execution_context import get_execution_identity

        current_identity = get_execution_identity()
    except Exception:
        return None
    if current_identity is None:
        return None
    return ExecutionIdentityRef(
        identity_type=current_identity.identity_type,
        identity_id=current_identity.identity_id,
        label=current_identity.label,
    )


def _execution_identity_to_metadata(identity: ExecutionIdentityRef | None) -> dict[str, str | None] | None:
    if identity is None:
        return None
    return {
        "identity_type": identity.identity_type,
        "identity_id": str(identity.identity_id) if identity.identity_id is not None else None,
        "label": identity.label,
    }


def _execution_identity_from_metadata(value: Any) -> ExecutionIdentityRef | None:
    if not isinstance(value, dict):
        return None
    identity_type = str(value.get("identity_type") or "").strip()
    if not identity_type:
        return None
    return ExecutionIdentityRef(
        identity_type=identity_type,
        identity_id=_maybe_uuid(value.get("identity_id")),
        label=str(value.get("label")) if value.get("label") is not None else None,
    )


def _runtime_json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_runtime_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_runtime_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _runtime_json_safe(item) for key, item in value.items()}
    return value


def _permission_profile_metadata(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _runtime_json_safe(value)
    if is_dataclass(value):
        return _runtime_json_safe(asdict(value))
    return None


def _permission_profile_mode(value: Any | None) -> str | None:
    metadata = _permission_profile_metadata(value)
    if not metadata:
        return None
    mode = metadata.get("mode")
    return str(mode) if mode else None


def _is_user_initiated_delegation(request: AgentDelegationRequest) -> bool:
    """Return true when a real user is the execution principal for this delegation.

    Current-channel IM/web turns enter the runtime with a ``delegated_user``
    identity. Unattended agent wakes use ``agent_bot`` or no execution identity
    and still need the Plan Mode backstop below.
    """
    identity = request.execution_identity
    if identity is None:
        return False
    if identity.identity_type != "delegated_user":
        return False
    return _maybe_uuid(identity.identity_id) is not None


async def _persist_delegation_event(
    *,
    task_id: str,
    status: str,
    parent_agent_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    trace_id: str | None = None,
    result_preview: str = "",
    timed_out: bool = False,
) -> None:
    """Best-effort persistence of delegation lifecycle events via activity logger."""
    if not parent_agent_id:
        return
    try:
        from app.services.activity_logger import log_activity

        detail: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "trace_id": trace_id or "",
            "child_agent": child_agent_name or "",
            "timed_out": timed_out,
        }
        if result_preview:
            detail["result_preview"] = result_preview
        await log_activity(
            agent_id=uuid.UUID(str(parent_agent_id)),
            action_type="delegation_" + status,
            summary="Delegation " + status + ": " + (child_agent_name or task_id),
            detail=detail,
        )
    except Exception as _persist_err:
        logger.debug("[Orchestrator] Delegation persistence failed: %s", _persist_err)


def _cleanup_stale_tasks() -> None:
    """Remove completed tasks that haven't been checked, to prevent unbounded growth."""
    if len(_async_tasks) <= _MAX_TRACKED_TASKS:
        return
    stale = [tid for tid, state in _async_tasks.items() if state.task.done()]
    for tid in stale:
        _async_tasks.pop(tid, None)
    if len(_async_tasks) > _MAX_TRACKED_TASKS:
        logger.warning("[Orchestrator] %d active async tasks exceeds cap %d", len(_async_tasks), _MAX_TRACKED_TASKS)


@dataclass(slots=True)
class AsyncDelegationHandle:
    task_id: str
    trace_id: str
    target_name: str
    status: str = "running"
    coordination_lease_id: str | None = None
    blocked_by_lease_id: str | None = None
    signal_thread_id: str | None = None


@dataclass(slots=True)
class AgentDelegationRequest:
    target: Any
    target_model: Any
    conversation_messages: list[dict]
    owner_id: uuid.UUID
    session_id: str
    tool_executor: ToolExecutor | None = None
    system_prompt_suffix: str = ""
    max_tool_rounds: int | None = None
    parent_agent_id: str | uuid.UUID | None = None
    parent_session_id: str | None = None
    trace_id: str | None = None
    depth: int = 1
    policy: OrchestrationPolicy = field(default_factory=OrchestrationPolicy)
    interaction_type: str = "delegation"
    execution_identity: ExecutionIdentityRef | None = None
    confirmed_plan_id: str | uuid.UUID | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None
    plan_exempt_reason: str | None = None
    # §9 P0: initiating tenant travels WITH the request so background tasks
    # (which outlive the request ContextVar) can pin their DB sessions to it.
    tenant_id: uuid.UUID | str | None = None
    # §9 P0 (切口③ 收尾): parent work-ledger todo this delegation serves.
    # Spawn stamps the child as owner; completion writes the status back.
    ledger_todo_id: str | None = None
    # F-1 dispatch symmetry: parent display-name for worker framing context.
    # Travels as structured metadata, never prefixed into the instruction text.
    parent_agent_name: str | None = None
    runtime_task_id: str | None = None
    restart_replay_contract: dict[str, Any] | None = None
    permission_profile: Any | None = None


def _delegation_coordination_key(request: AgentDelegationRequest) -> str:
    parent = str(request.parent_agent_id or request.owner_id)
    target = str(getattr(request.target, "id", getattr(request.target, "name", "unknown")))
    prompt = request.conversation_messages[-1].get("content", "") if request.conversation_messages else ""
    digest = hashlib.sha256(str(prompt).strip().encode("utf-8")).hexdigest()[:16]
    return f"delegation:{parent}:{target}:{digest}"


@dataclass(slots=True)
class AgentDelegationResult:
    content: str
    child_session_id: str
    trace_id: str
    depth: int
    timed_out: bool = False
    depth_limited: bool = False
    failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """P1-W3-2: structured representation for parent agents that need
        to branch on status (timed_out / depth_limited / failed) without
        regex-matching the content prefix.

        Includes a derived `status` so callers don't have to reconstruct
        it from individual flags. `content` is preserved verbatim — the
        message body is still the primary communication channel.
        """
        if self.failed and self.depth_limited:
            status = "depth_limited"
        elif self.failed and self.timed_out:
            status = "timed_out"
        elif self.failed:
            status = "failed"
        else:
            status = "ok"
        return {
            "status": status,
            "content": self.content,
            "child_session_id": self.child_session_id,
            "trace_id": self.trace_id,
            "depth": self.depth,
            "timed_out": self.timed_out,
            "depth_limited": self.depth_limited,
            "failed": self.failed,
        }

    def to_json(self) -> str:
        """JSON-encoded `to_dict()` payload — the surface tool callers consume."""
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False)


def _delegation_user_message(conversation_messages: list[dict[str, Any]]) -> str:
    """Return the parent's latest instruction verbatim as the child's first user message.

    F-1 (dispatch symmetry): the instruction the parent agent wrote must reach
    the child session byte-for-byte — no ``## Delegated Task Brief`` envelope,
    no transcript folding, no 3-section rewrite.  The parent's intelligence
    produced that instruction; the harness must not replace it with a template.

    Only when ``conversation_messages`` is empty (edge case) do we fall back to
    a one-line placeholder so the child session is never left with an empty turn.
    """
    if conversation_messages:
        last = conversation_messages[-1]
        content = last.get("content") or ""
        if content:
            return content
    return "Complete the delegated task and report the result."


def _build_runtime_task_metadata(request: AgentDelegationRequest, *, task_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "message_count": len(request.conversation_messages),
        "system_prompt_suffix": request.system_prompt_suffix,
        "tool_profile": request.policy.tool_profile,
    }
    if request.confirmed_plan_id is not None:
        metadata.update(
            {
                "plan_id": str(request.confirmed_plan_id),
                "plan_version": request.confirmed_plan_version,
                "plan_hash": request.confirmed_plan_hash,
            }
        )
    if request.plan_exempt_reason:
        metadata["plan_exempt_reason"] = request.plan_exempt_reason
    permission_profile = _permission_profile_metadata(request.permission_profile)
    if permission_profile:
        metadata["permission_profile"] = permission_profile
        permission_mode = _permission_profile_mode(request.permission_profile)
        if permission_mode:
            metadata["permission_mode"] = permission_mode
    execution_identity = _execution_identity_to_metadata(request.execution_identity)
    if execution_identity:
        metadata["execution_identity"] = execution_identity
    replay_safe = _delegation_profile_restart_replay_safe(request.policy.tool_profile)
    resumable = request.tool_executor is None
    blocker = ""
    if request.tool_executor is not None:
        blocker = "custom_tool_executor_not_replayable"
    if resumable:
        try:
            json.dumps(request.conversation_messages)
        except (TypeError, ValueError):
            resumable = False
            blocker = "non_json_conversation_messages"

    metadata["resumable_delegation"] = resumable
    metadata["resume_after_restart"] = resumable
    metadata["side_effect_risk"] = "read_only" if replay_safe else "mutating"
    if blocker:
        metadata["restart_resume_blocker"] = blocker
    if resumable:
        contract = build_restart_replay_contract(
            task_type="delegation",
            task_id=task_id,
            side_effect_risk=str(metadata["side_effect_risk"]),
            trace_id=request.trace_id,
            session_id=request.session_id,
        )
        request.restart_replay_contract = contract
        metadata.update(
            {
                "owner_id": str(request.owner_id),
                "target_agent_id": str(getattr(request.target, "id", "")),
                "conversation_messages": request.conversation_messages,
                "max_tool_rounds": request.max_tool_rounds,
                "timeout_seconds": request.policy.timeout_seconds,
                "restart_replay_contract": contract,
            }
        )
        metadata = merge_restart_replay_journal(
            metadata,
            build_restart_replay_journal_entry(
                task_type="delegation",
                task_id=task_id,
                side_effect_risk=str(metadata["side_effect_risk"]),
                phase="spawn_intent_recorded",
                trace_id=request.trace_id,
                session_id=request.session_id,
            ),
        )
    return metadata


async def _delegation_plan_gate_allows(request: AgentDelegationRequest) -> tuple[bool, str | None]:
    """Final Plan Mode backstop for async delegation startup."""
    if request.interaction_type != "delegation":
        return True, None
    if _is_user_initiated_delegation(request):
        return True, "user_initiated_delegation"
    parent_agent_id = _maybe_uuid(request.parent_agent_id)
    if parent_agent_id is None:
        return False, "missing_parent_agent"

    from app.database import tenant_scoped_session
    from app.services.plan_mode_gate import get_plan_mode_gate

    tenant = str(request.tenant_id) if request.tenant_id else None
    async with tenant_scoped_session(tenant) as db:
        decision = await get_plan_mode_gate().check(
            db,
            agent_id=parent_agent_id,
            action_kind="start_delegation",
            confirmed_plan_id=request.confirmed_plan_id,
            plan_version=request.confirmed_plan_version,
            plan_hash=request.confirmed_plan_hash,
            action_artifact={"metadata": {"plan_exempt_reason": request.plan_exempt_reason}},
        )
    return decision.allowed, decision.reason


async def _resolve_resumable_target_runtime(child_agent_id: uuid.UUID) -> tuple[Any, Any] | None:
    """Resolve a resumable native target agent and its model from persisted state."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # No explicit tenant here: resume runs inside a context where the tenant
    # ContextVar was pinned by the caller (daemon resume sets it from the
    # persisted RuntimeTask record).
    async with tenant_scoped_session() as db:
        result = await db.execute(select(Agent).where(Agent.id == child_agent_id))
        target = result.scalar_one_or_none()
        if not target:
            return None
        if target.status in ("expired", "stopped", "archived"):
            return None
        target_model = None
        if target.primary_model_id:
            model_r = await db.execute(
                select(LLMModel).where(LLMModel.id == target.primary_model_id, LLMModel.tenant_id == target.tenant_id)
            )
            target_model = model_r.scalar_one_or_none()

        if not target_model and target.fallback_model_id:
            fb_r = await db.execute(
                select(LLMModel).where(LLMModel.id == target.fallback_model_id, LLMModel.tenant_id == target.tenant_id)
            )
            target_model = fb_r.scalar_one_or_none()

        if not target_model:
            return None

        return target, target_model


async def delegate_to_agent(
    *,
    target: Any,
    target_model: Any,
    conversation_messages: list[dict],
    owner_id: uuid.UUID,
    session_id: str,
    tool_executor: ToolExecutor | None = None,
    system_prompt_suffix: str = "",
    max_tool_rounds: int | None = None,
    parent_agent_id: str | uuid.UUID | None = None,
    parent_agent_name: str | None = None,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    depth: int = 1,
    policy: OrchestrationPolicy | None = None,
    interaction_type: str = "delegation",
    execution_identity: ExecutionIdentityRef | None = None,
    tenant_id: uuid.UUID | str | None = None,
    ledger_todo_id: str | None = None,
    permission_profile: Any | None = None,
) -> str:
    """Delegate one conversational turn to another agent through the runtime."""
    request = AgentDelegationRequest(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        owner_id=owner_id,
        session_id=session_id,
        tool_executor=tool_executor,
        system_prompt_suffix=system_prompt_suffix,
        max_tool_rounds=max_tool_rounds,
        parent_agent_id=parent_agent_id,
        parent_agent_name=parent_agent_name,
        parent_session_id=parent_session_id,
        trace_id=trace_id,
        depth=depth,
        policy=policy or OrchestrationPolicy(),
        interaction_type=interaction_type,
        execution_identity=execution_identity or _capture_execution_identity_ref(),
        tenant_id=tenant_id,
        ledger_todo_id=ledger_todo_id,
        permission_profile=permission_profile,
    )
    result = await _delegate(request)
    return result.content


def _delegation_ledger_owner(request: AgentDelegationRequest) -> str:
    """Stable owner identity for the parent's ledger todo: the child agent id."""
    return str(getattr(request.target, "id", "") or getattr(request.target, "name", ""))


def _stamp_ledger_todo_owner(request: AgentDelegationRequest) -> None:
    """切口③ assign half: mark the parent's todo as delegated to the child.

    Ledger is an observation surface, never a control surface — failures are
    logged with context and never block the delegation itself.
    """
    if not request.ledger_todo_id:
        return
    parent_agent_id = _maybe_uuid(request.parent_agent_id)
    if parent_agent_id is None:
        return
    try:
        from app.services.agent_work_ledger import assign_todo_owner

        assign_todo_owner(
            agent_id=parent_agent_id,
            item_id=request.ledger_todo_id,
            owner=_delegation_ledger_owner(request),
            session_id=request.parent_session_id,
        )
    except Exception as exc:
        logger.warning(
            "[Orchestrator] ledger todo %s owner stamp failed (non-fatal): %s",
            request.ledger_todo_id,
            exc,
        )


def _write_back_ledger_todo(request: AgentDelegationRequest, *, failed: bool) -> None:
    """切口③ write-back half: completion → completed, failure → released to
    pending. ``expected_owner`` makes a stale child unable to flip a todo that
    was reassigned mid-flight (fail-closed inside record_delegated_todo_status)."""
    if not request.ledger_todo_id:
        return
    parent_agent_id = _maybe_uuid(request.parent_agent_id)
    if parent_agent_id is None:
        return
    try:
        from app.services.agent_work_ledger import record_delegated_todo_status

        record_delegated_todo_status(
            agent_id=parent_agent_id,
            item_id=request.ledger_todo_id,
            status="pending" if failed else "completed",
            expected_owner=_delegation_ledger_owner(request),
            session_id=request.parent_session_id,
        )
    except Exception as exc:
        logger.warning(
            "[Orchestrator] ledger todo %s write-back failed (non-fatal): %s",
            request.ledger_todo_id,
            exc,
        )


async def _delegate(request: AgentDelegationRequest) -> AgentDelegationResult:
    trace_id = request.trace_id or uuid.uuid4().hex
    child_session_id = request.session_id or uuid.uuid4().hex
    tool_profile = _resolve_delegation_tool_profile(request.policy.tool_profile)
    is_delegation = request.interaction_type == "delegation"

    if request.depth > request.policy.max_depth:
        return AgentDelegationResult(
            content=(
                f"⚠️ Delegation depth limit reached ({request.depth}/{request.policy.max_depth}). "
                "Refine the request instead of delegating further."
            ),
            child_session_id=child_session_id,
            trace_id=trace_id,
            depth=request.depth,
            depth_limited=True,
            failed=True,
        )

    # P0-3a: cycle detection along the same trace_id. depth check above only
    # blocks linear A→B→C→D chains; without this, a child can use the
    # messaging tool to bounce work back to a previously-active agent
    # (A→B→A→B…), since the delegation-tool blacklist doesn't cover messaging.
    target_agent_key = str(getattr(request.target, "id", ""))
    visited = _visited_agents_by_trace.setdefault(trace_id, set())
    if target_agent_key and target_agent_key in visited:
        target_label = getattr(request.target, "name", None) or target_agent_key
        return AgentDelegationResult(
            content=(
                f"⚠️ Delegation cycle detected: agent '{target_label}' is already active on this "
                f"trace ({trace_id[:8]}…). Refusing to re-enter — break the loop or restructure the task."
            ),
            child_session_id=child_session_id,
            trace_id=trace_id,
            depth=request.depth,
            failed=True,
        )
    if target_agent_key:
        visited.add(target_agent_key)

    _stamp_ledger_todo_owner(request)

    try:
        result = await _delegate_after_cycle_check(
            request,
            trace_id=trace_id,
            child_session_id=child_session_id,
            tool_profile=tool_profile,
            is_delegation=is_delegation,
        )
        _write_back_ledger_todo(request, failed=result.failed)
        return result
    finally:
        # Drop this hop from the visited set; clean the dict entry once empty
        # so long-lived processes don't leak memory across many short traces.
        if target_agent_key:
            current = _visited_agents_by_trace.get(trace_id)
            if current is not None:
                current.discard(target_agent_key)
                if not current:
                    _visited_agents_by_trace.pop(trace_id, None)


async def _delegate_after_cycle_check(
    request: AgentDelegationRequest,
    *,
    trace_id: str,
    child_session_id: str,
    tool_profile: Any,
    is_delegation: bool,
) -> AgentDelegationResult:
    """Original _delegate body, extracted so cycle tracking can wrap it
    with try/finally without indenting hundreds of lines."""
    if is_delegation:
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.DELEGATION_START,
                agent_id=request.target.id,
                session_id=child_session_id,
                source="agent",
                metadata={
                    "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                    "to_agent": str(request.target.id),
                    "to_agent_name": request.target.name,
                    "trace_id": trace_id,
                    "depth": request.depth,
                },
            )
        except Exception as _hook_err:
            logger.debug("[Orchestrator] DELEGATION_START hook failed (non-fatal): %s", _hook_err)

    delegation_user_message = _delegation_user_message(request.conversation_messages)
    combined_suffix = "\n\n".join(
        part.strip()
        for part in [
            request.system_prompt_suffix,
            _build_delegated_worker_prompt(tool_profile, parent_name=request.parent_agent_name),
        ]
        if part and part.strip()
    )

    session_metadata: dict[str, Any] = {
        "interaction_type": request.interaction_type,
    }
    permission_profile = _permission_profile_metadata(request.permission_profile)
    if permission_profile:
        session_metadata["permission_profile"] = permission_profile
        permission_mode = _permission_profile_mode(request.permission_profile)
        if permission_mode:
            session_metadata["permission_mode"] = permission_mode
    if request.runtime_task_id:
        session_metadata["runtime_task_id"] = request.runtime_task_id
    if request.restart_replay_contract:
        session_metadata["restart_replay_contract"] = dict(request.restart_replay_contract)
    if is_delegation:
        delegation_token = _issue_delegation_token_for_request(request, tool_profile)
        if delegation_token is None:
            return AgentDelegationResult(
                content="⚠️ Delegation token could not be issued; refusing to start unscoped delegated invocation.",
                child_session_id=child_session_id,
                trace_id=trace_id,
                depth=request.depth,
                failed=True,
            )
        session_metadata.update(
            {
                "delegation": True,
                "delegation_depth": request.depth,
                "delegation_trace_id": trace_id,
                "delegation_parent_agent_id": (
                    str(request.parent_agent_id) if request.parent_agent_id is not None else None
                ),
                "delegation_parent_session_id": request.parent_session_id,
                "delegation_tool_policy": tool_profile.tool_policy,
                "delegation_memory_policy": tool_profile.memory_policy,
                "delegation_allowed_tools": tool_profile.allowed_tools,
                "delegation_token_id": delegation_token.delegation_id if delegation_token is not None else None,
                "delegation_token_expires_at": delegation_token.expires_at if delegation_token is not None else None,
                "delegation_token_capabilities": (
                    sorted(delegation_token.granted_capabilities) if delegation_token is not None else []
                ),
            }
        )
    else:
        delegation_token = None
        session_metadata.update(
            {
                "agent_message": True,
                "agent_message_trace_id": trace_id,
                "agent_message_parent_agent_id": (
                    str(request.parent_agent_id) if request.parent_agent_id is not None else None
                ),
                "agent_message_parent_session_id": request.parent_session_id,
                "agent_message_tool_policy": tool_profile.tool_policy,
                "agent_message_memory_policy": tool_profile.memory_policy,
            }
        )

    transcript_enabled = False
    transcript_tenant_id = _maybe_uuid(request.tenant_id) or _maybe_uuid(getattr(request.target, "tenant_id", None))
    transcript_session_id = _maybe_uuid(child_session_id)
    transcript_parent_session_id = _maybe_uuid(request.parent_session_id)
    transcript_runtime_task_id = _maybe_uuid(request.runtime_task_id)

    async def _append_child_transcript_event(
        *,
        actor_type: str,
        event_type: str,
        role: str | None,
        content: str,
        t0_role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not transcript_enabled or transcript_tenant_id is None or transcript_session_id is None:
            return
        from app.database import tenant_scoped_session
        from app.services.chat_transcript import append_session_event

        async with tenant_scoped_session(transcript_tenant_id) as db:
            await append_session_event(
                db=db,
                agent_id=request.target.id,
                tenant_id=transcript_tenant_id,
                session_id=transcript_session_id,
                run_id=transcript_runtime_task_id,
                actor_type=actor_type,
                event_type=event_type,
                role=role,
                t0_role=t0_role,
                user_id=request.owner_id,
                content=content,
                parent_session_id=transcript_parent_session_id,
                root_session_id=transcript_parent_session_id or transcript_session_id,
                source="agent",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                metadata={
                    "source": "agent",
                    "interaction_type": request.interaction_type,
                    "delegation": is_delegation,
                    "trace_id": trace_id,
                    "depth": request.depth,
                    "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                    "to_agent": str(request.target.id),
                    "to_agent_name": request.target.name,
                    "runtime_task_id": request.runtime_task_id,
                    "semantic_memory_eligible": is_delegation,
                    **(metadata or {}),
                },
            )
            await db.commit()

    if is_delegation and transcript_tenant_id is not None and transcript_session_id is not None:
        from sqlalchemy import select

        from app.database import tenant_scoped_session
        from app.models.chat_session import ChatSession
        from app.models.participant import Participant
        from app.services.chat_transcript import append_session_event

        async with tenant_scoped_session(transcript_tenant_id) as db:
            result = await db.execute(select(ChatSession).where(ChatSession.id == transcript_session_id))
            session = result.scalar_one_or_none()
            if session is None:
                participant_result = await db.execute(
                    select(Participant).where(Participant.type == "agent", Participant.ref_id == request.target.id)
                )
                target_participant = participant_result.scalar_one_or_none()
                session = ChatSession(
                    id=transcript_session_id,
                    agent_id=request.target.id,
                    tenant_id=transcript_tenant_id,
                    user_id=request.owner_id,
                    participant_id=target_participant.id if target_participant else None,
                    peer_agent_id=_maybe_uuid(request.parent_agent_id),
                    source_channel="agent",
                    session_kind="delegation_run",
                    actor_type="agent",
                    runtime_source="delegation",
                    visibility_scope="agent_owner",
                    listed_surface="task_updates",
                    parent_session_id=transcript_parent_session_id,
                    root_session_id=transcript_parent_session_id,
                    runtime_task_id=transcript_runtime_task_id,
                    title=f"Delegation: {request.target.name}"[:200],
                    transcript_metadata_json={
                        "source": "agent",
                        "interaction_type": request.interaction_type,
                        "trace_id": trace_id,
                        "depth": request.depth,
                        "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                        "to_agent": str(request.target.id),
                        "to_agent_name": request.target.name,
                        **(
                            {
                                "permission_profile": permission_profile,
                                "permission_mode": session_metadata.get("permission_mode"),
                            }
                            if permission_profile
                            else {}
                        ),
                    },
                )
                db.add(session)
                await db.flush()
            transcript_enabled = True
            await append_session_event(
                db=db,
                agent_id=request.target.id,
                tenant_id=transcript_tenant_id,
                session_id=transcript_session_id,
                run_id=transcript_runtime_task_id,
                actor_type="user",
                event_type="user_message",
                role="user",
                user_id=request.owner_id,
                content=delegation_user_message,
                parent_session_id=transcript_parent_session_id,
                root_session_id=transcript_parent_session_id or transcript_session_id,
                source="agent",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                metadata={
                    "source": "agent",
                    "interaction_type": request.interaction_type,
                    "delegation": True,
                    "trace_id": trace_id,
                    "depth": request.depth,
                    "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                    "to_agent": str(request.target.id),
                    "to_agent_name": request.target.name,
                    "runtime_task_id": request.runtime_task_id,
                    "semantic_memory_eligible": True,
                },
            )
            session.last_message_at = datetime.now(timezone.utc)
            await db.commit()
    elif is_delegation:
        logger.warning(
            "[Orchestrator] Delegation transcript disabled: tenant/session missing target=%s session=%s tenant=%s",
            getattr(request.target, "id", None),
            child_session_id,
            transcript_tenant_id,
        )

    async def _on_delegation_tool_call(data: dict[str, Any]) -> None:
        if not transcript_enabled:
            return
        status = str(data.get("status") or "")
        payload = {
            "name": data.get("name", ""),
            "args": data.get("args"),
            "status": status,
            "tool_call_id": data.get("tool_call_id"),
            "step_id": data.get("step_id"),
            "visibility": data.get("visibility") or "collapsed",
            "started_at": data.get("started_at") or data.get("startedAt"),
            "completed_at": data.get("completed_at") or data.get("completedAt"),
            "duration_ms": data.get("duration_ms"),
            "reasoning_content": data.get("reasoning_content"),
            "reasoning_signature": data.get("reasoning_signature"),
        }
        if status in {"done", "completed", "failed"} or "result" in data:
            payload["result"] = str(data.get("result", ""))
        payload = {key: value for key, value in payload.items() if value is not None}
        await _append_child_transcript_event(
            actor_type="tool",
            event_type="tool_result" if status in {"done", "completed", "failed"} else "tool_call",
            role="tool_call",
            t0_role="tool",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            metadata={
                "tool_name": data.get("name", ""),
                "status": status,
                "tool_call_id": data.get("tool_call_id"),
                "step_id": data.get("step_id"),
                "duration_ms": data.get("duration_ms"),
                "visibility": data.get("visibility") or "collapsed",
            },
        )

    invocation = AgentInvocationRequest(
        model=request.target_model,
        messages=[{"role": "user", "content": delegation_user_message}],
        memory_messages=[],
        memory_session_id=child_session_id,
        session_context=SessionContext(
            session_id=child_session_id,
            source="agent",
            channel="agent",
            metadata=session_metadata,
        ),
        agent_name=request.target.name,
        role_description=request.target.role_description or "",
        agent_id=request.target.id,
        user_id=request.owner_id,
        execution_identity=request.execution_identity or _capture_execution_identity_ref(),
        system_prompt_suffix=combined_suffix,
        tool_executor=request.tool_executor,
        on_tool_call=_on_delegation_tool_call if transcript_enabled else None,
        core_tools_only=tool_profile.core_tools_only,
        allowed_tool_names=tool_profile.allowed_tools,
        excluded_tool_names=tool_profile.excluded_tools,
        max_tool_rounds=request.max_tool_rounds,
        delegation_token=delegation_token,
    )

    delegation_result: AgentDelegationResult
    _delegation_status = "success"

    try:
        result = await asyncio.wait_for(
            invoke_agent(invocation),
            timeout=max(request.policy.timeout_seconds, 0.01),
        )
        delegation_result = AgentDelegationResult(
            content=result.content or "",
            child_session_id=child_session_id,
            trace_id=trace_id,
            depth=request.depth,
        )
    except asyncio.TimeoutError:
        _delegation_status = "timeout"
        delegation_result = AgentDelegationResult(
            content=(f"⚠️ Delegation to {request.target.name} timed out after {request.policy.timeout_seconds:.2f}s."),
            child_session_id=child_session_id,
            trace_id=trace_id,
            depth=request.depth,
            timed_out=True,
            failed=True,
        )
    except Exception as exc:
        _delegation_status = "error"
        # M-22: Log full stack server-side; return only safe summary to LLM
        logger.error(
            "[Orchestrator] Child agent %s failed (depth=%d, trace=%s): %s",
            request.target.name,
            request.depth,
            trace_id,
            exc,
            exc_info=True,
        )
        delegation_result = AgentDelegationResult(
            content=(
                f"⚠️ Delegation to {request.target.name} failed: {type(exc).__name__}: {str(exc)[:300]}\n"
                f"Trace: {trace_id}, depth: {request.depth}"
            ),
            child_session_id=child_session_id,
            trace_id=trace_id,
            depth=request.depth,
            failed=True,
        )

    await _append_child_transcript_event(
        actor_type="assistant",
        event_type="assistant_message",
        role="assistant",
        content=delegation_result.content or "",
        metadata={
            "status": _delegation_status,
            "failed": delegation_result.failed,
            "timed_out": delegation_result.timed_out,
            "depth_limited": delegation_result.depth_limited,
        },
    )

    if is_delegation:
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.DELEGATION_END,
                agent_id=request.target.id,
                session_id=child_session_id,
                messages=[],
                source="agent",
                metadata={
                    "tenant_id": str(transcript_tenant_id) if transcript_tenant_id else None,
                    "runtime_task_id": request.runtime_task_id,
                    "semantic_memory_eligible": True,
                    "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                    "to_agent": str(request.target.id),
                    "to_agent_name": request.target.name,
                    "trace_id": trace_id,
                    "depth": request.depth,
                    "status": _delegation_status,
                    "failed": delegation_result.failed,
                },
            )
        except Exception as _hook_err:
            logger.debug("[Orchestrator] DELEGATION_END hook failed (non-fatal): %s", _hook_err)

    return delegation_result


async def _project_delegation_completion_to_parent(
    *,
    request: AgentDelegationRequest,
    task_id: str,
    status: str,
    summary: str,
) -> None:
    """Project an async-delegation terminal state onto the parent session timeline.

    Mirrors `update_subagent_child_session_state_for_run` in
    ``services/subagent_run_service.py``: peer/async delegation must surface a
    ``child_session`` event on the initiating (parent) session so the parent
    timeline shows the delegation finishing, instead of only being pollable via
    ``RuntimeTask.result_summary``. Headless delegations (no ``parent_session_id``)
    skip the projection without erroring, matching the child-transcript style.
    """
    parent_session_uuid = _maybe_uuid(request.parent_session_id)
    child_session_uuid = _maybe_uuid(request.session_id)
    if parent_session_uuid is None or child_session_uuid is None:
        return

    tenant_uuid = _maybe_uuid(request.tenant_id) or _maybe_uuid(getattr(request.target, "tenant_id", None))
    parent_agent_uuid = _maybe_uuid(request.parent_agent_id)
    if tenant_uuid is None or parent_agent_uuid is None:
        return

    from app.database import tenant_scoped_session
    from app.services.chat_message_parts import build_session_native_event
    from app.services.chat_transcript import append_session_event

    reason = "delegation_completed" if status == "completed" else "delegation_failed"
    parent_event_payload = {
        "type": "child_session",
        "message": summary,
        "status": status,
        "runtime_task_id": task_id,
        "child_session_id": str(child_session_uuid),
        "parent_session_id": str(parent_session_uuid),
        "root_session_id": str(parent_session_uuid),
        "reason": reason,
        "interaction_type": request.interaction_type,
        "to_agent": str(request.target.id),
        "to_agent_name": request.target.name,
        "trace_id": request.trace_id,
        "depth": request.depth,
    }
    parent_event = build_session_native_event(parent_event_payload)

    try:
        async with tenant_scoped_session(tenant_uuid) as db:
            await append_session_event(
                db=db,
                agent_id=parent_agent_uuid,
                tenant_id=tenant_uuid,
                session_id=parent_session_uuid,
                actor_type="system",
                event_type="child_session",
                content=summary,
                role="system",
                user_id=request.owner_id,
                run_id=task_id,
                runtime_task_id=task_id,
                root_session_id=parent_session_uuid,
                parent_session_id=parent_session_uuid,
                parts=[parent_event["part"]],
                metadata={
                    **parent_event_payload,
                    "source": "agent",
                    "delegation_session_state": status,
                },
                visibility_scope="team",
                listed_surface="chat",
                source="agent",
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "[Orchestrator] Failed to project delegation completion to parent session %s (task=%s): %s",
            parent_session_uuid,
            task_id,
            exc,
        )


def _spawn_async_delegation_task(
    *,
    task_id: str,
    request: AgentDelegationRequest,
    trace_id: str,
) -> None:
    async def _run() -> AgentDelegationResult:
        # §9 P0: pin the initiating tenant inside THIS task's context copy.
        # The spawn-time ContextVar snapshot covers request-spawned tasks, but
        # daemon resume paths have no request context — the request carries
        # the tenant explicitly so every session below scopes correctly.
        tenant_token = None
        if request.tenant_id:
            from app.database import reset_current_tenant, set_current_tenant

            tenant_token = set_current_tenant(str(request.tenant_id))
        try:
            plan_allowed, plan_reason = await _delegation_plan_gate_allows(request)
            if not plan_allowed:
                content = (
                    "Plan Mode blocked async delegation: "
                    f"{plan_reason or 'plan_required'}. Confirm a plan before delegating autonomous work."
                )
                try:
                    await update_runtime_task_record(
                        task_id,
                        status="failed",
                        result_summary=content,
                        trace_id=trace_id,
                        child_session_id=request.session_id,
                        metadata_json={"plan_gate_reason": plan_reason},
                    )
                except Exception as exc:
                    logger.warning("[Orchestrator] Failed to persist plan gate block %s: %s", task_id, exc)
                return AgentDelegationResult(
                    content=content,
                    child_session_id=request.session_id,
                    trace_id=trace_id,
                    depth=request.depth,
                    failed=True,
                )
            delegation_result = await _delegate(request)
            terminal_status = "failed" if delegation_result.failed else "completed"
            try:
                await update_runtime_task_record(
                    task_id,
                    status=terminal_status,
                    result_summary=delegation_result.content,
                    trace_id=delegation_result.trace_id,
                    child_session_id=delegation_result.child_session_id,
                    metadata_json={
                        "timed_out": delegation_result.timed_out,
                        "depth_limited": delegation_result.depth_limited,
                    },
                )
            except Exception as exc:
                logger.warning("[Orchestrator] Failed to update runtime task %s: %s", task_id, exc)
            await _project_delegation_completion_to_parent(
                request=request,
                task_id=task_id,
                status=terminal_status,
                summary=delegation_result.content or "",
            )
            return delegation_result
        except asyncio.CancelledError:
            try:
                await update_runtime_task_record(
                    task_id,
                    status="killed",
                    result_summary="Task cancelled by parent agent",
                    trace_id=trace_id,
                    child_session_id=request.session_id,
                    metadata_json={"timed_out": False, "depth_limited": False, "cancelled": True},
                )
            except Exception as update_exc:
                logger.warning("[Orchestrator] Failed to persist runtime task cancellation %s: %s", task_id, update_exc)
            raise
        except Exception as exc:
            logger.error("[Orchestrator] Async delegation %s failed: %s", task_id, exc)
            try:
                await update_runtime_task_record(
                    task_id,
                    status="failed",
                    result_summary=f"Async task {task_id} failed: {exc}",
                    trace_id=trace_id,
                    child_session_id=request.session_id,
                    metadata_json={"timed_out": False, "depth_limited": False},
                )
            except Exception as update_exc:
                logger.warning("[Orchestrator] Failed to persist runtime task failure %s: %s", task_id, update_exc)
            return AgentDelegationResult(
                content=f"Async task {task_id} failed: {exc}",
                child_session_id=request.session_id,
                trace_id=trace_id,
                depth=request.depth,
                failed=True,
            )
        finally:
            if tenant_token is not None:
                reset_current_tenant(tenant_token)

    task = asyncio.create_task(_run(), name="async-delegation-" + task_id)
    _async_tasks[task_id] = AsyncDelegationState(
        task=task,
        parent_agent_id=_maybe_uuid(request.parent_agent_id),
        child_agent_name=getattr(request.target, "name", None),
        child_session_id=request.session_id,
        trace_id=trace_id,
        started_at=datetime.now(timezone.utc),
    )


# ── Async (non-blocking) delegation ─────────────────────────────────


async def delegate_async(
    *,
    target: Any,
    target_model: Any,
    conversation_messages: list[dict],
    owner_id: uuid.UUID,
    session_id: str,
    tool_executor: ToolExecutor | None = None,
    system_prompt_suffix: str = "",
    max_tool_rounds: int | None = None,
    parent_agent_id: str | uuid.UUID | None = None,
    parent_agent_name: str | None = None,
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    depth: int = 1,
    policy: OrchestrationPolicy | None = None,
    interaction_type: str = "delegation",
    execution_identity: ExecutionIdentityRef | None = None,
    coordination_gateway: CoordinationGateway | None = None,
    tenant_id: uuid.UUID | str | None = None,
    confirmed_plan_id: str | uuid.UUID | None = None,
    confirmed_plan_version: int | None = None,
    confirmed_plan_hash: str | None = None,
    plan_exempt_reason: str | None = None,
    ledger_todo_id: str | None = None,
    permission_profile: Any | None = None,
) -> AsyncDelegationHandle:
    """Launch a child agent in the background and return immediately.

    `coordination_gateway` defaults to None so `gateway_scope()` picks the
    right backend per `settings.COORDINATION_BACKEND` (memory = in-process
    runtime, postgres = `CoordinationRepository` opened on a fresh session
    scoped to `tenant_id`). Callers that already hold an
    `AsyncSession`-bound gateway pass it in explicitly.
    """
    _cleanup_stale_tasks()
    task_id = uuid.uuid4().hex
    real_trace_id = trace_id or uuid.uuid4().hex
    request = AgentDelegationRequest(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        owner_id=owner_id,
        session_id=session_id,
        tool_executor=tool_executor,
        system_prompt_suffix=system_prompt_suffix,
        max_tool_rounds=max_tool_rounds,
        parent_agent_id=parent_agent_id,
        parent_agent_name=parent_agent_name,
        parent_session_id=parent_session_id,
        trace_id=real_trace_id,
        depth=depth,
        policy=policy or OrchestrationPolicy(timeout_seconds=ASYNC_DELEGATION_TIMEOUT_SECONDS),
        interaction_type=interaction_type,
        execution_identity=execution_identity or _capture_execution_identity_ref(),
        confirmed_plan_id=confirmed_plan_id,
        confirmed_plan_version=confirmed_plan_version,
        confirmed_plan_hash=confirmed_plan_hash,
        plan_exempt_reason=plan_exempt_reason,
        tenant_id=tenant_id,
        ledger_todo_id=ledger_todo_id,
        runtime_task_id=task_id,
        permission_profile=permission_profile,
    )
    plan_allowed, plan_reason = await _delegation_plan_gate_allows(request)
    if not plan_allowed:
        return AsyncDelegationHandle(
            task_id="plan_required",
            trace_id=real_trace_id,
            target_name=getattr(target, "name", "unknown"),
            status=f"plan_required:{plan_reason or 'no_confirmed_plan'}",
        )
    coordination_key = _delegation_coordination_key(request)
    lease_ttl = int((policy or request.policy).timeout_seconds) + 60
    async with gateway_scope(coordination_gateway, tenant_id=tenant_id) as gateway:
        lease_result = await gateway.acquire_lease(
            task_key=coordination_key,
            agent_id=str(parent_agent_id or owner_id),
            ttl_seconds=max(lease_ttl, 60),
        )
        if not lease_result.acquired:
            return AsyncDelegationHandle(
                task_id=lease_result.existing_lease_id or "blocked_by_lease",
                trace_id=real_trace_id,
                target_name=getattr(target, "name", "unknown"),
                status="blocked_by_lease",
                blocked_by_lease_id=lease_result.existing_lease_id,
            )
        signal = await gateway.send_signal(
            from_agent_id=str(parent_agent_id or owner_id),
            to_agent_id=str(getattr(target, "id", "")),
            content=conversation_messages[-1].get("content", "") if conversation_messages else "",
            signal_type="delegation_started",
            thread_id=real_trace_id,
        )
    metadata_json = _build_runtime_task_metadata(request, task_id=task_id)
    metadata_json.update(
        {
            "coordination_lease_id": lease_result.lease.id if lease_result.lease else None,
            "coordination_task_key": coordination_key,
            "signal_id": signal.id,
            "signal_thread_id": signal.thread_id,
        }
    )

    try:
        await create_runtime_task_record(
            task_id=task_id,
            task_type="delegation",
            status="pending",
            parent_agent_id=_maybe_uuid(parent_agent_id),
            child_agent_id=getattr(target, "id", None),
            child_agent_name=getattr(target, "name", None),
            prompt=conversation_messages[-1].get("content", "") if conversation_messages else None,
            trace_id=real_trace_id,
            parent_session_id=parent_session_id,
            child_session_id=session_id,
            depth=depth,
            metadata_json=metadata_json,
        )
    except Exception as exc:
        logger.warning("[Orchestrator] Failed to create runtime task record %s: %s", task_id, exc)
    _spawn_async_delegation_task(task_id=task_id, request=request, trace_id=real_trace_id)
    try:
        await update_runtime_task_record(
            task_id,
            status="running",
            trace_id=real_trace_id,
            child_session_id=session_id,
        )
    except Exception as exc:
        logger.warning("[Orchestrator] Failed to mark runtime task %s as running: %s", task_id, exc)

    # P1.8: Persist delegation start to activity log for observability
    await _persist_delegation_event(
        task_id=task_id,
        parent_agent_id=parent_agent_id,
        child_agent_name=target.name,
        trace_id=real_trace_id,
        status="started",
    )
    logger.info("[Orchestrator] Async delegation started: task_id=%s target=%s", task_id, target.name)
    return AsyncDelegationHandle(
        task_id=task_id,
        trace_id=real_trace_id,
        target_name=target.name,
        coordination_lease_id=lease_result.lease.id if lease_result.lease else None,
        signal_thread_id=signal.thread_id,
    )


async def check_async_delegation(
    task_id: str,
    *,
    parent_agent_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """Check status of an async delegation. Returns status + result if done."""

    def _session_payload(child_session_id: str | None, parent_session_id: str | None = None) -> dict[str, Any]:
        session_id = str(child_session_id or "").strip() or None
        return {
            "session_id": session_id,
            "child_session_id": session_id,
            "parent_session_id": str(parent_session_id or "").strip() or None,
        }

    state = _async_tasks.get(task_id)
    if state is None:
        try:
            persisted = await get_runtime_task_record(task_id)
        except Exception as exc:
            logger.warning("[Orchestrator] Failed to load runtime task %s: %s", task_id, exc)
            persisted = None
        if persisted is None:
            return {"task_id": task_id, "status": "not_found", "result": None}
        return {
            "task_id": task_id,
            "status": persisted.get("status", "not_found"),
            "result": persisted.get("result"),
            "timed_out": bool((persisted.get("metadata") or {}).get("timed_out", False)),
            **_session_payload(persisted.get("child_session_id"), persisted.get("parent_session_id")),
        }
    request_parent_agent_id = _maybe_uuid(parent_agent_id)
    if (
        request_parent_agent_id is not None
        and state.parent_agent_id is not None
        and request_parent_agent_id != state.parent_agent_id
    ):
        return {"task_id": task_id, "status": "forbidden", "result": None}
    if not state.task.done():
        return {
            "task_id": task_id,
            "status": "running",
            "result": None,
            **_session_payload(state.child_session_id),
        }
    # Remove completed task from registry after reading
    _async_tasks.pop(task_id, None)
    try:
        delegation_result = state.task.result()
        # P1.8: Persist delegation completion
        await _persist_delegation_event(
            task_id=task_id,
            status="failed" if delegation_result.failed else "completed",
            result_preview=delegation_result.content[:300] if delegation_result.content else "",
            timed_out=delegation_result.timed_out,
            parent_agent_id=state.parent_agent_id,
            child_agent_name=state.child_agent_name,
            trace_id=state.trace_id,
        )
        return {
            "task_id": task_id,
            "status": "failed" if delegation_result.failed else "completed",
            "result": delegation_result.content,
            "timed_out": delegation_result.timed_out,
            **_session_payload(delegation_result.child_session_id),
        }
    except asyncio.CancelledError:
        await _persist_delegation_event(
            task_id=task_id,
            status="killed",
            result_preview="Task cancelled by parent agent",
            parent_agent_id=state.parent_agent_id,
            child_agent_name=state.child_agent_name,
            trace_id=state.trace_id,
        )
        return {
            "task_id": task_id,
            "status": "killed",
            "result": "Task cancelled by parent agent",
            "timed_out": False,
            **_session_payload(state.child_session_id),
        }
    except Exception as exc:
        await _persist_delegation_event(
            task_id=task_id,
            status="error",
            result_preview=str(exc)[:300],
            parent_agent_id=state.parent_agent_id,
            child_agent_name=state.child_agent_name,
            trace_id=state.trace_id,
        )
        return {
            "task_id": task_id,
            "status": "error",
            "result": str(exc),
            **_session_payload(state.child_session_id),
        }


async def cancel_async_delegation(
    task_id: str,
    *,
    parent_agent_id: str | uuid.UUID | None = None,
    force: bool = False,
    min_runtime_seconds: float = ASYNC_DELEGATION_CANCEL_GRACE_SECONDS,
) -> dict[str, Any]:
    """Cancel a running async delegation if it belongs to the caller."""

    def _session_payload(child_session_id: str | None, parent_session_id: str | None = None) -> dict[str, Any]:
        session_id = str(child_session_id or "").strip() or None
        return {
            "session_id": session_id,
            "child_session_id": session_id,
            "parent_session_id": str(parent_session_id or "").strip() or None,
        }

    state = _async_tasks.get(task_id)
    request_parent_agent_id = _maybe_uuid(parent_agent_id)
    min_runtime_seconds = max(float(min_runtime_seconds), 0.0)

    if state is None:
        try:
            persisted = await get_runtime_task_record(task_id)
        except Exception as exc:
            logger.warning("[Orchestrator] Failed to load runtime task %s for cancellation: %s", task_id, exc)
            persisted = None
        if persisted is None:
            return {"task_id": task_id, "status": "not_found", "result": None}
        persisted_parent_agent_id = _maybe_uuid(persisted.get("parent_agent_id"))
        if (
            request_parent_agent_id is not None
            and persisted_parent_agent_id is not None
            and request_parent_agent_id != persisted_parent_agent_id
        ):
            return {"task_id": task_id, "status": "forbidden", "result": None}
        status = persisted.get("status") or "not_found"
        if status in {"completed", "failed", "killed"}:
            return {
                "task_id": task_id,
                "status": status,
                "result": persisted.get("result"),
                **_session_payload(persisted.get("child_session_id"), persisted.get("parent_session_id")),
            }
        if status == "running" and not force and min_runtime_seconds > 0:
            started_at = _coerce_aware_datetime(
                persisted.get("started_at")
                or persisted.get("startedAt")
                or persisted.get("created_at")
                or persisted.get("createdAt")
            )
            if started_at is not None:
                elapsed_seconds = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.0)
                if elapsed_seconds < min_runtime_seconds:
                    return _deferred_cancellation_result(
                        task_id,
                        elapsed_seconds=elapsed_seconds,
                        min_runtime_seconds=min_runtime_seconds,
                    )
        return {
            "task_id": task_id,
            "status": "not_running_here",
            "result": "Task exists but is not running in this worker process.",
            **_session_payload(persisted.get("child_session_id"), persisted.get("parent_session_id")),
        }

    if (
        request_parent_agent_id is not None
        and state.parent_agent_id is not None
        and request_parent_agent_id != state.parent_agent_id
    ):
        return {"task_id": task_id, "status": "forbidden", "result": None}

    if state.task.done():
        return await check_async_delegation(task_id, parent_agent_id=request_parent_agent_id)

    if not force and min_runtime_seconds > 0:
        elapsed_seconds = max((datetime.now(timezone.utc) - state.started_at).total_seconds(), 0.0)
        if elapsed_seconds < min_runtime_seconds:
            return _deferred_cancellation_result(
                task_id,
                elapsed_seconds=elapsed_seconds,
                min_runtime_seconds=min_runtime_seconds,
            )

    state.task.cancel()
    try:
        await state.task
    except asyncio.CancelledError:
        logger.debug("[orchestrator] async task %s cancelled as requested", task_id)
    finally:
        _async_tasks.pop(task_id, None)

    try:
        await update_runtime_task_record(
            task_id,
            status="killed",
            result_summary="Task cancelled by parent agent",
            trace_id=state.trace_id,
            metadata_json={"timed_out": False, "depth_limited": False, "cancelled": True},
        )
    except Exception as exc:
        logger.warning("[Orchestrator] Failed to persist cancelled runtime task %s: %s", task_id, exc)

    await _persist_delegation_event(
        task_id=task_id,
        status="killed",
        result_preview="Task cancelled by parent agent",
        parent_agent_id=state.parent_agent_id,
        child_agent_name=state.child_agent_name,
        trace_id=state.trace_id,
    )
    return {
        "task_id": task_id,
        "status": "killed",
        "result": "Task cancelled by parent agent",
        **_session_payload(state.child_session_id),
    }


def list_async_delegations(
    *,
    parent_agent_id: str | uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """List all tracked async delegations with their status."""
    results: list[dict[str, Any]] = []
    request_parent_agent_id = _maybe_uuid(parent_agent_id)
    for task_id, state in _async_tasks.items():
        if request_parent_agent_id is not None and state.parent_agent_id != request_parent_agent_id:
            continue
        if state.task.cancelled():
            status = "killed"
        else:
            status = "completed" if state.task.done() else "running"
        results.append(
            {
                "task_id": task_id,
                "status": status,
                "target_agent": state.child_agent_name,
                "session_id": state.child_session_id,
                "child_session_id": state.child_session_id,
                "trace_id": state.trace_id,
            }
        )
    return results


async def resume_persisted_async_delegations(*, limit: int = 50) -> list[str]:
    """Resume restart-safe async delegations from persisted runtime task records."""
    resumed: list[str] = []
    try:
        records = await list_active_runtime_task_records(limit=limit, statuses=("pending", "running"))
    except Exception as exc:
        logger.warning("[Orchestrator] Failed to load active runtime tasks for resume: %s", exc)
        return resumed

    for record in records:
        task_id = str(record.get("task_id") or "")
        if not task_id or task_id in _async_tasks:
            continue

        metadata = record.get("metadata") or {}
        if not metadata.get("resumable_delegation") or not metadata.get("resume_after_restart"):
            continue
        replay_safe = _delegation_profile_restart_replay_safe(metadata.get("tool_profile"))
        if not replay_safe:
            try:
                await update_runtime_task_record(
                    task_id,
                    status="needs_reconciliation",
                    result_summary=(
                        "Task was not resumed after restart because its delegation tool profile is not "
                        "safe to replay without duplicating side effects. Reconciliation is required before retry."
                    ),
                    metadata_json=build_restart_reconciliation_metadata(
                        metadata,
                        task_type="delegation",
                        task_id=task_id,
                        blocker="non_idempotent_tool_profile",
                        summary=(
                            "Task was not resumed after restart because its delegation tool profile is not "
                            "safe to replay without duplicating side effects. Reconciliation is required before retry."
                        ),
                        trace_id=str(record.get("trace_id") or ""),
                        session_id=str(record.get("child_session_id") or record.get("parent_session_id") or ""),
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "[Orchestrator] Failed to persist non-replay-safe resume failure for %s: %s", task_id, exc
                )
            continue

        target_agent_id = _maybe_uuid(metadata.get("target_agent_id") or record.get("child_agent_id"))
        owner_id = _maybe_uuid(metadata.get("owner_id"))
        conversation_messages = metadata.get("conversation_messages")
        if target_agent_id is None or owner_id is None or not isinstance(conversation_messages, list):
            logger.warning("[Orchestrator] Runtime task %s missing resumable metadata; cannot resume", task_id)
            continue

        resolved = await _resolve_resumable_target_runtime(target_agent_id)
        if resolved is None:
            try:
                await update_runtime_task_record(
                    task_id,
                    status="failed",
                    result_summary="Task could not be resumed after restart because the target agent runtime is unavailable.",
                    metadata_json={"resume_failed": True},
                )
            except Exception as exc:
                logger.warning("[Orchestrator] Failed to persist resume failure for %s: %s", task_id, exc)
            continue

        target, target_model = resolved
        request = AgentDelegationRequest(
            target=target,
            target_model=target_model,
            conversation_messages=conversation_messages,
            owner_id=owner_id,
            session_id=str(record.get("child_session_id") or uuid.uuid4().hex),
            tool_executor=None,
            system_prompt_suffix=str(metadata.get("system_prompt_suffix") or ""),
            max_tool_rounds=metadata.get("max_tool_rounds"),
            parent_agent_id=record.get("parent_agent_id"),
            parent_session_id=record.get("parent_session_id"),
            trace_id=str(record.get("trace_id") or uuid.uuid4().hex),
            depth=int(record.get("depth") or 1),
            policy=OrchestrationPolicy(
                timeout_seconds=float(metadata.get("timeout_seconds") or 120.0),
                tool_profile=str(metadata.get("tool_profile") or "worker_safe"),
            ),
            execution_identity=_execution_identity_from_metadata(metadata.get("execution_identity")),
            confirmed_plan_id=metadata.get("plan_id"),
            confirmed_plan_version=metadata.get("plan_version"),
            confirmed_plan_hash=metadata.get("plan_hash"),
            plan_exempt_reason=metadata.get("plan_exempt_reason"),
            runtime_task_id=task_id,
            restart_replay_contract=metadata.get("restart_replay_contract")
            if isinstance(metadata.get("restart_replay_contract"), dict)
            else None,
            permission_profile=metadata.get("permission_profile"),
        )

        _spawn_async_delegation_task(task_id=task_id, request=request, trace_id=request.trace_id or uuid.uuid4().hex)
        try:
            resume_metadata = merge_restart_replay_journal(
                metadata,
                build_restart_replay_journal_entry(
                    task_type="delegation",
                    task_id=task_id,
                    side_effect_risk=str(metadata.get("side_effect_risk") or "read_only"),
                    phase="resume_intent_recorded",
                    trace_id=request.trace_id,
                    session_id=request.session_id,
                ),
            )
            await update_runtime_task_record(
                task_id,
                status="running",
                trace_id=request.trace_id,
                child_session_id=request.session_id,
                metadata_json={
                    "resumed_after_restart": True,
                    "resumed_at": datetime.now(timezone.utc).isoformat(),
                    "restart_replay_journal": resume_metadata.get("restart_replay_journal"),
                },
            )
        except Exception as exc:
            logger.warning("[Orchestrator] Failed to mark resumed runtime task %s as running: %s", task_id, exc)
        resumed.append(task_id)

    return resumed
