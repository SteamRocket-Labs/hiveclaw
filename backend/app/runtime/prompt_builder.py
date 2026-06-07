"""Prompt assembly helpers for the unified runtime.

Three-layer prompt architecture:
  1. Frozen Prefix — stable within a session (identity, system, task rules, skill catalog)
  2. Dynamic Suffix — changes per round (active packs, retrieval, compaction hints)
  3. Per-turn Messages — normal conversation messages
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.memory.metrics import record_frozen_prefix_metering
from app.runtime.context_budget import ContextBudget, compute_system_prompt_budget
from app.services.prompt_cache import PROMPT_CACHE_BOUNDARY  # noqa: F401
from app.services.token_tracker import estimate_tokens_from_chars


# Re-export the cache boundary marker from the provider-agnostic prompt_cache
# module. The prompt assembler inserts it between frozen and dynamic sections;
# apply_cache_hints() splits on it per provider.

# Default fallbacks when no task-aware budget profile is provided.
# P1-W2-6: tool group budget tightened from 2000 → 1200 (matches the new
# active_tool_groups section default; tool groups are referential, not full docs).
_ACTIVE_PACKS_CHAR_BUDGET = 1200
_RETRIEVAL_CHAR_BUDGET = 3000
_CONTINUITY_CHAR_BUDGET = 2500
# P1-W2-2: Per-section caps in the dynamic suffix.
# Memory body gets 60% of the memory budget; the remainder is for
# continuity_context (which is also memory-flavored). System prompt suffix
# is user-supplied — fixed ceiling stops a runaway upstream caller from
# pushing the suffix past sensible round-trip cost.
_MEMORY_SNAPSHOT_BUDGET_RATIO = 0.6
_DEFAULT_MEMORY_SNAPSHOT_BUDGET = 8000
_SYSTEM_PROMPT_SUFFIX_CHAR_CAP = 5000

# P1-1b/W2-1: Frozen-prefix token guard rails.
# Guard rails are calibrated for long-context production agents: 16K frozen
# tokens is still a small slice of a 256K context, while 12K gives operators
# enough headroom to see static prefix growth before it hurts cache efficiency.
# `_CHARS_PER_TOKEN_ESTIMATE` mirrors token_tracker.estimate_tokens_from_chars
# (3.5 chars/token) — kept here so the inverse direction (token budget →
# char budget) does not silently drift if either side changes.
_FROZEN_PREFIX_TOKEN_WARN = 12000
_FROZEN_PREFIX_TOKEN_LIMIT = 16000
_CHARS_PER_TOKEN_ESTIMATE = 3.5
_FROZEN_PREFIX_CHAR_LIMIT = int(_FROZEN_PREFIX_TOKEN_LIMIT * _CHARS_PER_TOKEN_ESTIMATE)
_FROZEN_PREFIX_TRIM_NOTICE = (
    "\n\n...(frozen prefix trimmed to stay under cache budget — load extra skills via the load_skill tool)"
)
_FROZEN_PREFIX_SECTION_RE = re.compile(r"(?m)^#{2,3}\s+(.+?)\s*$")
_FROZEN_PREFIX_TOP_SECTION_LIMIT = 6


@dataclass(frozen=True, slots=True)
class FrozenPrefixSection:
    name: str
    chars: int
    tokens: int


# C3: cuts must stay observable — say a block was budget-trimmed, not a bare ellipsis.
_TRIM_MARKER = "\n...(trimmed to fit context budget)"


def _trim_block(text: str, *, budget_chars: int) -> str:
    if not text or budget_chars <= 0:
        return ""
    stripped = text.strip()
    if len(stripped) <= budget_chars:
        return stripped

    # Marker counts against the budget so callers' size contracts hold.
    line_budget = max(0, budget_chars - len(_TRIM_MARKER))
    lines = stripped.splitlines()
    kept: list[str] = []
    used = 0
    for line in lines:
        normalized = line.rstrip()
        if not normalized:
            continue
        line_cost = len(normalized) + 1
        if used + line_cost > line_budget:
            break
        kept.append(normalized)
        used += line_cost

    if not kept:
        head = stripped[:line_budget].rstrip()
        return (head + _TRIM_MARKER) if head else stripped[:budget_chars]

    return "\n".join(kept).rstrip() + _TRIM_MARKER


def _normalize_frozen_prefix_section_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", raw_name.strip().lower()).strip("_")
    return normalized or "unnamed"


def _measure_frozen_prefix_sections(prefix: str) -> list[FrozenPrefixSection]:
    """Return rendered frozen-prefix section sizes in render order.

    This is intentionally derived from the final rendered prefix, after any
    trimming. The diagnostic must explain what is actually sent to the model,
    not what an earlier untrimmed candidate looked like.
    """
    if not prefix:
        return []

    matches = list(_FROZEN_PREFIX_SECTION_RE.finditer(prefix))
    sections: list[FrozenPrefixSection] = []

    if not matches:
        chars = len(prefix)
        return [
            FrozenPrefixSection(
                name="unsectioned",
                chars=chars,
                tokens=estimate_tokens_from_chars(chars),
            )
        ]

    preamble = prefix[: matches[0].start()].strip()
    if preamble:
        chars = len(preamble)
        sections.append(
            FrozenPrefixSection(
                name="preamble",
                chars=chars,
                tokens=estimate_tokens_from_chars(chars),
            )
        )

    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(prefix)
        block = prefix[match.start() : end].strip()
        if not block:
            continue
        chars = len(block)
        sections.append(
            FrozenPrefixSection(
                name=_normalize_frozen_prefix_section_name(match.group(1)),
                chars=chars,
                tokens=estimate_tokens_from_chars(chars),
            )
        )

    return sections


def _format_frozen_prefix_top_sections(sections: list[FrozenPrefixSection], *, limit: int) -> str:
    if not sections:
        return "none"

    top = sorted(sections, key=lambda section: section.chars, reverse=True)[:limit]
    return ", ".join(f"{section.name}={section.tokens}t/{section.chars}c" for section in top)


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

    P1-1b: Every build is metered. Token estimate above
    `_FROZEN_PREFIX_TOKEN_WARN` logs a warning; above
    `_FROZEN_PREFIX_TOKEN_LIMIT` logs an error.

    P1-W2-1: Hard cap enforced at `_FROZEN_PREFIX_TOKEN_LIMIT`. Skill catalog
    is dropped/trimmed first (it's a progressive-disclosure index — the
    `load_skill` tool can pull full bodies on demand). Tail-trim is the
    last resort.
    """
    from app.runtime.prompt_sections import (
        build_system_section,
        build_tasks_section,
        build_tools_section,
    )

    # tone_style is included by agent_context (via build_agent_context).
    # output_efficiency was merged into tone_style.py — do not re-inject.
    base_parts = [
        agent_context,
        build_system_section(),
        build_tasks_section(),
        build_tools_section(),
    ]
    del memory_snapshot  # kept for backward-compatible callers; memory lives in dynamic suffix

    parts = list(base_parts)
    if skill_catalog:
        parts.append(skill_catalog)
    prefix = "\n\n".join(parts)

    if estimate_tokens_from_chars(len(prefix)) > _FROZEN_PREFIX_TOKEN_LIMIT:
        prefix = _enforce_frozen_prefix_budget(base_parts, skill_catalog)

    _meter_frozen_prefix(prefix)
    return prefix


