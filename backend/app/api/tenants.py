"""Tenant (Company) management API.

Public endpoints for self-service company creation and joining.
Admin endpoints for platform-level company management.
"""

import contextlib
import re
import secrets
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sqla_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, require_role
from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/tenants", tags=["tenants"])


# ─── Schemas ────────────────────────────────────────────


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    im_provider: str
    timezone: str = "UTC"
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TenantUpdate(BaseModel):
    name: str | None = None
    im_provider: str | None = None
    timezone: str | None = None
    is_active: bool | None = None


class TenantDeleteOut(BaseModel):
    fallback_tenant_id: uuid.UUID | None = None
    needs_company_setup: bool = False


# ─── Helpers ────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Generate a URL-friendly slug from a company name."""
    # Replace CJK and non-alphanumeric chars with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    slug = slug.strip("-")[:40]
    if not slug:
        slug = "company"
    # Add short random suffix for uniqueness
    slug = f"{slug}-{secrets.token_hex(3)}"
    return slug


def _normalize_invitation_code(code: str) -> str:
    return code.strip().upper()


async def _scope_session_to_tenant(db: AsyncSession, tenant_id: uuid.UUID | str) -> uuid.UUID:
    """Pin this request transaction to the tenant selected by a validated invite."""
    pinned = await pin_rls_tenant_context(db, tenant_id)
    if pinned is None:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return pinned


@contextlib.asynccontextmanager
async def _platform_admin_bypass_scope(
    db: AsyncSession,
    current_user: User,
    *,
    reason: str,
):
    """Use audited RLS bypass only for platform-wide tenant administration."""
    if current_user.role == "platform_admin":
        async with enter_rls_bypass(db, reason=reason, actor_id=str(current_user.id)) as bypass_db:
            yield bypass_db
        return
    yield db


# ─── Self-Service: Create Company ───────────────────────


