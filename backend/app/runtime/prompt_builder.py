"""Prompt assembly helpers for the unified runtime.

Three-layer prompt architecture:
  1. Frozen Prefix — stable within a session (identity, system, task rules, skill catalog)
  2. Dynamic Suffix — changes per round (active packs, retrieval, compaction hints)
  3. Per-turn Messages — normal conversation messages
"""

from __future__ import annotations

from typing import Any

from app.runtime.context_budget import ContextBudget, compute_system_prompt_budget

# Boundary marker between frozen (cacheable) and dynamic (volatile) prompt sections.
# apply_prompt_cache_hints() in llm_client splits at this marker to create two
# content blocks: frozen gets cache_control, dynamic does not.
PROMPT_CACHE_BOUNDARY = "__PROMPT_DYNAMIC_BOUNDARY__"

# Default fallbacks when no task-aware budget profile is provided.
_ACTIVE_PACKS_CHAR_BUDGET = 2000
_RETRIEVAL_CHAR_BUDGET = 3000
_CONTINUITY_CHAR_BUDGET = 2500


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
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(parts)


# ── Dynamic Suffix (per-round) ──────────────────────────────────


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

    from app.runtime.prompt_sections import build_active_packs_section

    packs_section = build_active_packs_section(active_packs or [], budget_chars=packs_budget)
    if packs_section:
        parts.append(packs_section)

    if retrieval_context:
        stripped_retrieval = retrieval_context.lstrip()
        if stripped_retrieval.startswith("## "):
            parts.append(retrieval_context.strip())
        else:
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

    budget = budget_profile.system_prompt_budget_chars if budget_profile else compute_system_prompt_budget(context_window_tokens)
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
