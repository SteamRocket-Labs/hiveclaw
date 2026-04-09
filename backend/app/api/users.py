"""User management API — admin-only user listing and quota management."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class UserQuotaUpdate(BaseModel):
    quota_tokens_per_day: int | None = None
    quota_tokens_per_month: int | None = None


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
    source: str = 'registered'

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[UserOut])
async def list_users(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all users in the specified tenant (admin only)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    # Platform admins can view any tenant; org_admins only their own
    tid = tenant_id if tenant_id and current_user.role == "platform_admin" else str(current_user.tenant_id)

    # Filter users by tenant — platform_admins only shown in their own tenant
    result = await db.execute(
        select(User).where(
            User.tenant_id == tid
        ).order_by(User.created_at.asc())
    )
    users = result.scalars().all()

    out = []
    for u in users:
        count_result = await db.execute(
            select(func.count()).select_from(Agent).where(
                Agent.creator_id == u.id,
            )
        )
        agents_count = count_result.scalar() or 0

        user_dict = {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "display_name": u.display_name,
            "role": u.role,
            "is_active": u.is_active,
            "quota_tokens_per_day": u.quota_tokens_per_day,
            "quota_tokens_per_month": u.quota_tokens_per_month,
            "tokens_used_today": u.tokens_used_today,
            "tokens_used_month": u.tokens_used_month,
            "tokens_used_total": u.tokens_used_total,
            "agents_count": agents_count,
            "feishu_open_id": getattr(u, 'feishu_open_id', None),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "source": 'feishu' if getattr(u, 'feishu_open_id', None) else 'registered',
        }
        out.append(UserOut(**user_dict))
    return out


@router.patch("/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: uuid.UUID,
    data: UserQuotaUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's quota settings (admin only)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role != "platform_admin" and user.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    if data.quota_tokens_per_day is not None:
        user.quota_tokens_per_day = data.quota_tokens_per_day
    if data.quota_tokens_per_month is not None:
        user.quota_tokens_per_month = data.quota_tokens_per_month

    await db.commit()
    await db.refresh(user)

    count_result = await db.execute(
        select(func.count()).select_from(Agent).where(Agent.creator_id == user.id)
    )
    agents_count = count_result.scalar() or 0

    return UserOut(
        id=user.id, username=user.username, email=user.email,
        display_name=user.display_name, role=user.role, is_active=user.is_active,
        quota_tokens_per_day=user.quota_tokens_per_day,
        quota_tokens_per_month=user.quota_tokens_per_month,
        tokens_used_today=user.tokens_used_today,
        tokens_used_month=user.tokens_used_month,
        tokens_used_total=user.tokens_used_total,
        agents_count=agents_count,
    )