@router.post("/self-create", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def self_create_company(
    data: TenantCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new company (self-service). The creator becomes org_admin."""
    # Must not already belong to a company
    if current_user.tenant_id is not None:
        raise HTTPException(status_code=400, detail="You already belong to a company")

    # Check if self-creation is allowed
    from app.models.system_settings import SystemSetting

    setting = await db.execute(select(SystemSetting).where(SystemSetting.key == "allow_self_create_company"))
    s = setting.scalar_one_or_none()
    allowed = s.value.get("enabled", True) if s else True
    if not allowed and current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Company self-creation is currently disabled")

    slug = _slugify(data.name)
    async with enter_rls_bypass(
        db,
        reason="self-service company creation",
        actor_id=str(current_user.id),
    ) as bypass_db:
        tenant = Tenant(name=data.name, slug=slug, im_provider="web_only")
        bypass_db.add(tenant)
        await bypass_db.flush()

        # The old User row is tenantless, so a tenant-scoped UPDATE cannot see
        # it through RLS USING.  Perform the validated NULL→tenant transition
        # before leaving the bootstrap authority scope.
        current_user.tenant_id = tenant.id
        current_user.role = "org_admin" if current_user.role == "member" else current_user.role
        current_user.quota_tokens_per_day = tenant.default_tokens_per_day
        current_user.quota_tokens_per_month = tenant.default_tokens_per_month
        await bypass_db.flush()

    await _scope_session_to_tenant(db, tenant.id)

    return TenantOut.model_validate(tenant)


# ─── Self-Service: Join Company via Invite Code ─────────


class JoinRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=32)


class JoinResponse(BaseModel):
    tenant: TenantOut
    role: str
    access_token: str


@router.post("/join", response_model=JoinResponse)
async def join_company(
    data: JoinRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Join an existing company using an invitation code."""
    if current_user.tenant_id is not None:
        raise HTTPException(status_code=400, detail="You already belong to a company")

    from app.models.invitation_code import InvitationCode

    normalized_code = _normalize_invitation_code(data.invitation_code)
    if not normalized_code:
        raise HTTPException(status_code=400, detail="Invalid invitation code")

    async with enter_rls_bypass(
        db,
        reason="tenant join invitation lookup",
        actor_id=str(current_user.id),
    ) as bypass_db:
        ic_result = await bypass_db.execute(
            select(InvitationCode)
            .where(
                InvitationCode.code == normalized_code,
                InvitationCode.is_active,
                InvitationCode.tenant_id.is_not(None),
            )
            .with_for_update()
        )
        code_obj = ic_result.scalar_one_or_none()
        if not code_obj:
            raise HTTPException(status_code=400, detail="Invalid invitation code")
        if code_obj.used_count >= code_obj.max_uses:
            raise HTTPException(status_code=400, detail="Invitation code has reached its usage limit")

        # Find the company while tenantless users still have no RLS tenant scope.
        t_result = await bypass_db.execute(select(Tenant).where(Tenant.id == code_obj.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if not tenant or not tenant.is_active:
            raise HTTPException(status_code=400, detail="Company not found or is disabled")

        # The old User row is tenantless and therefore invisible to a target-
        # tenant UPDATE policy.  Complete the validated NULL→tenant transition
        # and invitation receipt while this narrow, row-bound authority scope
        # is still active; only then pin normal route consumption to the tenant.
        admin_check = await bypass_db.execute(
            select(sqla_func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant.id,
                User.role.in_(["org_admin", "platform_admin"]),
            )
        )
        has_admin = admin_check.scalar() > 0

        # First joiner of an empty company becomes org_admin
        assigned_role = "member" if has_admin else "org_admin"

        # Assign user to company
        current_user.tenant_id = tenant.id
        if current_user.role == "member":
            current_user.role = assigned_role
        # Inherit token quota defaults from tenant
        current_user.quota_tokens_per_day = tenant.default_tokens_per_day
        current_user.quota_tokens_per_month = tenant.default_tokens_per_month

        # Increment invitation code usage in the same transaction as membership.
        code_obj.used_count += 1
        await bypass_db.flush()

    target_tenant_id = await _scope_session_to_tenant(db, code_obj.tenant_id)
    await db.commit()

    return JoinResponse(
        tenant=TenantOut.model_validate(tenant),
        role=current_user.role,
        access_token=create_access_token(
            str(current_user.id),
            current_user.role,
            tenant_id=str(target_tenant_id),
        ),
    )


# ─── Registration Config ───────────────────────────────


@router.get("/registration-config")
async def get_registration_config(db: AsyncSession = Depends(get_db)):
    """Public — returns whether self-creation of companies is allowed."""
    from app.models.system_settings import SystemSetting

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "allow_self_create_company"))
    s = result.scalar_one_or_none()
    allowed = s.value.get("enabled", True) if s else True
    return {"allow_self_create_company": allowed}


# ─── Authenticated: List / Get ──────────────────────────


@router.get("/", response_model=list[TenantOut])
async def list_tenants(
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants (platform_admin only)."""
    async with enter_rls_bypass(
        db,
        reason="platform-admin list tenants",
        actor_id=str(current_user.id),
    ) as bypass_db:
        result = await bypass_db.execute(
            select(Tenant).where(Tenant.id != TENANT_SCOPE_QUARANTINE_ID).order_by(Tenant.created_at.desc())
        )
    return [TenantOut.model_validate(t) for t in result.scalars().all()]


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant details. Platform admins can view any; org_admins only their own."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin" and str(current_user.tenant_id) != str(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")
    async with _platform_admin_bypass_scope(
        db,
        current_user,
        reason="platform-admin get tenant",
    ) as scoped_db:
        result = await scoped_db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    data: TenantUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant settings.

    Platform admins can update any tenant field.
    Org admins can update only their own tenant's basic profile fields.
    """
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin" and str(current_user.tenant_id) != str(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")

    updates = data.model_dump(exclude_unset=True)
    if current_user.role == "org_admin":
        allowed_fields = {"name", "timezone"}
        disallowed = sorted(set(updates) - allowed_fields)
        if disallowed:
            raise HTTPException(
                status_code=403,
                detail="Org admins can only update company name and timezone",
            )

    async with _platform_admin_bypass_scope(
        db,
        current_user,
        reason="platform-admin update tenant",
    ) as scoped_db:
        result = await scoped_db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        for field, value in updates.items():
            setattr(tenant, field, value)
        await scoped_db.flush()
    return TenantOut.model_validate(tenant)


@router.delete("/{tenant_id}", response_model=TenantDeleteOut)
async def delete_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a tenant and detach its users.

    This is intentionally a soft-delete flow. The codebase still has many
    tenant-linked records without uniform cascade rules, so hard deletion would
    be unsafe. Instead we:
    - mark the tenant inactive,
    - pause running agents in that tenant,
    - detach users from the tenant and departments,
    - optionally move platform admins to another active tenant for continuity.
    """
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin" and str(current_user.tenant_id) != str(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")

    async with _platform_admin_bypass_scope(
        db,
        current_user,
        reason="platform-admin delete tenant",
    ) as scoped_db:
        result = await scoped_db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        fallback_tenant_id: uuid.UUID | None = None
        if current_user.role == "platform_admin":
            fallback_result = await scoped_db.execute(
                select(Tenant)
                .where(
                    Tenant.id != tenant_id,
                    Tenant.is_active,
                )
                .order_by(Tenant.created_at.asc())
            )
            fallback_tenant = fallback_result.scalar_one_or_none()
            fallback_tenant_id = fallback_tenant.id if fallback_tenant else None

        running_agents = await scoped_db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.status == "running",
            )
        )
        for agent in running_agents.scalars().all():
            agent.status = "paused"

        tenant_users = await scoped_db.execute(select(User).where(User.tenant_id == tenant_id))
        for user in tenant_users.scalars().all():
            user.department_id = None
            if user.role == "platform_admin" and fallback_tenant_id is not None:
                user.tenant_id = fallback_tenant_id
                continue

            user.tenant_id = None
            if user.role != "platform_admin":
                user.role = "member"

        # Offboarding scrub: never leave a deactivated tenant's API keys in the DB.
        from app.services.tool_config_service import scrub_tenant_tool_secrets
        from app.services.channel_secret_storage import scrub_tenant_channel_secrets

        await scrub_tenant_tool_secrets(scoped_db, tenant_id)
        await scrub_tenant_channel_secrets(scoped_db, tenant_id)

        tenant.is_active = False
        await scoped_db.flush()

        return TenantDeleteOut(
            fallback_tenant_id=fallback_tenant_id,
            needs_company_setup=fallback_tenant_id is None,
        )


@router.put("/{tenant_id}/assign-user/{user_id}")
async def assign_user_to_tenant(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a user to a tenant with a specific role."""
    async with enter_rls_bypass(
        db,
        reason="platform-admin assign user to tenant",
        actor_id=str(current_user.id),
    ) as bypass_db:
        # Verify tenant
        t_result = await bypass_db.execute(select(Tenant).where(Tenant.id == tenant_id))
        if not t_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Tenant not found")

        # Verify user
        u_result = await bypass_db.execute(select(User).where(User.id == user_id))
        user = u_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if role not in ("org_admin", "member"):
            raise HTTPException(status_code=400, detail="Invalid role")

        user.tenant_id = tenant_id
        user.role = role
        await bypass_db.flush()
    return {"status": "ok", "user_id": str(user_id), "tenant_id": str(tenant_id), "role": role}
