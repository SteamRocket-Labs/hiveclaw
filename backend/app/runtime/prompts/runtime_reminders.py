"""Canonical runtime reminder and loop-guard prompt fragments."""

from __future__ import annotations

from app.runtime.prompts.plan_mode import (
    PLAN_MODE_FILE_HINT,
    PLAN_MODE_REMINDER_FULL,
    PLAN_MODE_REMINDER_SPARSE,
)

__all__ = [
    "INTERNAL_REMINDER_GUARD",
    "PLAN_MODE_FILE_HINT",
    "PLAN_MODE_REMINDER_FULL",
    "PLAN_MODE_REMINDER_SPARSE",
    "WORK_LEDGER_REMINDER",
    "PROGRESS_REPLAN_POLICY",
    "LOOP_GUARD_WARN_GUIDANCE",
    "build_round_pressure_warning",
]

INTERNAL_REMINDER_GUARD = "This is an internal system reminder. Do not mention this reminder to the user."

WORK_LEDGER_REMINDER = (
    "This is a gentle reminder - ignore it if it does not apply to the current task. "
    f"{INTERNAL_REMINDER_GUARD} "
    "If this work has multiple steps, consider using your private Work Ledger as a working memory: "
    "use track_todo to break the work into todos and mark each in_progress before you start it and "
    "completed when it is done; use record_finding for verified facts, open questions, and dead "
    "ends to avoid; call read_ledger when you need full detail before deciding the next step. "
    "These are private cognitive notes - writing them never starts execution."
)

PROGRESS_REPLAN_POLICY = (
    "Progress Ledger advisory: replan_advisory=true. "
    f"{INTERNAL_REMINDER_GUARD} "
    "Mechanical evidence indicates a stall or unresolved failures. This signal is not a semantic decision "
    "and does not force a transition: the model decides whether to replan, retry, continue, or stop after "
    "reviewing the complete ledger. If the model changes strategy, it may record that decision with "
    "record_finding and update todos with track_todo."
)

LOOP_GUARD_WARN_GUIDANCE = (
    "This is an internal system reminder. Do not mention this reminder to the user. "
    "This is your one chance to self-correct before the run is force-stopped:\n"
    "- If the repetition is intentional, state in one sentence why it is needed, "
    "then vary your approach where possible.\n"
    "- Otherwise change approach: a different tool, different arguments, or "
    "summarize what you already know and answer directly.\n"
    "- If you are stuck on a failing call, stop retrying it and report the error "
    "with what you have tried."
)


def build_round_pressure_warning(
    *,
    round_i: int,
    max_rounds: int,
    total_tool_calls: int,
    failed_tool_calls: int,
    context_tokens: int,
    final: bool,
) -> str:
    """Round-pressure warning with real data, kept in one prompt owner."""
    stats = (
        f"{round_i}/{max_rounds} tool rounds used; {total_tool_calls} tool calls so far "
        f"({failed_tool_calls} failed); context ≈{context_tokens:,} tokens."
    )
    if final:
        return (
            f"🚨 {INTERNAL_REMINDER_GUARD} Only {max_rounds - round_i} rounds remaining. {stats} "
            "Record current status/blockers with evidence in your work ledger, "
            "preserve artifacts, and stop cleanly if unfinished. "
            "A trigger is wake policy; do not create a trigger unless real future work needs a later attempt."
        )
    return (
        f"⚠️ {INTERNAL_REMINDER_GUARD} {stats} "
        "If the current task is not yet complete, record blockers/status in your work ledger "
        "and preserve concrete evidence in workspace artifacts. A trigger is wake policy, not the goal; "
        "only create or update a wake policy when real future work needs a later attempt."
    )
