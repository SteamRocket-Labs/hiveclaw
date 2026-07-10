"""Single user-facing projection for session Goal state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.agent_session_goal import AgentSessionGoal


_RESUMABLE_STATUSES = {"paused", "blocked", "usage_limited", "budget_limited"}
_TERMINAL_STATUSES = {"complete", "cancelled"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _remaining(limit: int | None, used: int | None) -> int | None:
    if limit is None:
        return None
    return max(0, int(limit) - max(0, int(used or 0)))


def _elapsed_seconds(created_at: datetime | None, now: datetime) -> int:
    if created_at is None:
        return 0
    normalized = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return max(0, int((now - normalized).total_seconds()))


def _blocked_reason(metadata: dict[str, Any]) -> str | None:
    decision = metadata.get("last_continuation_decision")
    if isinstance(decision, dict):
        reason = str(decision.get("reason") or "").strip()
        if reason:
            return reason
    for key in ("blocked_reason", "budget_limit_reason", "terminal_reason"):
        reason = str(metadata.get(key) or "").strip()
        if reason:
            return reason
    return None


def build_session_goal_projection(
    goal: AgentSessionGoal,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project durable Goal state once for API, commands, workbench, and UI."""

    current_time = now or datetime.now(timezone.utc)
    status = str(goal.status or "active")
    metadata = dict(goal.metadata_json or {})
    time_used_seconds = _elapsed_seconds(goal.created_at, current_time)
    return {
        "id": str(goal.id),
        "agent_id": str(goal.agent_id),
        "session_id": str(goal.chat_session_id),
        "objective": goal.objective,
        "status": status,
        "token_budget": goal.token_budget,
        "tokens_used": int(goal.tokens_used or 0),
        "remaining_tokens": _remaining(goal.token_budget, goal.tokens_used),
        "time_budget_seconds": goal.time_budget_seconds,
        "time_used_seconds": time_used_seconds,
        "remaining_time_seconds": _remaining(goal.time_budget_seconds, time_used_seconds),
        "continuation_count": int(goal.continuation_count or 0),
        "max_continuation_turns": goal.max_continuation_turns,
        "remaining_continuation_turns": _remaining(goal.max_continuation_turns, goal.continuation_count),
        "blocked_count": int(goal.blocked_count or 0),
        "blocked_reason": _blocked_reason(metadata),
        "completion_summary": goal.completion_summary,
        "controls": {
            "can_pause": status == "active",
            "can_resume": status in _RESUMABLE_STATUSES,
            "can_stop": status not in _TERMINAL_STATUSES,
        },
        "created_at": _iso(goal.created_at),
        "updated_at": _iso(goal.updated_at),
        "completed_at": _iso(goal.completed_at),
    }
