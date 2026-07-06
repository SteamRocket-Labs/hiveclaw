"""Prompt assembly helpers for the unified runtime.

Three-layer prompt architecture:
  1. Frozen Prefix — stable within a session (identity, system, task rules)
  2. Dynamic Suffix — changes per round (runtime tool groups, retrieval, compaction hints)
  3. Per-turn Messages — normal conversation messages
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from app.memory.metrics import record_frozen_prefix_metering
from app.runtime.context_budget import ContextBudget, compute_system_prompt_budget
from app.runtime.context_candidates import build_context_candidate_ref
from app.services.prompt_cache import PROMPT_CACHE_BOUNDARY  # noqa: F401
from app.services.token_tracker import estimate_tokens_from_text


# Re-export the cache boundary marker from the provider-agnostic prompt_cache
# module. The prompt assembler inserts it between frozen and dynamic sections;
# apply_cache_hints() splits on it per provider.

# Default fallbacks when no task-aware budget profile is provided.
# P1-W2-6: tool group budget tightened from 2000 → 1200 (matches the new
# active_tool_groups section default; tool groups are referential, not full docs).
_ACTIVE_TOOL_GROUPS_CHAR_BUDGET = 1200
_RETRIEVAL_CHAR_BUDGET = 3000
_CONTINUITY_CHAR_BUDGET = 2500
# P1-W2-2: Per-section caps in the dynamic suffix.
# Memory context is already selected by MemoryAssembler using score-aware
# trimming at memory_budget_chars. The prompt builder must not apply a smaller
# ratio cap afterwards; that would overwrite the assembler's ranking. System
# prompt suffix is user-supplied — fixed ceiling stops a runaway upstream caller
# from pushing the suffix past sensible round-trip cost.
_DEFAULT_MEMORY_SNAPSHOT_BUDGET = 8000
_SYSTEM_PROMPT_SUFFIX_CHAR_CAP = 5000

# P1-1b/W2-1: Frozen-prefix token guard rails.
# Guard rails are calibrated for long-context production agents: 16K frozen
# tokens is still a small slice of a 256K context, while 12K gives operators
# enough headroom to see static prefix growth before it hurts cache efficiency.
# `_CHARS_PER_TOKEN_ESTIMATE` is intentionally only for inverse direction
# (token budget → char budget). Measured text paths use CJK-aware token
# estimation from token_tracker.
_FROZEN_PREFIX_TOKEN_WARN = 12000
_FROZEN_PREFIX_TOKEN_LIMIT = 16000
# I.3: cap scales with context window: max(16K, min(10% of window, 32K)) tokens.
# 16K floor preserves cache economics on small models; 32K ceiling prevents
# the frozen prefix from consuming too much of even a 512K window.
_FROZEN_PREFIX_TOKEN_CAP_MIN = 16000
_FROZEN_PREFIX_TOKEN_CAP_MAX = 32000
_FROZEN_PREFIX_TOKEN_CAP_RATIO = 0.10
_CHARS_PER_TOKEN_ESTIMATE = 3.5
_FROZEN_PREFIX_CHAR_LIMIT = int(_FROZEN_PREFIX_TOKEN_LIMIT * _CHARS_PER_TOKEN_ESTIMATE)
_FROZEN_PREFIX_TRIM_NOTICE = (
    "\n\n...(frozen prefix trimmed to stay under cache budget — load extra skills via the load_skill tool)"
)
# I.3 Tier4: loud, distinct marker when the soul/identity section itself had to be trimmed.
# This is intentionally MORE alarming than the generic trim notice to make
# runaway soul growth immediately obvious in prompt logs/reviews.
_FROZEN_IDENTITY_OVERRUN_MARKER = (
    "\n\n[identity overrun: ## Identity & Mission was trimmed to fit the context budget — "
    "soul.md is too large; reduce it to restore full identity fidelity]"
)
_FROZEN_PREFIX_SECTION_RE = re.compile(r"(?m)^#{2,3}\s+(.+?)\s*$")
_FROZEN_PREFIX_TOP_SECTION_LIMIT = 6


def _compute_frozen_cap_tokens(context_window_tokens: int | None) -> int:
    """Derive the frozen-prefix token cap from the model context window.

    I.3 formula: max(16K, min(10% of window, 32K)).
    - Floor 16K: preserves cache economics on small/medium models.
    - Ceiling 32K: keeps the frozen prefix ≤ 12.5% of even a 256K window
      so there is headroom for conversation turns.
    - No window → 16K floor (backward-compatible default).
    """
    if not context_window_tokens or context_window_tokens <= 0:
        return _FROZEN_PREFIX_TOKEN_CAP_MIN
    scaled = int(_FROZEN_PREFIX_TOKEN_CAP_RATIO * context_window_tokens)
    return max(_FROZEN_PREFIX_TOKEN_CAP_MIN, min(scaled, _FROZEN_PREFIX_TOKEN_CAP_MAX))


@dataclass(frozen=True, slots=True)
class FrozenPrefixSection:
    name: str
    chars: int
    tokens: int


@dataclass(frozen=True, slots=True)
class ContextSectionCandidate:
    candidate_id: str
    kind: str
    name: str
    content: str
    render_order: int
    score: float = 1.0
    budget_key: str | None = None
    budget_chars: int | None = None
    enforce_budget: bool = False


def _context_section_source_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _append_context_section_decision(
    ledger: list[dict[str, Any]] | None,
    *,
    candidate: ContextSectionCandidate,
    rendered_content: str,
    decision: str,
) -> None:
    if ledger is None:
        return
    source_chars = len(candidate.content or "")
    rendered_chars = len(rendered_content or "")
    candidate_ref = build_context_candidate_ref(
        kind=candidate.kind,
        item_id=candidate.name,
        version="dynamic_section",
        payload=candidate.content,
    ).to_manifest(legacy_id=candidate.candidate_id)
    ledger.append(
        {
            "schema": "hive.ccplus.context_section_candidate.v1",
            "candidate_id": candidate.candidate_id,
            "candidate_ref": candidate_ref,
            "kind": candidate.kind,
            "name": candidate.name,
            "render_order": candidate.render_order,
            "score": candidate.score,
            "selected": bool(rendered_content),
            "decision": decision,
            "budget_key": candidate.budget_key,
            "budget_chars": candidate.budget_chars,
            "budget_enforced": candidate.enforce_budget,
            "source_chars": source_chars,
            "source_tokens": estimate_tokens_from_text(candidate.content or ""),
            "rendered_chars": rendered_chars,
            "rendered_tokens": estimate_tokens_from_text(rendered_content or ""),
            "source_hash": _context_section_source_hash(candidate.content or ""),
        }
    )


def _select_context_section_candidates(
    candidates: list[ContextSectionCandidate],
    *,
    context_section_ledger: list[dict[str, Any]] | None = None,
) -> list[str]:
    selected: list[tuple[int, str]] = []
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.render_order)):
        content = (candidate.content or "").strip()
        if not content:
            _append_context_section_decision(
                context_section_ledger,
                candidate=candidate,
                rendered_content="",
                decision="suppressed_empty",
            )
            continue

        rendered_content = content
        decision = "selected_within_budget"
        if candidate.budget_chars is not None and candidate.budget_chars > 0 and len(content) > candidate.budget_chars:
            if candidate.enforce_budget:
                rendered_content = _trim_block(content, budget_chars=candidate.budget_chars)
                decision = "selected_trimmed"
            else:
                decision = "selected_over_budget"

        selected.append((candidate.render_order, rendered_content))
        _append_context_section_decision(
            context_section_ledger,
            candidate=candidate,
            rendered_content=rendered_content,
            decision=decision,
        )

    return [content for _, content in sorted(selected, key=lambda item: item[0])]


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


def _char_limit_for_token_cap(text: str, cap_tokens: int, *, fallback_chars: int) -> int:
    """Find a char cap whose actual text estimate stays within `cap_tokens`."""
    if cap_tokens <= 0:
        return 0
    upper = max(min(len(text), fallback_chars), 0)
    if upper <= 0:
        return 0
    if estimate_tokens_from_text(text[:upper]) <= cap_tokens:
        return fallback_chars

    lo = 0
    hi = upper
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens_from_text(text[:mid]) <= cap_tokens:
            lo = mid
        else:
            hi = mid - 1
    return max(lo, 1)


def _trim_to_token_cap(text: str, cap_tokens: int) -> str:
    if estimate_tokens_from_text(text) <= cap_tokens:
        return text
    char_limit = _char_limit_for_token_cap(text, cap_tokens, fallback_chars=len(text))
    return _trim_block(text, budget_chars=char_limit)


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
                tokens=estimate_tokens_from_text(prefix),
            )
        ]

    preamble = prefix[: matches[0].start()].strip()
    if preamble:
        chars = len(preamble)
        sections.append(
            FrozenPrefixSection(
                name="preamble",
                chars=chars,
                tokens=estimate_tokens_from_text(preamble),
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
                tokens=estimate_tokens_from_text(block),
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
    context_window_tokens: int | None = None,
) -> str:
    """Build the session-stable prompt prefix.

    Contains: agent identity/soul/role, § System, § Doing Tasks, § Using Your Tools.
    These do NOT change within a single session.

    Step 9 (CC parity): the skill catalog moved OUT of the frozen prefix into the
    dynamic suffix (it changes when skills are added/distilled, which would bust
    the prompt-cache boundary). The `skill_catalog` parameter is retained for
    backward-compatible / inline-context callers, but the invoker's primary path
    no longer populates it — catalog flows through `build_dynamic_prompt_suffix`.

    P1-1b: Every build is metered. Token estimate above
    `_FROZEN_PREFIX_TOKEN_WARN` logs a warning; above
    `_FROZEN_PREFIX_TOKEN_LIMIT` logs an error.

    P1-W2-1: Hard cap enforced via identity-protected layered trim (I.3):
      Tier1 (skill catalog) → Tier2 (## Context Material) → Tier3 (Tools/Tasks/System
      bodies) → Tier4 (soul last-resort with loud marker + ERROR log).
    Cap scales with model context window via `_compute_frozen_cap_tokens`.

    Args:
        context_window_tokens: Model's max_input_tokens. When provided the cap
            scales as max(16K, min(10% of window, 32K)) tokens. Defaults to
            the 16K floor for backward compatibility.
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

    cap_tokens = _compute_frozen_cap_tokens(context_window_tokens)
    cap_chars = int(cap_tokens * _CHARS_PER_TOKEN_ESTIMATE)

    parts = list(base_parts)
    if skill_catalog:
        parts.append(skill_catalog)
    prefix = "\n\n".join(parts)

    if estimate_tokens_from_text(prefix) > cap_tokens:
        text_aware_cap_chars = _char_limit_for_token_cap(prefix, cap_tokens, fallback_chars=cap_chars)
        prefix = _enforce_frozen_prefix_budget(base_parts, skill_catalog, char_limit=text_aware_cap_chars)
        prefix = _trim_to_token_cap(prefix, cap_tokens)

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


