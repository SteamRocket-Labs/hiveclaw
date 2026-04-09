"""Guard Policy management endpoints (ARCHITECTURE.md §7.4).

GET  /guard-policies — read current tenant policy
PUT  /guard-policies — update policy (admin only), bumps guard version + sync_version
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.guard_policy import GuardPolicy
from app.models.user import User
from app.services.sync_service import bump_sync_version

router = APIRouter(tags=["guard-policies"])


# ─── Schemas ────────────────────────────────────────────


class GuardPolicyOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    zone_guard: dict
    egress_guard: dict

    model_config = {"from_attributes": True}


class GuardPolicyUpdate(BaseModel):
    zone_guard: dict | None = None
    egress_guard: dict | None = None


# ─── Helpers ────────────────────────────────────────────


async def _get_or_create_policy(db: AsyncSession, tenant_id: uuid.UUID) -> GuardPolicy:
    """Get existing policy or create a default empty one for the tenant."""
    result = await db.execute(
        select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id)
    )
    policy = result.scalar_one_or_none()
    if not policy:
        policy = GuardPolicy(tenant_id=tenant_id)
        db.add(policy)
        await db.flush()
    return policy


# ─── Endpoints ──────────────────────────────────────────


@router.get("/guard-policies", response_model=GuardPolicyOut)
async def get_guard_policy(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get the Guard policy for the current tenant."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant assigned")

    policy = await _get_or_create_policy(db, current_user.tenant_id)
    return GuardPolicyOut.model_validate(policy)


@router.put("/guard-policies", response_model=GuardPolicyOut)
async def update_guard_policy(
    body: GuardPolicyUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update the Guard policy for the current tenant (admin only).

    Bumps the policy version and the tenant sync_version so Desktop
    clients pick up the change on their next sync poll.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant assigned")

    policy = await _get_or_create_policy(db, current_user.tenant_id)

    if body.zone_guard is not None:
        policy.zone_guard = body.zone_guard
    if body.egress_guard is not None:
        policy.egress_guard = body.egress_guard
    policy.version += 1
    await db.flush()

    await bump_sync_version(db, current_user.tenant_id)
    return GuardPolicyOut.model_validate(policy)
