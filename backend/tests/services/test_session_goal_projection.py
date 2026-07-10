from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.agent_session_goal import AgentSessionGoal
from app.services.session_goal_projection import build_session_goal_projection


def test_session_goal_projection_exposes_semantic_progress_and_controls():
    now = datetime.now(timezone.utc)
    goal = AgentSessionGoal(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Finish the launch report",
        status="blocked",
        token_budget=1_000,
        tokens_used=400,
        time_budget_seconds=3_600,
        continuation_count=2,
        max_continuation_turns=5,
        blocked_count=3,
        metadata_json={
            "last_continuation_decision": {
                "reason": "provider timed out three times",
            },
        },
    )
    goal.created_at = now - timedelta(seconds=600)
    goal.updated_at = now

    projection = build_session_goal_projection(goal, now=now)

    assert projection["remaining_tokens"] == 600
    assert projection["remaining_continuation_turns"] == 3
    assert projection["time_used_seconds"] == 600
    assert projection["remaining_time_seconds"] == 3_000
    assert projection["blocked_reason"] == "provider timed out three times"
    assert projection["controls"] == {"can_pause": False, "can_resume": True, "can_stop": True}


def test_session_goal_projection_clamps_exhausted_budgets():
    goal = AgentSessionGoal(
        id=uuid4(),
        agent_id=uuid4(),
        chat_session_id=uuid4(),
        objective="Finish",
        status="budget_limited",
        token_budget=100,
        tokens_used=150,
        continuation_count=4,
        max_continuation_turns=3,
    )

    projection = build_session_goal_projection(goal)

    assert projection["remaining_tokens"] == 0
    assert projection["remaining_continuation_turns"] == 0
    assert projection["controls"]["can_resume"] is True
