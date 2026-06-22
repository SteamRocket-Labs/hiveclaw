"""Session-scoped Goal continuation runtime primitives.

This is a Codex-inspired, session-local mechanism. It is intentionally separate
from the retired organization-level objective subsystem and from Work Ledger.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    USAGE_LIMITED = "usage_limited"
    BUDGET_LIMITED = "budget_limited"
    CANCELLED = "cancelled"


class SessionGoal(BaseModel):
    id: UUID
    tenant_id: UUID | None = None
    agent_id: UUID
    chat_session_id: UUID
    objective: str
    status: GoalStatus = GoalStatus.ACTIVE
    token_budget: int | None = None
    tokens_used: int = 0
    time_budget_seconds: int | None = None
    continuation_count: int = 0
    max_continuation_turns: int | None = None
    blocked_count: int = 0
    metadata: dict = Field(default_factory=dict)


class GoalContinuationDecision(BaseModel):
    continue_goal: bool
    trigger: str = "turn_complete"
    reason: str
    next_status: GoalStatus | None = None


def should_continue_goal(
    goal: SessionGoal,
    *,
    plan_mode: bool,
    pending_user_input: bool,
    active_run_exists: bool,
    ephemeral: bool = False,
) -> GoalContinuationDecision:
    if goal.status != GoalStatus.ACTIVE:
        return GoalContinuationDecision(continue_goal=False, reason=f"goal status is {goal.status}")
    if plan_mode:
        return GoalContinuationDecision(continue_goal=False, reason="plan mode disables goal continuation")
    if ephemeral:
        return GoalContinuationDecision(continue_goal=False, reason="ephemeral sessions do not continue goals")
    if pending_user_input:
        return GoalContinuationDecision(continue_goal=False, reason="pending user input")
    if active_run_exists:
        return GoalContinuationDecision(continue_goal=False, reason="active run already exists")
    if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
        return GoalContinuationDecision(
            continue_goal=False,
            reason="token budget exhausted",
            next_status=GoalStatus.BUDGET_LIMITED,
        )
    if goal.max_continuation_turns is not None and goal.continuation_count >= goal.max_continuation_turns:
        return GoalContinuationDecision(continue_goal=False, reason="continuation turn cap reached")
    return GoalContinuationDecision(continue_goal=True, reason="active goal may continue")


def account_goal_tokens(goal: SessionGoal, tokens: int) -> SessionGoal:
    tokens_used = max(0, goal.tokens_used + max(0, tokens))
    status = goal.status
    if goal.token_budget is not None and tokens_used >= goal.token_budget and status == GoalStatus.ACTIVE:
        status = GoalStatus.BUDGET_LIMITED
    return goal.model_copy(update={"tokens_used": tokens_used, "status": status})


def mark_goal_blocked_if_repeated(goal: SessionGoal, *, threshold: int = 3) -> SessionGoal:
    blocked_count = goal.blocked_count + 1
    status = GoalStatus.BLOCKED if blocked_count >= threshold else goal.status
    return goal.model_copy(update={"blocked_count": blocked_count, "status": status})