# C2 (docs/agent-lifecycle-cc-alignment.md 主题 C): an over-budget catalog must
# never leave the model blind — degraded states keep a signpost back to skills.
_CATALOG_OMITTED_NOTICE = (
    "## Skills\n"
    "[Skill catalog omitted to fit the context budget — skills are still available: "
    "call load_skill(name) to load one, or list the skills/ directory to discover them.]"
)
_CATALOG_TRIMMED_SUFFIX = (
    "\n[... catalog truncated to fit the context budget — more skills exist: "
    "list the skills/ directory or call load_skill(name) directly.]"
)


def _enforce_frozen_prefix_budget(base_parts: list[str], skill_catalog: str) -> str:
    """Hard-cap the assembled prefix to `_FROZEN_PREFIX_CHAR_LIMIT`.

    Strategy (in order of preference):
      1. Drop the skill catalog body. It's the most replaceable section
         because `load_skill` can hydrate any skill body on demand —
         but ALWAYS leave a signpost so the model knows skills exist (C2).
      2. If base sections alone fit, optionally re-add a trimmed catalog
         in the leftover budget so frequently-used skills stay visible,
         with a trailing signpost for the skills that were cut.
      3. If base sections alone overflow, tail-trim with a notice. This is
         a last resort — base sections drive agent behavior and trimming
         them risks behavior regression.
    """
    base_only = "\n\n".join(base_parts)

    if len(base_only) <= _FROZEN_PREFIX_CHAR_LIMIT:
        if not skill_catalog:
            return base_only
        # Reserve room for the "\n\n" join and the "\n..." marker that
        # `_trim_block` may append (up to 4 extra chars over its budget).
        leftover = _FROZEN_PREFIX_CHAR_LIMIT - len(base_only) - 6
        # Below 200 chars a trimmed catalog is just noise — but never go
        # silently blind: leave the minimum-visibility signpost instead.
        if leftover < 200:
            return f"{base_only}\n\n{_CATALOG_OMITTED_NOTICE}"
        trimmed = _trim_block(skill_catalog, budget_chars=max(200, leftover - len(_CATALOG_TRIMMED_SUFFIX)))
        if not trimmed:
            return f"{base_only}\n\n{_CATALOG_OMITTED_NOTICE}"
        if len(trimmed) < len(skill_catalog):
            trimmed += _CATALOG_TRIMMED_SUFFIX
        result = f"{base_only}\n\n{trimmed}"
        # Defensive hard cap — the metering contract is strict.
        if len(result) > _FROZEN_PREFIX_CHAR_LIMIT:
            result = result[:_FROZEN_PREFIX_CHAR_LIMIT]
        return result

    # Base sections themselves overflow — tail-trim with a notice. We trim
    # `base_only` (catalog already dropped) so the most critical content
    # at the head (agent_context → system → tasks) is preserved.
    available = _FROZEN_PREFIX_CHAR_LIMIT - len(_FROZEN_PREFIX_TRIM_NOTICE)
    if available <= 0:
        return base_only[:_FROZEN_PREFIX_CHAR_LIMIT]
    return base_only[:available].rstrip() + _FROZEN_PREFIX_TRIM_NOTICE


