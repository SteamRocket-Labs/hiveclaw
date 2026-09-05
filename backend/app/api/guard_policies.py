"""Guard Policy management endpoints (ARCHITECTURE.md §7.4).

GET  /guard-policies — read current tenant policy
PUT  /guard-policies — update policy (admin only), bumps guard version + sync_version
"""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.database import get_db
from app.models.audit import AuditLog
from app.models.guard_policy import GuardPolicy
from app.models.user import User
from app.services.sync_service import bump_sync_version
from app.tools.guard_policy import validate_guard_policy_snapshot

router = APIRouter(tags=["guard-policies"])


def _require_org_admin(current_user: User) -> None:
    """Company administrator gate (PDEC-013): org admins and scoped platform
    administrators; the per-route tenant pin enforces the company binding."""
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator access required",
        )


# ─── Schemas ────────────────────────────────────────────


class GuardPolicyOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    zone_guard: dict
    egress_guard: dict

    model_config = {"from_attributes": True}


class GuardPolicyUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    zone_guard: dict | None = None
    egress_guard: dict | None = None

    @model_validator(mode="after")
    def require_changed_lane(self):
        if self.zone_guard is None and self.egress_guard is None:
            raise ValueError("At least one guardrail lane must be provided")
        return self


# ─── Helpers ────────────────────────────────────────────


async def _get_or_create_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> GuardPolicy:
    """Get existing policy or create a default empty one for the tenant."""
    statement = select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    policy = result.scalar_one_or_none()
    if not policy:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"guard_policy:{tenant_id}"},
        )
        result = await db.execute(statement)
        policy = result.scalar_one_or_none()
    if not policy:
        policy = GuardPolicy(tenant_id=tenant_id)
        db.add(policy)
        await db.flush()
    return policy


def _content_hash(*, zone_guard: dict, egress_guard: dict) -> str:
    payload = json.dumps(
        {"zone_guard": zone_guard, "egress_guard": egress_guard},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


# ─── Endpoints ──────────────────────────────────────────


@router.get("/guard-policies", response_model=GuardPolicyOut)
async def get_guard_policy(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get the Guard policy for the current tenant."""
    _require_org_admin(current_user)
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
    _require_org_admin(current_user)
    if not current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No tenant assigned")

    policy = await _get_or_create_policy(db, current_user.tenant_id, for_update=True)
    if policy.version != body.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "guard_policy_version_conflict",
                "expected_version": body.expected_version,
                "current_version": policy.version,
            },
        )

    previous_zone_guard = dict(policy.zone_guard or {})
    previous_egress_guard = dict(policy.egress_guard or {})
    next_zone_guard = dict(body.zone_guard) if body.zone_guard is not None else previous_zone_guard
    next_egress_guard = dict(body.egress_guard) if body.egress_guard is not None else previous_egress_guard
    try:
        validate_guard_policy_snapshot(
            {
                "zone_guard": next_zone_guard,
                "egress_guard": next_egress_guard,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    previous_version = policy.version
    changed_lanes = []
    if body.zone_guard is not None:
        policy.zone_guard = next_zone_guard
        changed_lanes.append("zone_guard")
    if body.egress_guard is not None:
        policy.egress_guard = next_egress_guard
        changed_lanes.append("egress_guard")
    policy.version += 1
    db.add(
        AuditLog(
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            action="tenant.guard_policy.updated",
            details={
                "previous_version": previous_version,
                "new_version": policy.version,
                "changed_lanes": changed_lanes,
                "previous_content_hash": _content_hash(
                    zone_guard=previous_zone_guard,
                    egress_guard=previous_egress_guard,
                ),
                "new_content_hash": _content_hash(
                    zone_guard=next_zone_guard,
                    egress_guard=next_egress_guard,
                ),
            },
        )
    )
    await db.flush()

    await bump_sync_version(db, current_user.tenant_id)
    return GuardPolicyOut.model_validate(policy)
