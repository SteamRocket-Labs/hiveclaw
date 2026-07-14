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
# Per-section sizes below are advisory telemetry labels. Semantic source bytes
# are never cut here; the final provider-sized assembly is the capacity gate.
_DEFAULT_MEMORY_SNAPSHOT_BUDGET = 8000
_SYSTEM_PROMPT_SUFFIX_ADVISORY_CHARS = 5000

# Frozen-prefix cache-economics telemetry.
# Advisory thresholds are calibrated for long-context production agents: 16K frozen
# tokens is still a small slice of a 256K context, while 12K gives operators
# enough headroom to see static prefix growth before it hurts cache efficiency.
_FROZEN_PREFIX_TOKEN_WARN = 12000
_FROZEN_PREFIX_TOKEN_ADVISORY = 16000
_FROZEN_PREFIX_SECTION_RE = re.compile(r"(?m)^#{2,3}\s+(.+?)\s*$")
_FROZEN_PREFIX_TOP_SECTION_LIMIT = 6


class PromptBudgetExceededError(ValueError):
    """Selected prompt sections cannot fit without violating their contracts."""

    def __init__(self, *, budget_chars: int, required_chars: int, frozen_prefix: str, dynamic_suffix: str) -> None:
        self.budget_chars = budget_chars
        self.required_chars = required_chars
        self.frozen_sha256 = hashlib.sha256(frozen_prefix.encode("utf-8")).hexdigest()
        self.dynamic_sha256 = hashlib.sha256(dynamic_suffix.encode("utf-8")).hexdigest()
        super().__init__(
            "immutable frozen prompt contract and selected dynamic sections exceed the model prompt budget; "
            f"required={required_chars} budget={budget_chars}. Refusing blind truncation."
        )


@dataclass(frozen=True, slots=True)
class FrozenPrefixSection:
    name: str
    chars: int
    tokens: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class ContextSectionCandidate:
    candidate_id: str
    kind: str
    name: str
    content: str
    render_order: int
    score: float = 1.0
    source_ref: str = "runtime.prompt_builder"
    reason: str = "context_section_present"
    budget_key: str | None = None
    budget_chars: int | None = None
    source_payload: str | None = None


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
    source_payload = candidate.source_payload if candidate.source_payload is not None else candidate.content
    source_chars = len(source_payload or "")
    rendered_chars = len(rendered_content or "")
    candidate_ref = build_context_candidate_ref(
        kind=candidate.kind,
        item_id=candidate.name,
        version="dynamic_section",
        payload=source_payload,
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
            "source_ref": candidate.source_ref,
            "reason": candidate.reason,
            "selected": bool(rendered_content),
            "decision": decision,
            "budget_key": candidate.budget_key,
            "budget_chars": candidate.budget_chars,
            "budget_enforced": False,
            "source_chars": source_chars,
            "source_tokens": estimate_tokens_from_text(source_payload or ""),
            "rendered_chars": rendered_chars,
            "rendered_tokens": estimate_tokens_from_text(rendered_content or ""),
            "source_hash": _context_section_source_hash(source_payload or ""),
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
            decision = "selected_over_advisory_budget"

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
    """Compatibility renderer; section budgets are advisory, never semantic selectors."""
    del budget_chars
    return text.strip()


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
                tokens=estimate_tokens_from_text(prefix),
                content_hash=hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
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
                content_hash=hashlib.sha256(preamble.encode("utf-8")).hexdigest(),
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
                content_hash=hashlib.sha256(block.encode("utf-8")).hexdigest(),
            )
        )

    return sections


