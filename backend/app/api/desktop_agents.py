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
from app.schemas.schemas import SmartModelRoutingConfig
from app.services.agent_identity_lifecycle import (
    ensure_agent_identity,
    get_agent_lifecycle_block_reason,
    soft_delete_agent,
)
from app.services.auto_provision import ensure_main_agent
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
    smart_model_routing: SmartModelRoutingConfig | None = None


class SubAgentUpdate(BaseModel):
    name: str | None = None
    role_description: str | None = None
    bio: str | None = None
    security_zone: SecurityZone | None = None
    execution_mode: ExecutionMode | None = None
    smart_model_routing: SmartModelRoutingConfig | None = None


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
    smart_model_routing: SmartModelRoutingConfig | None = None

    model_config = {"from_attributes": True}


# ─── Helpers ────────────────────────────────────────────


async def _get_owned_sub_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Agent:
    """Get an owned sub-agent. Root agents are not editable through Desktop."""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if get_agent_lifecycle_block_reason(agent):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your agent")
    if agent.parent_agent_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Desktop can only modify sub-agents")
    return agent


async def _get_admin_deletable_sub_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> Agent:
    """Get a tenant sub-agent asset for admin deletion."""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if get_agent_lifecycle_block_reason(agent):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.parent_agent_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Desktop can only delete sub-agents")
    agent_tenant_id = getattr(agent, "tenant_id", None)
    if user.role != "platform_admin" and agent_tenant_id is not None and agent_tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


# ─── Endpoints ──────────────────────────────────────────


@router.post("/agents", response_model=SubAgentOut, status_code=status.HTTP_201_CREATED)
async def create_sub_agent(
    body: SubAgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an agent owned by the current user."""
    main_agent = await ensure_main_agent(db, current_user)
    if main_agent is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Root agent is not available for this user",
        )

    agent = Agent(
        name=body.name,
        role_description=body.role_description,
        bio=body.bio,
        owner_user_id=current_user.id,
        parent_agent_id=main_agent.id,
        creator_id=current_user.id,
        sponsor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        security_zone=body.security_zone,
        execution_mode=body.execution_mode,
        smart_model_routing=body.smart_model_routing.model_dump() if body.smart_model_routing else None,
        config_version=1,
    )
    db.add(agent)
    await ensure_agent_identity(db, agent)

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
    agent = await _get_owned_sub_agent(db, current_user, agent_id)

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
    """Delete a Sub-Agent asset (admin only)."""
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sub-agent is an enterprise asset; only an admin can delete it.",
        )
    agent = await _get_admin_deletable_sub_agent(db, current_user, agent_id)

    await soft_delete_agent(db, agent, actor_id=current_user.id, reason="desktop_delete_sub_agent")
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)