def _meter_frozen_prefix(prefix: str) -> None:
    """Sample the frozen prefix size and bump warn/overrun counters.

    Isolated for testability — callers don't need a logger fixture.
    """
    import logging

    chars = len(prefix)
    tokens = estimate_tokens_from_chars(chars)
    warn = tokens >= _FROZEN_PREFIX_TOKEN_WARN
    overrun = tokens > _FROZEN_PREFIX_TOKEN_LIMIT
    record_frozen_prefix_metering(chars=chars, tokens=tokens, warn=warn, overrun=overrun)

    if not warn:
        return

    logger = logging.getLogger(__name__)
    sections = _measure_frozen_prefix_sections(prefix)
    section_tokens = {section.name: section.tokens for section in sections}
    section_chars = {section.name: section.chars for section in sections}
    top_sections = _format_frozen_prefix_top_sections(sections, limit=_FROZEN_PREFIX_TOP_SECTION_LIMIT)
    extra = {
        "metric": "frozen_prefix_size",
        "chars": chars,
        "tokens": tokens,
        "warn_threshold": _FROZEN_PREFIX_TOKEN_WARN,
        "hard_limit": _FROZEN_PREFIX_TOKEN_LIMIT,
        "section_tokens": section_tokens,
        "section_chars": section_chars,
        "top_sections": top_sections,
    }
    if overrun:
        logger.error(
            "[PromptBuilder] frozen prefix exceeds hard limit: ~%d tokens (chars=%d, limit=%d) — "
            "prompt cache hit-rate will degrade and per-call cost will rise. "
            "Trim agent_context / system / tasks / tools sections. top_sections=%s",
            tokens,
            chars,
            _FROZEN_PREFIX_TOKEN_LIMIT,
            top_sections,
            extra=extra,
        )
    else:
        logger.warning(
            "[PromptBuilder] frozen prefix above warn threshold: ~%d tokens "
            "(chars=%d, warn=%d, limit=%d, top_sections=%s)",
            tokens,
            chars,
            _FROZEN_PREFIX_TOKEN_WARN,
            _FROZEN_PREFIX_TOKEN_LIMIT,
            top_sections,
            extra=extra,
        )