# I.3: regex to locate the ## Context Material block inside agent_context.
# Matches from "## Context Material" through (but not including) the next ##-level
# heading, or through end-of-string. Used by the Tier2 stripper.
_CONTEXT_MATERIAL_BLOCK_RE = re.compile(
    r"(?m)^(## Context Material\b.*?)(?=^##\s|\Z)",
    re.DOTALL,
)
_CONTEXT_MATERIAL_OMITTED_NOTICE = (
    "\n## Context Material\n[context material omitted to fit context budget]"
)


def _strip_context_material(agent_context: str) -> str:
    """Tier2 trim: replace the ## Context Material block with a minimal signpost.

    Returns the agent_context string with the Context Material body replaced by
    a one-line notice. If the block is not found the input is returned unchanged.
    """
    match = _CONTEXT_MATERIAL_BLOCK_RE.search(agent_context)
    if not match:
        return agent_context
    return agent_context[: match.start()] + _CONTEXT_MATERIAL_OMITTED_NOTICE + agent_context[match.end() :]


def _enforce_frozen_prefix_budget(
    base_parts: list[str],
    skill_catalog: str,
    *,
    char_limit: int = _FROZEN_PREFIX_CHAR_LIMIT,
) -> str:
    """Hard-cap the assembled prefix to `char_limit` (default: `_FROZEN_PREFIX_CHAR_LIMIT`).

    I.3 identity-protected layered trim — executed in priority order:

    Tier1 (skill catalog)
        Drop the catalog body first. `load_skill` hydrates any skill body on
        demand, so this is the safest cut. Always leave a signpost (C2).

    Tier2 (## Context Material)
        Strip the company/org/channel boilerplate block from agent_context and
        replace with a one-line notice. This information is less critical than
        the tool/task rules that come after it.

    Tier3 (Tools / Tasks / System section bodies)
        Trim the tails of the system, tasks, and tools sections. These drive
        agent behaviour, so we trim them last among the non-identity sections.

    Tier4 (soul / identity — last resort)
        If the prefix STILL overflows after Tiers 1–3, trim the soul itself
        but emit _FROZEN_IDENTITY_OVERRUN_MARKER (a loud, distinct marker)
        AND log an ERROR. Never silently discard identity.

    The no-op path (prefix fits under cap) is unchanged from the previous
    implementation so warm-path performance is unaffected.
    """
    import logging

    base_only = "\n\n".join(base_parts)

    # ── No-op: catalog fits in the leftover budget ───────────────────────────
    if len(base_only) <= char_limit:
        if not skill_catalog:
            return base_only
        leftover = char_limit - len(base_only) - 6
        if leftover < 200:
            return f"{base_only}\n\n{_CATALOG_OMITTED_NOTICE}"
        trimmed = _trim_block(skill_catalog, budget_chars=max(200, leftover - len(_CATALOG_TRIMMED_SUFFIX)))
        if not trimmed:
            return f"{base_only}\n\n{_CATALOG_OMITTED_NOTICE}"
        if len(trimmed) < len(skill_catalog):
            trimmed += _CATALOG_TRIMMED_SUFFIX
        result = f"{base_only}\n\n{trimmed}"
        if len(result) > char_limit:
            result = result[:char_limit]
        return result

    # ── Base sections overflow — layered trim ───────────────────────────────
    # base_parts layout: [agent_context, system, tasks, tools]
    # Catalog is already implicitly dropped (not in base_parts).

    agent_context = base_parts[0] if base_parts else ""
    tail_parts = list(base_parts[1:])  # [system, tasks, tools]

    # Tier2: strip ## Context Material from agent_context
    agent_context_t2 = _strip_context_material(agent_context)
    candidate = "\n\n".join([agent_context_t2] + tail_parts)
    if len(candidate) <= char_limit:
        return candidate

    # Tier3: trim tail sections (system, tasks, tools) from the END inward.
    # We preserve their headers but trim the bodies.  Trim each from the tail
    # so that later sections (tools) shrink before earlier ones (tasks/system).
    # Simple approach: trim the whole assembled tail with a notice.
    identity_chars = len(agent_context_t2)
    tail_budget = char_limit - identity_chars - 4  # 4 = len("\n\n")
    if tail_budget > 80:
        trimmed_tail = _trim_block("\n\n".join(tail_parts), budget_chars=tail_budget)
        candidate = f"{agent_context_t2}\n\n{trimmed_tail}"
        if len(candidate) <= char_limit:
            return candidate

    # Tier4: soul itself is still too large — last resort.
    # Trim agent_context but emit the loud identity-overrun marker so this
    # catastrophic case is never silent.
    logger = logging.getLogger(__name__)
    logger.error(
        "[PromptBuilder] Tier4 soul/identity overrun: ## Identity & Mission section alone "
        "exceeds the frozen-prefix cap (%d chars). soul.md is too large — trim it to restore "
        "full identity fidelity. Applying emergency identity trim.",
        char_limit,
    )
    marker = _FROZEN_IDENTITY_OVERRUN_MARKER
    available_for_soul = char_limit - len(marker) - 4
    if available_for_soul <= 0:
        return agent_context[:char_limit]
    trimmed_soul = _trim_block(agent_context_t2, budget_chars=available_for_soul)
    return trimmed_soul + marker


