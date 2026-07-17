"""Session-scoped Goal API."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_transcript import append_session_event
from app.services.goal_continuation_service import continue_session_goal
from app.services.session_goal_projection import build_session_goal_projection
from app.services.session_live_input import submit_live_cancel_input
from app.services.web_chat_runtime import start_web_chat_run

router = APIRouter(prefix="/agents/{agent_id}/sessions/{session_id}/goals", tags=["session-goals"])


class StartGoalIn(BaseModel):
    request_id: uuid.UUID | None = None
    objective: str = Field(min_length=1)
    token_budget: int | None = Field(default=None, ge=0)
    max_continuation_turns: int | None = Field(default=None, ge=0)
    time_budget_seconds: int | None = Field(default=None, ge=0)
    content: str | None = None
    display_content: str | None = None
    file_name: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    parts: list[dict[str, Any]] = Field(default_factory=list)
    start_immediately: bool = False


class GoalTransitionIn(BaseModel):
    action: Literal["pause", "resume", "stop"]


async def _create_or_load_goal(
    *,
    db: AsyncSession,
    agent: Any,
    user: User,
    session_id: uuid.UUID,
    body: StartGoalIn,
) -> tuple[AgentSessionGoal, bool]:
    values = {
        "tenant_id": getattr(agent, "tenant_id", None),
        "agent_id": agent.id,
        "chat_session_id": session_id,
        "created_by_user_id": user.id,
        "objective": body.objective.strip(),
        "status": "active",
        "token_budget": body.token_budget,
        "max_continuation_turns": body.max_continuation_turns,
        "time_budget_seconds": body.time_budget_seconds,
    }
    if body.request_id is None:
        goal = AgentSessionGoal(**values)
        db.add(goal)
        await db.flush()
        return goal, True

    request_id = body.request_id
    statement = (
        pg_insert(AgentSessionGoal)
        .values(
            id=request_id,
            metadata_json={"request_id": str(request_id)},
            **values,
        )
        .on_conflict_do_nothing()
        .returning(AgentSessionGoal.id)
    )
    inserted_id = (await db.execute(statement)).scalar_one_or_none()
    result = await db.execute(
        select(AgentSessionGoal).where(
            AgentSessionGoal.id == request_id,
            AgentSessionGoal.agent_id == agent.id,
            AgentSessionGoal.chat_session_id == session_id,
        )
    )
    goal = result.scalar_one_or_none()
    if goal is not None:
        if goal.objective != values["objective"]:
            raise HTTPException(status_code=409, detail="Goal request id was already used for another objective")
        return goal, inserted_id is not None

    active_result = await db.execute(
        select(AgentSessionGoal).where(
            AgentSessionGoal.agent_id == agent.id,
            AgentSessionGoal.chat_session_id == session_id,
            AgentSessionGoal.status == "active",
        )
    )
    if active_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="This session already has an active goal")
    raise HTTPException(status_code=409, detail="Goal request could not be recovered")


async def _load_goal(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    goal_id: uuid.UUID,
) -> AgentSessionGoal:
    result = await db.execute(
        select(AgentSessionGoal).where(
            AgentSessionGoal.id == goal_id,
            AgentSessionGoal.agent_id == agent_id,
            AgentSessionGoal.chat_session_id == session_id,
        )
    )
    goal = result.scalar_one_or_none()
    if goal is None:
        raise HTTPException(status_code=404, detail="Session goal not found")
    return goal


async def _append_goal_transition_event(
    *,
    db: AsyncSession,
    agent: Any,
    user: User,
    session_id: uuid.UUID,
    goal: AgentSessionGoal,
    action: str,
) -> None:
    await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=session_id,
        actor_type="system",
        event_type="goal",
        role="system",
        user_id=user.id,
        content=f"Goal {action}: {goal.objective}",
        source="session_goal",
        materialize_chat_message=False,
        metadata={
            "goal_id": str(goal.id),
            "action": action,
            "status": goal.status,
            "objective": goal.objective,
        },
    )


async def _cancel_last_goal_run_if_active(
    *,
    db: AsyncSession,
    agent: Agent,
    session: ChatSession,
    user: User,
    goal: AgentSessionGoal,
) -> None:
    metadata = goal.metadata_json or {}
    run_id = str(metadata.get("last_continuation_run_id") or metadata.get("last_goal_run_id") or "").strip()
    if not run_id:
        return
    await submit_live_cancel_input(
        db=db,
        agent=agent,
        user=user,
        session=session,
        run_id=run_id,
        source="session_goal_transition",
        idempotency_key=f"goal:{goal.id}:cancel-run:{run_id}",
    )


@router.post("")
async def start_session_goal(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: StartGoalIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    decision = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="goal:start",
        require_writable=True,
    )
    agent = decision.agent
    goal, created = await _create_or_load_goal(
        db=db,
        agent=agent,
        user=current_user,
        session_id=session_id,
        body=body,
    )
    if created:
        await _append_goal_transition_event(
            db=db,
            agent=agent,
            user=current_user,
            session_id=session_id,
            goal=goal,
            action="started",
        )
    run = None
    if body.start_immediately:
        metadata = dict(goal.metadata_json or {})
        replay_run_id = str(metadata.get("last_goal_run_id") or "").strip()
        if not created and replay_run_id:
            run = {
                "run_id": replay_run_id,
                "status": str(metadata.get("last_goal_run_status") or "running"),
                "replayed": True,
            }
        elif goal.status == "active":
            run = await start_web_chat_run(
                db=db,
                agent=agent,
                user=current_user,
                session=decision.session,
                content=(body.content or body.objective).strip(),
                display_content=(body.display_content or body.objective).strip(),
                file_name=body.file_name,
                attachments=list(body.attachments),
                parts=list(body.parts),
                runtime_task_type="web_chat_turn",
                budget_interactive=False,
                extra_metadata={
                    "source": "session_goal",
                    "goal_id": str(goal.id),
                    "goal_objective": goal.objective,
                    **(
                        {
                            "goal_request_id": str(body.request_id),
                            "intent_id": f"goal:{body.request_id}",
                        }
                        if body.request_id
                        else {}
                    ),
                },
                run_id=body.request_id,
            )
            run_id = str((run or {}).get("run_id") or "").strip()
            if run_id:
                metadata = dict(goal.metadata_json or {})
                metadata["last_goal_run_id"] = run_id
                metadata["last_goal_run_status"] = str((run or {}).get("status") or "running")
                goal.metadata_json = metadata
                await db.flush()
    return {**build_session_goal_projection(goal), "run": run}


@router.post("/{goal_id}/continue")
async def continue_goal(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    goal_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    decision = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="goal:continue",
        require_writable=True,
    )
    agent = decision.agent
    goal = await _load_goal(db, agent_id=agent_id, session_id=session_id, goal_id=goal_id)

    return await continue_session_goal(
        db=db,
        agent=agent,
        user=current_user,
        session=decision.session,
        goal=goal,
    )


@router.post("/{goal_id}/transition")
async def transition_goal(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    goal_id: uuid.UUID,
    body: GoalTransitionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    decision = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=session_id,
        action=f"goal:{body.action}",
        require_writable=True,
    )
    goal = await _load_goal(db, agent_id=agent_id, session_id=session_id, goal_id=goal_id)
    status = str(goal.status or "active")
    continuation: dict[str, Any] | None = None

    if body.action == "pause":
        if status not in {"active", "paused"}:
            raise HTTPException(status_code=409, detail=f"Goal cannot be paused from {status}")
        goal.status = "paused"
        await _cancel_last_goal_run_if_active(
            db=db,
            agent=decision.agent,
            session=decision.session,
            user=current_user,
            goal=goal,
        )
    elif body.action == "resume":
        if status == "active":
            return {**build_session_goal_projection(goal), "continuation": None}
        if status not in {"paused", "blocked", "usage_limited", "budget_limited"}:
            raise HTTPException(status_code=409, detail=f"Goal cannot be resumed from {status}")
        goal.status = "active"
        goal.blocked_count = 0
        metadata = dict(goal.metadata_json or {})
        metadata["resumed_at"] = datetime.now(timezone.utc).isoformat()
        metadata["resumed_by_user_id"] = str(current_user.id)
        goal.metadata_json = metadata
    else:
        if status not in {"complete", "cancelled"}:
            goal.status = "cancelled"
            goal.completed_at = datetime.now(timezone.utc)
            await _cancel_last_goal_run_if_active(
                db=db,
                agent=decision.agent,
                session=decision.session,
                user=current_user,
                goal=goal,
            )

    await _append_goal_transition_event(
        db=db,
        agent=decision.agent,
        user=current_user,
        session_id=session_id,
        goal=goal,
        action=body.action,
    )
    await db.flush()

    if body.action == "resume":
        continuation = await continue_session_goal(
            db=db,
            agent=decision.agent,
            user=current_user,
            session=decision.session,
            goal=goal,
        )

    return {**build_session_goal_projection(goal), "continuation": continuation}
