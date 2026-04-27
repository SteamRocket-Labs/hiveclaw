"""Prompt assembly helpers for the unified runtime.

Three-layer prompt architecture:
  1. Frozen Prefix — stable within a session (identity, system, task rules, skill catalog)
  2. Dynamic Suffix — changes per round (active packs, retrieval, compaction hints)
  3. Per-turn Messages — normal conversation messages
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any, Awaitable, Callable

from app.runtime.context_budget import ContextBudget, compute_context_budget, compute_system_prompt_budget
from app.runtime.context import RuntimeContext
from app.services.agent_context import build_agent_context
from app.services.knowledge_inject import fetch_relevant_knowledge
from app.services.prompt_cache import PROMPT_CACHE_BOUNDARY  # noqa: F401


BuildAgentContextFn = Callable[[uuid.UUID | None, str, str, str | None], Awaitable[str]]
KnowledgeLookupFn = Callable[[str, uuid.UUID | None], Awaitable[str] | str]

# Re-export the cache boundary marker from the provider-agnostic prompt_cache
# module. The prompt assembler inserts it between frozen and dynamic sections;
# apply_cache_hints() splits on it per provider.

# Default fallbacks when no task-aware budget profile is provided.
_ACTIVE_PACKS_CHAR_BUDGET = 2000
_RETRIEVAL_CHAR_BUDGET = 3000
_CONTINUITY_CHAR_BUDGET = 2500


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _trim_block(text: str, *, budget_chars: int) -> str:
    if not text or budget_chars <= 0:
        return ""
    stripped = text.strip()
    if len(stripped) <= budget_chars:
        return stripped

    lines = stripped.splitlines()
    kept: list[str] = []
    used = 0
    for line in lines:
        normalized = line.rstrip()
        if not normalized:
            continue
        line_cost = len(normalized) + 1
        if used + line_cost > budget_chars:
            break
        kept.append(normalized)
        used += line_cost

    if not kept:
        return stripped[: max(budget_chars - 3, 0)].rstrip() + "..."

    result = "\n".join(kept).rstrip()
    if len(result) < len(stripped):
        result += "\n..."
    return result


# ── Frozen Prefix (session-stable) ──────────────────────────────


def build_frozen_prompt_prefix(
    *,
    agent_context: str,
    memory_snapshot: str = "",
    skill_catalog: str = "",
) -> str:
    """Build the session-stable prompt prefix.

    Contains: agent identity/soul/role, § System, § Doing Tasks, § Using Your Tools,
    and skill catalog.
    These do NOT change within a single session.
    """
    from app.runtime.prompt_sections import (
        build_output_efficiency_section,
        build_system_section,
        build_tasks_section,
        build_tools_section,
    )

    # NOTE: tone_style is already included by agent_context (via build_agent_context).
    # Do NOT add build_tone_style_section() here — it would double-inject.
    parts = [
        agent_context,
        build_system_section(),
        build_tasks_section(),
        build_tools_section(),
        build_output_efficiency_section(),
    ]
    del memory_snapshot  # kept for backward-compatible callers; memory lives in dynamic suffix
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(parts)


# ── Dynamic Suffix (per-round) ──────────────────────────────────


def _render_active_packs(active_packs: list[dict[str, Any]], *, budget_chars: int = _ACTIVE_PACKS_CHAR_BUDGET) -> str:
    """Delegate to modular section builder (kept for backward compat)."""
    from app.runtime.prompt_sections import build_active_packs_section

    return build_active_packs_section(active_packs, budget_chars=budget_chars)


def build_dynamic_prompt_suffix(
    *,
    active_packs: list[dict[str, Any]] | None = None,
    retrieval_context: str = "",
    continuity_context: str = "",
    system_prompt_suffix: str = "",
    budget_profile: ContextBudget | None = None,
    latest_user_query: str = "",
    memory_snapshot: str = "",
    user_name: str = "",
    channel: str = "",
    agent_name: str = "",
) -> str:
    """Build the per-round dynamic suffix.

    Contains: § Memory, active capability packs, knowledge retrieval results,
    § Environment, and request-specific suffix.
    These CAN change between rounds within the same session.
    """
    from app.runtime.prompt_sections import (
        build_environment_section,
        build_knowledge_section,
        build_memory_section,
        build_scenario_section,
    )

    parts: list[str] = []

    # § Memory (4-layer pyramid + current T3 snapshot)
    if memory_snapshot:
        parts.append(build_memory_section(memory_snapshot))

    memory_budget_chars = getattr(budget_profile, "memory_budget_chars", _CONTINUITY_CHAR_BUDGET)
    continuity_budget = min(
        max((memory_budget_chars // 3) if budget_profile else _CONTINUITY_CHAR_BUDGET, 800),
        _CONTINUITY_CHAR_BUDGET,
    )
    if continuity_context:
        continuity_block = _trim_block(continuity_context, budget_chars=continuity_budget)
        if continuity_block:
            parts.append(f"## Session Continuity\n{continuity_block}")

    packs_budget = budget_profile.active_packs_budget_chars if budget_profile else _ACTIVE_PACKS_CHAR_BUDGET
    retrieval_budget = budget_profile.retrieval_budget_chars if budget_profile else _RETRIEVAL_CHAR_BUDGET
    scenario_section = build_scenario_section(
        budget_profile.task_profile if budget_profile else None,
        query=latest_user_query,
    )
    if scenario_section:
        parts.append(scenario_section)

    packs_section = _render_active_packs(active_packs or [], budget_chars=packs_budget)
    if packs_section:
        parts.append(packs_section)

    if retrieval_context:
        knowledge = build_knowledge_section(retrieval_context, budget_chars=retrieval_budget)
        if knowledge:
            parts.append(knowledge)

    if budget_profile and not active_packs and budget_profile.task_profile.suggested_pack_names:
        hint_lines = [
            "## Likely Capability Packs",
            "These packs are likely useful for the current request. Activate them proactively when needed.",
        ]
        for pack_name in budget_profile.task_profile.suggested_pack_names:
            hint_lines.append(f"- {pack_name}")
        parts.append(_trim_block("\n".join(hint_lines), budget_chars=packs_budget))

    # § Environment (user, channel, time)
    env_section = build_environment_section(user_name=user_name, channel=channel, agent_name=agent_name)
    if env_section:
        parts.append(env_section)

    if system_prompt_suffix:
        parts.append(system_prompt_suffix)

    return "\n\n".join(parts)


def _join_prompt_sections(frozen_prefix: str, dynamic_suffix: str) -> str:
    if not dynamic_suffix:
        return frozen_prefix
    return f"{frozen_prefix}\n\n{PROMPT_CACHE_BOUNDARY}\n\n{dynamic_suffix}"


# ── Assembly ────────────────────────────────────────────────────


def _compute_system_prompt_budget(context_window_tokens: int | None) -> int:
    """Backward-compatible wrapper for existing imports/tests."""
    return compute_system_prompt_budget(context_window_tokens)


def assemble_runtime_prompt(
    frozen_prefix: str,
    dynamic_suffix: str,
    context_window_tokens: int | None = None,
    budget_profile: ContextBudget | None = None,
) -> str:
    """Combine frozen prefix + dynamic suffix into final system prompt.

    If total exceeds budget, frozen prefix is trimmed (dynamic suffix preserved
    because it contains per-round retrieval and pack context).

    Args:
        context_window_tokens: Model's context window in tokens. When provided,
            the budget scales proportionally instead of using the fixed 60K default.
    """
    import logging
    _logger = logging.getLogger(__name__)

    budget = budget_profile.system_prompt_budget_chars if budget_profile else _compute_system_prompt_budget(context_window_tokens)
    prompt = _join_prompt_sections(frozen_prefix, dynamic_suffix)

    # P0.4 Observability: log prompt budget metrics
    _frozen_len = len(frozen_prefix)
    _dynamic_len = len(dynamic_suffix) if dynamic_suffix else 0
    _total_len = len(prompt)
    _logger.debug(
        "[PromptBuilder] Prompt budget: %d/%d chars (%d frozen + %d dynamic, ctx_window=%s)",
        _total_len, budget, _frozen_len, _dynamic_len, context_window_tokens or "default",
        extra={
            "metric": "prompt_budget",
            "frozen_chars": _frozen_len,
            "dynamic_chars": _dynamic_len,
            "total_chars": _total_len,
            "budget_chars": budget,
            "utilization_pct": round(_total_len / budget * 100, 1) if budget else 0,
        },
    )

    if len(prompt) > budget:
        overshoot = len(prompt) - budget
        _logger.warning(
            "[PromptBuilder] System prompt exceeds budget: %d chars (budget=%d, ctx_window=%s, overshoot=%d) — trimming frozen prefix",
            len(prompt), budget, context_window_tokens or "default", overshoot,
        )
        # Trim frozen prefix from the end, preserve the cache boundary + dynamic suffix.
        if dynamic_suffix:
            dynamic_block = f"\n\n{PROMPT_CACHE_BOUNDARY}\n\n{dynamic_suffix}"
            truncation_notice = "\n\n...(system prompt truncated to fit context window)"
        else:
            dynamic_block = ""
            truncation_notice = "\n\n...(system prompt truncated to fit context window)"

        max_frozen = budget - len(dynamic_block) - len(truncation_notice)
        if max_frozen > 0:
            trimmed_frozen = frozen_prefix[:max_frozen].rstrip()
            prompt = f"{trimmed_frozen}{truncation_notice}{dynamic_block}"
        else:
            if dynamic_suffix:
                boundary_prefix = f"{PROMPT_CACHE_BOUNDARY}\n\n"
                available_dynamic = max(budget - len(boundary_prefix), 0)
                prompt = f"{boundary_prefix}{dynamic_suffix[:available_dynamic]}"
            else:
                prompt = frozen_prefix[:budget]
    return prompt


# ── Legacy-compatible full builder (used by invoker.py) ─────────


async def build_runtime_prompt(
    *,
    agent_id: uuid.UUID | None,
    agent_name: str,
    role_description: str,
    messages: list[dict],
    tenant_id: uuid.UUID | None,
    current_user_name: str | None,
    memory_context: str,
    system_prompt_suffix: str,
    runtime_context: RuntimeContext | None = None,
    build_agent_context_fn: BuildAgentContextFn | None = None,
    fetch_relevant_knowledge_fn: KnowledgeLookupFn | None = None,
) -> str:
    """Assemble the runtime system prompt from stable building blocks.

    Legacy-compatible entry point. Internally uses the frozen/dynamic split
    but does not cache — caching is managed by the kernel via SessionContext.
    """
    build_context = build_agent_context_fn or build_agent_context
    fetch_knowledge = fetch_relevant_knowledge_fn or fetch_relevant_knowledge

    last_user_msg = next(
        (
            message["content"]
            for message in reversed(messages)
            if message.get("role") == "user" and isinstance(message.get("content"), str)
        ),
        None,
    )
    context_window_tokens = None
    active_pack_count = 0
    if runtime_context:
        context_window_tokens = runtime_context.metadata.get("context_window_tokens")
        active_pack_count = len(runtime_context.session.active_packs)
    budget_profile = compute_context_budget(
        context_window_tokens=context_window_tokens,
        query=last_user_msg or "",
        messages=messages,
        active_pack_count=active_pack_count,
    )

    _build_kwargs = {"current_user_name": current_user_name}
    if "budget_profile" in inspect.signature(build_context).parameters:
        _build_kwargs["budget_profile"] = budget_profile
    agent_context = await build_context(
        agent_id,
        agent_name,
        role_description,
        **_build_kwargs,
    )

    # Build frozen prefix
    frozen = build_frozen_prompt_prefix(
        agent_context=agent_context,
    )

    retrieval = ""
    continuity_context = ""
    if agent_id and last_user_msg:
        _knowledge_kwargs = {}
        if "max_tokens" in inspect.signature(fetch_knowledge).parameters:
            _knowledge_kwargs["max_tokens"] = max(500, budget_profile.knowledge_budget_chars // 3)
        if "max_chars" in inspect.signature(fetch_knowledge).parameters:
            _knowledge_kwargs["max_chars"] = budget_profile.knowledge_budget_chars
        if "limit" in inspect.signature(fetch_knowledge).parameters:
            _knowledge_kwargs["limit"] = budget_profile.external_limit
        knowledge = await _maybe_await(fetch_knowledge(last_user_msg, tenant_id, **_knowledge_kwargs))
        if knowledge:
            retrieval = knowledge
        try:
            from app.services.session_memory import (
                get_compaction_summary_path,
                load_session_memory,
                render_session_memory_excerpt,
            )

            session_payload = load_session_memory(agent_id)
            continuity_parts: list[str] = []
            if session_payload is not None:
                continuity_parts.append(
                    render_session_memory_excerpt(
                        session_payload,
                        budget_chars=min(max(budget_profile.memory_budget_chars // 3, 800), _CONTINUITY_CHAR_BUDGET),
                    )
                )
            compaction_path = get_compaction_summary_path(agent_id)
            if compaction_path.exists():
                summary_text = compaction_path.read_text(encoding="utf-8", errors="replace").strip()
                if summary_text:
                    continuity_parts.append(_trim_block(summary_text, budget_chars=800))
            continuity_context = "\n\n".join(part for part in continuity_parts if part.strip())
        except Exception:
            continuity_context = ""

    active_packs = []
    if runtime_context and runtime_context.session.active_packs:
        active_packs = runtime_context.session.active_packs

    dynamic = build_dynamic_prompt_suffix(
        active_packs=active_packs,
        memory_snapshot=memory_context,
        continuity_context=continuity_context,
        retrieval_context=retrieval,
        system_prompt_suffix=system_prompt_suffix,
        budget_profile=budget_profile,
        latest_user_query=last_user_msg or "",
        user_name=current_user_name or "",
        channel=runtime_context.session.channel if runtime_context else "",
        agent_name=agent_name,
    )

    return assemble_runtime_prompt(
        frozen,
        dynamic,
        context_window_tokens=context_window_tokens,
        budget_profile=budget_profile,
    )