def _meter_frozen_prefix(prefix: str) -> None:
    """Sample the frozen prefix size and bump warn/overrun counters.

    Isolated for testability — callers don't need a logger fixture.
    """
    import logging

    chars = len(prefix)
    tokens = estimate_tokens_from_text(prefix)
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
    active_tool_groups: list[dict[str, Any]], *, budget_chars: int = _ACTIVE_TOOL_GROUPS_CHAR_BUDGET
) -> str:
    """Delegate to modular section builder (kept for backward compat)."""
    from app.runtime.prompt_sections import build_active_tool_groups_section

    return build_active_tool_groups_section(active_tool_groups, budget_chars=budget_chars)


def _render_deferred_tool_index(deferred_candidates: list[Any], *, budget_chars: int) -> str:
    if not deferred_candidates:
        return ""
    header = [
        "## Available Deferred Tools",
        "These tools are not loaded yet. To load exactly one schema, call `tool_search` with `select:<tool_name>`.",
    ]
    lines = list(header)
    rendered_count = 0
    for idx, candidate in enumerate(deferred_candidates):
        details = (
            f"group={candidate.group}; risk={candidate.risk}; "
            f"schema_tokens={candidate.schema_token_cost}; reason={candidate.reason}"
        )
        line = f"- {candidate.name} — `{candidate.selector}` ({details})"
        remaining_after = len(deferred_candidates) - idx - 1
        omitted_line = f"- ...(+{remaining_after} more available in manifest)" if remaining_after else ""
        tentative = "\n".join([*lines, line, *([omitted_line] if omitted_line else [])])
        if len(tentative) > budget_chars:
            break
        lines.append(line)
        rendered_count += 1

    if rendered_count < len(deferred_candidates):
        omitted_line = f"- ...(+{len(deferred_candidates) - rendered_count} more available in manifest)"
        while len(lines) > len(header) and len("\n".join([*lines, omitted_line])) > budget_chars:
            lines.pop()
            rendered_count -= 1
            omitted_line = f"- ...(+{len(deferred_candidates) - rendered_count} more available in manifest)"
        if len("\n".join([*lines, omitted_line])) <= budget_chars:
            lines.append(omitted_line)
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= budget_chars else _trim_block(rendered, budget_chars=budget_chars)


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
and writing workspace artifacts are always safe.
- **Authority unchanged**: external-visible or irreversible actions still require a confirmed plan or checkpoint — \
running autonomously does not expand what you may do.
- **Pacing**: if there is nothing useful to do, say so briefly and end the run cleanly — do not invent work and \
do not poll in a loop.
- **State recording**: before the run ends, leave your work ledger and workspace artifacts in a state where \
the next wake-up (or a human) can resume without guessing."""


def build_dynamic_prompt_suffix(
    *,
    active_tool_groups: list[dict[str, Any]] | None = None,
    available_deferred_tools: list[str] | tuple[str, ...] | None = None,
    retrieval_context: str = "",
    continuity_context: str = "",
    runtime_metadata_context: str = "",
    permissions_context: str = "",
    session_learning_projection: str = "",
    system_prompt_suffix: str = "",
    system_prompt_suffix_sections: list[str] | None = None,
    budget_profile: ContextBudget | None = None,
    latest_user_query: str = "",
    memory_snapshot: str = "",
    skill_catalog: str = "",
    user_name: str = "",
    channel: str = "",
    agent_name: str = "",
    source: str = "",
    context_section_ledger: list[dict[str, Any]] | None = None,
) -> str:
    """Build the per-round dynamic suffix.

    Contains: § Memory, runtime metadata,
    active runtime tool groups, § Skills catalog, external knowledge retrieval
    results, § Environment, and request-specific suffix.
    These CAN change between rounds within the same session.

    Step 9 (CC parity): the skill catalog lives here, not in the frozen prefix.
    It is progressive-disclosure metadata that changes when skills are added or
    distilled; keeping it out of the cached prefix preserves the prompt-cache
    boundary (CC ships its catalog as a dynamic system-reminder for the same
    reason).
    """
    from app.runtime.prompt_sections import (
        build_environment_section,
        build_knowledge_section,
        build_memory_section,
        build_scenario_section,
    )

    section_candidates: list[ContextSectionCandidate] = []

    def add_candidate(
        *,
        candidate_id: str,
        kind: str,
        name: str,
        content: str,
        budget_key: str | None = None,
        budget_chars: int | None = None,
        enforce_budget: bool = False,
        score: float = 1.0,
    ) -> None:
        section_candidates.append(
            ContextSectionCandidate(
                candidate_id=candidate_id,
                kind=kind,
                name=name,
                content=content,
                render_order=len(section_candidates),
                score=score,
                budget_key=budget_key,
                budget_chars=budget_chars,
                enforce_budget=enforce_budget,
            )
        )

    # § Autonomous Work — unified semantics for unattended runs (B4)
    if source in _AUTONOMOUS_SOURCES:
        add_candidate(
            candidate_id="dynamic:runtime:autonomous_work",
            kind="runtime_guidance",
            name="autonomous_work",
            content=_AUTONOMOUS_WORK_SECTION.format(source=source),
        )

    # § When to Suggest Planning First (A) — interactive surfaces only. The agent
    # may suggest Plan Mode in its reply; it never auto-enters (entry is the user's).
    from app.runtime.prompt_sections.plan_mode_guidance import (
        build_plan_mode_guidance_section,
        should_show_plan_mode_guidance,
    )

    if should_show_plan_mode_guidance(source, channel):
        add_candidate(
            candidate_id="dynamic:runtime:plan_mode_guidance",
            kind="runtime_guidance",
            name="plan_mode_guidance",
            content=build_plan_mode_guidance_section(),
        )

    memory_budget_chars = getattr(budget_profile, "memory_budget_chars", _DEFAULT_MEMORY_SNAPSHOT_BUDGET)

    # § Memory (4-layer pyramid + current query-scoped memory context) — the
    # resolver/assembler already ranks and trims to memory_budget_chars, so this
    # layer uses the same cap only as a hard safety guard for rogue callers.
    if memory_snapshot:
        snapshot_cap = max(int(memory_budget_chars), 1500)
        add_candidate(
            candidate_id="dynamic:memory:memory_snapshot",
            kind="memory",
            name="memory_snapshot",
            content=build_memory_section(memory_snapshot, budget_chars=snapshot_cap),
            budget_key="memory_budget_chars",
            budget_chars=memory_budget_chars,
        )

    if session_learning_projection:
        learning_block = _trim_block(session_learning_projection, budget_chars=1200)
        add_candidate(
            candidate_id="dynamic:memory:session_learning_projection",
            kind="memory",
            name="session_learning_projection",
            content=learning_block,
            budget_key="session_learning_projection_chars",
            budget_chars=1200,
        )

    continuity_budget = min(
        max((memory_budget_chars // 3) if budget_profile else _CONTINUITY_CHAR_BUDGET, 800),
        _CONTINUITY_CHAR_BUDGET,
    )
    if continuity_context:
        continuity_block = _trim_block(continuity_context, budget_chars=continuity_budget)
        add_candidate(
            candidate_id="dynamic:session:continuity",
            kind="session_continuity",
            name="continuity_context",
            content=f"## Session Continuity\n{continuity_block}" if continuity_block else "",
            budget_key="continuity_context_chars",
            budget_chars=continuity_budget,
        )

    runtime_budget = getattr(budget_profile, "runtime_triggers_budget_chars", 3000)
    runtime_block = _trim_block(runtime_metadata_context, budget_chars=runtime_budget) if runtime_metadata_context else ""
    add_candidate(
        candidate_id="dynamic:runtime:runtime_metadata",
        kind="runtime_metadata",
        name="runtime_metadata_context",
        content=runtime_block,
        budget_key="runtime_triggers_budget_chars",
        budget_chars=runtime_budget,
    )

    permissions_budget = min(runtime_budget, 2400)
    permissions_block = _trim_block(permissions_context, budget_chars=permissions_budget) if permissions_context else ""
    add_candidate(
        candidate_id="dynamic:permissions:permissions_context",
        kind="permissions",
        name="permissions_context",
        content=permissions_block,
        budget_key="permissions_context_chars",
        budget_chars=permissions_budget,
    )

    tool_groups_budget = (
        budget_profile.active_tool_groups_budget_chars if budget_profile else _ACTIVE_TOOL_GROUPS_CHAR_BUDGET
    )
    retrieval_budget = budget_profile.retrieval_budget_chars if budget_profile else _RETRIEVAL_CHAR_BUDGET
    scenario_section = build_scenario_section(
        budget_profile.task_profile if budget_profile else None,
        query=latest_user_query,
    )
    add_candidate(
        candidate_id="dynamic:scenario:task_profile",
        kind="scenario",
        name="task_profile",
        content=scenario_section,
    )

    tool_groups_section = _render_active_tool_groups(active_tool_groups or [], budget_chars=tool_groups_budget)
    add_candidate(
        candidate_id="dynamic:tools:active_tool_groups",
        kind="tools",
        name="active_tool_groups",
        content=tool_groups_section,
        budget_key="active_tool_groups_budget_chars",
        budget_chars=tool_groups_budget,
    )

    deferred_tools_section = ""
    if available_deferred_tools:
        from app.runtime.deferred_tools import coerce_deferred_tool_candidates

        deferred_candidates = coerce_deferred_tool_candidates(available_deferred_tools)
        if deferred_candidates:
            deferred_tools_section = _render_deferred_tool_index(
                deferred_candidates,
                budget_chars=min(tool_groups_budget, 1600),
            )
    add_candidate(
        candidate_id="dynamic:tools:available_deferred_tools",
        kind="tools",
        name="available_deferred_tools",
        content=deferred_tools_section,
        budget_key="active_tool_groups_budget_chars",
        budget_chars=min(tool_groups_budget, 1600),
    )

    # § Skills catalog (Step 9 — CC parity): progressive-disclosure index lives
    # in the dynamic suffix, next to the active tool groups it complements, so
    # adding/distilling a skill never busts the frozen prompt-cache boundary.
    skill_catalog_budget = getattr(budget_profile, "skill_catalog_budget_chars", None) if budget_profile else None
    add_candidate(
        candidate_id="dynamic:skill:skill_catalog",
        kind="skills",
        name="skill_catalog",
        content=skill_catalog,
        budget_key="skill_catalog_budget_chars",
        budget_chars=skill_catalog_budget,
        enforce_budget=True,
    )

    knowledge = build_knowledge_section(retrieval_context, budget_chars=retrieval_budget) if retrieval_context else ""
    add_candidate(
        candidate_id="dynamic:knowledge:retrieval_context",
        kind="knowledge",
        name="retrieval_context",
        content=knowledge,
        budget_key="retrieval_budget_chars",
        budget_chars=retrieval_budget,
    )

    suggested_deferred_tool_groups = (
        getattr(budget_profile.task_profile, "suggested_deferred_tool_group_names", ())
        if budget_profile
        else ()
    )
    if budget_profile and not active_tool_groups and suggested_deferred_tool_groups:
        hint_lines = [
            "## Likely Deferred Tool Groups",
            "These deferred tool groups are likely useful for the current request. Use `tool_search` to load "
            "matching schemas when the visible tools are not enough.",
        ]
        for group_name in suggested_deferred_tool_groups:
            hint_lines.append(f"- {group_name}")
        add_candidate(
            candidate_id="dynamic:tools:suggested_deferred_tool_groups",
            kind="tools",
            name="suggested_deferred_tool_groups",
            content=_trim_block("\n".join(hint_lines), budget_chars=tool_groups_budget),
            budget_key="active_tool_groups_budget_chars",
            budget_chars=tool_groups_budget,
        )

    # § Environment (user, channel, time)
    env_section = build_environment_section(
        user_name=user_name,
        channel=channel,
        agent_name=agent_name,
        include_time="## Current Time" not in runtime_metadata_context,
    )
    add_candidate(
        candidate_id="dynamic:environment:current",
        kind="environment",
        name="environment",
        content=env_section,
    )

    suffix_sections: list[str] = []
    if system_prompt_suffix:
        suffix_sections.append(system_prompt_suffix)
    suffix_sections.extend(section for section in (system_prompt_suffix_sections or []) if section)
    for idx, suffix_section in enumerate(suffix_sections):
        # P1-W2-2/A6: cap each request-specific suffix independently. Runtime
        # callers may inject multiple critical suffixes (for example delegation
        # handoff + coordinator mode); one large section must not erase another.
        add_candidate(
            candidate_id=f"dynamic:suffix:system_prompt_suffix:{idx}",
            kind="system_prompt_suffix",
            name="system_prompt_suffix",
            content=_trim_block(suffix_section, budget_chars=_SYSTEM_PROMPT_SUFFIX_CHAR_CAP),
            budget_key="system_prompt_suffix_chars",
            budget_chars=_SYSTEM_PROMPT_SUFFIX_CHAR_CAP,
        )

    parts = _select_context_section_candidates(section_candidates, context_section_ledger=context_section_ledger)
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
    because it contains per-round retrieval and runtime tool-group context).

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
