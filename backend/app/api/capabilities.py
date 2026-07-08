"""Capability policy management API routes."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.permissions import check_agent_access
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.capability_policy import CapabilityPolicy
from app.models.user import User
from app.services.capability_gate import get_all_capabilities
from app.services.capability_factor_intake import (
    archive_capability_factor,
    capture_capability_factor,
    create_promotion_proposal,
    decide_promotion_proposal,
    list_capability_factors,
    list_promotion_proposals,
)
from app.services.pack_service import get_capability_summary, get_session_runtime_summary

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])


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


class CapabilityFactorIn(BaseModel):
    factor_kind: str
    display_name: str
    summary: str | None = None
    source_refs: list[dict] = []
    trace_refs: list[dict] = []
    artifact_ref: str | None = None
    artifact_sha256: str | None = None
    upstream_source_ref: str | None = None
    upstream_content_sha256: str | None = None
    license_report: dict = {}
    authoring_contract: dict = {}
    declared_components: dict = {}
    declared_permissions: dict = {}
    sensitivity_report: dict = {}
    reuse_score: dict = {}
    suggested_scope: str | None = None


class PromotionProposalIn(BaseModel):
    proposed_snapshot_kind: str
    proposed_catalog_scope: str = "workspace"
    proposed_activation_policy: str = "requestable"
    proposed_selector: dict = {}


class PromotionDecisionIn(BaseModel):
    reason: str | None = None
    resulting_snapshot_id: uuid.UUID | None = None


@router.get("/enterprise/capabilities/definitions")
async def list_capability_definitions(
    current_user: User = Depends(get_current_user),
):
    """List all known capability definitions and their mapped tools."""
    return get_all_capabilities()


@router.get("/enterprise/capabilities")
async def list_capability_policies(
    agent_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List capability policies for the current tenant, optionally filtered by agent."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    query = select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == target_tenant_id)
    if agent_id is not None:
        query = query.where(CapabilityPolicy.agent_id == agent_id)

    result = await db.execute(query.order_by(CapabilityPolicy.capability))
    return [CapabilityPolicyOut.model_validate(p) for p in result.scalars().all()]


@router.put("/enterprise/capabilities")
async def upsert_capability_policy(
    data: CapabilityPolicyUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a capability policy for the current tenant."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    # Find existing policy
    query = select(CapabilityPolicy).where(
        CapabilityPolicy.tenant_id == target_tenant_id,
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
            tenant_id=target_tenant_id,
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
            tenant_id=target_tenant_id,
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


@router.delete("/enterprise/capabilities/{policy_id}")
async def delete_capability_policy(
    policy_id: uuid.UUID,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a capability policy (reverts to default allow)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(
        select(CapabilityPolicy).where(
            CapabilityPolicy.id == policy_id,
            CapabilityPolicy.tenant_id == target_tenant_id,
        )
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    await db.delete(policy)
    await db.commit()
    return {"status": "deleted"}


@router.get("/agents/{agent_id}/capability-factors")
async def list_agent_capability_factors(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    tenant_id = _require_user_tenant_id(current_user)
    return await list_capability_factors(db, tenant_id=tenant_id, agent_id=agent_id)


@router.post("/agents/{agent_id}/capability-factors")
async def create_agent_capability_factor(
    agent_id: uuid.UUID,
    data: CapabilityFactorIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    tenant_id = _require_user_tenant_id(current_user)
    try:
        return await capture_capability_factor(
            db,
            tenant_id=tenant_id,
            originating_agent_id=agent_id,
            originating_user_id=current_user.id,
            data=data.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/enterprise/capability-factors")
async def list_enterprise_capability_factors(
    status: str | None = None,
    factor_kind: str | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    return await list_capability_factors(db, tenant_id=target_tenant_id, status=status, factor_kind=factor_kind)


@router.post("/enterprise/capability-factors/{factor_id}/archive")
async def archive_enterprise_capability_factor(
    factor_id: uuid.UUID,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        return await archive_capability_factor(db, tenant_id=target_tenant_id, factor_id=factor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/enterprise/capability-promotion-proposals")
async def list_enterprise_capability_promotion_proposals(
    factor_id: uuid.UUID | None = None,
    decision: str | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    return await list_promotion_proposals(db, tenant_id=target_tenant_id, factor_id=factor_id, decision=decision)


@router.post("/enterprise/capability-factors/{factor_id}/promotion-proposals")
async def create_capability_promotion_proposal(
    factor_id: uuid.UUID,
    data: PromotionProposalIn,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        return await create_promotion_proposal(
            db,
            tenant_id=target_tenant_id,
            factor_id=factor_id,
            requested_by_user_id=current_user.id,
            data=data.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/enterprise/capability-promotion-proposals/{proposal_id}/approve")
async def approve_capability_promotion_proposal(
    proposal_id: uuid.UUID,
    data: PromotionDecisionIn,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        return await decide_promotion_proposal(
            db,
            tenant_id=target_tenant_id,
            proposal_id=proposal_id,
            approver_id=current_user.id,
            decision="approved",
            reason=data.reason,
            resulting_snapshot_id=data.resulting_snapshot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/enterprise/capability-promotion-proposals/{proposal_id}/reject")
async def reject_capability_promotion_proposal(
    proposal_id: uuid.UUID,
    data: PromotionDecisionIn,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        return await decide_promotion_proposal(
            db,
            tenant_id=target_tenant_id,
            proposal_id=proposal_id,
            approver_id=current_user.id,
            decision="rejected",
            reason=data.reason,
            resulting_snapshot_id=data.resulting_snapshot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/capability-summary")
async def get_agent_capability_summary(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Comprehensive capability summary: kernel tools, plugins, policies, pending approvals."""
    await check_agent_access(db, current_user, agent_id)
    return await get_capability_summary(db, agent_id)


@router.get("/chat/sessions/{session_id}/runtime-summary")
async def get_session_runtime(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Runtime summary for a chat session: activated tool groups, used tools, blocked capabilities."""
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await check_agent_access(db, current_user, session.agent_id)
    return await get_session_runtime_summary(db, session_id)


def _require_user_tenant_id(current_user: User) -> uuid.UUID:
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    return tenant_id
