"""Unified agent runtime invoker.

This module centralizes the LLM/tool loop so websocket chat, task execution,
heartbeat, scheduler, and agent-to-agent flows can share the same runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.kernel import (
    AgentKernel,
    ExecutionIdentityRef,
    InvocationRequest,
    KernelDependencies,
    RuntimeConfig,
    ToolExpansionResult,
)
from app.kernel.contracts import MidRunMessageDrain
from app.models.agent import Agent
from app.models.feature_flag import FeatureFlag
from app.models.user import User
from app.runtime.context_budget import (
    ContextBudget,
    _is_simple_turn_candidate,
    compute_context_budget,
    resolve_turn_model_route,
)
from app.runtime.context_engine import DefaultContextEngine
from app.runtime.prompt_builder import build_frozen_prompt_prefix
from app.runtime.session import SessionContext
from app.runtime.session_key import build_session_key, ensure_session_key
from app.services.agent_context import build_agent_context, build_agent_runtime_context
from app.services.agent_work_ledger import should_enable_work_ledger
from app.services.agent_tools import (
    CORE_TOOL_NAMES,
    execute_tool,
    get_agent_tools_for_llm,
    get_combined_openai_tools,
    list_agent_mcp_deferred_tools,
)
from app.services.feature_flags import is_enabled as is_feature_enabled
from app.services.knowledge_inject import fetch_relevant_knowledge
from app.services.llm_utils import LLMMessage, create_llm_client, get_max_tokens
from app.services.memory_service import (
    build_memory_context,
    maybe_compress_messages,
    persist_runtime_memory,
)
from app.services.quota_guard import QuotaExceeded, check_user_token_quota
from app.services.token_tracker import (
    estimate_tokens_from_chars,
    extract_usage_tokens,
    record_token_usage,
)
from app.tools.runtime_tool_groups import (
    RUNTIME_TOOL_GROUPS,
    iter_runtime_tool_groups,
    normalize_tool_query,
    runtime_tool_group_for_name,
)

logger = logging.getLogger(__name__)

ChunkCallback = Callable[[str], Awaitable[None] | None]
ThinkingCallback = Callable[[str], Awaitable[None] | None]
ToolCallback = Callable[[dict], Awaitable[None] | None]
ToolExecutor = Callable[..., Awaitable[str] | str]
EventCallback = Callable[[dict], Awaitable[None] | None]


_RUNTIME_FLAG_DEFAULTS: dict[str, bool] = {
    "runtime_continuity_v1": False,
    "skill_candidate_loop_v1": True,
}


def _context_engine() -> DefaultContextEngine:
    return DefaultContextEngine()


def _normalize_invocation_session_context(request: AgentInvocationRequest) -> None:
    if request.session_context is None:
        return
    metadata = _session_metadata(request.session_context)
    key = build_session_key(
        agent_id=request.agent_id,
        tenant_id=metadata.get("tenant_id"),
        source=request.session_context.source,
        channel=request.session_context.channel,
        external_conv_id=metadata.get("external_conv_id") or request.session_context.session_id,
        runtime_task_id=metadata.get("runtime_task_id") or metadata.get("task_id"),
        trace_id=metadata.get("trace_id"),
    )
    ensure_session_key(request.session_context, key)


@dataclass(slots=True)
class AgentInvocationRequest:
    model: Any
    messages: list[dict]
    agent_name: str
    role_description: str
    fallback_model: Any | None = None
    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    execution_identity: ExecutionIdentityRef | None = None
    on_chunk: ChunkCallback | None = None
    on_tool_call: ToolCallback | None = None
    on_thinking: ThinkingCallback | None = None
    on_event: EventCallback | None = None
    supports_vision: bool = False
    memory_context: str = ""
    memory_session_id: str | None = None
    memory_messages: list[dict] | None = None
    session_context: SessionContext | None = None
    system_prompt_suffix: str = ""
    # CC subagent semantics (replace, not layer): when set, this text IS the
    # entire system prompt — no host soul/memory/skills/tasks are assembled
    # around it. Subagent spawn sets this; channel/delegation paths keep using
    # system_prompt_suffix, whose layered semantics are unchanged.
    standalone_system_prompt: str = ""
    tool_executor: ToolExecutor | None = None
    mid_run_message_drain: MidRunMessageDrain | None = None
    cancel_event: asyncio.Event | None = None
    initial_tools: list[dict] | None = None
    core_tools_only: bool = True
    allowed_tool_names: tuple[str, ...] = ()
    excluded_tool_names: tuple[str, ...] = ()
    expand_tools: bool = True
    max_tool_rounds: int | None = None
    invocation_scope: str | None = None
    smart_model_routing: dict[str, Any] | None = None
    delegation_token: Any | None = None
    # RC11: when True the kernel exposes ZERO tools to the LLM (see get_agent_kernel).
    # Deep Research reasoning passes set this so the synthesis LLM returns its report
    # as text instead of routing it through a write_file call that blows the round budget.
    disable_tools: bool = False
    # Task1: per-call output-token ceiling override. Deep Research synthesis sets
    # this so the full report is not truncated at the model's chat default; the
    # kernel feeds it into get_max_tokens (still clamped to the hard limit).
    max_output_tokens: int | None = None


@dataclass(slots=True)
class AgentInvocationResult:
    content: str
    tokens_used: int = 0
    final_tools: list[dict] | None = None
    parts: list[dict] | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _session_metadata(session_context: SessionContext | None) -> dict[str, Any]:
    if session_context is None:
        return {}
    metadata = session_context.metadata
    if isinstance(metadata, dict):
        return metadata
    session_context.metadata = {}
    return session_context.metadata


def _plan_mode_interactive_available(session_context: SessionContext | None) -> bool:
    # Delegate to the shared boundary so the invoker and kernel never drift.
    from app.runtime.session import is_interactive_plan_eligible

    return is_interactive_plan_eligible(session_context)


def _plan_mode_unattended_available(session_context: SessionContext | None) -> bool:
    # Unattended (trigger/heartbeat) tool-intercept → main-loop Plan Mode
    # eligibility (path-unification §5.3 / cut ②). Shared boundary so the invoker
    # and kernel never drift on which sources defer to the agent's own loop.
    from app.runtime.session import is_unattended_plan_eligible

    return is_unattended_plan_eligible(session_context)


async def _resolve_runtime_config(agent_id: uuid.UUID | None) -> RuntimeConfig:
    # P0-1b: instead of silently returning tenant_id=None on failure paths,
    # set the tenant_resolution_error sentinel so kernel.engine can early-exit
    # with an error result before any tool runs. Governance (P0-1a) is the
    # second line of defence in case a caller bypasses the kernel.
    if not agent_id:
        logger.warning("[Invoker] _resolve_runtime_config called without agent_id — fail-closed")
        return RuntimeConfig(
            tenant_id=None,
            max_tool_rounds=200,
            tenant_resolution_error="No agent_id provided to runtime resolution",
        )

    try:
        async with async_session() as db, enter_rls_bypass(db, reason=f"runtime config bootstrap for agent {agent_id}"):
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning("[Invoker] Agent %s not found in DB — fail-closed", agent_id)
                return RuntimeConfig(
                    tenant_id=None,
                    max_tool_rounds=200,
                    tenant_resolution_error=f"Agent {agent_id} not found",
                )

            # Token quota enforcement is now at User level (quota_guard.check_user_llm_quota)
            quota_message = None
            local_default = bool(get_settings().DEBUG)

            async def _resolve_flag(flag_key: str) -> bool:
                flag_result = await db.execute(select(FeatureFlag).where(FeatureFlag.key == flag_key))
                flag = flag_result.scalar_one_or_none()
                if flag is None:
                    return _RUNTIME_FLAG_DEFAULTS.get(flag_key, local_default)
                return await is_feature_enabled(db, flag_key, tenant_id=agent.tenant_id)

            return RuntimeConfig(
                tenant_id=agent.tenant_id,
                max_tool_rounds=agent.max_tool_rounds or 200,
                quota_message=quota_message,
                execution_mode=getattr(agent, "execution_mode", None),
                runtime_continuity_enabled=await _resolve_flag("runtime_continuity_v1"),
                skill_candidate_loop_enabled=await _resolve_flag("skill_candidate_loop_v1"),
            )
    except Exception as exc:
        # Log full exception detail server-side for ops; surface only the
        # exception class to the LLM/UI to avoid leaking SQL state, table
        # names, or connection strings in DB errors. Code-reviewer flagged
        # the original `f"...: {exc}"` as a potential information leak.
        logger.exception("[Invoker] Failed to resolve runtime config for agent %s — fail-closed", agent_id)
        return RuntimeConfig(
            tenant_id=None,
            max_tool_rounds=200,
            tenant_resolution_error=f"Runtime config resolution failed for agent {agent_id} ({type(exc).__name__})",
        )


async def _resolve_current_user_name(user_id: uuid.UUID | None) -> str | None:
    if not user_id:
        return None

    try:
        async with async_session() as db, enter_rls_bypass(db, reason=f"display-name bootstrap for user {user_id}"):
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                return user.display_name or user.username
    except Exception as exc:
        logger.debug("Failed to resolve current user name for %s: %s", user_id, exc)
    return None


def _apply_vision_transform(api_messages: list[LLMMessage], supports_vision: bool) -> list[LLMMessage]:
    if supports_vision:
        image_pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
        for i, msg in enumerate(api_messages):
            if msg.role != "user" or not isinstance(msg.content, str):
                continue
            images = re.findall(image_pattern, msg.content)
            if not images:
                continue
            text = re.sub(image_pattern, "", msg.content).strip()
            parts: list[dict[str, Any]] = []
            for image_url in images:
                parts.append({"type": "image_url", "image_url": {"url": image_url}})
            if text:
                parts.append({"type": "text", "text": text})
            api_messages[i] = LLMMessage(role=msg.role, content=parts)  # type: ignore[arg-type]
        return api_messages

    strip_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
    for i, msg in enumerate(api_messages):
        if msg.role != "user" or not isinstance(msg.content, str) or "[image_data:" not in msg.content:
            continue
        image_count = len(re.findall(strip_pattern, msg.content))
        cleaned = re.sub(strip_pattern, "", msg.content).strip()
        if image_count > 0:
            cleaned += f"\n[用户发送了 {image_count} 张图片，但当前模型不支持视觉，无法查看图片内容]"
        api_messages[i] = LLMMessage(role=msg.role, content=cleaned)
    return api_messages


def _apply_cache_hints(
    api_messages: list[LLMMessage], provider: str, invocation_scope: str = "conversation"
) -> list[LLMMessage]:
    """Apply provider-specific prompt cache hints (Anthropic, OpenAI, DeepSeek, Gemini, etc.)."""
    from app.services.prompt_cache import apply_cache_hints

    return apply_cache_hints(api_messages, provider, invocation_scope=invocation_scope)


async def _build_system_prompt(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
    resolved_memory_context: str,
    current_user_name: str | None = None,
) -> str:
    # CC subagent semantics: a standalone prompt IS the whole frozen prefix —
    # the spawned agent is a clean specialist, not the host plus a suffix.
    standalone = (request.standalone_system_prompt or "").strip()
    if standalone:
        return standalone
    if current_user_name is None:
        current_user_name = await _resolve_current_user_name(request.user_id)
    del tenant_id  # reserved for future prompt builders
    budget_profile = _resolve_context_budget(request)
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None
    agent_context = await build_agent_context(
        agent_id=request.agent_id,
        agent_name=request.agent_name,
        role_description=request.role_description,
        current_user_name=current_user_name,
        include_memory_file=False,
        include_runtime_metadata=False,
        include_focus=False,
        budget_profile=budget_profile,
        invocation_scope=request.invocation_scope or "conversation",
    )
    return build_frozen_prompt_prefix(
        agent_context=agent_context,
        context_window_tokens=context_window_tokens,
    )


def _last_user_query(messages: list[dict]) -> str:
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _resolve_context_budget(request: AgentInvocationRequest) -> ContextBudget:
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None
    active_pack_count = len(request.session_context.active_tool_groups) if request.session_context else 0
    budget_profile = compute_context_budget(
        context_window_tokens=context_window_tokens,
        query=_last_user_query(request.messages),
        messages=request.messages,
        active_pack_count=active_pack_count,
    )
    if request.session_context is not None:
        session_metadata = _session_metadata(request.session_context)
        session_metadata["context_budget"] = budget_profile
        session_metadata["context_window_tokens"] = context_window_tokens
    return budget_profile


async def _resolve_memory_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
    # ALWAYS load memory context — even when prompt_prefix is cached.
    # The engine injects memory as a dynamic suffix outside the frozen prefix,
    # so fresh memory can vary without invalidating the stable system prompt cache.
    # CC subagent semantics: a standalone-prompt invocation is a clean specialist —
    # the HOST agent's memory pyramid must not leak into its context. (Its own
    # subagent 记忆.md is appended to the standalone prompt by the spawn layer.)
    if (request.standalone_system_prompt or "").strip():
        return ""
    parts: list[str] = []
    session_id = request.memory_session_id
    if not session_id and request.session_context:
        session_id = request.session_context.session_id
    budget_profile = _resolve_context_budget(request)
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None
    query = _last_user_query(request.messages)

    if request.agent_id and tenant_id:
        _memory_kwargs = {
            "session_id": session_id,
            "query": query,
        }
        _memory_sig = inspect.signature(build_memory_context).parameters
        current_user_name = None
        if request.user_id and ("current_user_id" in _memory_sig or "current_user_name" in _memory_sig):
            current_user_name = await _resolve_current_user_name(request.user_id)
        if "context_window_tokens" in _memory_sig:
            _memory_kwargs["context_window_tokens"] = context_window_tokens
        if "budget_profile" in _memory_sig:
            _memory_kwargs["budget_profile"] = budget_profile
        if "current_user_id" in _memory_sig:
            _memory_kwargs["current_user_id"] = request.user_id
        if "current_user_name" in _memory_sig:
            _memory_kwargs["current_user_name"] = current_user_name
        runtime_memory_context = await build_memory_context(request.agent_id, tenant_id, **_memory_kwargs)
        if runtime_memory_context:
            parts.append(
                _context_engine().inject(
                    request.session_context,
                    kind="memory_context",
                    source="memory_provider:context",
                    content=runtime_memory_context,
                )
            )

    if request.memory_context:
        parts.append(
            _context_engine().inject(
                request.session_context,
                kind="request_memory_context",
                source="request.memory_context",
                content=request.memory_context,
            )
        )

    if request.agent_id and session_id:
        try:
            from app.services.session_learning import render_active_session_learning_projection

            session_learning = render_active_session_learning_projection(
                data_root=Path(get_settings().AGENT_DATA_DIR),
                agent_id=request.agent_id,
                session_id=str(session_id),
            )
            if session_learning:
                parts.append(
                    _context_engine().inject(
                        request.session_context,
                        kind="session_learning_projection",
                        source="session_learning:projection",
                        content=session_learning,
                    )
                )
        except Exception as exc:
            logger.debug("[SessionLearning] dynamic projection skipped for %s: %s", request.agent_id, exc)

    # Skill evolution self-awareness (Gap3): surface the agent's own skill
    # assets + decay state so skills become a first-class evolution axis beside
    # memory. Dynamic suffix only — never enters the frozen prefix, so it cannot
    # invalidate the system-prompt cache.
    if request.agent_id:
        try:
            from app.services.evolution_view import render_skill_evolution_digest

            skill_digest = render_skill_evolution_digest(Path(get_settings().AGENT_DATA_DIR) / str(request.agent_id))
            if skill_digest:
                parts.append(
                    _context_engine().inject(
                        request.session_context,
                        kind="skill_evolution_digest",
                        source="skill_curator:digest",
                        content=skill_digest,
                    )
                )
        except Exception as exc:
            logger.debug("[SkillEvolution] digest skipped for %s: %s", request.agent_id, exc)

    return "\n\n".join(parts)


async def _resolve_memory_navigation_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
    if (request.standalone_system_prompt or "").strip():
        return ""  # CC subagent semantics: no host memory navigation
    if not request.agent_id:
        return ""

    principal_stack = None
    if tenant_id:
        try:
            from app.services.memory_service import _resolve_activation_context

            current_user_name = await _resolve_current_user_name(request.user_id) if request.user_id else None
            activation_context = await _resolve_activation_context(
                agent_id=request.agent_id,
                tenant_id=tenant_id,
                query=_last_user_query(request.messages),
                current_user_id=request.user_id,
                current_user_name=current_user_name,
            )
            if activation_context:
                principal_stack = activation_context.principal_stack
        except Exception as exc:  # noqa: BLE001 — default navigation visibility is the safe fallback
            logger.debug("[MemoryNavigation] principal stack unavailable for %s: %s", request.agent_id, exc)

    try:
        from app.runtime.prompt_sections import build_memory_navigation_section

        return build_memory_navigation_section(
            Path(get_settings().AGENT_DATA_DIR),
            request.agent_id,
            principal_stack=principal_stack,
        )
    except Exception as exc:  # noqa: BLE001 — navigation is optional context
        logger.debug("[MemoryNavigation] render skipped for %s: %s", request.agent_id, exc)
        return ""


async def _resolve_runtime_metadata_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
    del tenant_id
    if (request.standalone_system_prompt or "").strip():
        return ""  # CC subagent semantics: no host runtime metadata
    if not request.agent_id:
        return ""

    budget_profile = _resolve_context_budget(request)
    current_user_name = await _resolve_current_user_name(request.user_id) if request.user_id else None
    _runtime_kwargs = {"current_user_name": current_user_name}
    _runtime_sig = inspect.signature(build_agent_runtime_context).parameters
    if "budget_profile" in _runtime_sig:
        _runtime_kwargs["budget_profile"] = budget_profile
    runtime_context = await build_agent_runtime_context(request.agent_id, **_runtime_kwargs)
    if not runtime_context:
        return ""
    return _context_engine().inject(
        request.session_context,
        kind="agent_runtime_context",
        source="runtime_context:agent",
        content=runtime_context,
    )


async def _resolve_retrieval_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
    if (request.standalone_system_prompt or "").strip():
        return ""  # CC subagent semantics: no host retrieval context
    query = _last_user_query(request.messages)
    if not query:
        return ""

    parts: list[str] = []
    budget_profile = _resolve_context_budget(request)

    _knowledge_kwargs = {}
    _knowledge_sig = inspect.signature(fetch_relevant_knowledge).parameters
    if "max_tokens" in _knowledge_sig:
        _knowledge_kwargs["max_tokens"] = max(500, budget_profile.knowledge_budget_chars // 3)
    if "max_chars" in _knowledge_sig:
        _knowledge_kwargs["max_chars"] = budget_profile.knowledge_budget_chars
    if "limit" in _knowledge_sig:
        _knowledge_kwargs["limit"] = budget_profile.external_limit
    knowledge = await _maybe_await(fetch_relevant_knowledge(query, tenant_id, **_knowledge_kwargs))
    if knowledge:
        parts.append(
            _context_engine().inject(
                request.session_context,
                kind="knowledge_relevant",
                source="knowledge_provider:relevant",
                content=knowledge,
            )
        )

    return "\n\n".join(parts)


def _serialize_pack(pack) -> dict[str, Any]:
    return {
        "name": pack.name,
        "summary": pack.summary,
        "source": pack.source,
        "activation_mode": pack.activation_mode,
        "tools": list(pack.tools),
    }


def _tool_names_from_openai_tools(tools: list[dict]) -> list[str]:
    return [
        tool["function"]["name"]
        for tool in tools
        if tool.get("type") == "function"
        and tool.get("function", {}).get("name")
        and tool["function"]["name"] not in CORE_TOOL_NAMES
    ]


def _infer_active_tool_groups(
    tool_names: list[str],
    *,
    skill_name: str | None = None,
    declared_pack_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    requested = set(tool_names)
    packs = [
        _serialize_pack(pack)
        for pack in RUNTIME_TOOL_GROUPS
        if pack.infer_from_tools and requested.intersection(pack.tools)
    ]
    existing_names = {pack["name"] for pack in packs}
    for pack_name in declared_pack_names or []:
        if pack_name in existing_names:
            continue
        pack = runtime_tool_group_for_name(pack_name)
        if pack:
            packs.append(_serialize_pack(pack))
            existing_names.add(pack_name)
    if packs or not requested:
        return packs
    synthetic_name = f"discovery:{(skill_name or 'custom').strip().lower().replace(' ', '_')}"
    return [
        {
            "name": synthetic_name,
            "summary": f"Tools discovered by {skill_name or 'tool_search'}",
            "source": "discovery",
            "activation_mode": "通过 tool_search 发现",
            "tools": sorted(requested),
            "skill_name": skill_name,
        }
    ]


async def _deferred_tool_names_for_query(agent_id: uuid.UUID, query: str) -> list[str]:
    normalized = query.strip().lower()
    compact = normalize_tool_query(normalized)
    if normalized:
        for pack in RUNTIME_TOOL_GROUPS:
            for tool_name in pack.tools:
                if (
                    tool_name.lower() == normalized or normalize_tool_query(tool_name) == compact
                ) and tool_name not in CORE_TOOL_NAMES:
                    return [tool_name]
    requested: list[str] = []
    seen: set[str] = set()
    for pack in iter_runtime_tool_groups(query):
        for tool_name in pack.tools:
            if tool_name in CORE_TOOL_NAMES or tool_name in seen:
                continue
            requested.append(tool_name)
            seen.add(tool_name)
    # J: imported MCP server tools are deferred too — append the agent-reachable
    # ones (governed listing) so tool_search discovers MCP exactly as it does the
    # static packs. The shared enumerator keeps this schema path consistent with
    # the text result the model reads (🦴#2).
    for mcp_name in await list_agent_mcp_deferred_tools(agent_id, query):
        if mcp_name in CORE_TOOL_NAMES or mcp_name in seen:
            continue
        requested.append(mcp_name)
        seen.add(mcp_name)
    return requested


async def _resolve_tool_expansion(
    request: AgentInvocationRequest,
    tool_name: str,
    args: dict[str, Any],
) -> ToolExpansionResult | list[dict] | None:
    if not request.agent_id:
        return None

    if tool_name == "tool_search":
        query = str(args.get("query", "") or "").strip()
        requested_tool_names = await _deferred_tool_names_for_query(request.agent_id, query)
        if not requested_tool_names:
            return None
        tools = await get_agent_tools_for_llm(
            request.agent_id,
            core_only=False,
            requested_names=requested_tool_names,
        )
        expanded_tool_names = _tool_names_from_openai_tools(tools)
        if not expanded_tool_names:
            return None
        if request.session_context is None:
            request.session_context = SessionContext()
        discovered = request.session_context.track_discovered_tools(expanded_tool_names)
        packs = _infer_active_tool_groups(expanded_tool_names)
        return ToolExpansionResult(
            tools=tools,
            active_tool_groups=packs,
            event_payload={
                "type": "deferred_tools_delta",
                "packs": packs,
                "tool_groups": packs,
                "discovered_tools": discovered,
                "all_discovered_tools": list(request.session_context.discovered_tools),
                "message": f"Discovered deferred tools: {', '.join(discovered or expanded_tool_names)}",
                "status": "info",
                "trigger_tool": tool_name,
            },
        )

    if tool_name in {"discover_resources", "import_mcp_server"}:
        tools = await get_agent_tools_for_llm(
            request.agent_id,
            core_only=False,
            requested_names=[
                "discover_resources",
                "import_mcp_server",
                "list_mcp_resources",
                "read_mcp_resource",
            ],
        )
        expanded_tool_names = _tool_names_from_openai_tools(tools)
        if not expanded_tool_names:
            return None
        packs = _infer_active_tool_groups(expanded_tool_names)
        return ToolExpansionResult(
            tools=tools,
            active_tool_groups=packs,
            event_payload={
                "type": "tool_group_activation",
                "packs": packs,
                "tool_groups": packs,
                "message": "Activated MCP runtime tool group.",
                "status": "info",
                "trigger_tool": tool_name,
            },
        )

    return None


async def _execute_tool_with_request(
    tool_name: str,
    args: dict,
    request: AgentInvocationRequest,
    emit_event: Callable[[dict], Any],
) -> str:
    if request.tool_executor:
        executor_kwargs: dict[str, Any] = {}
        try:
            executor_params = inspect.signature(request.tool_executor).parameters
            accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in executor_params.values())
        except (TypeError, ValueError):
            executor_params = {}
            accepts_kwargs = False
        if accepts_kwargs or "delegation_token" in executor_params:
            executor_kwargs["delegation_token"] = request.delegation_token
        if accepts_kwargs or "event_callback" in executor_params:
            executor_kwargs["event_callback"] = emit_event
        if accepts_kwargs or "plan_mode_interactive_available" in executor_params:
            executor_kwargs["plan_mode_interactive_available"] = _plan_mode_interactive_available(
                request.session_context
            )
        if accepts_kwargs or "plan_mode_unattended_available" in executor_params:
            executor_kwargs["plan_mode_unattended_available"] = _plan_mode_unattended_available(request.session_context)
        return await _maybe_await(request.tool_executor(tool_name, args, **executor_kwargs))

    execute_kwargs: dict[str, Any] = {
        "agent_id": request.agent_id,
        "user_id": request.user_id or request.agent_id,
    }
    if "event_callback" in inspect.signature(execute_tool).parameters:
        execute_kwargs["event_callback"] = emit_event
    if "delegation_token" in inspect.signature(execute_tool).parameters:
        execute_kwargs["delegation_token"] = request.delegation_token
    if "session_id" in inspect.signature(execute_tool).parameters:
        execute_kwargs["session_id"] = request.memory_session_id or (
            request.session_context.session_id if request.session_context else None
        )
    if "plan_mode_interactive_available" in inspect.signature(execute_tool).parameters:
        execute_kwargs["plan_mode_interactive_available"] = _plan_mode_interactive_available(request.session_context)
    if "plan_mode_unattended_available" in inspect.signature(execute_tool).parameters:
        execute_kwargs["plan_mode_unattended_available"] = _plan_mode_unattended_available(request.session_context)
    return await execute_tool(
        tool_name,
        args,
        **execute_kwargs,
    )


def get_agent_kernel(request: AgentInvocationRequest | None = None) -> AgentKernel:
    allowed_tool_names = frozenset(request.allowed_tool_names if request else ())
    excluded_tool_names = frozenset(request.excluded_tool_names if request else ())
    disable_tools = bool(request.disable_tools) if request else False

    async def _kernel_build_system_prompt(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
        resolved_memory_context: str,
        current_user_name: str | None,
    ) -> str:
        return await _build_system_prompt(
            request,  # type: ignore[arg-type]
            tenant_id,
            resolved_memory_context,
            current_user_name=current_user_name,
        )

    async def _kernel_resolve_memory_context(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
    ) -> str:
        return await _resolve_memory_context(request, tenant_id)  # type: ignore[arg-type]

    async def _kernel_resolve_runtime_metadata_context(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
    ) -> str:
        return await _resolve_runtime_metadata_context(request, tenant_id)  # type: ignore[arg-type]

    async def _kernel_resolve_retrieval_context(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
    ) -> str:
        return await _resolve_retrieval_context(request, tenant_id)  # type: ignore[arg-type]

    async def _kernel_resolve_memory_navigation_context(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
    ) -> str:
        return await _resolve_memory_navigation_context(request, tenant_id)  # type: ignore[arg-type]

    async def _kernel_get_tools(agent_id: uuid.UUID, core_only: bool) -> list[dict]:
        # RC11: deep-research reasoning passes disable the tool surface entirely so the
        # synthesis LLM cannot route its report through a write_file call (which blew the
        # 1-round budget and surfaced as "[Error] Too many tool call rounds").
        if disable_tools:
            return []
        # Auto-include channel-specific tools so agents don't need to
        # manually load_skill before their channel tools become available.
        _requested_names: list[str] = []
        if core_only and request.session_context:
            _source = request.session_context.source
            if _source == "feishu":
                from app.tools.runtime_tool_groups import runtime_tool_group_for_name

                _pack = runtime_tool_group_for_name("feishu_pack")
                if _pack:
                    _requested_names.extend(_pack.tools)
            # R3 (closure plan §7): re-inject schemas for tools discovered via
            # tool_search in earlier turns, so a discovered capability survives
            # compaction / a fresh invocation. The in-run full_toolset
            # accumulator does not persist across invocations; session.metadata
            # carries discovered_tools and __post_init__ restores them.
            _requested_names.extend(request.session_context.discovered_tools)
        tools = await _maybe_await(
            get_agent_tools_for_llm(
                agent_id,
                core_only=core_only,
                requested_names=_requested_names or None,
            )
        )
        if allowed_tool_names:
            tools = [tool for tool in tools if tool["function"]["name"] in allowed_tool_names]
        if not excluded_tool_names:
            return tools
        return [tool for tool in tools if tool["function"]["name"] not in excluded_tool_names]

    def _kernel_create_client(model: Any):
        return create_llm_client(
            provider=model.provider,
            api_key=model.api_key,
            model=model.model,
            base_url=model.base_url,
            timeout=120.0,
        )

    async def _kernel_execute_tool(
        tool_name: str,
        args: dict,
        request: InvocationRequest,
        emit_event: Callable[[dict], Any],
    ) -> str:
        return await _execute_tool_with_request(tool_name, args, request, emit_event)  # type: ignore[arg-type]

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=_resolve_runtime_config,
            resolve_current_user_name=_resolve_current_user_name,
            build_system_prompt=_kernel_build_system_prompt,
            resolve_memory_context=_kernel_resolve_memory_context,
            resolve_runtime_metadata_context=_kernel_resolve_runtime_metadata_context,
            resolve_retrieval_context=_kernel_resolve_retrieval_context,
            resolve_memory_navigation_context=_kernel_resolve_memory_navigation_context,
            get_tools=_kernel_get_tools,
            resolve_tool_expansion=_resolve_tool_expansion,
            maybe_compress_messages=maybe_compress_messages,
            create_client=_kernel_create_client,
            execute_tool=_kernel_execute_tool,
            persist_memory=persist_runtime_memory,
            record_token_usage=record_token_usage,
            get_max_tokens=get_max_tokens,
            extract_usage_tokens=extract_usage_tokens,
            estimate_tokens_from_chars=estimate_tokens_from_chars,
            apply_vision_transform=_apply_vision_transform,
            apply_cache_hints=_apply_cache_hints,
        )
    )


def _resolve_eviction_dir(agent_id: uuid.UUID | None) -> "Path | None":
    """Resolve the workspace directory for storing evicted tool results."""
    if agent_id is None:
        return None
    from pathlib import Path
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace" / "tool_results"


def _resolve_kernel_for_request(request: AgentInvocationRequest) -> AgentKernel:
    factory = get_agent_kernel
    if inspect.signature(factory).parameters:
        return factory(request)
    return factory()


async def _resolve_quota_user_id(request: AgentInvocationRequest) -> uuid.UUID | None:
    if request.user_id is not None:
        return request.user_id
    if request.agent_id is None:
        return None

    async with async_session() as db, enter_rls_bypass(
        db,
        reason=f"quota user resolution for agent {request.agent_id}",
    ):
        result = await db.execute(select(Agent).where(Agent.id == request.agent_id))
        agent = result.scalar_one_or_none()
        if agent is None:
            return None
        return agent.owner_user_id or agent.creator_id


async def _enforce_invocation_quota(request: AgentInvocationRequest) -> AgentInvocationResult | None:
    try:
        quota_user_id = await _resolve_quota_user_id(request)
        if quota_user_id is None:
            return None
        await check_user_token_quota(quota_user_id)
        return None
    except QuotaExceeded as exc:
        event = {
            "type": "quota",
            "status": "denied",
            "quota_type": exc.quota_type,
            "message": exc.message,
        }
        if request.on_event:
            await _maybe_await(request.on_event(event))
        logger.warning("[Invoker] Token quota denied: %s", exc.message)
        return AgentInvocationResult(content=exc.message, tokens_used=0)
    except Exception as exc:  # noqa: BLE001 — quota check is a hard admission gate
        logger.exception("[Invoker] Token quota check failed — blocking invocation")
        return AgentInvocationResult(
            content=f"Unable to verify token quota; request blocked ({type(exc).__name__}).",
            tokens_used=0,
        )


async def _resolve_agent_smart_model_routing(agent_id: uuid.UUID | None) -> dict[str, Any] | None:
    if not agent_id:
        return None

    try:
        async with async_session() as db, enter_rls_bypass(db, reason=f"smart-routing bootstrap for agent {agent_id}"):
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if agent and isinstance(getattr(agent, "smart_model_routing", None), dict):
                return agent.smart_model_routing
    except Exception as exc:
        logger.debug("Failed to resolve smart model routing for agent %s: %s", agent_id, exc)
    return None


def _resolve_effective_turn_route(
    request: AgentInvocationRequest,
    *,
    routing_config: dict[str, Any] | None,
) -> dict[str, Any]:
    route = resolve_turn_model_route(
        primary_model=request.model,
        fallback_model=request.fallback_model,
        query=_last_user_query(request.messages),
        messages=request.messages,
        invocation_scope=request.invocation_scope,
        session_source=request.session_context.source if request.session_context else None,
        supports_vision=request.supports_vision,
        routing_config=routing_config,
    )
    route_metadata = {
        "selected_model": getattr(route.model, "model", None),
        "fallback_model": getattr(route.fallback_model, "model", None),
        "reason": route.reason,
        "task_profile": route.task_profile.name,
        "complexity": route.task_profile.complexity,
        "config_source": route.config_source,
    }
    if request.session_context is not None:
        session_metadata = _session_metadata(request.session_context)
        session_metadata["turn_route"] = route_metadata
        # Work Ledger 切口②/T-G1: decide on the general path whether the
        # cognitive scaffold may participate this turn (complex → scheduler
        # eligibility + compaction reboot; simple Q&A → zero overhead). The
        # kernel reads this flag; actual reminder frequency lives in the
        # scheduler and is inferred from behavior.
        session_metadata["work_ledger_enabled"] = should_enable_work_ledger(
            task_profile_name=route.task_profile.name,
            complexity=route.task_profile.complexity,
            is_simple_turn_candidate=_is_simple_turn_candidate(_last_user_query(request.messages), route.task_profile),
        )
    return {
        "model": route.model,
        "fallback_model": route.fallback_model,
        "supports_vision": route.supports_vision,
        "metadata": route_metadata,
    }


async def invoke_agent(request: AgentInvocationRequest) -> AgentInvocationResult:
    _normalize_invocation_session_context(request)
    quota_result = await _enforce_invocation_quota(request)
    if quota_result is not None:
        return quota_result

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
        on_tool_call=request.on_tool_call,
        on_thinking=request.on_thinking,
        on_event=request.on_event,
        supports_vision=effective_supports_vision,
        memory_context=request.memory_context,
        memory_session_id=request.memory_session_id,
        memory_messages=request.memory_messages,
        session_context=request.session_context,
        system_prompt_suffix=request.system_prompt_suffix,
        standalone_system_prompt=request.standalone_system_prompt,
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

    # ── SESSION_START hook ──
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        await emit_hook(
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
            },
        )
    except Exception as _start_err:
        logging.getLogger(__name__).debug("[Invoker] SESSION_START hook failed (non-fatal): %s", _start_err)

    result = await _resolve_kernel_for_request(request).handle(kernel_request)
    completed_messages = [*request.messages, {"role": "assistant", "content": result.content}]
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _session_metadata(request.session_context)
        _hook_metadata = {
            "agent_name": request.agent_name,
            "tenant_id": session_metadata.get("tenant_id"),
            "turn_count": len(completed_messages),
            "reason": "invoke_return",
            "important_files": list(getattr(request.session_context, "recent_files", []) or [])
            if request.session_context
            else [],
            "pending_work": list(getattr(request.session_context, "pending_items", []) or [])
            if request.session_context
            else [],
            "last_successful_step": result.content[:300],
        }
        await emit_hook(
            HookEvent.SESSION_CLOSE,
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
    )
