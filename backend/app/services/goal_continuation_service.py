"""Session Goal continuation service.

Goal continuation is a session-local control loop. It schedules another normal
chat-runtime invocation with a distinct task type; it does not bypass transcript
T0, tool governance, or web-chat runtime accounting.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.user import User
from app.runtime.prompts.goals import (
    ThreadGoalPromptState,
    budget_limit_prompt,
    budget_summary_contract_prompt,
    continuation_prompt,
    objective_updated_prompt,
)
from app.runtime.runtime_phase import RuntimePhase, build_phase_event
from app.services.session_goal_runtime import (
    RETRIABLE_TERMINAL_REASONS,
    GoalContinuationDecision,
    GoalStatus,
    SessionGoal,
    account_goal_tokens,
    build_goal_decision_entry,
    mark_goal_blocked_if_repeated,
    should_continue_goal,
)
from app.services.web_chat_runtime import broadcast_web_chat_event, start_web_chat_run

# A6: how many consecutive retriable terminal errors before a goal is Blocked.
_BLOCKED_THRESHOLD = 3


def goal_continuation_run_id(goal_id: uuid.UUID, continuation_count: int) -> uuid.UUID:
    """Stable run identity for one logical Goal continuation turn."""

    return uuid.uuid5(goal_id, f"continuation:{max(0, int(continuation_count))}")


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


def _prompt_state(goal: AgentSessionGoal) -> ThreadGoalPromptState:
    return ThreadGoalPromptState(
        objective=goal.objective,
        tokens_used=goal.tokens_used or 0,
        token_budget=goal.token_budget,
        time_used_seconds=0,
    )


def _continuation_prompt(goal: AgentSessionGoal) -> str:
    return continuation_prompt(_prompt_state(goal))


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


def _turn_tokens_from_metadata(metadata: dict[str, Any]) -> int:
    """A4: the effective tokens the completed turn spent toward the goal.

    web_chat_runtime records the invocation total under ``turn_tokens_used`` on the
    terminal RuntimeTask metadata (source: InvocationResult.tokens_used).
    """
    for key in ("turn_tokens_used", "tokens_used"):
        value = metadata.get(key)
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            continue
        if tokens > 0:
            return tokens
    return 0


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
    turn_tokens: int = 0,
) -> dict[str, Any]:
    reason = str(previous_terminal_reason or "").strip()
    runtime_goal = _goal_to_runtime_model(goal)

    # A6: bounded tolerance for retriable provider/turn/persistence errors. Each
    # consecutive occurrence bumps blocked_count; at the threshold the goal is
    # Blocked instead of continuing. loop_guard/clarification/user_cancel remain
    # immediate stops (handled inside should_continue_goal).
    forced_decision: GoalContinuationDecision | None = None
    retry_reason: str | None = None
    effective_previous_reason = previous_terminal_reason
    if reason in RETRIABLE_TERMINAL_REASONS:
        runtime_goal = mark_goal_blocked_if_repeated(runtime_goal, threshold=_BLOCKED_THRESHOLD)
        goal.blocked_count = runtime_goal.blocked_count
        if runtime_goal.status == GoalStatus.BLOCKED:
            forced_decision = GoalContinuationDecision(
                continue_goal=False,
                reason=f"blocked after {runtime_goal.blocked_count} consecutive errors ({reason})",
                next_status=GoalStatus.BLOCKED,
            )
        else:
            retry_reason = f"retry_after_error {runtime_goal.blocked_count}/{_BLOCKED_THRESHOLD}"
            effective_previous_reason = None

    # A4: account this run's tokens before the budget decision. account_goal_tokens
    # supplies the clamped total; the status decision stays single-sourced in
    # should_continue_goal so BUDGET_LIMITED still carries next_status + prompt.
    if turn_tokens and turn_tokens > 0:
        accounted_tokens = account_goal_tokens(runtime_goal, turn_tokens).tokens_used
        goal.tokens_used = accounted_tokens
        runtime_goal = runtime_goal.model_copy(update={"tokens_used": accounted_tokens})

    if forced_decision is not None:
        decision = forced_decision
    else:
        decision = should_continue_goal(
            runtime_goal,
            plan_mode=plan_mode,
            pending_user_input=pending_user_input,
            active_run_exists=active_run_exists,
            ephemeral=ephemeral,
            previous_terminal_reason=effective_previous_reason,
        )
        if retry_reason and decision.continue_goal:
            decision = decision.model_copy(update={"reason": retry_reason})

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
                metadata["budget_limit_prompt"] = budget_limit_prompt(_prompt_state(goal))
        goal.metadata_json = metadata
        await db.flush()
        return {"ok": False, "goal_id": str(goal.id), "decision": decision_payload}

    # A6: a clean continuation (no error this turn) clears the error streak.
    if goal.blocked_count and reason in {"", "turn_stop"}:
        goal.blocked_count = 0

    # A7: if the objective was re-scoped via update_goal, re-orient this turn and
    # consume the one-shot steering flag so later turns do not repeat it.
    prompt = _continuation_prompt(goal)
    if metadata.get("objective_updated_pending"):
        prompt = f"{objective_updated_prompt(_prompt_state(goal))}\n\n{prompt}"
        metadata["objective_updated_pending"] = False

    continuation_index = int(goal.continuation_count or 0)
    continuation_run_id = goal_continuation_run_id(goal.id, continuation_index)
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
        run_id=continuation_run_id,
        extra_metadata={
            "source": "goal_continuation",
            "goal_id": str(goal.id),
            "goal_objective": goal.objective,
            "continuation_count_before": continuation_index,
            "intent_id": f"goal:{goal.id}:continuation:{continuation_index}",
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


def _summary_goal_prompt_state(goal: AgentSessionGoal | None) -> ThreadGoalPromptState:
    if goal is None:
        return ThreadGoalPromptState(objective="(no active session goal)")
    return _prompt_state(goal)


async def _finalize_goal_after_summary(
    db: AsyncSession,
    *,
    goal_id: Any,
    outcome: str,
) -> None:
    """Backstop: if the summarizing turn did not record a terminal goal state via
    update_goal, park the goal at BUDGET_LIMITED so the autonomous loop stays shut."""
    if not goal_id:
        return
    result = await db.execute(select(AgentSessionGoal).where(AgentSessionGoal.id == goal_id).limit(1))
    goal = result.scalar_one_or_none()
    if goal is None:
        return
    metadata = dict(goal.metadata_json or {})
    metadata["budget_summary_outcome"] = outcome
    if str(goal.status or "") == GoalStatus.ACTIVE.value:
        goal.status = GoalStatus.BUDGET_LIMITED.value
        metadata["budget_limit_prompt"] = budget_limit_prompt(_prompt_state(goal))
    goal.metadata_json = metadata
    await db.flush()


async def _handle_budget_summary_turn_completion(
    *,
    db: AsyncSession,
    agent_id: Any,
    session_id: Any,
    user_id: Any,
    completed_status: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """State machine for a finished summarizing turn (§2).

    completed  -> seal the lane: summary_only -> hard_stopped.
    failed x1  -> retry the finalization turn exactly once.
    failed x2  -> seal the lane, record summary_failed, tell the user.
    """
    from app.services.runtime_budget_service import RuntimeBudgetService

    budget_run_id_raw = metadata.get("budget_run_id")
    tenant_id_raw = metadata.get("budget_summary_tenant_id")
    goal_id = metadata.get("goal_id")
    try:
        budget_run_id = uuid.UUID(str(budget_run_id_raw))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "summary_turn_missing_budget_run"}
    tenant_id = None
    try:
        tenant_id = uuid.UUID(str(tenant_id_raw)) if tenant_id_raw else None
    except (TypeError, ValueError):
        tenant_id = None

    service = RuntimeBudgetService()

    if str(completed_status or "") == "completed":
        await service.mark_summary_turn_state(
            tenant_id=tenant_id,
            budget_run_id=budget_run_id,
            expected_states=("issued", "retried"),
            new_state="completed",
        )
        await service.hard_stop_run(
            tenant_id=tenant_id,
            budget_run_id=budget_run_id,
            reason="budget_summary_completed",
            actor="goal_continuation_summary",
        )
        await _finalize_goal_after_summary(db, goal_id=goal_id, outcome="summary_completed")
        return {"ok": True, "reason": "budget_summary_completed", "budget_run_id": str(budget_run_id)}

    # The finalization turn itself failed/was killed.
    retried = await service.mark_summary_turn_state(
        tenant_id=tenant_id,
        budget_run_id=budget_run_id,
        expected_states=("issued",),
        new_state="retried",
    )
    if retried:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id).limit(1))
        agent = agent_result.scalar_one_or_none()
        session_result = await db.execute(select(ChatSession).where(ChatSession.id == session_id).limit(1))
        session = session_result.scalar_one_or_none()
        user_result = await db.execute(select(User).where(User.id == user_id).limit(1))
        user = user_result.scalar_one_or_none()
        goal = None
        if goal_id:
            goal_result = await db.execute(select(AgentSessionGoal).where(AgentSessionGoal.id == goal_id).limit(1))
            goal = goal_result.scalar_one_or_none()
        if agent is None or session is None or user is None:
            # Cannot rebuild the turn context: seal the lane instead of leaking it.
            await service.mark_summary_turn_state(
                tenant_id=tenant_id,
                budget_run_id=budget_run_id,
                expected_states=("retried",),
                new_state="failed",
            )
            await service.hard_stop_run(
                tenant_id=tenant_id,
                budget_run_id=budget_run_id,
                reason="budget_summary_failed",
                actor="goal_continuation_summary",
            )
            await _finalize_goal_after_summary(db, goal_id=goal_id, outcome="summary_failed")
            return {"ok": False, "reason": "summary_retry_context_missing"}
        run = await start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=budget_summary_contract_prompt(_summary_goal_prompt_state(goal)),
            display_content="",
            file_name="",
            append_user_message=False,
            runtime_task_type="goal_continuation",
            extra_metadata={
                "source": "goal_continuation",
                "budget_summary_turn": True,
                "budget_summary_tenant_id": str(tenant_id) if tenant_id else None,
                "budget_run_id": str(budget_run_id),
                "goal_id": str(goal_id) if goal_id else None,
                "summary_attempt": 2,
            },
        )
        return {"ok": True, "reason": "budget_summary_retry", "run": run}

    # Second failure: seal the lane and surface the failure to the session.
    sealed = await service.mark_summary_turn_state(
        tenant_id=tenant_id,
        budget_run_id=budget_run_id,
        expected_states=("retried",),
        new_state="failed",
    )
    if sealed:
        await service.hard_stop_run(
            tenant_id=tenant_id,
            budget_run_id=budget_run_id,
            reason="budget_summary_failed",
            actor="goal_continuation_summary",
        )
        await _finalize_goal_after_summary(db, goal_id=goal_id, outcome="summary_failed")
        await broadcast_web_chat_event(
            agent_id,
            session_id,
            {
                "type": "runtime_action_failed",
                "status": "summary_failed",
                "message": "The budget finalization summary could not be generated.",
            },
        )
    return {"ok": False, "reason": "budget_summary_failed", "budget_run_id": str(budget_run_id)}


async def _maybe_issue_budget_summary_turn(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    goal: AgentSessionGoal,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """§2 finalization wake: when the budget plane degraded this session's run to
    summary_only, schedule exactly one summarizing turn instead of a normal
    continuation. Returns None when the budget run is healthy (normal path)."""
    from app.services.runtime_budget_service import RuntimeBudgetService

    budget_run_id_raw = metadata.get("budget_run_id")
    if not budget_run_id_raw:
        return None
    try:
        budget_run_id = uuid.UUID(str(budget_run_id_raw))
    except (TypeError, ValueError):
        return None

    service = RuntimeBudgetService()
    run = await service.get_run(tenant_id=agent.tenant_id, budget_run_id=budget_run_id)
    if run is None or run.status != "summary_only":
        return None

    issued = await service.mark_summary_turn_state(
        tenant_id=agent.tenant_id,
        budget_run_id=budget_run_id,
        expected_states=(None,),
        new_state="issued",
        extra={"summary_goal_id": str(goal.id)},
    )
    if not issued:
        # A finalization turn was already issued (or already settled): the lane
        # is spoken for. Never fall through to a normal continuation on a
        # summary_only run.
        return {
            "ok": False,
            "reason": "budget_summary_already_issued",
            "budget_run_id": str(budget_run_id),
        }

    await broadcast_web_chat_event(
        agent.id,
        str(session.id),
        build_phase_event(RuntimePhase.AWAITING_BUDGET, detail={"budget_run_id": str(budget_run_id)}),
    )
    summary_run = await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=budget_summary_contract_prompt(_summary_goal_prompt_state(goal)),
        display_content="",
        file_name="",
        append_user_message=False,
        runtime_task_type="goal_continuation",
        extra_metadata={
            "source": "goal_continuation",
            "budget_summary_turn": True,
            "budget_summary_tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
            "budget_run_id": str(budget_run_id),
            "goal_id": str(goal.id),
            "summary_attempt": 1,
        },
    )
    goal_metadata = dict(goal.metadata_json or {})
    goal_metadata["budget_summary_run_id"] = summary_run.get("run_id")
    goal_metadata["budget_summary_issued_at"] = datetime.now(timezone.utc).isoformat()
    goal.metadata_json = goal_metadata
    await db.flush()
    return {
        "ok": True,
        "reason": "budget_summary_issued",
        "budget_run_id": str(budget_run_id),
        "run": summary_run,
    }


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
    """Dispatch the session Goal continuation loop after a turn completes.

    This is intentionally a post-turn bridge, not a new executor. The next turn is
    still scheduled through ``start_web_chat_run`` as ``goal_continuation``.

    A3 autonomous loop: completed ``goal_continuation`` turns feed the next
    continuation (idle → continue until a terminal state, Codex-aligned). The
    loop's stops are ``should_continue_goal`` — continuation cap, token budget
    (BUDGET_LIMITED), repeated-error blocking, terminal-reason mapping — plus
    the inherited budget-plane run enforced by the web-chat runtime; not a
    one-step-per-user-turn gate.
    """

    if str(completed_task_type or "") not in {"web_chat_turn", "goal_continuation"}:
        return {"ok": False, "reason": "unsupported_task_type"}

    metadata = dict(metadata_json or {})

    # §2: a finished finalization turn runs its own state machine (it must also
    # observe failed/killed outcomes, so it sits before the completed-status gate).
    if metadata.get("budget_summary_turn"):
        return await _handle_budget_summary_turn_completion(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            completed_status=completed_status,
            metadata=metadata,
        )

    if str(completed_status or "") != "completed":
        return {"ok": False, "reason": "non_completed_turn"}

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

    missing = [name for name, value in (("agent", agent), ("session", session), ("user", user)) if value is None]
    if missing:
        return {"ok": False, "reason": "missing_context", "missing": missing, "goal_id": str(goal.id)}

    # §2 finalization wake: a summary_only budget run schedules exactly one
    # summarizing turn instead of a normal continuation.
    summary_wake = await _maybe_issue_budget_summary_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        goal=goal,
        metadata=metadata,
    )
    if summary_wake is not None:
        return summary_wake

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
        turn_tokens=_turn_tokens_from_metadata(metadata),
    )
