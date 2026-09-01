"""Advanced Plan runtime API.

This is the runnable substrate for FreeCode-style ultraplan/advanced planning:
it is a durable chat-runtime run with ``task_type=advanced_plan`` instead of a
plain inline plan message.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action
from app.core.security import get_current_user
from app.database import get_db
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
    decision = await authorize_session_action(
        db,
        current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="advanced_plan:start",
        require_writable=True,
    )

    prompt = (
        "Run an advanced planning pass for the current session.\n\n"
        f"Objective: {body.objective.strip()}\n\n"
        "Produce a concrete plan, required evidence, risk gates, success criteria, and the next executable handoff. "
        "Do not perform irreversible actions from this planning pass."
    )
    return await start_web_chat_run(
        db=db,
        agent=decision.agent,
        user=current_user,
        session=decision.session,
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
