"""Desktop Agent CRUD endpoints (ARCHITECTURE.md §7.3).

Desktop can create/update/delete Sub-Agents only.
Main Agents are provisioned by Cloud and cannot be modified from Desktop.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.sync_service import bump_sync_version

router = APIRouter(prefix="/desktop", tags=["desktop-agents"])


# ─── Schemas ────────────────────────────────────────────


SecurityZone = Literal["public", "standard", "restricted"]
ExecutionMode = Literal["standard", "coordinator"]


class SubAgentCreate(BaseModel):
    name: str
    role_description: str = ""
    bio: str | None = None
    security_zone: SecurityZone = "standard"
    execution_mode: ExecutionMode = "standard"


class SubAgentUpdate(BaseModel):
    name: str | None = None
    role_description: str | None = None
    bio: str | None = None
    security_zone: SecurityZone | None = None
    execution_mode: ExecutionMode | None = None


class SubAgentOut(BaseModel):
    id: uuid.UUID
    name: str
    role_description: str
    bio: str | None = None
    parent_agent_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    config_version: int
    security_zone: str
    execution_mode: ExecutionMode = "standard"

    model_config = {"from_attributes": True}


# ─── Helpers ────────────────────────────────────────────


async def _get_own_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Agent:
    """Get an agent owned by the user. Raises 403/404 on mismatch."""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your agent")
    return agent


# ─── Endpoints ──────────────────────────────────────────


@router.post("/agents", response_model=SubAgentOut, status_code=status.HTTP_201_CREATED)
async def create_sub_agent(
    body: SubAgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an agent owned by the current user."""
    agent = Agent(
        name=body.name,
        role_description=body.role_description,
        bio=body.bio,
        owner_user_id=current_user.id,
        creator_id=current_user.id,
        tenant_id=current_user.tenant_id,
        security_zone=body.security_zone,
        execution_mode=body.execution_mode,
        config_version=1,
    )
    db.add(agent)
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)

    return SubAgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}", response_model=SubAgentOut)
async def update_sub_agent(
    agent_id: uuid.UUID,
    body: SubAgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a Sub-Agent owned by the current user."""
    agent = await _get_own_agent(db, current_user, agent_id)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(agent, field, value)
    agent.config_version += 1
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)

    return SubAgentOut.model_validate(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sub_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a Sub-Agent owned by the current user."""
    agent = await _get_own_agent(db, current_user, agent_id)

    await db.delete(agent)
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)
