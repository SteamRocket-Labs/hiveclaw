"""User management API — admin-only user listing and quota management."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.core.security import get_current_user
from app.core.permissions import agent_owned_by_clause
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.local_bridge import LocalAgentBridgePairingSession
from app.models.user import User
from app.services.external_principal_service import platform_member_user_predicate
from app.services.user_offboarding_service import (
    build_user_offboarding_preview,
    find_user_offboarding_replay,
    offboard_loaded_user,
    publish_user_offboarding_runtime_cancellations,
)

router = APIRouter(prefix="/users", tags=["users"])


def _require_org_admin(current_user: User) -> None:
    """Company administrator surface (PDEC-013).

    Organization administrators and scoped platform administrators (operating
    inside their selected company) both manage company users; ordinary
    members are denied. The tenant binding itself is enforced downstream by
    ``resolve_and_pin_tenant_scope``/``_load_target_user``.
    """
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization administrator access required")


class UserQuotaUpdate(BaseModel):
    quota_tokens_per_day: int | None = Field(default=None, ge=0)
    quota_tokens_per_month: int | None = Field(default=None, ge=0)


class UserRoleUpdate(BaseModel):
    role: str = Field(pattern="^(member|org_admin)$")


class UserOffboardingRequest(BaseModel):
    successor_user_id: uuid.UUID
    expected_agent_ids: list[uuid.UUID]
    reason: str = Field(min_length=3, max_length=500)
    request_id: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    role: str
    is_active: bool
    # Token quota
    quota_tokens_per_day: int | None = None
    quota_tokens_per_month: int | None = None
    tokens_used_today: int = 0
    tokens_used_month: int = 0
    tokens_used_total: int = 0
    # Computed
    agents_count: int = 0
    # Source info
    feishu_open_id: str | None = None
    created_at: str | None = None
    source: str = "registered"

    model_config = {"from_attributes": True}


def _offboarding_response(receipt) -> dict:
    return {
        "status": receipt.status,
        "user_id": str(receipt.user_id),
        "successor_user_id": str(receipt.successor_user_id),
        "transferred_agent_ids": [str(value) for value in receipt.transferred_agent_ids],
        "transferred_agent_count": len(receipt.transferred_agent_ids),
        "revocations": {
            "agent_permissions": receipt.revocations.agent_permissions,
            "resource_permissions": receipt.revocations.resource_permissions,
            "knowledge_grants": receipt.revocations.knowledge_grants,
            "refresh_tokens": receipt.revocations.refresh_tokens,
            "external_principals": receipt.revocations.external_principals,
            "local_bridge_connections": receipt.revocations.local_bridge_connections,
            "runtime_tasks": receipt.revocations.runtime_tasks,
            "pending_approvals": receipt.revocations.pending_approvals,
        },
        "request_id": receipt.request_id,
    }


async def _agent_count(db: AsyncSession, user: User) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Agent)
        .where(
            agent_owned_by_clause(user.id),
            Agent.tenant_id == user.tenant_id,
        )
    )
    return int(result.scalar() or 0)


def _user_out(user: User, *, agents_count: int) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        quota_tokens_per_day=user.quota_tokens_per_day,
        quota_tokens_per_month=user.quota_tokens_per_month,
        tokens_used_today=user.tokens_used_today,
        tokens_used_month=user.tokens_used_month,
        tokens_used_total=user.tokens_used_total,
        agents_count=agents_count,
        feishu_open_id=getattr(user, "feishu_open_id", None),
        created_at=user.created_at.isoformat() if getattr(user, "created_at", None) else None,
        source="feishu" if getattr(user, "feishu_open_id", None) else "registered",
    )


async def _load_target_user(
    db: AsyncSession,
    *,
    current_user: User,
    user_id: uuid.UUID,
    tenant_id: str | None,
    lock: bool = False,
) -> User:
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    stmt = select(User).where(User.id == user_id, User.tenant_id == target_tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    target_user = (await db.execute(stmt)).scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return target_user


async def _lock_target_user_claimable_pairings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Lock the member's claimable pairing rows before any identity lock.

    The claimable set (pending/approved for exactly this tenant+member)
    matches the authority-revocation UPDATE executed later in the same
    transaction, so a device-code exchange for this member either waits
    here — and re-reads a rejected pairing after offboarding commits — or
    commits first and its fresh connection is revoked by that UPDATE.
    """
    await db.execute(
        select(LocalAgentBridgePairingSession)
        .where(
            LocalAgentBridgePairingSession.tenant_id == tenant_id,
            LocalAgentBridgePairingSession.user_id == user_id,
            LocalAgentBridgePairingSession.status.in_(("pending", "approved")),
        )
        .order_by(LocalAgentBridgePairingSession.id)
        .with_for_update()
    )


