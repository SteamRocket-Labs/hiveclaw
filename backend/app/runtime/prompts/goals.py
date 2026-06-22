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
        "Continue working toward the active session goal.\n"
        f"{_common(goal)}\n"
        "Use the existing conversation, Work Ledger, memory, tools, artifacts, and T0 evidence as the source of truth. "
        "If the goal is complete, say so clearly and do not start unrelated work."
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
