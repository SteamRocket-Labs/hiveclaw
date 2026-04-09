"""Desktop Bootstrap & Sync endpoints (ARCHITECTURE.md §7.2).

GET /desktop/bootstrap — full initial payload for Desktop startup
GET /desktop/sync?v={n} — incremental sync based on global sync_version
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.guard_policy import GuardPolicy
from app.models.llm import LLMModel
from app.models.user import User

router = APIRouter(prefix="/desktop", tags=["desktop-sync"])


# ─── Response schemas ───────────────────────────────────


class AgentProjection(BaseModel):
    id: uuid.UUID
    name: str
    role_description: str
    bio: str | None = None
    parent_agent_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    channel_perms: bool = False
    config_version: int = 1
    security_zone: str = "standard"
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    status: str = "creating"

    model_config = {"from_attributes": True}


class LLMConfigItem(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    label: str
    supports_vision: bool = False
    enabled: bool = True

    model_config = {"from_attributes": True}


class UserProjection(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str
    role: str
    tenant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class BootstrapResponse(BaseModel):
    sync_version: int
    user: UserProjection
    agents: list[AgentProjection] = []
    policy: dict = {}
    llm_config: list[LLMConfigItem] = []


class SyncResponse(BaseModel):
    not_modified: bool = False
    sync_version: int = 0
    agents: list[AgentProjection] | None = None
    policy: dict | None = None
    llm_config: list[LLMConfigItem] | None = None


# ─── Helpers ────────────────────────────────────────────


async def _get_user_agents(db: AsyncSession, user: User) -> list[AgentProjection]:
    """Load the user's agents (tenant-isolated)."""
    result = await db.execute(
        select(Agent).where(
            Agent.owner_user_id == user.id,
            Agent.tenant_id == user.tenant_id,
        )
    )
    return [AgentProjection.model_validate(a) for a in result.scalars().all()]


async def _get_llm_config(db: AsyncSession, tenant_id: uuid.UUID | None) -> list[LLMConfigItem]:
    """Load enabled LLM models for the tenant."""
    if not tenant_id:
        return []
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    return [LLMConfigItem.model_validate(m) for m in result.scalars().all()]


async def _get_guard_policy(db: AsyncSession, tenant_id: uuid.UUID | None) -> dict:
    """Load the Guard policy for the tenant, or return empty defaults."""
    if not tenant_id:
        return {"version": 0, "zone_guard": {}, "egress_guard": {}}
    result = await db.execute(
        select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id)
    )
    policy = result.scalar_one_or_none()
    if not policy:
        return {"version": 0, "zone_guard": {}, "egress_guard": {}}
    return {"version": policy.version, "zone_guard": policy.zone_guard, "egress_guard": policy.egress_guard}


async def _get_tenant_sync_version(db: AsyncSession, tenant_id: uuid.UUID | None) -> int:
    """Get the current sync_version for the tenant."""
    if not tenant_id:
        return 0
    from app.models.tenant import Tenant
    tenant = await db.get(Tenant, tenant_id)
    return tenant.sync_version if tenant else 0


# ─── Endpoints ──────────────────────────────────────────


@router.get("/bootstrap", response_model=BootstrapResponse)
async def desktop_bootstrap(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full initial payload for Desktop startup.

    Returns the user's profile, main agent, sub-agents, guard policy,
    and available LLM models. Desktop should call this once on first
    login and cache the result locally.
    """
    sync_version = await _get_tenant_sync_version(db, current_user.tenant_id)
    agents = await _get_user_agents(db, current_user)
    llm_config = await _get_llm_config(db, current_user.tenant_id)
    policy = await _get_guard_policy(db, current_user.tenant_id)

    return BootstrapResponse(
        sync_version=sync_version,
        user=UserProjection.model_validate(current_user),
        agents=agents,
        policy=policy,
        llm_config=llm_config,
    )


@router.get("/sync", response_model=SyncResponse)
async def desktop_sync(
    v: int = Query(..., description="Client's last-known sync_version"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Incremental sync — returns changed resources if sync_version advanced.

    Desktop polls this endpoint periodically. If v matches the current
    sync_version, returns not_modified=true with no payload. Otherwise,
    returns the full current state of all Desktop-visible resources.
    """
    current_version = await _get_tenant_sync_version(db, current_user.tenant_id)

    if v >= current_version:
        return SyncResponse(not_modified=True, sync_version=current_version)

    agents = await _get_user_agents(db, current_user)
    llm_config = await _get_llm_config(db, current_user.tenant_id)
    policy = await _get_guard_policy(db, current_user.tenant_id)

    return SyncResponse(
        not_modified=False,
        sync_version=current_version,
        agents=agents,
        policy=policy,
        llm_config=llm_config,
    )