# ── Dynamic Suffix (per-round) ──────────────────────────────────


def _render_active_tool_groups(
    active_tool_groups: list[dict[str, Any]], *, budget_chars: int = _ACTIVE_PACKS_CHAR_BUDGET
) -> str:
    """Delegate to modular section builder (kept for backward compat)."""
    from app.runtime.prompt_sections import build_active_tool_groups_section

    return build_active_tool_groups_section(active_tool_groups, budget_chars=budget_chars)


# B4 (docs/agent-lifecycle-cc-alignment.md 主题 B): autonomous-work semantics
# for wake-to-work runs — CC's "# Autonomous work" equivalent.
#
# Trigger ONLY. Heartbeat is deliberately excluded: it is the distiller
# (T2→T3 curation, "a librarian shelving books") whose semantics are fully
# owned by the identity heartbeat template + the HEARTBEAT.md SOP — which
# explicitly forbids external-facing actions, so this section's "bias toward
# action" / "external actions via plan/checkpoint" framing would both
# duplicate and contradict it.
_AUTONOMOUS_SOURCES = frozenset({"trigger"})

_AUTONOMOUS_WORK_SECTION = """\
## Autonomous Work
You are running autonomously (source: {source}) — no live user is watching this run.
- **Wake context**: treat the trigger message as "you're awake — what now?", not as a fresh user request.
- **Bias toward action**: prefer doing useful work over asking questions nobody will answer. Reading, analyzing, \
writing workspace artifacts, and updating the Objective Ledger are always safe.
- **Authority unchanged**: external-visible or irreversible actions still require a confirmed plan or checkpoint — \
running autonomously does not expand what you may do.
- **Pacing**: if there is nothing useful to do, say so briefly and end the run cleanly — do not invent work and \
do not poll in a loop.
- **State recording**: before the run ends, leave the Objective Ledger / focus.md / artifacts in a state where \
the next wake-up (or a human) can resume without guessing."""


