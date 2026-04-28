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
    lines.append(
        f"_Inferred profile: **{task_profile.name}** (complexity={task_profile.complexity}). "
        "If this does not match the actual work, ignore this playbook and follow the user's "
        "direct request instead — inference can be wrong._"
    )

    if task_profile.name == "research":
        lines.extend(
            [
                "",
                "<research_playbook>",
                "- Verify sources before concluding — stale or unreliable sources cause hallucination. Prefer primary sources and current documents over secondary summaries.",
                "- Use absolute dates when discussing recency, timelines, releases, or news — relative dates degrade as context compresses.",
                "- Separate confirmed facts from your own inference, and say when a point is an inference.",
                "- When multiple sources disagree, compare recency and provenance instead of averaging them.",
                "",
                "**Good**: `Per ModelVendor's 2026-03-12 release notes, Model X supports 1M-token context (primary source: vendor.example/news). Inference: likely replaces the previous long-context model for this workflow.`",
                "**Bad**: `Model X probably supports longer context.` (no source, no date, speculation unmarked)",
                "</research_playbook>",
            ]
        )
    elif task_profile.name == "coding":
        lines.extend(
            [
                "",
                "<coding_playbook>",
                "- Read the relevant files before proposing changes. Keep edits scoped to the user's goal.",
                "- Verify behavior with tests, reproduction steps, or direct evidence before claiming success.",
                "- Preserve working state clearly: file paths, failing conditions, fixes, and any remaining risks.",
                "- Prefer concrete code or patches over abstract discussion when implementation is expected.",
                "",
                "**Good**: `Read middleware.py:138-148, reordered refresh check, ran pytest tests/auth → 24 passed.`",
                "**Bad**: `I think the fix is to move the refresh check. Should work.` (no read, no test, no evidence)",
                "</coding_playbook>",
            ]
        )
    elif task_profile.name == "operations":
        lines.extend(
            [
                "",
                "<operations_playbook>",
                "- Verify live state before acting. Prefer reversible checks before irreversible changes.",
                "- Minimize blast radius: make one operational change at a time and confirm its effect.",
                "- Surface rollback paths, active blockers, and observable evidence for each operational step.",
                "- Distinguish current state, intended action, and confirmed outcome explicitly.",
                "",
                "**Good**: `Current: trigger queue has 142 stuck items. Plan: pause scheduler → drain one batch → verify count → resume. Rollback: redis-cli SET scheduler:paused 0. Starting with pause now.`",
                "**Bad**: `Fixing the trigger queue backlog.` (no pre-state, no rollback path, no step granularity)",
                "</operations_playbook>",
            ]
        )
    elif task_profile.name == "memory_recall":
        lines.extend(
            [
                "",
                "<memory_recall_playbook>",
                "- Use search_memory first and prefer session transcript evidence over compressed recollection when reconstructing prior decisions.",
                "- Rebuild the answer from concrete artifacts: session transcript windows, timestamps, file paths, outputs, and explicit commitments.",
                "- Separate confirmed facts from likely reconstruction. Say when memory is partial, conflicting, or absent.",
                "- Prefer the smallest accurate recap that helps the user continue, instead of inventing continuity.",
                "",
                "**Good**: `search_memory returned 3 hits for 'auth token'. Transcript 2026-04-09 14:22 shows: fixed middleware.py:142, 24 tests passed. No later contradicting entry.`",
                "**Bad**: `We probably handled the auth issue a while back and it's working now.` (no search, no transcript, invented continuity)",
                "</memory_recall_playbook>",
            ]
        )
    elif task_profile.name == "self_evolution":
        lines.extend(
            [
                "",
                "<self_evolution_playbook>",
                "- Promote only repeatedly successful workflows into skills. Stable inputs, steps, and outputs must be visible before using save_skill.",
                "- Use save_skill for reusable operating procedures, not one-off transcript fragments, temporary notes, or private context.",
                "- If a loaded skill keeps missing the mark, patch the existing skill instead of creating a duplicate skill.",
                "- Capture the why: when the skill should be used, what tools it depends on, and what outcome it reliably produces.",
                "- If the pattern is not yet stable, keep it in memory or working context instead of freezing it as a skill.",
                "",
                "**Good**: `Workflow 'web_search → web_fetch → write workspace file' succeeded 4× this week across different queries → save_skill 'research-brief' with that tool chain.`",
                "**Bad**: `User asked one research question today → save_skill 'answer-questions'.` (single instance; generic; no stable shape)",
                "</self_evolution_playbook>",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "<default_playbook>",
                "- Keep the response evidence-driven and explicit about what is confirmed versus assumed.",
                "- Prefer the smallest sufficient action that resolves the user's request without drift.",
                "</default_playbook>",
            ]
        )

    if _needs_review_overlay(query):
        lines.extend(
            [
                "",
                "### Verification / Review Overlay",
                "<review_overlay>",
                "- Findings first. Lead with concrete issues before summaries or praise.",
                "- Order findings by severity and support each one with evidence, impact, and file references.",
                "- Separate confirmed defects from open questions or residual risk.",
                "- Do not drift into implementation unless the user explicitly asks for fixes.",
                "",
                "**Good**: `P0: middleware.py:142 swallows expired-token error (see test failure). P1: refresh.py:87 bare except. Open question: is token_store.py:55 TTL a defect?`",
                "**Bad**: `The code looks good overall! Here are some suggestions…` (no severity, no evidence, implementation drift)",
                "</review_overlay>",
            ]
        )

    return "\n".join(lines)
