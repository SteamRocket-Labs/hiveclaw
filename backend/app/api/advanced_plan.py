"""Advanced Plan runtime API.

This is the runnable substrate for FreeCode-style ultraplan/advanced planning:
it is a durable chat-runtime run with ``task_type=advanced_plan`` instead of a
plain inline plan message.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.web_chat_runtime import start_web_chat_run

router = APIRouter(prefix="/agents/{agent_id}/sessions/{session_id}/advanced-plan", tags=["advanced-plan"])


class StartAdvancedPlanIn(BaseModel):
    objective: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


@router.post("")
async def start_advanced_plan(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: StartAdvancedPlanIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    session_result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.agent_id == agent_id,
        )
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")

    prompt = (
        "Run an advanced planning pass for the current session.\n\n"
        f"Objective: {body.objective.strip()}\n\n"
        "Produce a concrete plan, required evidence, risk gates, success criteria, and the next executable handoff. "
        "Do not perform irreversible actions from this planning pass."
    )
    return await start_web_chat_run(
        db=db,
        agent=agent,
        user=current_user,
        session=session,
        content=prompt,
        display_content="",
        file_name="",
        append_user_message=False,
        runtime_task_type="advanced_plan",
        extra_metadata={
            "source": "advanced_plan",
            "advanced_plan": True,
            "objective": body.objective.strip(),
            "context": body.context,
        },
    )
