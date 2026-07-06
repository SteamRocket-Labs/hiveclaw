"""Session Goal continuation service.

Goal continuation is a session-local control loop. It schedules another normal
chat-runtime invocation with a distinct task type; it does not bypass transcript
T0, tool governance, or web-chat runtime accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.prompts.goals import ThreadGoalPromptState, budget_limit_prompt, continuation_prompt
from app.services.session_goal_runtime import GoalStatus, SessionGoal, build_goal_decision_entry, should_continue_goal
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
    return continuation_prompt(
        ThreadGoalPromptState(
            objective=goal.objective,
            tokens_used=goal.tokens_used or 0,
            token_budget=goal.token_budget,
            time_used_seconds=0,
        )
    )


def _progress_evidence_from_metadata(metadata: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    terminal_reason = str(metadata.get("terminal_reason") or "").strip()
    if terminal_reason:
        evidence.append(f"terminal_reason:{terminal_reason}")
    for key, prefix in (
        ("artifact_ids", "artifact"),
        ("artifact_paths", "artifact_path"),
        ("file_change_paths", "file_change"),
        ("declared_artifact_paths", "declared_artifact"),
    ):
        value = metadata.get(key)
        if isinstance(value, list | tuple):
            evidence.extend(f"{prefix}:{item}" for item in value if str(item).strip())
    interactive_pause = str(metadata.get("interactive_pause") or "").strip()
    if interactive_pause:
        evidence.append(f"interactive_pause:{interactive_pause}")
    return evidence


def _append_goal_decision_entry(metadata: dict[str, Any], entry: dict[str, Any], *, limit: int = 100) -> None:
    ledger = [dict(item) for item in metadata.get("goal_decision_ledger", []) if isinstance(item, dict)]
    ledger.append(dict(entry))
    if len(ledger) > limit:
        del ledger[: len(ledger) - limit]
    metadata["goal_decision_ledger"] = ledger
    metadata["last_goal_decision_entry"] = dict(entry)


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
    previous_terminal_reason: str | None = None,
    progress_evidence: list[str] | None = None,
) -> dict[str, Any]:
    runtime_goal = _goal_to_runtime_model(goal)
    decision = should_continue_goal(
        runtime_goal,
        plan_mode=plan_mode,
        pending_user_input=pending_user_input,
        active_run_exists=active_run_exists,
        ephemeral=ephemeral,
        previous_terminal_reason=previous_terminal_reason,
    )
    decision_payload = decision.model_dump(mode="json")
    metadata = dict(goal.metadata_json or {})
    goal_decision_entry = build_goal_decision_entry(
        runtime_goal,
        decision,
        previous_terminal_reason=previous_terminal_reason,
        progress_evidence=progress_evidence,
    ).model_dump(mode="json", by_alias=True)
    _append_goal_decision_entry(metadata, goal_decision_entry)
    metadata["last_continuation_decision"] = {
        **decision_payload,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    if not decision.continue_goal:
        if decision.next_status is not None:
            goal.status = decision.next_status.value
            if decision.next_status == GoalStatus.BUDGET_LIMITED:
                metadata["budget_limit_prompt"] = budget_limit_prompt(
                    ThreadGoalPromptState(
                        objective=goal.objective,
                        tokens_used=goal.tokens_used or 0,
                        token_budget=goal.token_budget,
                        time_used_seconds=0,
                    )
                )
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


async def maybe_continue_session_goal_after_turn(
    *,
    db: AsyncSession,
    agent_id: Any,
    session_id: Any,
    user_id: Any,
    completed_task_type: str,
    completed_status: str,
    metadata_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch the session Goal continuation loop after a normal user turn completes.

    This is intentionally a post-turn bridge, not a new executor. The next turn is
    still scheduled through ``start_web_chat_run`` as ``goal_continuation``.
    """

    if str(completed_task_type or "") != "web_chat_turn":
        return {"ok": False, "reason": "unsupported_task_type"}
    if str(completed_status or "") != "completed":
        return {"ok": False, "reason": "non_completed_turn"}

    metadata = dict(metadata_json or {})
    if metadata.get("source") == "goal_continuation":
        return {"ok": False, "reason": "recursive_goal_continuation"}

    goal_result = await db.execute(
        select(AgentSessionGoal)
        .where(
            AgentSessionGoal.agent_id == agent_id,
            AgentSessionGoal.chat_session_id == session_id,
            AgentSessionGoal.status == GoalStatus.ACTIVE.value,
        )
        .order_by(AgentSessionGoal.created_at.desc())
        .limit(1)
    )
    goal = goal_result.scalar_one_or_none()
    if goal is None:
        return {"ok": False, "reason": "no_active_goal"}

    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id).limit(1))
    agent = agent_result.scalar_one_or_none()
    session_result = await db.execute(select(ChatSession).where(ChatSession.id == session_id).limit(1))
    session = session_result.scalar_one_or_none()
    user_result = await db.execute(select(User).where(User.id == user_id).limit(1))
    user = user_result.scalar_one_or_none()

    missing = [
        name
        for name, value in (("agent", agent), ("session", session), ("user", user))
        if value is None
    ]
    if missing:
        return {"ok": False, "reason": "missing_context", "missing": missing, "goal_id": str(goal.id)}

    return await continue_session_goal(
        db=db,
        agent=agent,
        user=user,
        session=session,
        goal=goal,
        plan_mode=bool(metadata.get("plan_mode") or metadata.get("plan_mode_requested")),
        pending_user_input=bool(metadata.get("pending_user_input") or metadata.get("awaiting_user_input")),
        active_run_exists=False,
        ephemeral=bool(metadata.get("ephemeral") or metadata.get("is_ephemeral")),
        previous_terminal_reason=str(metadata.get("terminal_reason") or "") or None,
        progress_evidence=_progress_evidence_from_metadata(metadata),
    )
