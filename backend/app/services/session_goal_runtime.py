"""Session-scoped Goal continuation runtime primitives.

This is a Codex-inspired, session-local mechanism. It is intentionally separate
from the retired organization-level objective subsystem and from Work Ledger.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.decision_ledger import build_agent_cycle_decision_entry


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    USAGE_LIMITED = "usage_limited"
    BUDGET_LIMITED = "budget_limited"
    CANCELLED = "cancelled"


# Transient terminal errors that should be retried a bounded number of times
# before the goal is declared Blocked (A6, mirrors Codex turn_error tolerance).
# Unlike loop_guard / clarification / user_cancel these are not immediate stops;
# the continuation caller counts consecutive occurrences via blocked_count.
RETRIABLE_TERMINAL_REASONS: frozenset[str] = frozenset({"provider_error", "turn_abort", "persistence_error"})


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


class GoalDecisionEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="hive.ccplus.goal_decision.v1", alias="schema")
    previous_terminal_reason: str | None = None
    progress_evidence: list[str] = Field(default_factory=list)
    continue_reason: str | None = None
    stop_reason: str | None = None
    status_transition: dict[str, str | None]
    user_visible_next_action: str
    agent_cycle_decision_entry: dict[str, Any] = Field(default_factory=dict)


def _previous_terminal_decision(previous_terminal_reason: str | None) -> GoalContinuationDecision | None:
    reason = str(previous_terminal_reason or "").strip()
    if not reason or reason == "turn_stop":
        return None
    if reason == "tool_budget":
        return GoalContinuationDecision(
            continue_goal=False,
            reason="previous turn reached tool budget",
            next_status=GoalStatus.USAGE_LIMITED,
        )
    if reason == "loop_guard":
        return GoalContinuationDecision(
            continue_goal=False,
            reason="previous turn was blocked by loop guard",
            next_status=GoalStatus.BLOCKED,
        )
    if reason == "clarification_required":
        return GoalContinuationDecision(
            continue_goal=False,
            reason="previous turn requires user clarification",
            next_status=GoalStatus.PAUSED,
        )
    if reason == "user_cancel":
        return GoalContinuationDecision(
            continue_goal=False,
            reason="previous turn was cancelled by user",
            next_status=GoalStatus.CANCELLED,
        )
    # RETRIABLE_TERMINAL_REASONS (provider_error/turn_abort/persistence_error) fall
    # through to None on purpose: continue_session_goal increments blocked_count and
    # decides retry-vs-Blocked with a bounded threshold instead of stopping here.
    return None


def _user_visible_next_action(decision: GoalContinuationDecision) -> str:
    if decision.continue_goal:
        return "continuation_scheduled"
    if decision.next_status == GoalStatus.BUDGET_LIMITED:
        return "show_budget_limit_prompt"
    if decision.next_status == GoalStatus.USAGE_LIMITED:
        return "ask_user_to_continue"
    if decision.next_status == GoalStatus.BLOCKED:
        return "show_blocked_reason"
    if decision.next_status == GoalStatus.PAUSED:
        return "wait_for_user_input"
    if decision.next_status == GoalStatus.CANCELLED:
        return "stopped_by_user"
    return "no_action"


def build_goal_decision_entry(
    goal: SessionGoal,
    decision: GoalContinuationDecision,
    *,
    previous_terminal_reason: str | None = None,
    progress_evidence: list[str] | None = None,
) -> GoalDecisionEntry:
    next_status = decision.next_status or goal.status
    user_visible_next_action = _user_visible_next_action(decision)
    return GoalDecisionEntry(
        previous_terminal_reason=previous_terminal_reason,
        progress_evidence=list(progress_evidence or []),
        continue_reason=decision.reason if decision.continue_goal else None,
        stop_reason=None if decision.continue_goal else decision.reason,
        status_transition={"from": goal.status.value, "to": next_status.value},
        user_visible_next_action=user_visible_next_action,
        agent_cycle_decision_entry=build_agent_cycle_decision_entry(
            subsystem="goal",
            trigger=decision.trigger,
            judge="session_goal_runtime.should_continue_goal",
            decision="continue" if decision.continue_goal else "stop",
            outcome=next_status.value,
            next_action=user_visible_next_action,
            model_interaction="start_goal_continuation_run" if decision.continue_goal else "none",
            user_visible=user_visible_next_action != "no_action",
            permission_result="inherits_web_chat_runtime",
            budget_result="limited"
            if next_status in {GoalStatus.BUDGET_LIMITED, GoalStatus.USAGE_LIMITED}
            else "within_budget",
            details={
                "previous_terminal_reason": previous_terminal_reason,
                "progress_evidence": list(progress_evidence or []),
                "reason": decision.reason,
            },
        ),
    )


def should_continue_goal(
    goal: SessionGoal,
    *,
    plan_mode: bool,
    pending_user_input: bool,
    active_run_exists: bool,
    ephemeral: bool = False,
    previous_terminal_reason: str | None = None,
) -> GoalContinuationDecision:
    previous_decision = _previous_terminal_decision(previous_terminal_reason)
    if previous_decision is not None:
        return previous_decision
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