def build_dynamic_prompt_suffix(
    *,
    active_tool_groups: list[dict[str, Any]] | None = None,
    retrieval_context: str = "",
    continuity_context: str = "",
    runtime_metadata_context: str = "",
    session_learning_projection: str = "",
    system_prompt_suffix: str = "",
    system_prompt_suffix_sections: list[str] | None = None,
    budget_profile: ContextBudget | None = None,
    latest_user_query: str = "",
    memory_snapshot: str = "",
    memory_navigation: str = "",
    user_name: str = "",
    channel: str = "",
    agent_name: str = "",
    source: str = "",
) -> str:
    """Build the per-round dynamic suffix.

    Contains: § Memory, § Memory Navigation, active runtime tool groups,
    knowledge retrieval results, § Environment, and request-specific suffix.
    These CAN change between rounds within the same session.
    """
    from app.runtime.prompt_sections import (
        build_environment_section,
        build_knowledge_section,
        build_memory_section,
        build_scenario_section,
    )

    parts: list[str] = []

    # § Autonomous Work — unified semantics for unattended runs (B4)
    if source in _AUTONOMOUS_SOURCES:
        parts.append(_AUTONOMOUS_WORK_SECTION.format(source=source))

    memory_budget_chars = getattr(budget_profile, "memory_budget_chars", _DEFAULT_MEMORY_SNAPSHOT_BUDGET)

    # § Memory (4-layer pyramid + current T3 snapshot) — body capped to 60%
    # of the memory budget so continuity_context has room to breathe in the
    # remaining 40%.
    if memory_snapshot:
        snapshot_cap = max(int(memory_budget_chars * _MEMORY_SNAPSHOT_BUDGET_RATIO), 1500)
        parts.append(build_memory_section(memory_snapshot, budget_chars=snapshot_cap))

    # § Memory Navigation (spec §8 / §12 P6) — heat-ordered entry index as
    # its own section (never inside soul); pairs with load_memory for
    # progressive disclosure.
    if memory_navigation:
        navigation_block = _trim_block(memory_navigation, budget_chars=4000)
        if navigation_block:
            parts.append(navigation_block)

    if session_learning_projection:
        learning_block = _trim_block(session_learning_projection, budget_chars=1200)
        if learning_block:
            parts.append(learning_block)

    continuity_budget = min(
        max((memory_budget_chars // 3) if budget_profile else _CONTINUITY_CHAR_BUDGET, 800),
        _CONTINUITY_CHAR_BUDGET,
    )
    if continuity_context:
        continuity_block = _trim_block(continuity_context, budget_chars=continuity_budget)
        if continuity_block:
            parts.append(f"## Session Continuity\n{continuity_block}")

    runtime_budget = getattr(budget_profile, "runtime_triggers_budget_chars", 3000)
    if runtime_metadata_context:
        runtime_block = _trim_block(runtime_metadata_context, budget_chars=runtime_budget)
        if runtime_block:
            parts.append(runtime_block)

    packs_budget = budget_profile.active_tool_groups_budget_chars if budget_profile else _ACTIVE_PACKS_CHAR_BUDGET
    retrieval_budget = budget_profile.retrieval_budget_chars if budget_profile else _RETRIEVAL_CHAR_BUDGET
    scenario_section = build_scenario_section(
        budget_profile.task_profile if budget_profile else None,
        query=latest_user_query,
    )
    if scenario_section:
        parts.append(scenario_section)

    packs_section = _render_active_tool_groups(active_tool_groups or [], budget_chars=packs_budget)
    if packs_section:
        parts.append(packs_section)

    if retrieval_context:
        knowledge = build_knowledge_section(retrieval_context, budget_chars=retrieval_budget)
        if knowledge:
            parts.append(knowledge)

    if budget_profile and not active_tool_groups and budget_profile.task_profile.suggested_pack_names:
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

    suffix_sections: list[str] = []
    if system_prompt_suffix:
        suffix_sections.append(system_prompt_suffix)
    suffix_sections.extend(section for section in (system_prompt_suffix_sections or []) if section)
    for suffix_section in suffix_sections:
        # P1-W2-2/A6: cap each request-specific suffix independently. Runtime
        # callers may inject multiple critical suffixes (for example delegation
        # handoff + coordinator mode); one large section must not erase another.
        parts.append(_trim_block(suffix_section, budget_chars=_SYSTEM_PROMPT_SUFFIX_CHAR_CAP))

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

    budget = (
        budget_profile.system_prompt_budget_chars
        if budget_profile
        else _compute_system_prompt_budget(context_window_tokens)
    )
    prompt = _join_prompt_sections(frozen_prefix, dynamic_suffix)

    # P0.4 Observability: log prompt budget metrics
    _frozen_len = len(frozen_prefix)
    _dynamic_len = len(dynamic_suffix) if dynamic_suffix else 0
    _total_len = len(prompt)
    _logger.debug(
        "[PromptBuilder] Prompt budget: %d/%d chars (%d frozen + %d dynamic, ctx_window=%s)",
        _total_len,
        budget,
        _frozen_len,
        _dynamic_len,
        context_window_tokens or "default",
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
            len(prompt),
            budget,
            context_window_tokens or "default",
            overshoot,
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
