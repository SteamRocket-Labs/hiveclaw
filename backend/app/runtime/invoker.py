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
from app.database import async_session
from app.kernel import (
    AgentKernel,
    ExecutionIdentityRef,
    InvocationRequest,
    KernelDependencies,
    RuntimeConfig,
    ToolExpansionResult,
)
from app.models.agent import Agent
from app.models.feature_flag import FeatureFlag
from app.models.user import User
from app.runtime.context_budget import ContextBudget, compute_context_budget, resolve_turn_model_route
from app.runtime.context_engine import DefaultContextEngine
from app.runtime.prompt_builder import build_frozen_prompt_prefix
from app.runtime.session import SessionContext
from app.runtime.session_key import build_session_key, ensure_session_key
from app.skills import SkillParser, SkillRegistry, WorkspaceSkillLoader
from app.services.agent_context import build_agent_context, build_agent_runtime_context
from app.services.agent_tools import CORE_TOOL_NAMES, execute_tool, get_agent_tools_for_llm, get_combined_openai_tools
from app.services.feature_flags import is_enabled as is_feature_enabled
from app.services.knowledge_inject import fetch_relevant_knowledge
from app.services.llm_utils import LLMMessage, create_llm_client, get_max_tokens
from app.services.memory_service import (
    build_memory_snapshot,
    build_memory_context,
    maybe_compress_messages,
    persist_runtime_memory,
)
from app.services.token_tracker import (
    estimate_tokens_from_chars,
    extract_usage_tokens,
    record_token_usage,
)
from app.tools import ensure_workspace
from app.tools.packs import TOOL_PACKS, pack_for_name

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
        objective_id=metadata.get("objective_id") or metadata.get("focus_ref"),
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
    tool_executor: ToolExecutor | None = None
    cancel_event: asyncio.Event | None = None
    initial_tools: list[dict] | None = None
    core_tools_only: bool = True
    allowed_tool_names: tuple[str, ...] = ()
    excluded_tool_names: tuple[str, ...] = ()
    expand_tools: bool = True
    max_tool_rounds: int | None = None
    execution_mode: str | None = None
    smart_model_routing: dict[str, Any] | None = None
    delegation_token: Any | None = None


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
        async with async_session() as db:
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
        async with async_session() as db:
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
    api_messages: list[LLMMessage], provider: str, execution_mode: str = "conversation"
) -> list[LLMMessage]:
    """Apply provider-specific prompt cache hints (Anthropic, OpenAI, DeepSeek, Gemini, etc.)."""
    from app.services.prompt_cache import apply_cache_hints

    return apply_cache_hints(api_messages, provider, execution_mode=execution_mode)


async def _build_system_prompt(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
    resolved_memory_context: str,
    current_user_name: str | None = None,
) -> str:
    if current_user_name is None:
        current_user_name = await _resolve_current_user_name(request.user_id)
    del tenant_id  # reserved for future prompt builders
    budget_profile = _resolve_context_budget(request)
    agent_context = await build_agent_context(
        agent_id=request.agent_id,
        agent_name=request.agent_name,
        role_description=request.role_description,
        current_user_name=current_user_name,
        include_memory_file=False,
        include_runtime_metadata=False,
        include_focus=False,
        budget_profile=budget_profile,
        execution_mode=request.execution_mode or "conversation",
    )
    return build_frozen_prompt_prefix(
        agent_context=agent_context,
    )


