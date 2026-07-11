"""Pack Catalog and Agent Capability APIs."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import authorize_session_action, check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.pack_service import (
    get_capability_summary,
    get_session_runtime_summary,
)

router = APIRouter(tags=["packs"])


@router.get("/agents/{agent_id}/capability-summary")
async def get_agent_capability_summary(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Comprehensive capability summary: kernel tools, packs, policies, pending approvals."""
    await check_agent_access(db, current_user, agent_id)
    return await get_capability_summary(db, agent_id)


@router.get("/chat/sessions/{session_id}/runtime-summary")
async def get_session_runtime(
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Runtime summary for a chat session: activated packs, used tools, blocked capabilities."""
    # Resolve agent_id from session and verify access
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await authorize_session_action(
        db,
        current_user,
        agent_id=session.agent_id,
        session_id=session.id,
        action="read_runtime_summary",
        allow_manager_override=operator_view is True,
        manager_override_reason=operator_reason,
    )
    return await get_session_runtime_summary(db, session_id)
