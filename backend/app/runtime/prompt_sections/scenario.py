"""Task-aware prompt addenda for high-value execution scenarios."""

from __future__ import annotations

from app.runtime.context_budget import TaskProfile

_REVIEW_HINTS = (
    "review",
    "audit",
    "verify",
    "verification",
    "inspect",
    "code review",
    "排查",
    "审查",
    "复核",
    "评审",
)


def _needs_review_overlay(query: str) -> bool:
    haystack = (query or "").lower()
    return any(hint in haystack for hint in _REVIEW_HINTS)


def build_scenario_section(
    task_profile: TaskProfile | None,
    *,
    query: str = "",
) -> str:
    """Build a compact task-specific playbook for the current request."""
    if task_profile is None:
        return ""

    lines: list[str] = ["## Task Playbook"]

    if task_profile.name == "research":
        lines.extend([
            "- Verify sources before concluding — stale or unreliable sources cause hallucination. Prefer primary sources and current documents over secondary summaries.",
            "- Use absolute dates when discussing recency, timelines, releases, or news — relative dates degrade as context compresses.",
            "- Separate confirmed facts from your own inference, and say when a point is an inference.",
            "- When multiple sources disagree, compare recency and provenance instead of averaging them.",
        ])
    elif task_profile.name == "coding":
        lines.extend([
            "- Read the relevant files before proposing changes. Keep edits scoped to the user's goal.",
            "- Verify behavior with tests, reproduction steps, or direct evidence before claiming success.",
            "- Preserve working state clearly: file paths, failing conditions, fixes, and any remaining risks.",
            "- Prefer concrete code or patches over abstract discussion when implementation is expected.",
        ])
    elif task_profile.name == "operations":
        lines.extend([
            "- Verify live state before acting. Prefer reversible checks before irreversible changes.",
            "- Minimize blast radius: make one operational change at a time and confirm its effect.",
            "- Surface rollback paths, active blockers, and observable evidence for each operational step.",
            "- Distinguish current state, intended action, and confirmed outcome explicitly.",
        ])
    elif task_profile.name == "memory_recall":
        lines.extend([
            "- Use search_memory first and prefer session transcript evidence over compressed recollection when reconstructing prior decisions.",
            "- Rebuild the answer from concrete artifacts: session transcript windows, timestamps, file paths, outputs, and explicit commitments.",
            "- Separate confirmed facts from likely reconstruction. Say when memory is partial, conflicting, or absent.",
            "- Prefer the smallest accurate recap that helps the user continue, instead of inventing continuity.",
        ])
    elif task_profile.name == "self_evolution":
        lines.extend([
            "- Promote only repeatedly successful workflows into skills. Stable inputs, steps, and outputs must be visible before using save_skill.",
            "- Use save_skill for reusable operating procedures, not one-off transcript fragments, temporary notes, or private context.",
            "- If a loaded skill keeps missing the mark, patch the existing skill instead of creating a duplicate skill.",
            "- Capture the why: when the skill should be used, what tools it depends on, and what outcome it reliably produces.",
            "- If the pattern is not yet stable, keep it in memory or working context instead of freezing it as a skill.",
        ])
    else:
        lines.extend([
            "- Keep the response evidence-driven and explicit about what is confirmed versus assumed.",
            "- Prefer the smallest sufficient action that resolves the user's request without drift.",
        ])

    if _needs_review_overlay(query):
        lines.extend([
            "",
            "### Verification / Review Overlay",
            "- Findings first. Lead with concrete issues before summaries or praise.",
            "- Order findings by severity and support each one with evidence, impact, and file references.",
            "- Separate confirmed defects from open questions or residual risk.",
            "- Do not drift into implementation unless the user explicitly asks for fixes.",
        ])

    return "\n".join(lines)