def _last_user_query(messages: list[dict]) -> str:
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _resolve_context_budget(request: AgentInvocationRequest) -> ContextBudget:
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None
    active_pack_count = len(request.session_context.active_packs) if request.session_context else 0
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
    # ALWAYS load memory — even when prompt_prefix is cached.
    # The engine injects memory as a dynamic suffix outside the frozen prefix,
    # so fresh memory can vary without invalidating the stable system prompt cache.
    parts: list[str] = []
    session_id = request.memory_session_id
    if not session_id and request.session_context:
        session_id = request.session_context.session_id
    budget_profile = _resolve_context_budget(request)
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None

    if request.agent_id and tenant_id:
        _snapshot_kwargs = {"session_id": session_id}
        _snapshot_sig = inspect.signature(build_memory_snapshot).parameters
        if "context_window_tokens" in _snapshot_sig:
            _snapshot_kwargs["context_window_tokens"] = context_window_tokens
        if "budget_profile" in _snapshot_sig:
            _snapshot_kwargs["budget_profile"] = budget_profile
        runtime_memory_context = await build_memory_snapshot(request.agent_id, tenant_id, **_snapshot_kwargs)
        if runtime_memory_context:
            parts.append(
                _context_engine().inject(
                    request.session_context,
                    kind="memory_snapshot",
                    source="memory_provider:snapshot",
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

    return "\n\n".join(parts)


async def _resolve_retrieval_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
    query = _last_user_query(request.messages)
    if not query:
        return ""

    parts: list[str] = []
    budget_profile = _resolve_context_budget(request)
    context_window_tokens = getattr(request.model, "max_input_tokens", None) if request.model else None
    session_id = request.memory_session_id
    if not session_id and request.session_context:
        session_id = request.session_context.session_id

    if request.agent_id and tenant_id:
        current_user_name = await _resolve_current_user_name(request.user_id)
        _runtime_kwargs = {"current_user_name": current_user_name}
        _runtime_sig = inspect.signature(build_agent_runtime_context).parameters
        if "budget_profile" in _runtime_sig:
            _runtime_kwargs["budget_profile"] = budget_profile
        runtime_context = await build_agent_runtime_context(request.agent_id, **_runtime_kwargs)
        if runtime_context:
            parts.append(
                _context_engine().inject(
                    request.session_context,
                    kind="agent_runtime_context",
                    source="runtime_context:agent",
                    content=runtime_context,
                )
            )

        _memory_kwargs = {
            "session_id": session_id,
            "query": query,
        }
        _memory_sig = inspect.signature(build_memory_context).parameters
        if "context_window_tokens" in _memory_sig:
            _memory_kwargs["context_window_tokens"] = context_window_tokens
        if "budget_profile" in _memory_sig:
            _memory_kwargs["budget_profile"] = budget_profile
        memory_recall = await build_memory_context(request.agent_id, tenant_id, **_memory_kwargs)
        if memory_recall:
            parts.append(
                _context_engine().inject(
                    request.session_context,
                    kind="memory_recall",
                    source="memory_provider:recall",
                    content=memory_recall,
                )
            )

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


def _build_skill_registry_for_workspace(workspace: Any) -> SkillRegistry:
    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(workspace))
    return registry


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


def _infer_active_packs(
    tool_names: list[str],
    *,
    skill_name: str | None = None,
    declared_pack_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    requested = set(tool_names)
    packs = [
        _serialize_pack(pack)
        for pack in TOOL_PACKS
        if pack.infer_from_tools and requested.intersection(pack.tools)
    ]
    existing_names = {pack["name"] for pack in packs}
    for pack_name in declared_pack_names or []:
        if pack_name in existing_names:
            continue
        pack = pack_for_name(pack_name)
        if pack:
            packs.append(_serialize_pack(pack))
            existing_names.add(pack_name)
    if packs or not requested:
        return packs
    synthetic_name = f"skill:{(skill_name or 'custom').strip().lower().replace(' ', '_')}"
    return [
        {
            "name": synthetic_name,
            "summary": f"Tools activated by skill {skill_name or 'custom skill'}",
            "source": "skill",
            "activation_mode": "通过 load_skill 激活",
            "tools": sorted(requested),
            "skill_name": skill_name,
        }
    ]


def _declared_skill_tool_names(
    *,
    declared_tools: tuple[str, ...] | list[str] | None = None,
    declared_packs: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()

    for tool_name in declared_tools or ():
        if tool_name and tool_name not in seen:
            requested.append(tool_name)
            seen.add(tool_name)

    for pack_name in declared_packs or ():
        pack = pack_for_name(pack_name)
        if not pack:
            continue
        for tool_name in pack.tools:
            if tool_name not in seen:
                requested.append(tool_name)
                seen.add(tool_name)

    return requested


async def _resolve_tool_expansion(
    request: AgentInvocationRequest,
    tool_name: str,
    args: dict[str, Any],
) -> ToolExpansionResult | list[dict] | None:
    if not request.agent_id:
        return None

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
        packs = _infer_active_packs(expanded_tool_names)
        return ToolExpansionResult(
            tools=tools,
            active_packs=packs,
            event_payload={
                "type": "pack_activation",
                "packs": packs,
                "message": "Activated MCP capability pack.",
                "status": "info",
                "trigger_tool": tool_name,
            },
        )

    try:
        workspace = await ensure_workspace(request.agent_id)
        registry = _build_skill_registry_for_workspace(workspace)
    except Exception as exc:
        # Skill expansion is opportunistic — if workspace/registry can't be built
        # (e.g. agent has no workspace yet, FS error), fall back to no expansion
        # rather than failing the whole tool call. Log so this is observable.
        logger.debug(
            "[Invoker] Skill expansion skipped — workspace/registry unavailable for agent %s: %s",
            request.agent_id,
            exc,
        )
        return None

    if tool_name == "load_skill":
        requested = str(args.get("name", "") or "").strip()
        if not requested:
            return None
        try:
            skill = registry.resolve(requested)
        except KeyError as _ke:
            logger.debug("[Invoker] Skill not found in registry: %s", _ke)
            return None
        requested_tool_names = _declared_skill_tool_names(
            declared_tools=skill.metadata.declared_tools,
            declared_packs=skill.metadata.declared_packs,
        )
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
        packs = _infer_active_packs(
            expanded_tool_names,
            skill_name=skill.metadata.name,
            declared_pack_names=list(skill.metadata.declared_packs),
        )
        return ToolExpansionResult(
            tools=tools,
            active_packs=packs,
            event_payload={
                "type": "pack_activation",
                "packs": packs,
                "message": f"Activated capability packs after loading skill: {skill.metadata.name}",
                "status": "info",
                "skill_name": skill.metadata.name,
                "trigger_tool": tool_name,
            },
        )

    if tool_name in {"read_file", "fs_read"}:
        if tool_name == "fs_read":
            mode = str(args.get("mode") or "text").strip().lower()
            if mode != "text":
                return None
        skill_path_arg = str(args.get("path", "") or "").strip()
        if "SKILL.md" not in skill_path_arg:
            return None
        skill_path = (workspace / skill_path_arg).resolve()
        skills_root = (workspace / "skills").resolve()
        if not skill_path.is_file() or not str(skill_path).startswith(str(skills_root)):
            return None
        parsed = SkillParser().parse_file(
            skill_path,
            relative_path=skill_path.relative_to(workspace).as_posix(),
            default_name=skill_path.parent.name if skill_path.name.lower() == "skill.md" else skill_path.stem,
        )
        requested_tool_names = _declared_skill_tool_names(
            declared_tools=parsed.metadata.declared_tools,
            declared_packs=parsed.metadata.declared_packs,
        )
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
        packs = _infer_active_packs(
            expanded_tool_names,
            skill_name=parsed.metadata.name,
            declared_pack_names=list(parsed.metadata.declared_packs),
        )
        return ToolExpansionResult(
            tools=tools,
            active_packs=packs,
            event_payload={
                "type": "pack_activation",
                "packs": packs,
                "message": f"Activated capability packs from skill file: {parsed.metadata.name}",
                "status": "info",
                "skill_name": parsed.metadata.name,
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
        return await _maybe_await(request.tool_executor(tool_name, args, **executor_kwargs))

    execute_kwargs: dict[str, Any] = {
        "agent_id": request.agent_id,
        "user_id": request.user_id or request.agent_id,
    }
    if "event_callback" in inspect.signature(execute_tool).parameters:
        execute_kwargs["event_callback"] = emit_event
    if "delegation_token" in inspect.signature(execute_tool).parameters:
        execute_kwargs["delegation_token"] = request.delegation_token
    return await execute_tool(
        tool_name,
        args,
        **execute_kwargs,
    )


def get_agent_kernel(request: AgentInvocationRequest | None = None) -> AgentKernel:
    allowed_tool_names = frozenset(request.allowed_tool_names if request else ())
    excluded_tool_names = frozenset(request.excluded_tool_names if request else ())

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

    async def _kernel_resolve_retrieval_context(
        request: InvocationRequest,
        tenant_id: uuid.UUID | None,
    ) -> str:
        return await _resolve_retrieval_context(request, tenant_id)  # type: ignore[arg-type]

    async def _kernel_get_tools(agent_id: uuid.UUID, core_only: bool) -> list[dict]:
        # Auto-include channel-specific tools so agents don't need to
        # manually load_skill before their channel tools become available.
        _channel_tools: list[str] | None = None
        if core_only and request.session_context:
            _source = request.session_context.source
            if _source == "feishu":
                from app.tools.packs import pack_for_name

                _pack = pack_for_name("feishu_pack")
                if _pack:
                    _channel_tools = list(_pack.tools)
        tools = await _maybe_await(
            get_agent_tools_for_llm(
                agent_id,
                core_only=core_only,
                requested_names=_channel_tools,
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
            resolve_retrieval_context=_kernel_resolve_retrieval_context,
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


async def _resolve_agent_smart_model_routing(agent_id: uuid.UUID | None) -> dict[str, Any] | None:
    if not agent_id:
        return None

    try:
        async with async_session() as db:
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
        execution_mode=request.execution_mode,
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
    return {
        "model": route.model,
        "fallback_model": route.fallback_model,
        "supports_vision": route.supports_vision,
        "metadata": route_metadata,
    }


async def invoke_agent(request: AgentInvocationRequest) -> AgentInvocationResult:
    _normalize_invocation_session_context(request)

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
        tool_executor=request.tool_executor,
        cancel_event=request.cancel_event,
        initial_tools=request.initial_tools or (get_combined_openai_tools() if request.agent_id is None else None),
        core_tools_only=request.core_tools_only,
        allowed_tool_names=request.allowed_tool_names,
        excluded_tool_names=request.excluded_tool_names,
        expand_tools=request.expand_tools,
        max_tool_rounds=request.max_tool_rounds,
        eviction_dir=_resolve_eviction_dir(request.agent_id),
        execution_mode=request.execution_mode,
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
                "execution_mode": request.execution_mode,
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
