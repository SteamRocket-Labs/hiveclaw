"""Session Goal continuation service.

Goal continuation is a session-local control loop. It schedules another normal
chat-runtime invocation with a distinct task type; it does not bypass transcript
T0, tool governance, or web-chat runtime accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.session_goal_runtime import GoalStatus, SessionGoal, should_continue_goal
from app.services.web_chat_runtime import start_web_chat_run


def _goal_to_runtime_model(goal: AgentSessionGoal) -> SessionGoal:
    return SessionGoal(
        id=goal.id,
        tenant_id=goal.tenant_id,
        agent_id=goal.agent_id,
        chat_session_id=goal.chat_session_id,
        objective=goal.objective,
        status=GoalStatus(str(goal.status or GoalStatus.ACTIVE)),
        token_budget=goal.token_budget,
        tokens_used=goal.tokens_used or 0,
        time_budget_seconds=goal.time_budget_seconds,
        continuation_count=goal.continuation_count or 0,
        max_continuation_turns=goal.max_continuation_turns,
        blocked_count=goal.blocked_count or 0,
        metadata=dict(goal.metadata_json or {}),
    )


def _continuation_prompt(goal: AgentSessionGoal) -> str:
    return (
        "Continue working toward the active session goal.\n\n"
        f"Goal: {goal.objective}\n\n"
        "Use the existing conversation, Work Ledger, memory, tools, and artifacts as the source of truth. "
        "If you are complete, say so clearly and do not start unrelated work."
    )


async def continue_session_goal(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    goal: AgentSessionGoal,
    plan_mode: bool = False,
    pending_user_input: bool = False,
    active_run_exists: bool = False,
    ephemeral: bool = False,
) -> dict[str, Any]:
    runtime_goal = _goal_to_runtime_model(goal)
    decision = should_continue_goal(
        runtime_goal,
        plan_mode=plan_mode,
        pending_user_input=pending_user_input,
        active_run_exists=active_run_exists,
        ephemeral=ephemeral,
    )
    decision_payload = decision.model_dump(mode="json")
    metadata = dict(goal.metadata_json or {})
    metadata["last_continuation_decision"] = {
        **decision_payload,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    if not decision.continue_goal:
        if decision.next_status is not None:
            goal.status = decision.next_status.value
        goal.metadata_json = metadata
        await db.flush()
        return {"ok": False, "goal_id": str(goal.id), "decision": decision_payload}

    prompt = _continuation_prompt(goal)
    run = await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=prompt,
        display_content="",
        file_name="",
        append_user_message=False,
        runtime_task_type="goal_continuation",
        extra_metadata={
            "source": "goal_continuation",
            "goal_id": str(goal.id),
            "goal_objective": goal.objective,
            "continuation_count_before": goal.continuation_count or 0,
        },
    )

    goal.continuation_count = (goal.continuation_count or 0) + 1
    metadata.update(
        {
            "last_continuation_run_id": run.get("run_id"),
            "last_continuation_started_at": datetime.now(timezone.utc).isoformat(),
            "last_continuation_prompt": prompt,
        }
    )
    goal.metadata_json = metadata
    await db.flush()
    return {"ok": True, "goal_id": str(goal.id), "decision": decision_payload, "run": run}