@router.get("/", response_model=list[UserOut])
async def list_users(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the specified tenant (admin only)."""
    _require_org_admin(current_user)
    tid = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    # Organization membership is always scoped to the administrator's tenant.
    result = await db.execute(
        select(User)
        .where(
            User.tenant_id == tid,
            platform_member_user_predicate(User.email),
        )
        .order_by(User.created_at.asc())
    )
    users = result.scalars().all()

    out = []
    for u in users:
        out.append(_user_out(u, agents_count=await _agent_count(db, u)))
    return out


@router.patch("/{user_id}/role", response_model=UserOut)
async def update_user_role(
    user_id: uuid.UUID,
    data: UserRoleUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a tenant member role without trusting a browser-side role guess."""
    _require_org_admin(current_user)
    target = await _load_target_user(
        db,
        current_user=current_user,
        user_id=user_id,
        tenant_id=tenant_id,
        lock=True,
    )
    if target.role == "platform_admin":
        raise HTTPException(status_code=403, detail="Platform administrator role cannot be changed here")
    if target.id == current_user.id and data.role != "org_admin":
        remaining = await db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == target.tenant_id,
                User.is_active.is_(True),
                User.role == "org_admin",
                User.id != target.id,
            )
        )
        if int(remaining.scalar() or 0) == 0:
            raise HTTPException(
                status_code=409, detail="Assign another company administrator before changing this role"
            )
    old_role = target.role
    target.role = data.role
    db.add(
        AuditLog(
            user_id=current_user.id,
            tenant_id=target.tenant_id,
            action="user:role_changed",
            details={"target_user_id": str(target.id), "from_role": old_role, "to_role": data.role},
        )
    )
    await db.flush()
    return _user_out(target, agents_count=await _agent_count(db, target))


@router.get("/{user_id}/offboarding-preview")
async def preview_user_offboarding(
    user_id: uuid.UUID,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the exact impact and eligible successor set before offboarding."""
    _require_org_admin(current_user)
    target = await _load_target_user(
        db,
        current_user=current_user,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    if target.role == "platform_admin":
        raise HTTPException(status_code=400, detail="Platform administrators cannot be offboarded from a tenant")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Administrators cannot offboard their own account")
    preview = await build_user_offboarding_preview(db, target_user=target, actor=current_user)
    return {
        "user_id": str(preview.user_id),
        "display_name": preview.display_name,
        "is_active": preview.is_active,
        "owned_agents": preview.owned_agents,
        "eligible_successors": preview.eligible_successors,
        "default_successor_id": str(preview.default_successor_id) if preview.default_successor_id else None,
        "revocations": {
            "agent_permissions": preview.agent_permissions,
            "resource_permissions": preview.resource_permissions,
            "knowledge_grants": preview.knowledge_grants,
            "refresh_tokens": preview.refresh_tokens,
            "external_principals": preview.external_principals,
            "local_bridge_connections": preview.local_bridge_connections,
            "runtime_tasks": preview.runtime_tasks,
            "pending_approvals": preview.pending_approvals,
        },
        "blockers": [] if preview.eligible_successors else ["No active company administrator can receive ownership"],
    }


@router.post("/{user_id}/offboard")
async def offboard_user(
    user_id: uuid.UUID,
    data: UserOffboardingRequest,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically transfer Agent ownership, revoke authority, and deactivate a User."""
    _require_org_admin(current_user)
    # Pairing lifecycle ordering: an in-flight device-code exchange holds
    # its pairing row FOR UPDATE and then takes implicit FK KEY SHARE
    # locks on User/Agent rows when it inserts the connection. Locking
    # this member's claimable pairings FIRST — before any identity row
    # lock — keeps the global order pairing→identity, so offboarding can
    # never hold User/Agent locks while an exchange waits behind them
    # (the previous identity-first order produced the single-user sibling
    # of the tenant retirement ABBA cycle, PostgreSQL SQLSTATE 40P01).
    # The plain preview and role routes never touch pairing rows and keep
    # their old lock set.
    pinned_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    await _lock_target_user_claimable_pairings(db, tenant_id=pinned_tenant_id, user_id=user_id)
    target = await _load_target_user(
        db,
        current_user=current_user,
        user_id=user_id,
        tenant_id=tenant_id,
        lock=True,
    )
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Administrators cannot offboard their own account")
    replay = await find_user_offboarding_replay(
        db,
        target_user=target,
        successor_user_id=data.successor_user_id,
        expected_agent_ids=data.expected_agent_ids,
        reason=data.reason,
        request_id=data.request_id,
    )
    if replay is not None:
        await db.commit()
        await publish_user_offboarding_runtime_cancellations(replay)
        return _offboarding_response(replay)
    successor = (
        await db.execute(
            select(User).where(User.id == data.successor_user_id, User.tenant_id == target.tenant_id).with_for_update()
        )
    ).scalar_one_or_none()
    if successor is None:
        raise HTTPException(status_code=404, detail="Successor not found")
    receipt = await offboard_loaded_user(
        db,
        target_user=target,
        successor=successor,
        actor=current_user,
        expected_agent_ids=data.expected_agent_ids,
        reason=data.reason,
        request_id=data.request_id,
    )
    # Commit the complete authority change before advisory cross-process stop
    # signals are published. The RuntimeTask status + claim-version fence is
    # authoritative even when Redis is unavailable.
    await db.commit()
    await publish_user_offboarding_runtime_cancellations(receipt)
    return _offboarding_response(receipt)


@router.patch("/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: uuid.UUID,
    data: UserQuotaUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's quota settings (admin only)."""
    _require_org_admin(current_user)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    if "quota_tokens_per_day" in data.model_fields_set:
        user.quota_tokens_per_day = data.quota_tokens_per_day
    if "quota_tokens_per_month" in data.model_fields_set:
        user.quota_tokens_per_month = data.quota_tokens_per_month

    await db.commit()
    await db.refresh(user)

    count_result = await db.execute(
        select(func.count())
        .select_from(Agent)
        .where(
            agent_owned_by_clause(user.id),
            Agent.tenant_id == user.tenant_id,
        )
    )
    agents_count = count_result.scalar() or 0

    return _user_out(user, agents_count=agents_count)
