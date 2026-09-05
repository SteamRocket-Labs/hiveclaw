"""Tenant (Company) management API.

Public endpoints for self-service company creation and joining.
Admin endpoints for platform-level company management.
"""

import contextlib
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func as sqla_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, require_role
from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.local_bridge import LocalAgentBridgePairingSession
from app.models.tenant import Tenant
from app.models.user import User
from app.services.user_offboarding_service import (
    publish_user_authority_runtime_cancellations,
    revoke_user_authority,
)

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


class TenantRetiredUserOut(BaseModel):
    user_id: uuid.UUID
    status: Literal["deactivated", "already_inactive"]
    is_active: Literal[False] = False
    revocations: dict[str, int]


class TenantRetirementRequest(BaseModel):
    expected_user_ids: list[uuid.UUID]
    reason: str = Field(min_length=3, max_length=500)
    request_id: str = Field(min_length=1, max_length=200)

    model_config = {"extra": "forbid"}


class TenantDeleteOut(BaseModel):
    fallback_tenant_id: uuid.UUID | None = None
    needs_company_setup: bool = False
    retirement_status: Literal["not_requested", "retired", "already_retired"] = "not_requested"
    retirement_request_id: str | None = None
    retired_users: list[TenantRetiredUserOut] = Field(default_factory=list)


class TenantUserAssignment(BaseModel):
    email: EmailStr
    role: str = Field(default="member", pattern="^(member|org_admin)$")


class TenantUserAssignmentOut(BaseModel):
    status: Literal["ok", "already_assigned"]
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Literal["member", "org_admin"]
    membership_committed: bool
    client_token_refresh_required: bool


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


def _apply_new_tenant_membership(user: User, tenant: Tenant, *, role: str) -> None:
    """Apply a fresh membership without carrying usage from a former company."""
    user.tenant_id = tenant.id
    user.role = role
    user.department_id = None
    user.quota_tokens_per_day = tenant.default_tokens_per_day
    user.quota_tokens_per_month = tenant.default_tokens_per_month
    user.tokens_used_today = 0
    user.tokens_used_month = 0
    user.tokens_used_total = 0
    user.tokens_reset_at = None


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
        user_result = await bypass_db.execute(select(User).where(User.id == current_user.id).with_for_update())
        locked_user = user_result.scalar_one_or_none()
        if locked_user is None or not locked_user.is_active:
            raise HTTPException(status_code=409, detail="User is no longer eligible to create a company")
        if locked_user.tenant_id is not None:
            raise HTTPException(status_code=409, detail="User already belongs to a company")

        tenant = Tenant(name=data.name, slug=slug, im_provider="web_only")
        bypass_db.add(tenant)
        await bypass_db.flush()

        # The old User row is tenantless, so a tenant-scoped UPDATE cannot see
        # it through RLS USING.  Perform the validated NULL→tenant transition
        # before leaving the bootstrap authority scope.
        role = "org_admin" if locked_user.role == "member" else locked_user.role
        _apply_new_tenant_membership(locked_user, tenant, role=role)
        await bypass_db.flush()

    await _scope_session_to_tenant(db, tenant.id)

    return TenantOut.model_validate(tenant)


# ─── Self-Service: Join Company via Invite Code ─────────


