"""Strict-RLS bootstrap matrix for public auth and process startup.

These paths intentionally create rows that have no tenant authority yet:
public users before company join, global builtin Skills, operator-only system
audit events, and the first default Tenant.  The non-owner production role
must reach them only through a narrow audited BYPASS owner.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import auth as auth_api
from app.api import tenants as tenants_api
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.invitation_code import InvitationCode
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
            quota_tokens_per_day=999_999,
            quota_tokens_per_month=9_999_999,
            tokens_used_today=7,
            tokens_used_month=70,
            tokens_used_total=700,
            tokens_reset_at=datetime(2026, 8, 31, tzinfo=UTC),
        )
        db.add_all([tenant, platform_admin, target])
        await db.commit()

    from app.core.security import create_access_token, create_refresh_token, decode_access_token, get_current_user
    from app.database import pin_rls_tenant_context

    stale_tenantless_token = create_access_token(str(target.id), "member")
    stale_scoped_token = create_access_token(str(target.id), "member", tenant_id=str(uuid.uuid4()))
    async with owner_sessionmaker() as db:
        refresh_token = await create_refresh_token(db, target.id, "bootstrap-device")
        await db.commit()

    async with app_user_sessionmaker() as db:
        receipt = await tenants_api.assign_user_to_tenant_by_email(
            tenant_id=tenant.id,
            data=tenants_api.TenantUserAssignment(email=email.upper(), role="org_admin"),
            current_user=SimpleNamespace(id=platform_admin.id, role="platform_admin"),
            db=db,
        )

    assert receipt.membership_committed is True
    assert receipt.client_token_refresh_required is True
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
    assert assigned.tokens_used_today == 0
    assert assigned.tokens_used_month == 0
    assert assigned.tokens_used_total == 0
    assert assigned.tokens_reset_at is None
    assert audit.tenant_id == tenant.id
    assert audit.details["target_user_id"] == str(target.id)

    request = Request({"type": "http", "method": "GET", "path": "/api/auth/me", "headers": []})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=stale_tenantless_token)
    async with app_user_sessionmaker() as db:
        current = await get_current_user(request=request, credentials=credentials, db=db)
    assert current.id == target.id
    assert current.tenant_id == tenant.id
    assert current.role == "org_admin"

    stale_scope = decode_access_token(stale_scoped_token)["tid"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, stale_scope)
        with pytest.raises(HTTPException) as stale_error:
            await get_current_user(
                request=request,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=stale_scoped_token),
                db=db,
            )
    assert stale_error.value.status_code == 401

    from app.api import desktop_auth

    async with app_user_sessionmaker() as db:
        exchange = await desktop_auth.exchange_refresh_token(
            desktop_auth.DesktopExchangeRequest(
                refresh_token=refresh_token,
                device_id="bootstrap-device",
            ),
            db=db,
        )
    refreshed_claims = decode_access_token(exchange.access_token)
    assert refreshed_claims["tid"] == str(tenant.id)
    assert refreshed_claims["role"] == "org_admin"


async def test_tenant_delete_serializes_tenantless_assignment_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.services import tool_config_service

    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        tenant = Tenant(name=f"Race {suffix}", slug=f"race-{suffix}", im_provider="web_only")
        platform_admin = User(
            username=f"race-platform-{suffix}",
            email=f"race-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="Race Platform Admin",
            role="platform_admin",
        )
        target = User(
            username=f"race-target-{suffix}",
            email=f"race-target-{suffix}@example.com",
            password_hash="hash",
            display_name="Race Target",
            role="member",
        )
        db.add_all([tenant, platform_admin, target])
        await db.commit()

    delete_at_scrub = asyncio.Event()
    release_delete = asyncio.Event()
    assign_started = asyncio.Event()
    delete_pid: int | None = None
    assign_pid: int | None = None
    real_scrub = tool_config_service.scrub_tenant_tool_secrets

    async def gated_scrub(db, tenant_id):
        delete_at_scrub.set()
        await release_delete.wait()
        return await real_scrub(db, tenant_id)

    monkeypatch.setattr(tool_config_service, "scrub_tenant_tool_secrets", gated_scrub)

    async def run_delete():
        nonlocal delete_pid
        async with app_user_sessionmaker() as db:
            delete_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            receipt = await tenants_api.delete_tenant(
                tenant_id=tenant.id,
                current_user=SimpleNamespace(id=platform_admin.id, role="platform_admin", tenant_id=None),
                db=db,
            )
            await db.commit()
            return receipt

    async def run_assignment():
        nonlocal assign_pid
        async with app_user_sessionmaker() as db:
            assign_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            assign_started.set()
            try:
                return await tenants_api.assign_user_to_tenant_by_email(
                    tenant_id=tenant.id,
                    data=tenants_api.TenantUserAssignment(email=target.email, role="org_admin"),
                    current_user=SimpleNamespace(id=platform_admin.id, role="platform_admin"),
                    db=db,
                )
            except HTTPException as exc:
                return exc

    delete_task = asyncio.create_task(run_delete())
    assign_task = None
    try:
        await asyncio.wait_for(delete_at_scrub.wait(), timeout=10)
        assign_task = asyncio.create_task(run_assignment())
        await asyncio.wait_for(assign_started.wait(), timeout=10)
        assert delete_pid is not None and assign_pid is not None

        blocked = False
        async with owner_sessionmaker() as observer:
            async with asyncio.timeout(10):
                while not blocked:
                    if assign_task.done():
                        pytest.fail("assignment completed before delete released the tenant lock")
                    blockers = await observer.scalar(
                        text("SELECT pg_blocking_pids(:waiting_pid)"),
                        {"waiting_pid": assign_pid},
                    )
                    blocked = delete_pid in (blockers or [])
                    if not blocked:
                        await asyncio.sleep(0.02)

        release_delete.set()
        delete_receipt, assignment_result = await asyncio.wait_for(
            asyncio.gather(delete_task, assign_task),
            timeout=20,
        )
    finally:
        release_delete.set()
        pending = [task for task in (delete_task, assign_task) if task is not None and not task.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=20)

    assert isinstance(delete_receipt.needs_company_setup, bool)
    assert isinstance(assignment_result, HTTPException)
    assert assignment_result.status_code == 409
    assert assignment_result.detail == "Cannot assign users to a disabled tenant"

    async with owner_sessionmaker() as db:
        inactive_tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
        untouched_target = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
        audit_count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "tenant:user_assigned", AuditLog.user_id == platform_admin.id)
        )
    assert inactive_tenant.is_active is False
    assert untouched_target.tenant_id is None
    assert untouched_target.role == "member"
    assert audit_count == 0


async def test_platform_admin_cross_tenant_company_create_and_toggle_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.api import admin as admin_api
    from app.database import pin_rls_tenant_context

    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        home = Tenant(id=uuid.uuid4(), name=f"Home {suffix}", slug=f"home-{suffix}", im_provider="web_only")
        target = Tenant(
            id=uuid.uuid4(),
            name=f"Target {suffix}",
            slug=f"target-{suffix}",
            im_provider="web_only",
        )
        platform_admin = User(
            username=f"company-platform-{suffix}",
            email=f"company-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="Company Platform Admin",
            role="platform_admin",
            tenant_id=home.id,
        )
        db.add_all([home, target, platform_admin])
        await db.flush()
        running_agent = Agent(
            name=f"Running {suffix}",
            creator_id=platform_admin.id,
            tenant_id=target.id,
            status="running",
        )
        db.add(running_agent)
        await db.commit()

    actor = SimpleNamespace(id=platform_admin.id, role="platform_admin", tenant_id=home.id)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, home.id)
        toggle_receipt = await admin_api.toggle_company(
            company_id=target.id,
            current_user=actor,
            db=db,
        )
    assert toggle_receipt == {"ok": True, "is_active": False}

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, home.id)
        create_receipt = await admin_api.create_company(
            data=admin_api.CompanyCreateRequest(name=f"Created {suffix}"),
            current_user=actor,
            db=db,
        )

    async with owner_sessionmaker() as db:
        disabled_target = (await db.execute(select(Tenant).where(Tenant.id == target.id))).scalar_one()
        stopped_agent = (await db.execute(select(Agent).where(Agent.id == running_agent.id))).scalar_one()
        created_tenant = (await db.execute(select(Tenant).where(Tenant.id == create_receipt.company.id))).scalar_one()
        created_invite = (
            await db.execute(
                select(InvitationCode).where(
                    InvitationCode.code == create_receipt.admin_invitation_code,
                    InvitationCode.tenant_id == created_tenant.id,
                )
            )
        ).scalar_one()
    assert disabled_target.is_active is False
    assert stopped_agent.status == "stopped"
    assert created_tenant.name == f"Created {suffix}"
    assert created_invite.max_uses == 1
    assert created_invite.granted_role == "org_admin"


async def test_platform_admin_company_create_rolls_back_tenant_when_invite_insert_conflicts(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.api import admin as admin_api
    from app.database import pin_rls_tenant_context

    suffix = uuid.uuid4().hex[:10]
    duplicate_code = f"DUP{suffix.upper()}"
    async with owner_sessionmaker() as db:
        home = Tenant(name=f"Conflict Home {suffix}", slug=f"conflict-home-{suffix}", im_provider="web_only")
        platform_admin = User(
            username=f"conflict-platform-{suffix}",
            email=f"conflict-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="Conflict Platform Admin",
            role="platform_admin",
            tenant_id=home.id,
        )
        db.add_all([home, platform_admin])
        await db.flush()
        db.add(
            InvitationCode(
                code=duplicate_code,
                tenant_id=home.id,
                max_uses=1,
                created_by=platform_admin.id,
                granted_role="member",
            )
        )
        await db.commit()

    monkeypatch.setattr(admin_api.secrets, "token_hex", lambda _size: suffix[:6])
    monkeypatch.setattr(admin_api.secrets, "token_urlsafe", lambda _size: duplicate_code)
    actor = SimpleNamespace(id=platform_admin.id, role="platform_admin", tenant_id=home.id)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, home.id)
        with pytest.raises(HTTPException) as exc_info:
            await admin_api.create_company(
                data=admin_api.CompanyCreateRequest(name="Invite Conflict Candidate"),
                current_user=actor,
                db=db,
            )
    assert exc_info.value.status_code == 409

    candidate_slug = f"invite-conflict-candidate-{suffix[:6]}"
    async with owner_sessionmaker() as db:
        tenant_count = await db.scalar(select(func.count()).select_from(Tenant).where(Tenant.slug == candidate_slug))
        invite_count = await db.scalar(
            select(func.count()).select_from(InvitationCode).where(InvitationCode.code == duplicate_code)
        )
    assert tenant_count == 0
    assert invite_count == 1


async def test_single_use_invitation_consume_serializes_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        tenant = Tenant(
            name=f"Concurrent Invite {suffix}",
            slug=f"concurrent-invite-{suffix}",
            im_provider="web_only",
        )
        first = User(
            username=f"concurrent-first-{suffix}",
            email=f"concurrent-first-{suffix}@example.com",
            password_hash="hash",
            display_name="Concurrent First",
            role="member",
        )
        second = User(
            username=f"concurrent-second-{suffix}",
            email=f"concurrent-second-{suffix}@example.com",
            password_hash="hash",
            display_name="Concurrent Second",
            role="member",
        )
        db.add_all((tenant, first, second))
        await db.flush()
        code = InvitationCode(
            code=f"ONE{suffix.upper()}",
            tenant_id=tenant.id,
            max_uses=1,
            granted_role="member",
        )
        db.add(code)
        await db.commit()

    async def consume(user: User):
        async with app_user_sessionmaker() as db:
            try:
                return await tenants_api.join_company(
                    data=tenants_api.JoinRequest(invitation_code=code.code),
                    current_user=SimpleNamespace(
                        id=user.id,
                        tenant_id=None,
                        role="member",
                        is_active=True,
                    ),
                    db=db,
                )
            except HTTPException as exc:
                return exc

    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(consume(first), consume(second)),
        timeout=20,
    )
    successes = [result for result in (first_result, second_result) if isinstance(result, tenants_api.JoinResponse)]
    conflicts = [result for result in (first_result, second_result) if isinstance(result, HTTPException)]
    assert len(successes) == 1
    assert [(conflict.status_code, conflict.detail) for conflict in conflicts] == [
        (400, "Invitation code has reached its usage limit")
    ]

    async with owner_sessionmaker() as db:
        persisted_code = await db.get(InvitationCode, code.id)
        users = list((await db.execute(select(User).where(User.id.in_((first.id, second.id))))).scalars())
    assert persisted_code.used_count == 1
    assert sum(user.tenant_id == tenant.id for user in users) == 1
    assert sum(user.tenant_id is None for user in users) == 1


async def test_admin_company_and_two_level_invitation_http_journey_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    """Exercise the mounted HTTP routes against the non-owner PostgreSQL role."""
    from httpx import ASGITransport, AsyncClient

    from app.api import admin as admin_api
    from app.core.security import create_access_token
    from app.database import get_current_tenant_id, get_db, pin_rls_tenant_context
    from app.main import app

    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        home = Tenant(
            id=uuid.uuid4(),
            name=f"HTTP Home {suffix}",
            slug=f"http-home-{suffix}",
            im_provider="web_only",
        )
        platform_admin = User(
            username=f"http-platform-{suffix}",
            email=f"http-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="HTTP Platform Admin",
            role="platform_admin",
            tenant_id=home.id,
        )
        tenantless_platform_admin = User(
            username=f"http-tenantless-platform-{suffix}",
            email=f"http-tenantless-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="HTTP Tenantless Platform Admin",
            role="platform_admin",
        )
        db.add_all([home, platform_admin, tenantless_platform_admin])
        await db.commit()

    async def override_get_db():
        async with app_user_sessionmaker() as db:
            try:
                await pin_rls_tenant_context(db, get_current_tenant_id())
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    platform_token = create_access_token(
        str(platform_admin.id),
        "platform_admin",
        tenant_id=str(home.id),
    )
    platform_headers = {"Authorization": f"Bearer {platform_token}"}
    tenantless_platform_headers = {
        "Authorization": f"Bearer {create_access_token(str(tenantless_platform_admin.id), 'platform_admin')}"
    }
    missing = object()
    previous_override = app.dependency_overrides.get(get_db, missing)
    app.dependency_overrides[get_db] = override_get_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def register_tenantless(label: str) -> dict:
                response = await client.post(
                    "/api/auth/register",
                    json={
                        "username": f"{label}-{suffix}",
                        "email": f"{label}-{suffix}@example.com",
                        "password": "AtomicPass123!",
                        "display_name": label,
                    },
                )
                assert response.status_code == 201, response.text
                payload = response.json()
                assert payload["needs_company_setup"] is True
                assert payload["user"]["tenant_id"] is None
                return payload

            invited_admin = await register_tenantless("invited-admin")
            invited_member = await register_tenantless("invited-member")
            email_assignee = await register_tenantless("email-assignee")

            create_response = await client.post(
                "/api/admin/companies",
                json={"name": f"HTTP Created {suffix}"},
                headers=platform_headers,
            )
            assert create_response.status_code == 201, create_response.text
            create_payload = create_response.json()
            company_id = uuid.UUID(create_payload["company"]["id"])
            admin_code = create_payload["admin_invitation_code"]

            platform_admin_join = await client.post(
                "/api/tenants/join",
                json={"invitation_code": admin_code},
                headers=tenantless_platform_headers,
            )
            assert platform_admin_join.status_code == 409
            assert platform_admin_join.json()["detail"] == "Platform administrators cannot consume company invitations"

            async with owner_sessionmaker() as db:
                untouched_admin_invite = (
                    await db.execute(select(InvitationCode).where(InvitationCode.code == admin_code))
                ).scalar_one()
                assert untouched_admin_invite.used_count == 0
                assert untouched_admin_invite.is_active is True

            admin_join = await client.post(
                "/api/tenants/join",
                json={"invitation_code": admin_code},
                headers={"Authorization": f"Bearer {invited_admin['access_token']}"},
            )
            assert admin_join.status_code == 200, admin_join.text
            assert admin_join.json()["role"] == "org_admin"
            org_admin_token = admin_join.json()["access_token"]

            admin_join_replay = await client.post(
                "/api/tenants/join",
                json={"invitation_code": admin_code},
                headers={"Authorization": f"Bearer {invited_admin['access_token']}"},
            )
            assert admin_join_replay.status_code == 200, admin_join_replay.text
            assert admin_join_replay.json()["tenant"]["id"] == str(company_id)
            assert admin_join_replay.json()["role"] == "org_admin"

            second_company = await client.post(
                "/api/admin/companies",
                json={"name": f"HTTP Cross Tenant {suffix}"},
                headers=platform_headers,
            )
            assert second_company.status_code == 201, second_company.text
            cross_tenant_admin_code = second_company.json()["admin_invitation_code"]
            cross_tenant_replay = await client.post(
                "/api/tenants/join",
                json={"invitation_code": cross_tenant_admin_code},
                headers={"Authorization": f"Bearer {invited_admin['access_token']}"},
            )
            assert cross_tenant_replay.status_code == 409
            assert cross_tenant_replay.json()["detail"] == "User already belongs to another company"

            async with owner_sessionmaker() as db:
                invitation_counts = dict(
                    (
                        await db.execute(
                            select(InvitationCode.code, InvitationCode.used_count).where(
                                InvitationCode.code.in_((admin_code, cross_tenant_admin_code))
                            )
                        )
                    ).all()
                )
            assert invitation_counts == {
                admin_code: 1,
                cross_tenant_admin_code: 0,
            }

            admin_code_reuse = await client.post(
                "/api/tenants/join",
                json={"invitation_code": admin_code},
                headers={"Authorization": f"Bearer {invited_member['access_token']}"},
            )
            assert admin_code_reuse.status_code == 400
            assert admin_code_reuse.json()["detail"] == "Invitation code has reached its usage limit"

            cross_tenant_invite = await client.post(
                f"/api/enterprise/invitation-codes?tenant_id={home.id}",
                json={"count": 1, "max_uses": 1},
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert cross_tenant_invite.status_code == 403

            role_injection = await client.post(
                "/api/enterprise/invitation-codes",
                json={"count": 1, "max_uses": 1, "granted_role": "org_admin"},
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert role_injection.status_code == 422

            member_invite = await client.post(
                "/api/enterprise/invitation-codes",
                json={"count": 1, "max_uses": 1},
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert member_invite.status_code == 200, member_invite.text
            member_code = member_invite.json()["codes"][0]

            invitation_list = await client.get(
                "/api/enterprise/invitation-codes",
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert invitation_list.status_code == 200, invitation_list.text
            assert invitation_list.json()["total"] == 1
            assert [item["code"] for item in invitation_list.json()["items"]] == [member_code]

            hidden_admin_search = await client.get(
                "/api/enterprise/invitation-codes",
                params={"search": admin_code},
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert hidden_admin_search.status_code == 200, hidden_admin_search.text
            assert hidden_admin_search.json()["total"] == 0
            assert hidden_admin_search.json()["items"] == []

            invitation_export = await client.get(
                "/api/enterprise/invitation-codes/export",
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert invitation_export.status_code == 200, invitation_export.text
            assert member_code in invitation_export.text
            assert admin_code not in invitation_export.text

            async with owner_sessionmaker() as db:
                admin_invite_id = await db.scalar(select(InvitationCode.id).where(InvitationCode.code == admin_code))

            admin_deactivate = await client.delete(
                f"/api/enterprise/invitation-codes/{admin_invite_id}",
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert admin_deactivate.status_code == 404
            assert admin_deactivate.json()["detail"] == "Code not found"

            join_role_injection = await client.post(
                "/api/tenants/join",
                json={"invitation_code": member_code, "role": "org_admin"},
                headers={"Authorization": f"Bearer {invited_member['access_token']}"},
            )
            assert join_role_injection.status_code == 422

            member_join = await client.post(
                "/api/tenants/join",
                json={"invitation_code": member_code},
                headers={"Authorization": f"Bearer {invited_member['access_token']}"},
            )
            assert member_join.status_code == 200, member_join.text
            assert member_join.json()["role"] == "member"

            async with owner_sessionmaker() as db:
                member_invite_id = await db.scalar(select(InvitationCode.id).where(InvitationCode.code == member_code))

            member_deactivate = await client.delete(
                f"/api/enterprise/invitation-codes/{member_invite_id}",
                headers={"Authorization": f"Bearer {org_admin_token}"},
            )
            assert member_deactivate.status_code == 200, member_deactivate.text
            assert member_deactivate.json() == {"status": "deactivated"}

            member_overreach = await client.post(
                "/api/enterprise/invitation-codes",
                json={"count": 1, "max_uses": 1},
                headers={"Authorization": f"Bearer {member_join.json()['access_token']}"},
            )
            assert member_overreach.status_code == 403

            assignment = await client.put(
                f"/api/tenants/{company_id}/assign-user",
                json={"email": email_assignee["user"]["email"].upper(), "role": "org_admin"},
                headers=platform_headers,
            )
            assert assignment.status_code == 200, assignment.text
            assert assignment.json() == {
                "status": "ok",
                "user_id": email_assignee["user"]["id"],
                "tenant_id": str(company_id),
                "role": "org_admin",
                "membership_committed": True,
                "client_token_refresh_required": True,
            }

            cross_tenant_assignment = await client.put(
                f"/api/tenants/{home.id}/assign-user",
                json={"email": invited_member["user"]["email"], "role": "member"},
                headers=platform_headers,
            )
            assert cross_tenant_assignment.status_code == 409
            assert cross_tenant_assignment.json()["detail"] == "User already belongs to another tenant"

            monkeypatch.setattr(admin_api.secrets, "token_hex", lambda _size: "a1b2c3")
            monkeypatch.setattr(admin_api.secrets, "token_urlsafe", lambda _size: "FIXEDADMINCODE01")
            first_conflict_candidate = await client.post(
                "/api/admin/companies",
                json={"name": "Constraint Authority"},
                headers=platform_headers,
            )
            assert first_conflict_candidate.status_code == 201, first_conflict_candidate.text
            duplicate = await client.post(
                "/api/admin/companies",
                json={"name": "Constraint Authority"},
                headers=platform_headers,
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"] == "Company creation conflicted; retry"

            async with owner_sessionmaker() as db:
                joined_admin = (
                    await db.execute(select(User).where(User.id == uuid.UUID(invited_admin["user"]["id"])))
                ).scalar_one()
                joined_member = (
                    await db.execute(select(User).where(User.id == uuid.UUID(invited_member["user"]["id"])))
                ).scalar_one()
                assigned = (
                    await db.execute(select(User).where(User.id == uuid.UUID(email_assignee["user"]["id"])))
                ).scalar_one()
                admin_invite = (
                    await db.execute(select(InvitationCode).where(InvitationCode.code == admin_code))
                ).scalar_one()
                created_member_invite = (
                    await db.execute(select(InvitationCode).where(InvitationCode.code == member_code))
                ).scalar_one()
                conflict_tenant_count = await db.scalar(
                    select(func.count()).select_from(Tenant).where(Tenant.slug == "constraint-authority-a1b2c3")
                )
                conflict_invite_count = await db.scalar(
                    select(func.count()).select_from(InvitationCode).where(InvitationCode.code == "FIXEDADMINCODE01")
                )

                assert joined_admin.tenant_id == company_id
                assert joined_admin.role == "org_admin"
                assert joined_member.tenant_id == company_id
                assert joined_member.role == "member"
                assert assigned.tenant_id == company_id
                assert assigned.role == "org_admin"
                assert admin_invite.tenant_id == company_id
                assert admin_invite.max_uses == 1
                assert admin_invite.used_count == 1
                assert admin_invite.granted_role == "org_admin"
                assert admin_invite.is_active is True
                assert created_member_invite.tenant_id == company_id
                assert created_member_invite.created_by == joined_admin.id
                assert created_member_invite.granted_role == "member"
                assert created_member_invite.is_active is False
                assert conflict_tenant_count == 1
                assert conflict_invite_count == 1

                running_agent = Agent(
                    name=f"HTTP Running {suffix}",
                    creator_id=joined_admin.id,
                    tenant_id=company_id,
                    status="running",
                )
                db.add(running_agent)
                await db.commit()

            disable = await client.put(
                f"/api/admin/companies/{company_id}/toggle",
                headers=platform_headers,
            )
            assert disable.status_code == 200, disable.text
            assert disable.json() == {"ok": True, "is_active": False}

            async with owner_sessionmaker() as db:
                disabled = (await db.execute(select(Tenant).where(Tenant.id == company_id))).scalar_one()
                stopped = (await db.execute(select(Agent).where(Agent.id == running_agent.id))).scalar_one()
            assert disabled.is_active is False
            assert stopped.status == "stopped"
    finally:
        if previous_override is missing:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override


async def test_assignment_audit_failure_rolls_back_user_mutation_under_app_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        tenant = Tenant(name=f"Rollback {suffix}", slug=f"rollback-{suffix}", im_provider="web_only")
        platform_admin = User(
            username=f"rollback-platform-{suffix}",
            email=f"rollback-platform-{suffix}@example.com",
            password_hash="hash",
            display_name="Platform Admin",
            role="platform_admin",
        )
        target = User(
            username=f"rollback-target-{suffix}",
            email=f"rollback-target-{suffix}@example.com",
            password_hash="hash",
            display_name="Rollback Target",
            role="member",
        )
        db.add_all([tenant, platform_admin, target])
        await db.commit()

    real_audit_log = tenants_api.AuditLog
    missing_tenant_id = uuid.uuid4()

    def invalid_audit_log(**kwargs):
        return real_audit_log(**{**kwargs, "tenant_id": missing_tenant_id})

    monkeypatch.setattr(tenants_api, "AuditLog", invalid_audit_log)
    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as exc_info:
            await tenants_api.assign_user_to_tenant_by_email(
                tenant_id=tenant.id,
                data=tenants_api.TenantUserAssignment(email=target.email, role="org_admin"),
                current_user=SimpleNamespace(id=platform_admin.id, role="platform_admin"),
                db=db,
            )
    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.__cause__, IntegrityError)

    async with owner_sessionmaker() as db:
        unchanged = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
        audit_count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "tenant:user_assigned", AuditLog.user_id == platform_admin.id)
        )
    assert unchanged.tenant_id is None
    assert unchanged.role == "member"
    assert audit_count == 0


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
