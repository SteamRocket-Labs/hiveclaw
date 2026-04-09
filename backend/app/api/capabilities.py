"""Capability policy management API routes."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.capability_policy import CapabilityPolicy
from app.models.user import User
from app.services.capability_gate import get_all_capabilities

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise/capabilities", tags=["capabilities"])


class CapabilityPolicyUpdate(BaseModel):
    capability: str
    agent_id: uuid.UUID | None = None
    allowed: bool = False
    requires_approval: bool = True
    conditions: dict = {}


class CapabilityPolicyOut(BaseModel):
    id: uuid.UUID
    capability: str
    agent_id: uuid.UUID | None = None
    allowed: bool
    requires_approval: bool
    conditions: dict

    model_config = {"from_attributes": True}


@router.get("/definitions")
async def list_capability_definitions(
    current_user: User = Depends(get_current_user),
):
    """List all known capability definitions and their mapped tools."""
    return get_all_capabilities()


@router.get("")
async def list_capability_policies(
    agent_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List capability policies for the current tenant, optionally filtered by agent."""
    if not current_user.tenant_id:
        return []

    query = select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == current_user.tenant_id)
    if agent_id is not None:
        query = query.where(CapabilityPolicy.agent_id == agent_id)

    result = await db.execute(query.order_by(CapabilityPolicy.capability))
    return [CapabilityPolicyOut.model_validate(p) for p in result.scalars().all()]


@router.put("")
async def upsert_capability_policy(
    data: CapabilityPolicyUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a capability policy for the current tenant."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")

    # Find existing policy
    query = select(CapabilityPolicy).where(
        CapabilityPolicy.tenant_id == current_user.tenant_id,
        CapabilityPolicy.capability == data.capability,
    )
    if data.agent_id:
        query = query.where(CapabilityPolicy.agent_id == data.agent_id)
    else:
        query = query.where(CapabilityPolicy.agent_id.is_(None))

    result = await db.execute(query)
    policy = result.scalar_one_or_none()

    if policy:
        policy.allowed = data.allowed
        policy.requires_approval = data.requires_approval
        policy.conditions = data.conditions
    else:
        policy = CapabilityPolicy(
            tenant_id=current_user.tenant_id,
            agent_id=data.agent_id,
            capability=data.capability,
            allowed=data.allowed,
            requires_approval=data.requires_approval,
            conditions=data.conditions,
        )
        db.add(policy)

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="capability.configured",
            severity="warn",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="configure_capability",
            details={
                "capability": data.capability,
                "allowed": data.allowed,
                "requires_approval": data.requires_approval,
                "agent_id": str(data.agent_id) if data.agent_id else None,
            },
        )
    except Exception:
        logger.warning("Audit write failed for capability.configured", exc_info=True)

    await db.commit()
    await db.refresh(policy)
    return CapabilityPolicyOut.model_validate(policy)


@router.delete("/{policy_id}")
async def delete_capability_policy(
    policy_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a capability policy (reverts to default allow)."""
    result = await db.execute(
        select(CapabilityPolicy).where(
            CapabilityPolicy.id == policy_id,
            CapabilityPolicy.tenant_id == current_user.tenant_id,
        )
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await db.delete(policy)
    await db.commit()
    return {"status": "deleted"}
