"""Authentication API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context
from app.models.user import User
from app.schemas.schemas import TokenResponse, UserLogin, UserOut, UserRegister, UserUpdate

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_taken_detail(existing_email_user: User | None = None) -> dict:
    is_feishu_shadow = bool(
        existing_email_user
        and getattr(existing_email_user, "feishu_open_id", None)
        and getattr(existing_email_user, "must_change_password", False)
    )
    if is_feishu_shadow:
        return {
            "code": "email_linked_to_feishu",
            "field": "email",
            "message": "This email was imported from Feishu. Log in with the default password 123456 and change it after signing in.",
            "suggest_login": True,
            "default_password_hint": True,
        }
    return {
        "code": "email_taken",
        "field": "email",
        "message": "Email already registered",
        "suggest_login": True,
    }


def _username_taken_detail() -> dict:
    return {
        "code": "username_taken",
        "field": "username",
        "message": "Username already taken",
        "suggest_login": False,
    }


def _register_integrity_conflict_detail(exc: IntegrityError) -> dict | None:
    message = str(exc).lower()
    if "ix_users_email" in message or "users_email_key" in message:
        return _email_taken_detail()
    if "ix_users_username" in message or "users_username_key" in message:
        return _username_taken_detail()
    return None


async def _scope_public_login_session_to_user_tenant(db: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """After public credential lookup succeeds, continue under the user's tenant.

    Public login starts without a request tenant, but the success/failure audit
    row is tenant-scoped by RLS. Once the user identity is known, the rest of
    the transaction should use that tenant scope instead of the anonymous
    fail-closed scope.
    """
    await pin_rls_tenant_context(db, tenant_id)


@router.get("/registration-config")
async def get_registration_config(db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns registration requirements (no auth needed)."""
    from app.models.system_settings import SystemSetting

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "invitation_code_enabled"))
    setting = result.scalar_one_or_none()
    enabled = setting.value.get("enabled", False) if setting else False
    return {"invitation_code_required": enabled}


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    """Register a new user account.

    The first user to register becomes the platform admin automatically and is
    assigned to the default company as org_admin. Subsequent users register
    without a company — they must create or join one via /tenants/self-create
    or /tenants/join.
    """
    # Resolve tenant and role for first user only.
    tenant_uuid = None
    role = "member"
    quota_defaults: dict = {}

    # Check existing — report which field clashed so the UI can guide the user.
    # Priority: email clash surfaces first because it points the user to "go
    # log in / reset password" rather than just picking a new username.
    # Registration is the authority owner for identities that do not have a
    # tenant yet.  Keep the uniqueness reads and both identity writes inside
    # one audited scope; exiting after the reads makes a subsequent tenantless
    # User/Participant flush fail closed under the production ``app_rls`` role.
    async with enter_rls_bypass(db, reason="public registration uniqueness and bootstrap checks"):
        email_hit = await db.execute(select(User).where(User.email == data.email))
        existing_email_user = email_hit.scalar_one_or_none()
        if existing_email_user:
            # Shadow account created by Feishu org sync: username=feishu_*,
            # feishu_open_id set, must_change_password=True, default pw "123456".
            # Tell the user to log in with that default and rotate it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_email_taken_detail(existing_email_user),
            )

        username_hit = await db.execute(select(User).where(User.username == data.username))
        if username_hit.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_username_taken_detail(),
            )

        # Check if this is the first user (→ platform admin + default company org_admin)
        user_count = await db.execute(select(func.count()).select_from(User))
        is_first_user = user_count.scalar() == 0

        if is_first_user:
            from app.models.tenant import Tenant

            default = await db.execute(select(Tenant).where(Tenant.slug == "default"))
            tenant = default.scalar_one_or_none()
            if not tenant:
                tenant = Tenant(name="Default", slug="default", im_provider="web_only")
                db.add(tenant)
                await db.flush()
            tenant_uuid = tenant.id
            role = "platform_admin"
            quota_defaults = {
                "quota_tokens_per_day": tenant.default_tokens_per_day,
                "quota_tokens_per_month": tenant.default_tokens_per_month,
            }

        # Note: invitation code validation has been moved to the company-join
        # flow.  A first user already has tenant authority; later public users
        # intentionally remain in this audited bootstrap scope until both their
        # User and Participant rows exist.
        if tenant_uuid is not None:
            await pin_rls_tenant_context(db, tenant_uuid)

        user = User(
            username=data.username,
            email=data.email,
            password_hash=hash_password(data.password),
            display_name=data.display_name or data.username,
            role=role,
            tenant_id=tenant_uuid,
            **quota_defaults,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            detail = _register_integrity_conflict_detail(exc)
            if detail:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
            raise

        # Auto-create Participant identity for the new user in the same
        # authority scope as its parent User row.
        from app.models.participant import Participant

        db.add(
            Participant(
                type="user",
                ref_id=user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
            )
        )
        await db.flush()

    # Seed default agents after first user (platform admin) registration
    if is_first_user:
        await db.commit()  # commit user first so seeder can find the admin
        try:
            from app.services.agent_seeder import seed_default_agents

            await seed_default_agents()
        except Exception as e:
            logger.warning(f"Failed to seed default agents: {e}")

    needs_setup = tenant_uuid is None
    token = create_access_token(str(user.id), user.role, tenant_id=str(user.tenant_id) if user.tenant_id else None)
    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        needs_company_setup=needs_setup,
    )


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    """Login with username-or-email and password.

    The input field is still called `username` for API backward-compat, but
    employees imported via Feishu org sync get a machine-generated username
    (`feishu_<user_id>`) they never see. They know their email instead, so
    we also accept email as a fallback login identifier. Username match
    takes precedence (canonical); email lookup only runs when the input
    contains "@" and no username matched.
    """
    identifier = data.username.strip()
    if not identifier:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    tenant = None
    async with enter_rls_bypass(db, reason="public login identifier lookup") as bypass_db:
        result = await bypass_db.execute(select(User).where(User.username == identifier))
        user = result.scalar_one_or_none()
        if user is None and "@" in identifier:
            result = await bypass_db.execute(select(User).where(func.lower(User.email) == identifier.lower()))
            user = result.scalar_one_or_none()

        if user and user.tenant_id:
            from app.models.tenant import Tenant

            t_result = await bypass_db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
            tenant = t_result.scalar_one_or_none()

    if user:
        await _scope_public_login_session_to_user_tenant(db, user.tenant_id)

    if not user or not verify_password(data.password, user.password_hash):
        # Audit: login failed
        try:
            from app.core.policy import write_audit_event

            if user:
                await write_audit_event(
                    db,
                    event_type="auth.login_failed",
                    severity="warn",
                    actor_type="user",
                    actor_id=user.id,
                    tenant_id=user.tenant_id or uuid.UUID(int=0),
                    action="login_failed",
                    details={"username": identifier},
                )
                await db.commit()
        except Exception:
            logger.warning("Audit write failed for auth.login_failed", exc_info=True)
            await db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    # Check if user's company is disabled.
    if tenant and not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your company has been disabled. Please contact the platform administrator.",
        )

    user_id = user.id
    user_role = user.role
    user_tenant_id = user.tenant_id
    user_out = UserOut.model_validate(user)
    needs_setup = user_tenant_id is None
    needs_password_change = bool(getattr(user, "must_change_password", False))
    token = create_access_token(str(user_id), user_role, tenant_id=str(user_tenant_id) if user_tenant_id else None)

    # Audit: login success
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="auth.login",
            severity="info",
            actor_type="user",
            actor_id=user_id,
            tenant_id=user_tenant_id or uuid.UUID(int=0),
            action="login",
            details={"username": identifier},
        )
    except Exception:
        logger.warning("Audit write failed for auth.login", exc_info=True)
        await db.rollback()

    return TokenResponse(
        access_token=token,
        user=user_out,
        needs_company_setup=needs_setup,
        needs_password_change=needs_password_change,
    )


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user profile."""
    return UserOut.model_validate(current_user)


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update current user profile."""
    update_data = data.model_dump(exclude_unset=True)

    # Validate username uniqueness if changing
    if "username" in update_data and update_data["username"] != current_user.username:
        existing = await db.execute(select(User).where(User.username == update_data["username"]))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")

    for field, value in update_data.items():
        setattr(current_user, field, value)
    await db.flush()
    return UserOut.model_validate(current_user)


@router.put("/me/password")
async def change_password(
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change current user's password. Requires old_password verification."""
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Both old_password and new_password are required")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    if not verify_password(old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(new_password)
    # Clear the "must change default password" flag — the user has now
    # rotated away from the shared SSO-import default, so future logins
    # no longer surface the nag banner.
    if getattr(current_user, "must_change_password", False):
        current_user.must_change_password = False
    await db.flush()
    return {"ok": True}