class JoinRequest(BaseModel):
    invitation_code: str = Field(min_length=1, max_length=32)

    model_config = {"extra": "forbid"}


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
    if current_user.role == "platform_admin":
        raise HTTPException(status_code=409, detail="Platform administrators cannot consume company invitations")

    from app.models.invitation_code import InvitationCode

    normalized_code = _normalize_invitation_code(data.invitation_code)
    if not normalized_code:
        raise HTTPException(status_code=400, detail="Invalid invitation code")

    if current_user.tenant_id is not None:
        # Recover an exact post-commit retry without consuming the code again.
        # The explicit tenant predicate keeps a code from another company
        # indistinguishable from any other invalid replay under normal RLS.
        existing_tenant_id = await _scope_session_to_tenant(db, current_user.tenant_id)
        code_result = await db.execute(
            select(InvitationCode.id).where(
                InvitationCode.code == normalized_code,
                InvitationCode.tenant_id == existing_tenant_id,
            )
        )
        if code_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="User already belongs to another company")
        tenant_result = await db.execute(
            select(Tenant).where(
                Tenant.id == existing_tenant_id,
                Tenant.is_active,
            )
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None:
            raise HTTPException(status_code=409, detail="User's company is no longer available")
        return JoinResponse(
            tenant=TenantOut.model_validate(tenant),
            role=current_user.role,
            access_token=create_access_token(
                str(current_user.id),
                current_user.role,
                tenant_id=str(existing_tenant_id),
            ),
        )

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
        t_result = await bypass_db.execute(select(Tenant).where(Tenant.id == code_obj.tenant_id).with_for_update())
        tenant = t_result.scalar_one_or_none()
        if not tenant or not tenant.is_active:
            raise HTTPException(status_code=400, detail="Company not found or is disabled")

        user_result = await bypass_db.execute(select(User).where(User.id == current_user.id).with_for_update())
        locked_user = user_result.scalar_one_or_none()
        if locked_user is None or not locked_user.is_active:
            raise HTTPException(status_code=409, detail="User is no longer eligible to join a company")
        if locked_user.tenant_id is not None:
            raise HTTPException(status_code=409, detail="User already belongs to a company")
        if locked_user.role == "platform_admin":
            raise HTTPException(status_code=409, detail="Platform administrators cannot consume company invitations")

        # The old User row is tenantless and therefore invisible to a target-
        # tenant UPDATE policy.  Complete the validated NULL→tenant transition
        # and invitation receipt while this narrow, row-bound authority scope
        # is still active; only then pin normal route consumption to the tenant.
        invited_role = code_obj.granted_role
        if invited_role not in {"member", "org_admin"}:
            raise HTTPException(status_code=409, detail="Invitation code role is invalid")

        role = invited_role
        _apply_new_tenant_membership(locked_user, tenant, role=role)

        # Increment invitation code usage in the same transaction as membership.
        code_obj.used_count += 1
        await bypass_db.flush()

    target_tenant_id = await _scope_session_to_tenant(db, code_obj.tenant_id)
    await db.commit()

    return JoinResponse(
        tenant=TenantOut.model_validate(tenant),
        role=locked_user.role,
        access_token=create_access_token(
            str(locked_user.id),
            locked_user.role,
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
    if "is_active" in updates:
        raise HTTPException(
            status_code=400,
            detail="Use the company toggle endpoint to change company status",
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
    retirement: TenantRetirementRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a tenant, with optional platform-owned identity retirement.

    This is intentionally a soft-delete flow. The codebase still has many
    tenant-linked records without uniform cascade rules, so hard deletion would
    be unsafe. Without a retirement payload, users remain active accounts and
    are detached from the company. A platform administrator may instead supply
    the exact complete non-platform User set; after every target-tenant Agent is
    already soft-deleted (regardless of owner), those Users are revoked and
    deactivated in the same transaction.
    Platform administrators are only moved or detached, never deactivated.
    """
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.role == "org_admin" and str(current_user.tenant_id) != str(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")
    if retirement is not None and current_user.role != "platform_admin":
        raise HTTPException(status_code=403, detail="Platform administrator access required for identity retirement")

    retirement_reason = str(retirement.reason or "").strip() if retirement is not None else ""
    retirement_request_id = str(retirement.request_id or "").strip() if retirement is not None else ""
    requested_user_ids = list(retirement.expected_user_ids) if retirement is not None else []
    if retirement is not None:
        if not retirement_reason:
            raise HTTPException(status_code=400, detail="Tenant retirement reason is required")
        if not retirement_request_id:
            raise HTTPException(status_code=400, detail="Tenant retirement request_id is required")
        if len(requested_user_ids) != len(set(requested_user_ids)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "tenant_retirement_user_set_stale"},
            )

    runtime_revocations = []
    response: TenantDeleteOut | None = None

    async with _platform_admin_bypass_scope(
        db,
        current_user,
        reason="platform-admin delete tenant",
    ) as scoped_db:
        # Pairing lifecycle ordering: an in-flight device-code exchange holds
        # its pairing row FOR UPDATE and then takes implicit FK KEY SHARE
        # locks on Tenant/User/Agent rows when it inserts the connection.
        # Locking this tenant's claimable pairings FIRST — before any
        # identity row lock — keeps the global order pairing→identity, so
        # retirement can never hold Tenant/User/Agent locks while an
        # exchange waits on them (the previous identity-first order produced
        # a real ABBA cycle, PostgreSQL SQLSTATE 40P01). The plain no-body
        # DELETE never touches pairing rows and keeps its old lock set.
        if retirement is not None:
            await scoped_db.execute(
                select(LocalAgentBridgePairingSession)
                .where(
                    LocalAgentBridgePairingSession.tenant_id == tenant_id,
                    LocalAgentBridgePairingSession.status.in_(("pending", "approved")),
                )
                .order_by(LocalAgentBridgePairingSession.id)
                .with_for_update()
            )
        # ponytail: tenant deletion globally locks active tenant rows; replace
        # with ordered lifecycle advisory locks only if delete throughput matters.
        result = await scoped_db.execute(
            select(Tenant).where(or_(Tenant.id == tenant_id, Tenant.is_active)).order_by(Tenant.id).with_for_update()
        )
        locked_tenants = result.scalars().all()
        tenant = next((row for row in locked_tenants if row.id == tenant_id), None)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        fallback_tenant_id: uuid.UUID | None = None
        fallback_tenant: Tenant | None = None
        if current_user.role == "platform_admin":
            fallback_tenant = next(
                (
                    row
                    for row in sorted(
                        locked_tenants,
                        key=lambda candidate: (
                            candidate.created_at is None,
                            str(candidate.created_at or ""),
                            str(candidate.id),
                        ),
                    )
                    if row.id != tenant_id and row.is_active
                ),
                None,
            )
            fallback_tenant_id = fallback_tenant.id if fallback_tenant else None

        if retirement is not None:
            replay_result = await scoped_db.execute(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action == "tenant:retired",
                    AuditLog.details["request_id"].as_string() == retirement_request_id,
                )
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
            replay = replay_result.scalar_one_or_none()
            if replay is not None:
                replay_details = dict(replay.details or {})
                same_input = str(replay_details.get("reason") or "").strip() == retirement_reason and {
                    str(value) for value in replay_details.get("expected_user_ids", [])
                } == {str(value) for value in requested_user_ids}
                if not same_input:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "tenant_retirement_idempotency_conflict",
                            "request_id": retirement_request_id,
                            "tenant_id": str(tenant_id),
                        },
                    )
                replay_fallback = replay_details.get("fallback_tenant_id")
                return TenantDeleteOut(
                    fallback_tenant_id=uuid.UUID(str(replay_fallback)) if replay_fallback else None,
                    needs_company_setup=not bool(replay_fallback),
                    retirement_status="already_retired",
                    retirement_request_id=retirement_request_id,
                    retired_users=[
                        TenantRetiredUserOut.model_validate(value) for value in replay_details.get("retired_users", [])
                    ],
                )
            if not tenant.is_active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "tenant_retirement_tenant_inactive", "tenant_id": str(tenant_id)},
                )

        tenant_users_result = await scoped_db.execute(
            select(User).where(User.tenant_id == tenant_id).order_by(User.id).with_for_update()
        )
        tenant_users = list(tenant_users_result.scalars().all())

        retired_users: list[TenantRetiredUserOut] = []
        if retirement is not None:
            retirable_users = [user for user in tenant_users if user.role != "platform_admin"]
            current_user_ids = {user.id for user in retirable_users}
            requested_user_id_set = set(requested_user_ids)
            protected_user_ids = sorted(
                str(user.id)
                for user in tenant_users
                if user.role == "platform_admin" and user.id in requested_user_id_set
            )
            if requested_user_id_set != current_user_ids or protected_user_ids:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "tenant_retirement_user_set_stale",
                        "expected_user_ids": sorted(str(value) for value in requested_user_ids),
                        "current_user_ids": sorted(str(value) for value in current_user_ids),
                        "protected_user_ids": protected_user_ids,
                    },
                )

            tenant_agents_result = await scoped_db.execute(
                select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.id).with_for_update()
            )
            tenant_agents = list(tenant_agents_result.scalars().all())
            # Any non-deleted target-tenant Agent blocks retirement, not only
            # ones owned by a retiring non-platform user: a platform-admin
            # owner is rehomed and stays active, and would otherwise keep
            # manage authority over a retired company's Agent.
            blocking_agent_ids = sorted(str(agent.id) for agent in tenant_agents if agent.deleted_at is None)
            if blocking_agent_ids:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "tenant_retirement_owned_agents_active",
                        "agent_ids": blocking_agent_ids,
                    },
                )

            now = datetime.now(timezone.utc)
            for user in retirable_users:
                was_active = bool(user.is_active)
                revocations = await revoke_user_authority(
                    scoped_db,
                    target_user=user,
                    actor_user=current_user,
                    now=now,
                )
                user.is_active = False
                user.department_id = None
                retired_user = TenantRetiredUserOut(
                    user_id=user.id,
                    status="deactivated" if was_active else "already_inactive",
                    is_active=False,
                    revocations={
                        "agent_permissions": revocations.agent_permissions,
                        "resource_permissions": revocations.resource_permissions,
                        "knowledge_grants": revocations.knowledge_grants,
                        "refresh_tokens": revocations.refresh_tokens,
                        "external_principals": revocations.external_principals,
                        "local_bridge_connections": revocations.local_bridge_connections,
                        "runtime_tasks": revocations.runtime_tasks,
                        "pending_approvals": revocations.pending_approvals,
                    },
                )
                retired_users.append(retired_user)
                runtime_revocations.append((user.id, revocations))

            for agent in tenant_agents:
                if agent.status == "running":
                    agent.status = "stopped"
        else:
            running_agents = await scoped_db.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.status == "running",
                )
            )
            for agent in running_agents.scalars().all():
                agent.status = "stopped"

        for user in tenant_users:
            if retirement is not None and user.role != "platform_admin":
                continue
            user.department_id = None
            if user.role == "platform_admin" and fallback_tenant is not None:
                _apply_new_tenant_membership(user, fallback_tenant, role="platform_admin")
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
        if retirement is not None:
            scoped_db.add(
                AuditLog(
                    user_id=current_user.id,
                    tenant_id=tenant_id,
                    action="tenant:retired",
                    details={
                        "schema": "hive.tenant_retirement.v1",
                        "request_id": retirement_request_id,
                        "reason": retirement_reason,
                        "expected_user_ids": sorted(str(value) for value in requested_user_ids),
                        "fallback_tenant_id": str(fallback_tenant_id) if fallback_tenant_id else None,
                        "retired_users": [value.model_dump(mode="json") for value in retired_users],
                    },
                )
            )
        await scoped_db.flush()
        response = TenantDeleteOut(
            fallback_tenant_id=fallback_tenant_id,
            needs_company_setup=fallback_tenant_id is None,
            retirement_status="retired" if retirement is not None else "not_requested",
            retirement_request_id=retirement_request_id or None,
            retired_users=retired_users,
        )

    if retirement is not None:
        await db.commit()
        for user_id, revocations in runtime_revocations:
            await publish_user_authority_runtime_cancellations(
                user_id=user_id,
                revocations=revocations,
            )
    if response is None:
        raise RuntimeError("Tenant deletion completed without a receipt")
    return response


async def _assign_user_to_tenant(
    *,
    tenant_id: uuid.UUID,
    role: str,
    current_user: User,
    db: AsyncSession,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
) -> TenantUserAssignmentOut:
    if role not in ("org_admin", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")

    async with enter_rls_bypass(
        db,
        reason="platform-admin assign user to tenant",
        actor_id=str(current_user.id),
    ) as bypass_db:
        t_result = await bypass_db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        tenant = t_result.scalar_one_or_none()
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if not tenant.is_active:
            raise HTTPException(status_code=409, detail="Cannot assign users to a disabled tenant")

        if user_id is not None:
            u_result = await bypass_db.execute(select(User).where(User.id == user_id).with_for_update())
            user = u_result.scalar_one_or_none()
        else:
            u_result = await bypass_db.execute(
                select(User)
                .where(sqla_func.lower(User.email) == str(email).lower())
                .order_by(User.id)
                .limit(2)
                .with_for_update()
            )
            users = u_result.scalars().all()
            if len(users) > 1:
                raise HTTPException(status_code=409, detail="Multiple accounts match this email")
            user = users[0] if users else None
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not getattr(user, "is_active", True):
            raise HTTPException(status_code=409, detail="Cannot assign a disabled user")
        if user.role == "platform_admin":
            raise HTTPException(status_code=403, detail="Platform administrator membership cannot be changed here")
        previous_tenant_id = user.tenant_id
        if previous_tenant_id is not None:
            if str(previous_tenant_id) != str(tenant_id):
                raise HTTPException(status_code=409, detail="User already belongs to another tenant")
            if user.role == role:
                return TenantUserAssignmentOut(
                    status="already_assigned",
                    user_id=user.id,
                    tenant_id=tenant_id,
                    role=role,
                    membership_committed=True,
                    client_token_refresh_required=True,
                )
            raise HTTPException(
                status_code=409,
                detail="User already belongs to this tenant; use tenant user management to change roles",
            )
        previous_role = user.role
        _apply_new_tenant_membership(user, tenant, role=role)
        bypass_db.add(
            AuditLog(
                user_id=current_user.id,
                tenant_id=tenant_id,
                action="tenant:user_assigned",
                details={
                    "target_user_id": str(user.id),
                    "previous_tenant_id": str(previous_tenant_id) if previous_tenant_id else None,
                    "previous_role": previous_role,
                    "role": role,
                },
            )
        )
        try:
            await bypass_db.flush()
            await bypass_db.commit()
        except Exception as exc:
            await bypass_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="User assignment was not committed",
            ) from exc
    return TenantUserAssignmentOut(
        status="ok",
        user_id=user.id,
        tenant_id=tenant_id,
        role=role,
        membership_committed=True,
        client_token_refresh_required=True,
    )


@router.put("/{tenant_id}/assign-user", response_model=TenantUserAssignmentOut)
async def assign_user_to_tenant_by_email(
    tenant_id: uuid.UUID,
    data: TenantUserAssignment,
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a registered tenantless user by email from the platform control plane."""
    return await _assign_user_to_tenant(
        tenant_id=tenant_id,
        email=str(data.email),
        role=data.role,
        current_user=current_user,
        db=db,
    )


@router.put("/{tenant_id}/assign-user/{user_id}", response_model=TenantUserAssignmentOut)
async def assign_user_to_tenant(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    current_user: User = Depends(require_role("platform_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a registered tenantless user by id; retained for API compatibility."""
    return await _assign_user_to_tenant(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        current_user=current_user,
        db=db,
    )
