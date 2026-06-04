"""Explicit multi-agent orchestration helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.agents.coordination_gateway import CoordinationGateway
from app.agents.coordination_wiring import gateway_scope
from app.agents.delegation_token import DEFAULT_DELEGATION_TTL_SECONDS, issue_delegation_token
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.agent_tools import CORE_TOOL_NAMES
from app.services.capability_gate import CAPABILITY_MAP
from app.services.runtime_task_service import (
    create_runtime_task_record,
    get_runtime_task_record,
    list_active_runtime_task_records,
    update_runtime_task_record,
)

logger = logging.getLogger(__name__)

ToolExecutor = Callable[..., Awaitable[str] | str]
_DELEGATION_BASE_EXCLUDED_TOOLS = (
    "delegate_to_agent",
    "send_message_to_agent",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "send_channel_file",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
)
_DELEGATION_BRIEF_MAX_CHARS = 4_000
_DELEGATION_SOURCE_MAX_MESSAGES = 8


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
        excluded_tools=_DELEGATION_BASE_EXCLUDED_TOOLS + ("save_skill", "save_memory", "search_memory", "load_memory"),
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
        excluded_tools=_DELEGATION_BASE_EXCLUDED_TOOLS + ("save_skill", "save_memory"),
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
}


def _resolve_delegation_tool_profile(name: str | None) -> DelegationToolProfile:
    return _DELEGATION_TOOL_PROFILES.get(name or "worker_safe", _DELEGATION_TOOL_PROFILES["worker_safe"])


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
    return issue_delegation_token(
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        granted_capabilities=_delegation_capability_grants(profile),
        ttl_seconds=ttl_seconds,
    )


def _build_delegated_worker_prompt(profile: DelegationToolProfile) -> str:
    return (
        "<role>\n"
        "You are a delegated worker running in an isolated child session. A parent\n"
        "agent (or coordinator) handed you a scoped task with a specific brief.\n"
        "Your job: execute the brief and return a structured report the parent can\n"
        "consume. You are NOT a chat assistant — you produce work, not conversation.\n"
        "</role>\n\n"
        "<isolation_contract>\n"
        "- The delegated task brief is the ONLY authoritative context you have.\n"
        "- The parent agent's conversation history is NOT available to you.\n"
        "- Do not assume shared state with the parent beyond what the brief says.\n"
        "- Do not leak information about the parent's other tasks or sessions.\n"
        "- Delegation tools are disabled in worker sessions — do not try to spawn\n"
        "  nested workers. If the task truly exceeds a single worker's scope,\n"
        "  report it as a Blocker and let the parent re-scope.\n"
        "</isolation_contract>\n\n"
        "<tool_policy>\n"
        f"- {profile.tool_rule}\n"
        f"- {profile.memory_rule}\n"
        "</tool_policy>\n\n"
        "<return_format>\n"
        "Every reply MUST end with exactly these three sections. No prose outside\n"
        "the sections. No filler. The parent parses this structure.\n\n"
        "```\n"
        "Completed:\n"
        "- <concrete outcome — verb + object + result state>\n"
        "- <one bullet per discrete outcome>\n"
        "\n"
        "Evidence:\n"
        "- <file:line or tool-result reference for each claim above>\n"
        "- <test/verification output where relevant>\n"
        "\n"
        "Blockers:\n"
        "- <specific unresolved items, or 'none'>\n"
        "```\n"
        "</return_format>\n\n"
        "<good_return_examples>\n"
        "**Example A — implementation task**\n"
        "```\n"
        "Completed:\n"
        "- Fixed token-expiry race in middleware.py:142 by moving the refresh check\n"
        "  before the response header write\n"
        "- Added regression test covering the expired-token path\n"
        "\n"
        "Evidence:\n"
        "- Diff: backend/app/auth/middleware.py:138-148 (3 lines changed)\n"
        "- New test: backend/tests/auth/test_middleware.py::test_expired_token_refreshes\n"
        "- pytest run: 1 new test passed, 23 existing tests still pass\n"
        "\n"
        "Blockers:\n"
        "- none\n"
        "```\n\n"
        "**Example B — research task**\n"
        "```\n"
        "Completed:\n"
        "- Audited backend/app/auth/*.py and mapped all token-expiry handling paths\n"
        "- Identified 2 bug candidates and 1 design smell\n"
        "\n"
        "Evidence:\n"
        "- Bug 1: middleware.py:142 — refresh check runs AFTER response write,\n"
        "  so an expired token leaks a 401 once per session\n"
        "- Bug 2: refresh.py:87 — exception path swallows the refresh failure\n"
        "  (bare except), masking upstream issues\n"
        "- Design smell: token_store.py:55 — in-memory cache has no TTL, grows\n"
        "  unboundedly in long-running workers\n"
        "\n"
        "Blockers:\n"
        "- none\n"
        "```\n\n"
        "**Example C — task that couldn't fully complete**\n"
        "```\n"
        "Completed:\n"
        "- Read the specified config files (entrypoint.sh, Dockerfile, railway.json)\n"
        "- Drafted 80% of the deploy-flow diagram\n"
        "\n"
        "Evidence:\n"
        "- Notes saved to workspace/deploy_audit.md\n"
        "- Diagram draft: workspace/deploy_flow.mermaid (covers build + deploy,\n"
        "  missing health-check/rollback branches)\n"
        "\n"
        "Blockers:\n"
        "- Cannot access .env.production (not in workspace) — need the parent to\n"
        "  confirm which env vars are live so the diagram is accurate\n"
        "```\n"
        "</good_return_examples>\n\n"
        "<bad_return_examples>\n"
        "DO NOT return any of these:\n\n"
        "❌ **Empty Completed claim**\n"
        "```\n"
        "Completed:\n"
        "- Task done.\n"
        "Evidence:\n"
        "- See above.\n"
        "```\n"
        "(No concrete outcome, no verifiable evidence. The parent cannot act on this.)\n\n"
        "❌ **Prose wrapping the structured block**\n"
        "```\n"
        "Sure! I went ahead and worked on the task. Here's what I did:\n"
        "Completed: ...\n"
        "Let me know if you need anything else!\n"
        "```\n"
        "(The parent parses the structure; surrounding prose contaminates parsing\n"
        "and wastes tokens.)\n\n"
        "❌ **Fabricated evidence**\n"
        "```\n"
        "Completed:\n"
        "- Fixed the bug\n"
        "Evidence:\n"
        "- Tests pass (I didn't actually run them)\n"
        "```\n"
        "(If you didn't run the tests, say 'test run skipped, needs verification'\n"
        "in Blockers. Never claim evidence you don't have.)\n\n"
        "❌ **Leaking parent context or other sessions**\n"
        "```\n"
        "Completed:\n"
        "- Did what the user asked in the previous message\n"
        "```\n"
        "(You don't have access to the parent's conversation. The brief is your\n"
        "only authoritative context. Refer to it by the task content, not by\n"
        "pronouns that only the parent can resolve.)\n"
        "</bad_return_examples>"
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
    trace_id: str


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


def _normalize_delegation_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown").strip().lower()
    content = str(message.get("content") or "").strip()
    if not content:
        return ""
    role_label = {
        "user": "User",
        "assistant": "Assistant",
        "system": "System",
        "tool": "Tool",
    }.get(role, role.title() or "Message")
    return f"{role_label}: {content}"


def _build_delegation_brief(conversation_messages: list[dict[str, Any]]) -> str:
    """Collapse parent transcript into a fresh child-session brief.

    Child agents should receive a focused brief, not the parent's raw transcript.
    This keeps tool noise and unrelated history out of the child context while
    still preserving the latest task framing.
    """
    normalized = [
        _normalize_delegation_message(message) for message in conversation_messages[-_DELEGATION_SOURCE_MAX_MESSAGES:]
    ]
    transcript = "\n".join(line for line in normalized if line).strip()
    if len(transcript) > _DELEGATION_BRIEF_MAX_CHARS:
        transcript = transcript[-_DELEGATION_BRIEF_MAX_CHARS:]
        transcript = "...\n" + transcript.lstrip()

    if not transcript:
        transcript = "User: Complete the delegated task and report the concrete result."

    return (
        "## Delegated Task Brief\n"
        "You are receiving a synthesized task brief from a parent agent.\n"
        "Work only from the brief below. If context is missing, state the gap explicitly instead of inventing details.\n\n"
        "### Parent Context Snapshot\n"
        f"{transcript}\n\n"
        "### Expected Return\n"
        "- What you completed\n"
        "- Concrete evidence or artifacts\n"
        "- Remaining blockers or unknowns"
    )


def _build_runtime_task_metadata(request: AgentDelegationRequest) -> dict[str, Any]:
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
    resumable = request.tool_executor is None
    if resumable:
        try:
            json.dumps(request.conversation_messages)
        except (TypeError, ValueError):
            resumable = False

    metadata["resumable_delegation"] = resumable
    metadata["resume_after_restart"] = resumable
    if resumable:
        metadata.update(
            {
                "owner_id": str(request.owner_id),
                "target_agent_id": str(getattr(request.target, "id", "")),
                "conversation_messages": request.conversation_messages,
                "max_tool_rounds": request.max_tool_rounds,
                "timeout_seconds": request.policy.timeout_seconds,
            }
        )
    return metadata


async def _delegation_plan_gate_allows(request: AgentDelegationRequest) -> tuple[bool, str | None]:
    """Final Plan Mode backstop for async delegation startup."""
    if request.interaction_type != "delegation":
        return True, None
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
        if getattr(target, "agent_type", "native") == "openclaw":
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
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    depth: int = 1,
    policy: OrchestrationPolicy | None = None,
    interaction_type: str = "delegation",
    tenant_id: uuid.UUID | str | None = None,
    ledger_todo_id: str | None = None,
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
        parent_session_id=parent_session_id,
        trace_id=trace_id,
        depth=depth,
        policy=policy or OrchestrationPolicy(),
        interaction_type=interaction_type,
        tenant_id=tenant_id,
        ledger_todo_id=ledger_todo_id,
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

    delegated_brief = _build_delegation_brief(request.conversation_messages)
    combined_suffix = "\n\n".join(
        part.strip()
        for part in [request.system_prompt_suffix, _build_delegated_worker_prompt(tool_profile)]
        if part and part.strip()
    )

    session_metadata: dict[str, Any] = {
        "interaction_type": request.interaction_type,
    }
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

    invocation = AgentInvocationRequest(
        model=request.target_model,
        messages=[{"role": "user", "content": delegated_brief}],
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
        system_prompt_suffix=combined_suffix,
        tool_executor=request.tool_executor,
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

    if is_delegation:
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.DELEGATION_END,
                agent_id=request.target.id,
                session_id=child_session_id,
                messages=request.conversation_messages,
                source="agent",
                metadata={
                    "from_agent": str(request.parent_agent_id) if request.parent_agent_id else None,
                    "to_agent": str(request.target.id),
                    "to_agent_name": request.target.name,
                    "trace_id": trace_id,
                    "depth": request.depth,
                    "status": _delegation_status,
                    "failed": delegation_result.failed,
                    "task": (
                        request.conversation_messages[-1].get("content", "")[:500]
                        if request.conversation_messages
                        else ""
                    ),
                    "result": (delegation_result.content or "")[:2000],
                },
            )
        except Exception as _hook_err:
            logger.debug("[Orchestrator] DELEGATION_END hook failed (non-fatal): %s", _hook_err)

    return delegation_result


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
        if request.tenant_id:
            from app.database import set_current_tenant

            set_current_tenant(str(request.tenant_id))
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
            try:
                await update_runtime_task_record(
                    task_id,
                    status="failed" if delegation_result.failed else "completed",
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

    task = asyncio.create_task(_run(), name="async-delegation-" + task_id)
    _async_tasks[task_id] = AsyncDelegationState(
        task=task,
        parent_agent_id=_maybe_uuid(request.parent_agent_id),
        child_agent_name=getattr(request.target, "name", None),
        trace_id=trace_id,
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
    parent_session_id: str | None = None,
    trace_id: str | None = None,
    depth: int = 1,
    policy: OrchestrationPolicy | None = None,
    interaction_type: str = "delegation",
    coordination_gateway: CoordinationGateway | None = None,
    tenant_id: uuid.UUID | str | None = None,
    confirmed_plan_id: str | uuid.UUID | None = None,
    confirmed_plan_version: int | None = None,
    confirmed_plan_hash: str | None = None,
    plan_exempt_reason: str | None = None,
    ledger_todo_id: str | None = None,
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
        parent_session_id=parent_session_id,
        trace_id=real_trace_id,
        depth=depth,
        policy=policy or OrchestrationPolicy(timeout_seconds=120.0),
        interaction_type=interaction_type,
        confirmed_plan_id=confirmed_plan_id,
        confirmed_plan_version=confirmed_plan_version,
        confirmed_plan_hash=confirmed_plan_hash,
        plan_exempt_reason=plan_exempt_reason,
        tenant_id=tenant_id,
        ledger_todo_id=ledger_todo_id,
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
    metadata_json = _build_runtime_task_metadata(request)
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
        }
    request_parent_agent_id = _maybe_uuid(parent_agent_id)
    if (
        request_parent_agent_id is not None
        and state.parent_agent_id is not None
        and request_parent_agent_id != state.parent_agent_id
    ):
        return {"task_id": task_id, "status": "forbidden", "result": None}
    if not state.task.done():
        return {"task_id": task_id, "status": "running", "result": None}
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
        return {"task_id": task_id, "status": "error", "result": str(exc)}


async def cancel_async_delegation(
    task_id: str,
    *,
    parent_agent_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """Cancel a running async delegation if it belongs to the caller."""
    state = _async_tasks.get(task_id)
    request_parent_agent_id = _maybe_uuid(parent_agent_id)

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
            return {"task_id": task_id, "status": status, "result": persisted.get("result")}
        return {
            "task_id": task_id,
            "status": "not_running_here",
            "result": "Task exists but is not running in this worker process.",
        }

    if (
        request_parent_agent_id is not None
        and state.parent_agent_id is not None
        and request_parent_agent_id != state.parent_agent_id
    ):
        return {"task_id": task_id, "status": "forbidden", "result": None}

    if state.task.done():
        return await check_async_delegation(task_id, parent_agent_id=request_parent_agent_id)

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
            confirmed_plan_id=metadata.get("plan_id"),
            confirmed_plan_version=metadata.get("plan_version"),
            confirmed_plan_hash=metadata.get("plan_hash"),
            plan_exempt_reason=metadata.get("plan_exempt_reason"),
        )

        _spawn_async_delegation_task(task_id=task_id, request=request, trace_id=request.trace_id or uuid.uuid4().hex)
        try:
            await update_runtime_task_record(
                task_id,
                status="running",
                trace_id=request.trace_id,
                child_session_id=request.session_id,
                metadata_json={
                    "resumed_after_restart": True,
                    "resumed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("[Orchestrator] Failed to mark resumed runtime task %s as running: %s", task_id, exc)
        resumed.append(task_id)

    return resumed
