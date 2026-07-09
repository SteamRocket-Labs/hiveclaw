"""Provider-neutral session goal prompt templates."""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape


@dataclass(frozen=True, slots=True)
class ThreadGoalPromptState:
    objective: str
    tokens_used: int = 0
    token_budget: int | None = None
    time_used_seconds: int = 0

    @property
    def remaining_tokens(self) -> str:
        if self.token_budget is None:
            return "unbounded"
        return str(max(0, int(self.token_budget) - max(0, int(self.tokens_used))))

    @property
    def token_budget_text(self) -> str:
        return "none" if self.token_budget is None else str(self.token_budget)


def _xml(value: str) -> str:
    return escape(value or "")


def _common(goal: ThreadGoalPromptState) -> str:
    return (
        "<session_goal>\n"
        f"<objective>{_xml(goal.objective)}</objective>\n"
        f"<tokens_used>{max(0, int(goal.tokens_used))}</tokens_used>\n"
        f"<token_budget>{goal.token_budget_text}</token_budget>\n"
        f"<remaining_tokens>{goal.remaining_tokens}</remaining_tokens>\n"
        "</session_goal>"
    )


def continuation_prompt(goal: ThreadGoalPromptState) -> str:
    return (
        "Continue working toward the active session goal below. Ground every step in the existing conversation, "
        "Work Ledger, memory, tools, artifacts, and T0 evidence — that is the source of truth.\n"
        f"{_common(goal)}\n"
        "Before you act this turn, run these three audit gates in order:\n"
        "\n"
        "1. Completion audit — Decide whether the goal is already met, using verifiable evidence "
        "(artifacts written this session, passing tests/checks, Work Ledger entries), not optimism. "
        'If it is met, call update_goal(status="complete", summary=...) with a concise evidence-backed '
        "summary and stop. Do NOT keep exploring just because budget remains; remaining budget is a ceiling, "
        "not a quota to spend.\n"
        "2. Blocked audit — Decide whether you are stuck: repeated failures on the same step, a missing "
        'permission or credential, or a decision only the user can make. If so, call update_goal(status="blocked") '
        "or state plainly what you are waiting for, then stop instead of thrashing.\n"
        "3. Fidelity audit — Re-read the <objective> and confirm the work you are about to do is strictly inside "
        "its scope. Do not drift into adjacent or newly interesting tasks; anything outside the objective is out "
        "of scope. If the objective itself no longer fits reality, call update_goal(objective=...) rather than "
        "silently redefining it.\n"
        "\n"
        "If none of the gates fire, take the single most valuable next step toward the objective and continue."
    )


def budget_limit_prompt(goal: ThreadGoalPromptState) -> str:
    return (
        "The active session goal has reached its token budget. Wrap up with a concise status and remaining risks.\n"
        f"{_common(goal)}\n"
        f"<time_used_seconds>{max(0, int(goal.time_used_seconds))}</time_used_seconds>"
    )


def objective_updated_prompt(goal: ThreadGoalPromptState) -> str:
    return (
        "The active session goal was updated by the user. Re-orient to the latest objective before continuing.\n"
        f"{_common(goal)}"
    )
