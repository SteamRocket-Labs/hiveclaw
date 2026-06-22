"""Session-scoped Goal API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.goal_continuation_service import continue_session_goal

router = APIRouter(prefix="/agents/{agent_id}/sessions/{session_id}/goals", tags=["session-goals"])


class StartGoalIn(BaseModel):
    objective: str = Field(min_length=1)
    token_budget: int | None = Field(default=None, ge=0)
    max_continuation_turns: int | None = Field(default=None, ge=0)
    time_budget_seconds: int | None = Field(default=None, ge=0)


@router.post("")
async def start_session_goal(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: StartGoalIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    goal = AgentSessionGoal(
        tenant_id=getattr(agent, "tenant_id", None),
        agent_id=agent_id,
        chat_session_id=session_id,
        created_by_user_id=current_user.id,
        objective=body.objective.strip(),
        token_budget=body.token_budget,
        max_continuation_turns=body.max_continuation_turns,
        time_budget_seconds=body.time_budget_seconds,
    )
    db.add(goal)
    await db.flush()
    return {
        "id": str(goal.id),
        "agent_id": str(goal.agent_id),
        "session_id": str(goal.chat_session_id),
        "objective": goal.objective,
        "status": goal.status,
        "token_budget": goal.token_budget,
        "tokens_used": goal.tokens_used,
        "max_continuation_turns": goal.max_continuation_turns,
        "continuation_count": goal.continuation_count,
    }


@router.post("/{goal_id}/continue")
async def continue_goal(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    goal_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    goal_result = await db.execute(
        select(AgentSessionGoal).where(
            AgentSessionGoal.id == goal_id,
            AgentSessionGoal.agent_id == agent_id,
            AgentSessionGoal.chat_session_id == session_id,
        )
    )
    goal = goal_result.scalar_one_or_none()
    if goal is None:
        raise HTTPException(status_code=404, detail="Session goal not found")

    session_result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.agent_id == agent_id,
        )
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")

    return await continue_session_goal(
        db=db,
        agent=agent,
        user=current_user,
        session=session,
        goal=goal,
    )
