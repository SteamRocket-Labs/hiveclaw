"""Strict-RLS bootstrap matrix for public auth and process startup.

These paths intentionally create rows that have no tenant authority yet:
public users before company join, global builtin Skills, operator-only system
audit events, and the first default Tenant.  The non-owner production role
must reach them only through a narrow audited BYPASS owner.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy import delete, func, select

from app.api import auth as auth_api
from app.api import tenants as tenants_api
from app.models.audit import AuditLog
from app.models.participant import Participant
from app.models.skill import Skill
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.schemas import UserRegister


async def test_public_registration_creates_tenantless_user_and_participant_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        tenant = Tenant(name=f"Auth {suffix}", slug=f"auth-{suffix}", im_provider="web_only")
        db.add(tenant)
        await db.flush()
        db.add(
            User(
                username=f"existing-{suffix}",
                email=f"existing-{suffix}@example.com",
                password_hash="hash",
                display_name="Existing",
                role="org_admin",
                tenant_id=tenant.id,
            )
        )
        await db.commit()

    monkeypatch.setattr(auth_api, "hash_password", lambda _password: "hash")
    monkeypatch.setattr(auth_api, "create_access_token", lambda *_args, **_kwargs: "jwt-stub")
    username = f"tenantless-{suffix}"
    email = f"tenantless-{suffix}@example.com"

    async with app_user_sessionmaker() as db:
        response = await auth_api.register(
            UserRegister(username=username, email=email, password="AtomicPass123!"),
            db,
        )
        await db.commit()

    assert response.needs_company_setup is True
    assert response.user.tenant_id is None
    async with owner_sessionmaker() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "user",
                    Participant.ref_id == user.id,
                )
            )
        ).scalar_one()
    assert participant.display_name == username


async def test_platform_admin_assigns_tenantless_org_admin_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    suffix = uuid.uuid4().hex[:10]
    email = f"bootstrap-{suffix}@example.com"
    async with owner_sessionmaker() as db:
        tenant = Tenant(
            name=f"Bootstrap {suffix}",
            slug=f"bootstrap-{suffix}",
            im_provider="web_only",
            default_tokens_per_day=1_000,
            default_tokens_per_month=20_000,
        )
        platform_admin = User(
            username=f"platform-{suffix}",
            email=f"platform-{suffix}@example.com",
            password_hash="hash",
            display_name="Platform Admin",
            role="platform_admin",
        )
        target = User(
            username=f"bootstrap-{suffix}",
            email=email,
            password_hash="hash",
            display_name="Bootstrap Admin",
            role="member",
        )
        db.add_all([tenant, platform_admin, target])
        await db.commit()

    async with app_user_sessionmaker() as db:
        receipt = await tenants_api.assign_user_to_tenant_by_email(
            tenant_id=tenant.id,
            data=tenants_api.TenantUserAssignment(email=email.upper(), role="org_admin"),
            current_user=SimpleNamespace(id=platform_admin.id, role="platform_admin"),
            db=db,
        )
        await db.commit()

    assert receipt["reauthentication_required"] is True
    async with owner_sessionmaker() as db:
        assigned = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "tenant:user_assigned",
                    AuditLog.user_id == platform_admin.id,
                )
            )
        ).scalar_one()
    assert assigned.tenant_id == tenant.id
    assert assigned.role == "org_admin"
    assert assigned.quota_tokens_per_day == 1_000
    assert assigned.quota_tokens_per_month == 20_000
    assert audit.tenant_id == tenant.id
    assert audit.details["target_user_id"] == str(target.id)


async def test_startup_default_tenant_seed_sees_existing_row_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.services.startup_bootstrap import ensure_default_tenant

    async with owner_sessionmaker() as db:
        existing = (await db.execute(select(Tenant).where(Tenant.slug == "default"))).scalar_one_or_none()
        if existing is None:
            db.add(Tenant(name="Default", slug="default", im_provider="web_only"))
            await db.commit()

    async with app_user_sessionmaker() as db:
        created = await ensure_default_tenant(db)
        await db.commit()

    assert created is False
    async with owner_sessionmaker() as db:
        count = await db.scalar(select(func.count()).select_from(Tenant).where(Tenant.slug == "default"))
    assert count == 1


async def test_startup_default_tenant_seed_is_exact_once_across_two_app_rls_sessions(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.services.startup_bootstrap import ensure_default_tenant

    preserved_slug: str | None = None
    async with owner_sessionmaker() as db:
        existing = (await db.execute(select(Tenant).where(Tenant.slug == "default"))).scalar_one_or_none()
        if existing is not None:
            preserved_slug = f"default-preserved-{uuid.uuid4().hex[:10]}"
            existing.slug = preserved_slug
        await db.commit()

    async def seed_once() -> bool:
        async with app_user_sessionmaker() as db:
            try:
                created = await ensure_default_tenant(db)
                await db.commit()
                return created
            except Exception:
                await db.rollback()
                raise

    try:
        results = await asyncio.gather(seed_once(), seed_once())
        assert sorted(results) == [False, True]
        async with owner_sessionmaker() as db:
            count = await db.scalar(select(func.count()).select_from(Tenant).where(Tenant.slug == "default"))
        assert count == 1
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(Tenant).where(Tenant.slug == "default"))
            if preserved_slug is not None:
                preserved = (await db.execute(select(Tenant).where(Tenant.slug == preserved_slug))).scalar_one()
                preserved.slug = "default"
            await db.commit()


async def test_builtin_skill_seed_writes_global_skill_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.services import skill_seeder

    folder = f"rls-bootstrap-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(skill_seeder, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(
        skill_seeder,
        "BUILTIN_SKILLS",
        [
            {
                "name": "RLS Bootstrap Skill",
                "description": "Strict-RLS startup regression fixture",
                "category": "system",
                "icon": "test",
                "folder_name": folder,
                "is_default": False,
                "files": [],
            }
        ],
    )
    monkeypatch.setattr(skill_seeder, "_load_pack_skill_dicts", lambda: [])

    await skill_seeder.seed_skills()

    async with owner_sessionmaker() as db:
        skill = (await db.execute(select(Skill).where(Skill.folder_name == folder))).scalar_one()
        assert skill.tenant_id is None
        assert skill.is_builtin is True
        await db.execute(delete(Skill).where(Skill.id == skill.id))
        await db.commit()


async def test_system_startup_audit_event_writes_operator_row_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.services import audit_logger

    action = f"server_startup_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(audit_logger, "async_session", app_user_sessionmaker)

    await audit_logger.write_audit_log(action, {"pid": 1})

    async with owner_sessionmaker() as db:
        row = (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalar_one_or_none()
        assert row is not None
        assert row.tenant_id is None
        # The integration database is disposable and the action is unique.
        # Canonical audit evidence is append-only, so test cleanup must not
        # weaken the same database invariant this path is expected to honor.


async def test_tenantless_security_event_writes_operator_row_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.core.policy import write_audit_event
    from app.services import audit_logger, platform_security_audit

    actor_id = uuid.uuid4()
    marker = uuid.uuid4().hex
    monkeypatch.setattr(audit_logger, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(platform_security_audit, "async_session", app_user_sessionmaker)

    async with app_user_sessionmaker() as request_db:
        receipt = await write_audit_event(
            request_db,
            event_type="auth.login_failed",
            severity="warn",
            actor_type="user",
            actor_id=actor_id,
            tenant_id=None,
            action="login_failed",
            details={"marker": marker},
        )

    assert receipt.scope == "platform_operator"
    assert receipt.tenant_id is None
    async with owner_sessionmaker() as verification_db:
        row = (await verification_db.execute(select(AuditLog).where(AuditLog.id == receipt.event_id))).scalar_one()
    assert row.tenant_id is None
    assert row.action == "platform_security.auth.login_failed"
    assert row.user_id is None
    assert row.agent_id is None
    assert row.details["schema_version"] == "hive.platform_security_audit.v2"
    assert row.details["sequence_num"] >= 2
    assert len(row.details["event_hash"]) == 64
    assert row.details["actor"] == {"type": "user", "id": str(actor_id)}
    assert row.details["details"] == {"marker": marker}

    queried = await platform_security_audit.query_platform_security_audit_events(
        event_type="auth.login_failed",
        severity="warn",
        actor_id=actor_id,
        request_id=None,
        limit=10,
        offset=0,
    )
    assert queried["total"] == 1
    assert queried["items"][0]["id"] == str(receipt.event_id)
    assert queried["items"][0]["chain_status"] == "chained"

    verification = await platform_security_audit.verify_persisted_platform_security_audit_chain()
    assert verification["valid"] is True
    assert verification["total_events"] >= 2
    assert verification["first_invalid_event_id"] is None
