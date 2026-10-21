"""Current access to the employee coordinating a canonical creation draft."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.models.user import User
from app.services.runtime_user_identity import load_runtime_user
from app.services.tool_visibility import is_hr_agent

CREATION_PRECLAIM_BLOCKS = frozenset({
    "capability_denied", "approval_required", "capability_unavailable", "source_execute_denied",
})


async def require_creation_capability(db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Apply enterprise limits; a blueprint click only supplies user confirmation."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.services.capability_gate import check_capability

    try:
        result = await check_capability(db, tenant_id, agent_id, "create_digital_employee")
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={
            "error": "capability_unavailable", "message": "Employee creation policy is temporarily unavailable.",
        }) from exc
    if result.denied:
        raise HTTPException(status_code=403, detail={
            "error": "capability_denied", "message": "Employee creation is denied by the current capability policy.",
        })
    if result.escalate_to_l3 and result.policy_found:
        raise HTTPException(status_code=409, detail={
            "error": "approval_required", "message": "Enterprise approval is required before employee creation.",
        })


async def load_creation_requester(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Recover canonical identity for receipts; execution checks liveness below."""
    return await load_runtime_user(db, user_id, include_inactive=True)


async def load_creation_source_agent(
    db: AsyncSession, user: User, agent_id: uuid.UUID, *, require_execute: bool = False,
):
    if require_execute:
        user = await load_runtime_user(db, user.id)
        if user is None:
            raise HTTPException(status_code=403, detail="Employee creation requester is unavailable")
    agent, access = await check_agent_access(db, user, agent_id)
    native = agent.agent_type == "native" and agent.agent_class == "internal_tenant"
    if not is_hr_agent(agent) and not native:
        raise HTTPException(status_code=404, detail="Employee creation draft not found")
    if not user.is_active or agent.deleted_at is not None or agent.deactivated_at is not None:
        raise HTTPException(status_code=403, detail="Employee creation source is unavailable")
    if require_execute and access not in {"use", "manage"}:
        raise HTTPException(status_code=403, detail="Employee creation requires current execution access")
    return agent