def build_frozen_context_dependency_manifest(prefix: str) -> dict[str, Any]:
    """Build the deterministic cache/evidence manifest from what the model sees.

    The rendered frozen prefix is the only complete dependency closure: every
    file, DB read model, policy-facing section, and fallback that actually
    reached the model is represented in these bytes. Deriving the key from
    optional caller-supplied signatures can silently omit a dependency, so
    callers must rebuild once per turn and verify this manifest before reuse.
    """
    sections = _measure_frozen_prefix_sections(prefix)
    return {
        "schema": "hive.frozen_context_dependency_manifest.v1",
        "complete": True,
        "root_hash": hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        "chars": len(prefix),
        "sections": [
            {
                "name": section.name,
                "chars": section.chars,
                "tokens": section.tokens,
                "content_hash": section.content_hash,
            }
            for section in sections
        ],
    }


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

    Every build is metered for cache/cost observability. Cache economics are
    never allowed to trim identity or context; the final provider-sized prompt
    assembly is the only capacity gate and fails loudly when the complete
    contract cannot fit.

    Args:
        context_window_tokens: Retained for API compatibility and metering.
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

    _meter_frozen_prefix(prefix)
    return prefix


def _enforce_frozen_prefix_budget(
    base_parts: list[str],
    skill_catalog: str,
    *,
    char_limit: int | None = None,
) -> str:
    """Compatibility helper that preserves every prefix byte.

    ``char_limit`` is intentionally ignored: it represented a cache-economics
    target, not a provider resource boundary. Callers must use
    ``assemble_runtime_prompt`` for the real, fail-loud capacity check.
    """

    del char_limit
    parts = [*base_parts]
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(parts)


