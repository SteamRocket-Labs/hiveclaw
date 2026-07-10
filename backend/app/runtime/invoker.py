"""Unified agent runtime invoker.

This module centralizes the LLM/tool loop so websocket chat, task execution,
heartbeat, scheduler, and agent-to-agent flows can share the same runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import async_session, tenant_scoped_session
from app.kernel import (
    AgentKernel,
    ExecutionIdentityRef,
    InvocationRequest,
    KernelDependencies,
    RuntimeConfig,
    ToolExpansionResult,
)
from app.kernel.contracts import MidRunMessageDrain, TerminalReason
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
from app.runtime.ccplus_contracts import PermissionProfileV1, build_permission_profile
from app.runtime.prompt_builder import build_frozen_prompt_prefix
from app.runtime.session import SessionContext
from app.runtime.session_key import build_session_key, ensure_session_key
from app.services.agent_context import (
    build_agent_context,
    build_agent_runtime_context,
    build_skill_catalog_section_for_agent,
)
from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason
from app.services.agent_work_ledger import should_enable_work_ledger
from app.services.agent_tools import (
    CORE_TOOL_NAMES,
    discoverable_tool_names_for_query,
    execute_tool,
    get_agent_tools_for_llm,
    get_combined_openai_tools,
)
from app.services.feature_flags import is_enabled as is_feature_enabled
from app.services.llm_utils import LLMMessage, create_llm_client, get_max_tokens
from app.services.runtime_budget_llm import RuntimeBudgetedLLMClient
from app.services.invocation_trace import persist_invocation_span
from app.services.governance_capability_taxonomy import capability_descriptor_for_name, iter_runtime_l2_capabilities
from app.services.memory_service import (
    build_memory_context,
    maybe_compress_messages,
    persist_runtime_memory,
)
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_user
from app.services.quota_guard import QuotaExceeded, check_user_token_quota
from app.services.skill_runtime_telemetry import record_skill_runtime_usage_for_invocation
from app.services.token_tracker import (
    estimate_tokens_from_chars,
    extract_usage_tokens,
    record_token_usage,
)
from app.tools.result_envelope import ToolContentEnvelope

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
_DEFAULT_TURN_TOKENS_PER_ROUND = 8192
_MAX_DERIVED_TURN_TOKEN_BUDGET = 1_000_000


def _context_engine() -> DefaultContextEngine:
    return DefaultContextEngine()


def _derive_turn_token_budget(max_tool_rounds: int | None) -> int:
    rounds = max(1, int(max_tool_rounds or 200))
    return min(
        _MAX_DERIVED_TURN_TOKEN_BUDGET, max(_DEFAULT_TURN_TOKENS_PER_ROUND, rounds * _DEFAULT_TURN_TOKENS_PER_ROUND)
    )


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
    # Specialized reasoning passes set this so text synthesis cannot route through
    # write_file and blow the round budget.
    disable_tools: bool = False
    # Task1: per-call output-token ceiling override. Specialized synthesis can
    # set this so long output is not truncated at the model's chat default; the
    # kernel feeds it into get_max_tokens (still clamped to the hard limit).
    max_output_tokens: int | None = None
    # Durable chat runtimes append their terminal assistant transcript after the
    # kernel returns; they emit TURN_STOP themselves once that write has committed.
    emit_turn_stop: bool = True


@dataclass(slots=True)
class AgentInvocationResult:
    content: str
    tokens_used: int = 0
    final_tools: list[dict] | None = None
    parts: list[dict] | None = None
    reasoning_signature: str | None = None
    terminal_reason: TerminalReason = TerminalReason.TURN_STOP


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


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _prefixed_turn_id(prefix: str, value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith(f"{prefix}-"):
        return raw
    if not raw:
        raw = uuid.uuid4().hex
    return f"{prefix}-{raw}"


def _ensure_turn_metadata(request: AgentInvocationRequest) -> dict[str, Any]:
    metadata = _session_metadata(request.session_context)
    seed = (
        metadata.get("turn_id")
        or metadata.get("runtime_task_id")
        or metadata.get("request_id")
        or request.memory_session_id
        or uuid.uuid4().hex
    )
    turn_id = _prefixed_turn_id("turn", metadata.get("turn_id") or seed)
    intent_seed = metadata.get("intent_id") or metadata.get("request_id") or turn_id.removeprefix("turn-")
    intent_id = _prefixed_turn_id("intent", intent_seed)
    metadata["turn_id"] = turn_id
    metadata["intent_id"] = intent_id
    if request.session_context is not None:
        request.session_context.metadata = metadata
    return metadata


def _latest_user_prompt(messages: list[dict] | tuple[dict, ...] | None) -> str:
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return str(content or "")
    return ""


_SKILL_CATALOG_SCENARIO_METADATA_KEYS = (
    "task_profile",
    "current_task",
    "task_context",
    "goal",
    "objective",
)


def _stringify_skill_catalog_scenario_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _skill_catalog_ranking_inputs(request: AgentInvocationRequest) -> dict[str, Any]:
    metadata = request.session_context.metadata if request.session_context is not None else {}
    scenario_parts = [_latest_user_prompt(request.messages)]
    for key in _SKILL_CATALOG_SCENARIO_METADATA_KEYS:
        text = _stringify_skill_catalog_scenario_value(metadata.get(key))
        if text:
            scenario_parts.append(text)
    plan_mode = metadata.get("plan_mode")
    if isinstance(plan_mode, dict):
        for key in ("original_request", "intent_type", "action_kind", "tool_name"):
            text = _stringify_skill_catalog_scenario_value(plan_mode.get(key))
            if text:
                scenario_parts.append(text)
    path_triggered_skill_names: list[str] = []
    for item in metadata.get("conditional_skill_activations") or []:
        if not isinstance(item, dict):
            continue
        skill_name = str(item.get("skill_name") or "").strip()
        if skill_name and skill_name not in path_triggered_skill_names:
            path_triggered_skill_names.append(skill_name)
    return {
        "scenario_text": "\n".join(part for part in scenario_parts if part),
        "active_skill_names": tuple(request.session_context.active_skills if request.session_context else ()),
        "path_triggered_skill_names": tuple(path_triggered_skill_names),
    }


def _format_hook_additional_contexts(contexts: list[str]) -> str:
    cleaned = [str(item).strip() for item in contexts if str(item).strip()]
    if not cleaned:
        return ""
    body = "\n\n".join(cleaned)
    return f"## Hook Additional Context\n{body}"


def _plan_mode_interactive_available(session_context: SessionContext | None) -> bool:
    # Compatibility-only: ToolRuntimeService ignores this for Plan Mode entry.
    from app.runtime.session import is_interactive_plan_eligible

    return is_interactive_plan_eligible(session_context)


def _plan_mode_unattended_available(session_context: SessionContext | None) -> bool:
    # Compatibility-only: ToolRuntimeService ignores this for Plan Mode entry.
    from app.runtime.session import is_unattended_plan_eligible

    return is_unattended_plan_eligible(session_context)


def _permission_profile_from_session_context(session_context: SessionContext | None) -> PermissionProfileV1 | None:
    """Resolve the optional per-turn PermissionProfileV1 from runtime metadata."""
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return None
    try:
        from app.services.skill_execution_adapter import apply_skill_execution_plans_to_metadata

        apply_skill_execution_plans_to_metadata(metadata)
    except Exception:
        logger.debug("[Invoker] skill execution plan metadata consumption failed", exc_info=True)
    raw = metadata.get("permission_profile")
    if raw is None:
        return None
    if isinstance(raw, PermissionProfileV1):
        return raw
    if isinstance(raw, dict):
        return build_permission_profile(raw)
    return None


def _tool_frame_kwargs_from_session_context(session_context: SessionContext | None) -> dict[str, Any]:
    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if not isinstance(metadata, dict):
        return {}
    t0_refs = metadata.get("t0_refs") or metadata.get("t0_event_refs") or ()
    if isinstance(t0_refs, str):
        t0_refs = (t0_refs,)
    round_state = dict(metadata.get("round_state") if isinstance(metadata.get("round_state"), dict) else {})
    turn_route = metadata.get("turn_route") if isinstance(metadata.get("turn_route"), dict) else {}
    execution_shape = str(turn_route.get("execution_shape") or metadata.get("execution_shape") or "").strip()
    if execution_shape and "execution_shape" not in round_state:
        round_state["execution_shape"] = execution_shape
    execution_contract = _execution_contract_from_session_context(session_context)
    if execution_contract and not isinstance(round_state.get("execution_contract"), dict):
        round_state["execution_contract"] = execution_contract
    result: dict[str, Any] = {
        "round_state": round_state,
        "t0_refs": tuple(str(ref) for ref in (t0_refs or ()) if str(ref).strip()),
    }
    if metadata.get("turn_id"):
        result["turn_id"] = str(metadata["turn_id"])
    if metadata.get("runtime_task_id") or metadata.get("task_id"):
        result["runtime_task_id"] = str(metadata.get("runtime_task_id") or metadata.get("task_id"))
    if metadata.get("budget_run_id"):
        result["budget_run_id"] = str(metadata["budget_run_id"])
    origin_channel = (
        metadata.get("origin_channel")
        or getattr(session_context, "channel", None)
        or getattr(session_context, "source", None)
    )
    if origin_channel:
        result["origin_channel"] = str(origin_channel)
    return result


def _execution_contract_from_session_context(session_context: SessionContext | None) -> dict[str, Any] | None:
    if session_context is None:
        return None
    plan_state = getattr(session_context, "plan_mode", None)
    state_contract = getattr(plan_state, "execution_contract", None)
    if isinstance(state_contract, dict) and state_contract:
        return dict(state_contract)
    metadata = getattr(session_context, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    mirror = metadata.get("plan_mode")
    if isinstance(mirror, dict) and isinstance(mirror.get("execution_contract"), dict) and mirror["execution_contract"]:
        return dict(mirror["execution_contract"])
    if isinstance(metadata.get("execution_contract"), dict) and metadata["execution_contract"]:
        return dict(metadata["execution_contract"])
    return None


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
        tenant_id = await resolve_tenant_for_agent(
            agent_id,
            session_factory=async_session,
        )
        if tenant_id is None:
            logger.warning("[Invoker] Agent %s not found in DB — fail-closed", agent_id)
            return RuntimeConfig(
                tenant_id=None,
                max_tool_rounds=200,
                tenant_resolution_error=f"Agent {agent_id} not found",
            )
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="agent_runtime_config_bootstrap",
        ) as db:
            result = await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning("[Invoker] Agent %s not found in DB — fail-closed", agent_id)
                return RuntimeConfig(
                    tenant_id=None,
                    max_tool_rounds=200,
                    tenant_resolution_error=f"Agent {agent_id} not found",
                )
            lifecycle_reason = get_agent_lifecycle_block_reason(agent)
            if lifecycle_reason:
                logger.warning("[Invoker] Agent %s is not active (%s) — fail-closed", agent_id, lifecycle_reason)
                return RuntimeConfig(
                    tenant_id=agent.tenant_id,
                    max_tool_rounds=agent.max_tool_rounds or 200,
                    tenant_resolution_error=f"Agent {agent_id} is not active: {lifecycle_reason}",
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

            runtime_continuity_enabled = await _resolve_flag("runtime_continuity_v1")
            skill_candidate_loop_enabled = await _resolve_flag("skill_candidate_loop_v1")
            return RuntimeConfig(
                tenant_id=agent.tenant_id,
                max_tool_rounds=agent.max_tool_rounds or 200,
                quota_message=quota_message,
                turn_token_budget=_derive_turn_token_budget(agent.max_tool_rounds or 200),
                execution_mode=getattr(agent, "execution_mode", None),
                runtime_continuity_enabled=runtime_continuity_enabled,
                skill_candidate_loop_enabled=skill_candidate_loop_enabled,
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
        tenant_id = await resolve_tenant_for_user(
            user_id,
            session_factory=async_session,
        )
        if tenant_id is None:
            return None
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="current_user_display_name_bootstrap",
        ) as db:
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
            cleaned += (
                f"\n[The user sent {image_count} image(s), but the current model does not support vision, "
                "so the image content is unavailable.]"
            )
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
        include_skill_catalog=False,  # Step 9: catalog flows via dynamic suffix, not the frozen prefix
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


async def fetch_relevant_knowledge(
    query: str,
    tenant_id: uuid.UUID | None = None,
    *,
    agent_id: uuid.UUID | str | None = None,
    current_user_id: uuid.UUID | str | None = None,
    source_collector: list[dict[str, Any]] | None = None,
    max_tokens: int = 500,
    max_chars: int | None = None,
    limit: int = 3,
) -> str:
    """Prompt-context seam over TruthSearchService.

    Kept local to the invoker so older tests can patch retrieval without
    reintroducing the retired OpenViking prompt-injection module.
    """
    from app.services.truth_search_service import TruthSearchService

    service = TruthSearchService()
    char_budget = max_chars if max_chars and max_chars > 0 else max_tokens * 3
    evidence = await service.search(
        query,
        tenant_id=tenant_id,
        agent_id=agent_id,
        current_user_id=current_user_id,
        limit=limit,
        source_collector=source_collector,
        max_chars=char_budget,
    )
    return service.render_prompt_context(evidence, max_chars=char_budget)


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
    # the subagent memory.md is appended to the standalone prompt by the spawn layer.)
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


async def _resolve_runtime_metadata_context(
    request: AgentInvocationRequest,
    tenant_id: uuid.UUID | None,
) -> str:
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
    if "tenant_id" in _runtime_sig:
        _runtime_kwargs["tenant_id"] = tenant_id
    if "session_id" in _runtime_sig and request.session_context is not None:
        _runtime_kwargs["session_id"] = request.session_context.session_id
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
    connector_source_items: list[dict[str, Any]] = []

    _knowledge_kwargs = {}
    _knowledge_sig = inspect.signature(fetch_relevant_knowledge).parameters
    if "max_tokens" in _knowledge_sig:
        _knowledge_kwargs["max_tokens"] = max(500, budget_profile.knowledge_budget_chars // 3)
    if "max_chars" in _knowledge_sig:
        _knowledge_kwargs["max_chars"] = budget_profile.knowledge_budget_chars
    if "limit" in _knowledge_sig:
        _knowledge_kwargs["limit"] = budget_profile.external_limit
    if "agent_id" in _knowledge_sig:
        _knowledge_kwargs["agent_id"] = request.agent_id
    if "current_user_id" in _knowledge_sig:
        _knowledge_kwargs["current_user_id"] = request.user_id
    if "source_collector" in _knowledge_sig:
        _knowledge_kwargs["source_collector"] = connector_source_items
    knowledge = await _maybe_await(fetch_relevant_knowledge(query, tenant_id, **_knowledge_kwargs))
    if connector_source_items and request.session_context is not None:
        from app.services.connector_acl import register_connector_source_items

        register_connector_source_items(
            request.session_context,
            connector_source_items,
            origin="knowledge_provider:relevant",
        )
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


def _serialize_capability_pack(descriptor) -> dict[str, Any]:
    return {
        "name": descriptor.name,
        "summary": descriptor.notes or descriptor.name,
        "source": descriptor.source,
        "activation_mode": "Enabled by default." if descriptor.default_enabled else "Disabled until enabled.",
        "tools": list(descriptor.tools),
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
        _serialize_capability_pack(descriptor)
        for descriptor in iter_runtime_l2_capabilities()
        if requested.intersection(descriptor.tools)
    ]
    existing_names = {pack["name"] for pack in packs}
    for pack_name in declared_pack_names or []:
        if pack_name in existing_names:
            continue
        descriptor = capability_descriptor_for_name(pack_name)
        if descriptor and descriptor.l2_visible:
            packs.append(_serialize_capability_pack(descriptor))
            existing_names.add(pack_name)
    if packs or not requested:
        return packs
    synthetic_name = f"discovery:{(skill_name or 'custom').strip().lower().replace(' ', '_')}"
    return [
        {
            "name": synthetic_name,
            "summary": f"Tools discovered by {skill_name or 'tool_search'}",
            "source": "discovery",
            "activation_mode": "Discovered through tool_search.",
            "tools": sorted(requested),
            "skill_name": skill_name,
        }
    ]


async def _deferred_tool_names_for_query(agent_id: uuid.UUID, query: str) -> list[str]:
    # Step 3: single source of truth — both this schema path and the text path
    # (workspace._tool_search) route through discoverable_tool_names_for_query so
    # "what the model is told it can discover" == "what actually loads".
    return await discoverable_tool_names_for_query(agent_id, query)


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
        from app.services.tool_search_manifest import build_tool_search_manifest

        tool_search_manifest = build_tool_search_manifest(
            query=query,
            loaded_tool_names=expanded_tool_names,
            skills=(),
            subagents=(),
        )
        manifests = request.session_context.metadata.setdefault("tool_search_manifests", [])
        if isinstance(manifests, list):
            manifests.append(tool_search_manifest)
        return ToolExpansionResult(
            tools=tools,
            active_tool_groups=packs,
            event_payload={
                "type": "deferred_tools_delta",
                "tool_groups": packs,
                "discovered_tools": discovered,
                "all_discovered_tools": list(request.session_context.discovered_tools),
                "tool_search_manifest": tool_search_manifest,
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
    *,
    tool_call_id: str | None = None,
    trace_metadata_sink: dict[str, Any] | None = None,
) -> str | ToolContentEnvelope:
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
        if accepts_kwargs or "permission_profile" in executor_params:
            executor_kwargs["permission_profile"] = _permission_profile_from_session_context(request.session_context)
        if accepts_kwargs or "tool_call_id" in executor_params:
            executor_kwargs["tool_call_id"] = tool_call_id
        if trace_metadata_sink is not None and (accepts_kwargs or "trace_metadata_sink" in executor_params):
            executor_kwargs["trace_metadata_sink"] = trace_metadata_sink
        if accepts_kwargs or "emit_runtime_hooks" in executor_params:
            executor_kwargs["emit_runtime_hooks"] = False
        frame_kwargs = _tool_frame_kwargs_from_session_context(request.session_context)
        for key, value in frame_kwargs.items():
            if accepts_kwargs or key in executor_params:
                executor_kwargs[key] = value
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
    if "permission_profile" in inspect.signature(execute_tool).parameters:
        execute_kwargs["permission_profile"] = _permission_profile_from_session_context(request.session_context)
    if "tool_call_id" in inspect.signature(execute_tool).parameters:
        execute_kwargs["tool_call_id"] = tool_call_id
    if trace_metadata_sink is not None and "trace_metadata_sink" in inspect.signature(execute_tool).parameters:
        execute_kwargs["trace_metadata_sink"] = trace_metadata_sink
    execute_params = inspect.signature(execute_tool).parameters
    for key, value in _tool_frame_kwargs_from_session_context(request.session_context).items():
        if key in execute_params:
            execute_kwargs[key] = value
    if "emit_runtime_hooks" in inspect.signature(execute_tool).parameters:
        execute_kwargs["emit_runtime_hooks"] = False
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

    async def _kernel_get_tools(agent_id: uuid.UUID, core_only: bool) -> list[dict]:
        # RC11: specialized reasoning passes can disable the tool surface entirely so
        # synthesis cannot route through write_file and exhaust the round budget.
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
        client = create_llm_client(
            provider=model.provider,
            api_key=model.api_key,
            model=model.model,
            base_url=model.base_url,
            timeout=120.0,
        )
        metadata = _session_metadata(request.session_context)
        budget_run_id = _uuid_or_none(metadata.get("budget_run_id"))
        if budget_run_id is None:
            return client
        return RuntimeBudgetedLLMClient(
            client,
            budget_run_id=budget_run_id,
            runtime_task_id=_uuid_or_none(metadata.get("runtime_task_id") or metadata.get("request_id")),
            summary_lane=bool(metadata.get("budget_summary_turn")),
        )

    async def _kernel_execute_tool(
        tool_name: str,
        args: dict,
        request: InvocationRequest,
        emit_event: Callable[[dict], Any],
        *,
        tool_call_id: str | None = None,
        trace_metadata_sink: dict[str, Any] | None = None,
    ) -> str | ToolContentEnvelope:
        return await _execute_tool_with_request(
            tool_name,
            args,
            request,
            emit_event,
            tool_call_id=tool_call_id,
            trace_metadata_sink=trace_metadata_sink,
        )  # type: ignore[arg-type]

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=_resolve_runtime_config,
            resolve_current_user_name=_resolve_current_user_name,
            build_system_prompt=_kernel_build_system_prompt,
            resolve_memory_context=_kernel_resolve_memory_context,
            resolve_runtime_metadata_context=_kernel_resolve_runtime_metadata_context,
            resolve_retrieval_context=_kernel_resolve_retrieval_context,
            get_tools=_kernel_get_tools,
            resolve_tool_expansion=_resolve_tool_expansion,
            maybe_compress_messages=maybe_compress_messages,
            create_client=_kernel_create_client,
            execute_tool=_kernel_execute_tool,
            persist_memory=persist_runtime_memory,
            record_token_usage=record_token_usage,
            record_invocation_span=persist_invocation_span,
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

    tenant_id = await resolve_tenant_for_agent(
        request.agent_id,
        session_factory=async_session,
    )
    if tenant_id is None:
        return None
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="quota_user_resolution",
    ) as db:
        result = await db.execute(select(Agent).where(Agent.id == request.agent_id))
        agent = result.scalar_one_or_none()
        if agent is None:
            return None
        return agent.owner_user_id or agent.creator_id


async def _enforce_invocation_quota(request: AgentInvocationRequest) -> AgentInvocationResult | None:
    try:
        quota_user_id = await _resolve_quota_user_id(request)
        if quota_user_id is None and request.agent_id is None:
            return None
        quota_check_params = inspect.signature(check_user_token_quota).parameters
        if "agent_id" in quota_check_params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in quota_check_params.values()
        ):
            await check_user_token_quota(quota_user_id, agent_id=request.agent_id)
        else:
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
        return AgentInvocationResult(content=exc.message, tokens_used=0, terminal_reason=TerminalReason.QUOTA_DENIED)
    except Exception as exc:  # noqa: BLE001 — quota check is a hard admission gate
        logger.exception("[Invoker] Token quota check failed — blocking invocation")
        return AgentInvocationResult(
            content=f"Unable to verify token quota; request blocked ({type(exc).__name__}).",
            tokens_used=0,
            terminal_reason=TerminalReason.PROVIDER_ERROR,
        )


async def _resolve_agent_smart_model_routing(agent_id: uuid.UUID | None) -> dict[str, Any] | None:
    if not agent_id:
        return None

    try:
        tenant_id = await resolve_tenant_for_agent(
            agent_id,
            session_factory=async_session,
        )
        if tenant_id is None:
            return None
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="smart_model_routing_bootstrap",
        ) as db:
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
        "execution_shape": route.task_profile.execution_shape,
        "config_source": route.config_source,
    }
    if request.session_context is not None:
        session_metadata = _session_metadata(request.session_context)
        session_metadata["turn_route"] = route_metadata
        # Work Ledger slice 2 / T-G1: decide on the general path whether the
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

    skill_runtime_tool_events: list[dict[str, Any]] = []

    async def _on_tool_call_with_skill_runtime_usage(data: dict) -> None:
        if isinstance(data, dict) and data.get("status") == "done":
            skill_runtime_tool_events.append(
                {
                    "name": data.get("name"),
                    "args": data.get("args"),
                    "status": data.get("status"),
                }
            )
        if request.on_tool_call is not None:
            await _maybe_await(request.on_tool_call(data))

    def _record_skill_runtime_usage(terminal_status: str, assistant_text: str) -> None:
        if request.agent_id is None or not skill_runtime_tool_events:
            return
        try:
            record_skill_runtime_usage_for_invocation(
                agent_id=request.agent_id,
                session_context=request.session_context,
                tool_events=skill_runtime_tool_events,
                terminal_status=terminal_status,
                assistant_text=assistant_text,
                note=assistant_text[:500],
            )
        except Exception as exc:  # noqa: BLE001 - telemetry cannot fail the user-facing invocation
            logger.warning(
                "[Invoker] skill runtime usage telemetry failed: agent_id=%s session_id=%s error=%s",
                request.agent_id,
                request.session_context.session_id if request.session_context else None,
                exc,
            )

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

    # Step 9 (CC parity): load the skill catalog once and thread it through the
    # dynamic suffix (InvocationRequest.skill_catalog → kernel → dynamic suffix),
    # NOT the frozen prefix. Standalone subagents carry no host catalog.
    skill_catalog_text = ""
    skill_catalog_ranking: list[dict[str, Any]] = []
    if request.agent_id is not None and not (request.standalone_system_prompt or "").strip():
        ranking_inputs = _skill_catalog_ranking_inputs(request)
        skill_catalog_text = build_skill_catalog_section_for_agent(
            request.agent_id,
            budget_profile=_resolve_context_budget(request),
            scenario_text=ranking_inputs["scenario_text"],
            session_id=(
                getattr(request.session_context, "session_id", None)
                if request.session_context is not None
                else request.memory_session_id
            ),
            active_skill_names=ranking_inputs["active_skill_names"],
            path_triggered_skill_names=ranking_inputs["path_triggered_skill_names"],
            ranking_manifest=skill_catalog_ranking,
        )
        if request.session_context is not None:
            from app.runtime.context import ensure_runtime_assembly_state

            ensure_runtime_assembly_state(request.session_context).record_skill_catalog_ranking(
                ranking=skill_catalog_ranking,
                inputs={
                    "scenario_text_present": bool(ranking_inputs["scenario_text"]),
                    "active_skill_names": list(ranking_inputs["active_skill_names"]),
                    "path_triggered_skill_names": list(ranking_inputs["path_triggered_skill_names"]),
                },
            )

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
        on_tool_call=_on_tool_call_with_skill_runtime_usage,
        on_thinking=request.on_thinking,
        on_event=request.on_event,
        supports_vision=effective_supports_vision,
        memory_context=request.memory_context,
        memory_session_id=request.memory_session_id,
        memory_messages=request.memory_messages,
        session_context=request.session_context,
        system_prompt_suffix=request.system_prompt_suffix,
        standalone_system_prompt=request.standalone_system_prompt,
        skill_catalog=skill_catalog_text,
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

    # ── USER_PROMPT_SUBMIT hook ──
    # Entry points are responsible for durable DB/T0 append before invoking the
    # runtime. This hook is the shared post-append, pre-model lifecycle boundary.
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
        prompt_text = _latest_user_prompt(request.messages)
        prompt_result = await emit_hook(
            HookEvent.USER_PROMPT_SUBMIT,
            agent_id=request.agent_id,
            session_id=request.memory_session_id
            or (request.session_context.session_id if request.session_context else None),
            prompt=prompt_text,
            source=_session_source,
            metadata={
                "tenant_id": session_metadata.get("tenant_id"),
                "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
                "request_id": session_metadata.get("request_id"),
                "trace_id": session_metadata.get("trace_id"),
                "turn_id": session_metadata.get("turn_id"),
                "intent_id": session_metadata.get("intent_id"),
                "agent_name": request.agent_name,
                "execution_mode": request.invocation_scope,
            },
        )
        if prompt_result and prompt_result.block:
            return AgentInvocationResult(
                content=f"Blocked by prompt hook: {prompt_result.reason or 'policy'}",
                tokens_used=0,
                final_tools=[],
                parts=[],
                terminal_reason=TerminalReason.HOOK_STOPPED,
            )
        if prompt_result and prompt_result.additional_contexts:
            hook_context = _format_hook_additional_contexts(prompt_result.additional_contexts)
            if hook_context:
                kernel_request.system_prompt_suffix = "\n\n".join(
                    part for part in (kernel_request.system_prompt_suffix, hook_context) if part
                )
    except Exception as _prompt_err:
        logging.getLogger(__name__).debug("[Invoker] USER_PROMPT_SUBMIT hook failed (non-fatal): %s", _prompt_err)

    # ── SESSION_START hook ──
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
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
                "tenant_id": session_metadata.get("tenant_id"),
                "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
                "request_id": session_metadata.get("request_id"),
                "trace_id": session_metadata.get("trace_id"),
                "turn_id": session_metadata.get("turn_id"),
                "intent_id": session_metadata.get("intent_id"),
            },
        )
    except Exception as _start_err:
        logging.getLogger(__name__).debug("[Invoker] SESSION_START hook failed (non-fatal): %s", _start_err)

    try:
        result = await _resolve_kernel_for_request(request).handle(kernel_request)
    except Exception as exc:
        _record_skill_runtime_usage("failed", f"{type(exc).__name__}: {exc}")
        raise

    _record_skill_runtime_usage("completed", str(result.content or ""))
    completed_messages = [*request.messages, {"role": "assistant", "content": result.content}]
    try:
        from app.runtime.hooks import HookEvent, emit_hook

        _session_source = request.session_context.source if request.session_context else "runtime"
        session_metadata = _ensure_turn_metadata(request)
        _hook_metadata = {
            "agent_name": request.agent_name,
            "tenant_id": session_metadata.get("tenant_id"),
            "runtime_task_id": session_metadata.get("runtime_task_id") or session_metadata.get("task_id"),
            "request_id": session_metadata.get("request_id"),
            "trace_id": session_metadata.get("trace_id"),
            "turn_id": session_metadata.get("turn_id"),
            "intent_id": session_metadata.get("intent_id"),
            "turn_count": len(completed_messages),
            "reason": "invoke_return",
            "checkpoint_kind": "user_turn_stop",
            "important_files": list(getattr(request.session_context, "recent_files", []) or [])
            if request.session_context
            else [],
            "pending_work": list(getattr(request.session_context, "pending_items", []) or [])
            if request.session_context
            else [],
            "last_successful_step": result.content[:300],
            "activation_events": list((request.session_context.metadata or {}).get("activation_events") or [])
            if request.session_context
            else [],
        }
        await emit_hook(
            HookEvent.SESSION_END,
            agent_id=request.agent_id,
            session_id=request.memory_session_id,
            source=_session_source,
            messages=completed_messages,
            metadata=_hook_metadata,
        )
        if request.emit_turn_stop:
            await emit_hook(
                HookEvent.TURN_STOP,
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
        reasoning_signature=getattr(result, "reasoning_signature", None),
        terminal_reason=getattr(result, "terminal_reason", TerminalReason.TURN_STOP),
    )