def _meter_frozen_prefix(prefix: str) -> None:
    """Sample the frozen prefix size and bump warn/overrun counters.

    Isolated for testability — callers don't need a logger fixture.
    """
    import logging

    chars = len(prefix)
    tokens = estimate_tokens_from_text(prefix)
    warn = tokens >= _FROZEN_PREFIX_TOKEN_WARN
    overrun = tokens > _FROZEN_PREFIX_TOKEN_ADVISORY
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
        "advisory_threshold": _FROZEN_PREFIX_TOKEN_ADVISORY,
        "section_tokens": section_tokens,
        "section_chars": section_chars,
        "top_sections": top_sections,
    }
    if overrun:
        logger.warning(
            "[PromptBuilder] frozen prefix above cache advisory threshold: ~%d tokens (chars=%d, advisory=%d) — "
            "prompt cache hit-rate will degrade and per-call cost will rise. "
            "Preserve semantic bytes; use the final provider budget gate or model-led compaction. top_sections=%s",
            tokens,
            chars,
            _FROZEN_PREFIX_TOKEN_ADVISORY,
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
            _FROZEN_PREFIX_TOKEN_ADVISORY,
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
    del budget_chars  # Compatibility-only: complete discovery metadata is part of the model contract.
    header = [
        "## Available Deferred Tools",
        "These tools are not loaded yet. To load exactly one schema, call `tool_search` with `select:<tool_name>`.",
    ]
    lines = list(header)
    for candidate in deferred_candidates:
        details = (
            f"group={candidate.group}; risk={candidate.risk}; "
            f"schema_tokens={candidate.schema_token_cost}; reason={candidate.reason}"
        )
        line = f"- {candidate.name} — `{candidate.selector}` ({details})"
        lines.append(line)
    return "\n".join(lines)


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
        score: float = 1.0,
        source_ref: str = "runtime.prompt_builder",
        reason: str = "context_section_present",
        source_payload: str | None = None,
    ) -> None:
        section_candidates.append(
            ContextSectionCandidate(
                candidate_id=candidate_id,
                kind=kind,
                name=name,
                content=content,
                render_order=len(section_candidates),
                score=score,
                source_ref=source_ref,
                reason=reason,
                budget_key=budget_key,
                budget_chars=budget_chars,
                source_payload=source_payload,
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

    # § Memory (4-layer pyramid + current query-scoped memory context). The
    # supplied budget is recorded for observability; these bytes remain intact.
    if memory_snapshot:
        add_candidate(
            candidate_id="dynamic:memory:memory_snapshot",
            kind="memory",
            name="memory_snapshot",
            content=build_memory_section(memory_snapshot, budget_chars=None),
            budget_key="memory_budget_chars",
            budget_chars=memory_budget_chars,
            source_payload=memory_snapshot,
        )

    if session_learning_projection:
        add_candidate(
            candidate_id="dynamic:memory:session_learning_projection",
            kind="memory",
            name="session_learning_projection",
            content=session_learning_projection,
            budget_key="session_learning_projection_chars",
            budget_chars=1200,
        )

    continuity_budget = min(
        max((memory_budget_chars // 3) if budget_profile else _CONTINUITY_CHAR_BUDGET, 800),
        _CONTINUITY_CHAR_BUDGET,
    )
    if continuity_context:
        add_candidate(
            candidate_id="dynamic:session:continuity",
            kind="session_continuity",
            name="continuity_context",
            content=f"## Session Continuity\n{continuity_context}",
            budget_key="continuity_context_chars",
            budget_chars=continuity_budget,
            source_payload=continuity_context,
        )

    runtime_budget = getattr(budget_profile, "runtime_triggers_budget_chars", 3000)
    add_candidate(
        candidate_id="dynamic:runtime:runtime_metadata",
        kind="runtime_metadata",
        name="runtime_metadata_context",
        content=runtime_metadata_context,
        budget_key="runtime_triggers_budget_chars",
        budget_chars=runtime_budget,
    )

    permissions_budget = min(runtime_budget, 2400)
    add_candidate(
        candidate_id="dynamic:permissions:permissions_context",
        kind="permissions",
        name="permissions_context",
        content=permissions_context,
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
    )

    knowledge = build_knowledge_section(retrieval_context, budget_chars=None) if retrieval_context else ""
    add_candidate(
        candidate_id="dynamic:knowledge:retrieval_context",
        kind="knowledge",
        name="retrieval_context",
        content=knowledge,
        budget_key="retrieval_budget_chars",
        budget_chars=retrieval_budget,
        source_payload=retrieval_context,
    )

    suggested_deferred_tool_groups = (
        getattr(budget_profile.task_profile, "suggested_deferred_tool_group_names", ()) if budget_profile else ()
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
        # Record each request-specific suffix independently so an oversized
        # caller is attributable without erasing another critical section.
        is_hook_context = suffix_section.lstrip().startswith("## Hook Additional Context")
        add_candidate(
            candidate_id=(
                f"dynamic:hook:user_prompt_submit:{idx}"
                if is_hook_context
                else f"dynamic:suffix:system_prompt_suffix:{idx}"
            ),
            kind="hook_context" if is_hook_context else "system_prompt_suffix",
            name="user_prompt_submit" if is_hook_context else "system_prompt_suffix",
            content=suffix_section,
            budget_key="hook_context_chars" if is_hook_context else "system_prompt_suffix_chars",
            budget_chars=_SYSTEM_PROMPT_SUFFIX_ADVISORY_CHARS,
            source_ref="hook:user_prompt_submit" if is_hook_context else "runtime.system_prompt_suffix",
            reason="hook_additional_context" if is_hook_context else "system_prompt_suffix_present",
            source_payload=suffix_section,
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
    """Combine already-budgeted frozen and dynamic prompt sections.

    This final layer is not a second semantic selection authority. Upstream
    section ledgers must inline or defer recoverable material first; if their
    selected result still cannot fit, fail loudly rather than byte-slicing an
    immutable contract or an unpersisted dynamic section.

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
            "[PromptBuilder] System prompt exceeds budget: %d chars (budget=%d, ctx_window=%s, overshoot=%d) — refusing blind truncation",
            len(prompt),
            budget,
            context_window_tokens or "default",
            overshoot,
        )
        raise PromptBudgetExceededError(
            budget_chars=budget,
            required_chars=len(prompt),
            frozen_prefix=frozen_prefix,
            dynamic_suffix=dynamic_suffix,
        )
    return prompt
