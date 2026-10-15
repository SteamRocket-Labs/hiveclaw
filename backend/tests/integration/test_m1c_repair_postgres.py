"""Repair checks for M1-C identity continuity under real PostgreSQL RLS."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.desktop_auth import _store_oauth_state, router
from app.database import get_db, pin_rls_tenant_context
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.auth_provider import FeishuIdentityConflict, feishu_auth_provider
from app.services.identity_links import create_manual_link, persistent_grant_audiences
from app.services.org_sync_service import org_sync_service


@pytest.fixture
async def company(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tag = uuid.uuid4().hex[:10]
    tenant, provider, first, admin = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name=tag, slug="repair-" + tag))
        await db.flush()
        db.add(
            IdentityProvider(
                id=provider,
                tenant_id=tenant,
                provider_type="feishu",
                name="Feishu",
                is_active=True,
                sso_login_enabled=True,
                config={"source": "tenant_setting"},
            )
        )
        db.add(TenantSetting(tenant_id=tenant, key="feishu_org_sync", value={"app_id": "cli_probe", "app_secret": "synthetic-secret"}))
        db.add(
            User(
                id=first,
                tenant_id=tenant,
                username="first-" + tag,
                email="first-" + tag + "@example.test",
                password_hash="unmodified-hash",
                display_name="first",
                role="member",
                is_active=True,
                feishu_open_id="ou-" + tag,
                feishu_user_id="uid-" + tag,
            )
        )
        db.add(
            User(
                id=admin,
                tenant_id=tenant,
                username="admin-" + tag,
                email="admin-" + tag + "@example.test",
                password_hash="unmodified-hash",
                display_name="admin",
                role="org_admin",
                is_active=True,
            )
        )
        member = OrgMember(
            tenant_id=tenant,
            provider_id=provider,
            name="Directory First",
            status="active",
            feishu_open_id="ou-" + tag,
            feishu_user_id="uid-" + tag,
        )
        db.add(member)
        await db.commit()
        member_id = member.id
    import app.database as database

    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(
        "app.services.auth_provider._get_runtime_settings",
        lambda: SimpleNamespace(
            FEISHU_APP_ID="global_probe",
            FEISHU_APP_SECRET="synthetic-global",
            FEISHU_REDIRECT_URI="http://127.0.0.1:9/api/auth/feishu/callback",
            FEISHU_PLATFORM_REGION="feishu_cn",
            FEISHU_OPEN_API_DOMAIN="",
            FEISHU_OAUTH_AUTHORIZE_URL="",
            PUBLIC_BASE_URL="",
            BASE_URL="",
        ),
    )
    profile = {
        "open_id": "ou-" + tag,
        "user_id": "uid-" + tag,
        "union_id": "",
        "name": "First",
        "email": "first-" + tag + "@example.test",
        "raw_profile": {"source": "synthetic-oauth"},
    }

    async def exchange(self, config, code):
        return dict(profile)

    monkeypatch.setattr(type(feishu_auth_provider), "_exchange_code_for_user", exchange)
    return SimpleNamespace(
        tag=tag,
        tenant=tenant,
        provider=provider,
        first=first,
        admin=admin,
        member_id=member_id,
        profile=profile,
        app=app_user_sessionmaker,
        owner=owner_sessionmaker,
    )


async def test_sso_and_manual_repair_share_a_non_deadlocking_lock_order(company, monkeypatch):
    import app.services.auth_provider as auth_module
    import app.services.identity_links as links

    entered = asyncio.Event()
    manual_locked = asyncio.Event()
    release = asyncio.Event()
    original_apply = auth_module.apply_observed_subject_link
    original_lock = links._lock

    async def barrier(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_apply(*args, **kwargs)

    async def tracked_lock(session, keys):
        await original_lock(session, keys)
        if asyncio.current_task().get_name() == "manual-repair":
            manual_locked.set()

    monkeypatch.setattr(auth_module, "apply_observed_subject_link", barrier)
    monkeypatch.setattr(links, "_lock", tracked_lock)

    async def authenticate():
        async with company.app() as db:
            await pin_rls_tenant_context(db, company.tenant)
            user, _token = await feishu_auth_provider.authenticate_with_code(
                db, code="synthetic", tenant_id=company.tenant
            )
            await db.commit()
            return user.id

    async def manual():
        async with company.app() as db:
            await pin_rls_tenant_context(db, company.tenant)
            result = await create_manual_link(
                db,
                tenant_id=company.tenant,
                actor_id=company.admin,
                org_member_id=company.member_id,
                user_id=company.first,
                provider_id=company.provider,
                subject_kind="provider_open_id",
            )
            await db.commit()
            return result.code

    sign_in = asyncio.create_task(authenticate(), name="sso-repair")
    await asyncio.wait_for(entered.wait(), 10)
    repair = asyncio.create_task(manual(), name="manual-repair")
    try:
        await asyncio.wait_for(manual_locked.wait(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(sign_in, repair, return_exceptions=True), 20)
    finally:
        release.set()
        for pending in (sign_in, repair):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(sign_in, repair, return_exceptions=True)

    failures = [result for result in results if isinstance(result, BaseException)]
    assert not failures, [(type(item).__name__, str(item)[:500]) for item in failures]
    assert results[0] == company.first
    async with company.owner() as db:
        active = (
            await db.execute(
                select(OrgMemberUserLink.subject_kind).where(
                    OrgMemberUserLink.org_member_id == company.member_id,
                    OrgMemberUserLink.status == "active",
                )
            )
        ).scalars().all()
    assert "provider_open_id" in active


async def test_desktop_global_callback_issues_a_refreshable_deep_link(company, monkeypatch):
    global_user = uuid.uuid4()
    global_provider = uuid.uuid4()
    async with company.owner() as db:
        db.add(
            IdentityProvider(
                id=global_provider,
                tenant_id=None,
                provider_type="feishu",
                name="Global",
                is_active=True,
                sso_login_enabled=True,
                config={"source": "global_env"},
            )
        )
        db.add(
            User(
                id=global_user,
                tenant_id=None,
                username="global-" + company.tag,
                email="global-" + company.tag + "@example.test",
                password_hash="unmodified-hash",
                display_name="Global",
                role="member",
                is_active=True,
                feishu_open_id="ou-global-" + company.tag,
            )
        )
        await db.flush()
        db.add(
            ExternalIdentity(
                provider_id=global_provider,
                user_id=global_user,
                provider_open_id="ou-global-" + company.tag,
            )
        )
        await db.commit()
    company.profile.update(open_id="ou-global-" + company.tag, user_id="", email="global-" + company.tag + "@example.test")

    async def get_session():
        async with company.app() as db:
            await pin_rls_tenant_context(db, None)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = get_session
    await _store_oauth_state("nonce-" + company.tag, "device-probe")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.get(
            "/api/auth/feishu/callback-desktop",
            params={"code": "synthetic", "state": "nonce-" + company.tag},
            follow_redirects=False,
        )
        location = response.headers.get("location", "")
        assert response.status_code == 302
        assert "://auth/callback?" in location
        refresh_token = parse_qs(urlparse(location).query)["refresh_token"][0]
        exchanged = await client.post(
            "/api/auth/desktop/exchange",
            json={"refresh_token": refresh_token, "device_id": "device-probe"},
        )
    assert exchanged.status_code == 200
    assert exchanged.json()["access_token"]

    company.profile.update(open_id="ou-" + company.tag, user_id="uid-" + company.tag)
    await _store_oauth_state("nonce-company-" + company.tag, "device-probe")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
        stolen = await client.get(
            "/api/auth/feishu/callback-desktop",
            params={"code": "synthetic", "state": "nonce-company-" + company.tag},
            follow_redirects=False,
        )
    stolen_location = stolen.headers.get("location", "")
    assert stolen.status_code == 302
    assert "reason=contradictory_subjects" in stolen_location
    assert "://auth/callback?" not in stolen_location
    assert str(company.first) not in stolen_location


def _desktop_app(company):
    async def get_session():
        async with company.app() as db:
            await pin_rls_tenant_context(db, None)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = get_session
    return app


async def _callback(company, state: str) -> str:
    await _store_oauth_state(state, "device-probe")
    async with AsyncClient(transport=ASGITransport(app=_desktop_app(company)), base_url="http://synthetic") as client:
        response = await client.get(
            "/api/auth/feishu/callback-desktop",
            params={"code": "synthetic", "state": state},
            follow_redirects=False,
        )
    assert response.status_code == 302
    return response.headers.get("location", "")


@pytest.mark.parametrize("foreign_kind", ["feishu", "oidc"])
async def test_global_callback_keeps_provider_subject_namespaces(company, foreign_kind):
    global_provider, global_user = uuid.uuid4(), uuid.uuid4()
    shared = "scoped-id-" + company.tag
    company.profile.update(
        open_id="ou-global-" + company.tag,
        user_id=shared,
        email="global-" + company.tag + "@example.test",
    )
    async with company.owner() as db:
        other = company.provider
        if foreign_kind != "feishu":
            other = uuid.uuid4()
            db.add(
                IdentityProvider(
                    id=other,
                    tenant_id=company.tenant,
                    provider_type=foreign_kind,
                    name="Other",
                    is_active=True,
                    sso_login_enabled=True,
                    config={},
                )
            )
        db.add(
            IdentityProvider(
                id=global_provider,
                tenant_id=None,
                provider_type="feishu",
                name="Global",
                is_active=True,
                sso_login_enabled=True,
                config={"source": "global_env"},
            )
        )
        db.add(
            User(
                id=global_user,
                tenant_id=None,
                username="global-ns-" + company.tag,
                email=company.profile["email"],
                password_hash="unchanged",
                display_name="Global",
                role="member",
                is_active=True,
                feishu_open_id=company.profile["open_id"],
            )
        )
        await db.flush()
        db.add_all(
            [
                ExternalIdentity(
                    provider_id=global_provider,
                    user_id=global_user,
                    provider_open_id=company.profile["open_id"],
                    provider_user_id=shared,
                ),
                ExternalIdentity(provider_id=other, user_id=company.first, provider_user_id=shared),
            ]
        )
        await db.commit()
    location = await _callback(company, "namespace-" + foreign_kind + "-" + company.tag)
    assert "://auth/callback?" in location
    assert "reason=contradictory_subjects" not in location


async def test_global_existing_identity_ignores_foreign_legacy_user_id(company):
    async with company.owner() as db:
        foreign = await db.get(User, company.first)
        foreign.feishu_user_id = "scoped-id-" + company.tag
        await db.commit()
    await test_global_callback_keeps_provider_subject_namespaces(company, "feishu")


async def test_global_existing_identity_ignores_foreign_legacy_union_id(company):
    company.profile["union_id"] = "union-" + company.tag
    async with company.owner() as db:
        foreign = await db.get(User, company.first)
        foreign.feishu_union_id = company.profile["union_id"]
        await db.commit()
    await test_global_callback_keeps_provider_subject_namespaces(company, "feishu")


async def test_global_login_refuses_a_tenant_subject_on_the_same_provider(company):
    proof = "ou-owned-" + company.tag
    async with company.owner() as db:
        providers = (
            await db.execute(
                select(IdentityProvider).where(
                    IdentityProvider.tenant_id.is_(None),
                    IdentityProvider.provider_type == "feishu",
                )
            )
        ).scalars().all()
        for provider in providers:
            await db.delete(provider)
        await db.flush()
        global_provider = uuid.uuid4()
        db.add(
            IdentityProvider(
                id=global_provider,
                tenant_id=None,
                provider_type="feishu",
                name="Global",
                is_active=True,
                sso_login_enabled=True,
                config={"source": "global_env"},
            )
        )
        await db.flush()
        db.add(ExternalIdentity(provider_id=global_provider, user_id=company.admin, provider_open_id=proof))
        await db.commit()
    company.profile.update(open_id=proof, user_id="", email="fresh-" + company.tag + "@example.test")
    location = await _callback(company, "same-provider-" + company.tag)
    assert "reason=contradictory_subjects" in location
    assert "://auth/callback?" not in location
    async with company.owner() as db:
        created = (
            await db.execute(select(User.id).where(User.email == "fresh-" + company.tag + "@example.test"))
        ).scalar_one_or_none()
    assert created is None


async def _replace_directory(company, people: list[OrgMember]) -> None:
    async with company.owner() as db:
        current = await db.get(OrgMember, company.member_id)
        if current is not None:
            await db.delete(current)
            await db.flush()
        db.add_all(people)
        await db.commit()


async def test_two_person_sync_and_manual_repair_do_not_deadlock(company, monkeypatch):
    second_user = uuid.uuid4()
    first_member = uuid.UUID("00000000-0000-4000-8000-000000000011")
    second_member = uuid.UUID("ffffffff-ffff-4000-8000-000000000022")
    second_open = "ou-second-" + company.tag
    second_uid = "uid-second-" + company.tag
    async with company.owner() as db:
        db.add(
            User(
                id=second_user,
                tenant_id=company.tenant,
                username="second-" + company.tag,
                email="second-" + company.tag + "@example.test",
                password_hash="unmodified-hash",
                display_name="second",
                role="member",
                is_active=True,
                feishu_open_id=second_open,
                feishu_user_id=second_uid,
            )
        )
        await db.commit()
    await _replace_directory(
        company,
        [
            OrgMember(
                id=first_member,
                tenant_id=company.tenant,
                provider_id=company.provider,
                name="First",
                status="active",
                feishu_open_id=company.profile["open_id"],
                feishu_user_id=company.profile["user_id"],
            ),
            OrgMember(
                id=second_member,
                tenant_id=company.tenant,
                provider_id=company.provider,
                name="Second",
                status="active",
                feishu_open_id=second_open,
                feishu_user_id=second_uid,
            ),
        ],
    )
    import app.services.org_sync_service as sync_module
    import app.services.identity_links as links

    entered, held, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_apply, original_lock = sync_module.apply_observed_subject_link, links._lock

    async def barrier(*args, **kwargs):
        if kwargs["member"].id == second_member and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_apply(*args, **kwargs)

    async def tracked_lock(session, keys):
        await original_lock(session, keys)
        if asyncio.current_task().get_name() == "manual-second":
            held.set()

    monkeypatch.setattr(sync_module, "apply_observed_subject_link", barrier)
    monkeypatch.setattr(links, "_lock", tracked_lock)
    snapshot = {
        "complete": True,
        "departments": [],
        "members": [
            {**company.profile, "department_id": "0"},
            {
                "open_id": second_open,
                "user_id": second_uid,
                "union_id": "",
                "name": "Second",
                "email": "second-" + company.tag + "@example.test",
                "department_id": "0",
            },
        ],
    }

    async def fetch(self, app_id, app_secret):
        return snapshot

    monkeypatch.setattr(type(org_sync_service), "_fetch_directory_readonly", fetch)

    async def manual():
        async with company.app() as db:
            await pin_rls_tenant_context(db, company.tenant)
            result = await create_manual_link(
                db,
                tenant_id=company.tenant,
                actor_id=company.admin,
                org_member_id=second_member,
                user_id=second_user,
                provider_id=company.provider,
                subject_kind="provider_open_id",
            )
            await db.commit()
            return result.code

    task = asyncio.create_task(org_sync_service.full_sync(company.tenant, actor_id=company.admin), name="sync-two")
    await asyncio.wait_for(entered.wait(), 10)
    repair = asyncio.create_task(manual(), name="manual-second")
    try:
        await asyncio.wait_for(held.wait(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(task, repair, return_exceptions=True), 20)
    finally:
        release.set()
        for pending in (task, repair):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, repair, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    assert not failures


async def test_two_member_sso_and_manual_repair_do_not_deadlock(company, monkeypatch):
    low = uuid.UUID("00000000-0000-4000-8000-000000000031")
    high = uuid.UUID("ffffffff-ffff-4000-8000-000000000032")
    await _replace_directory(
        company,
        [
            OrgMember(
                id=low,
                tenant_id=company.tenant,
                provider_id=company.provider,
                name="Open",
                status="active",
                feishu_open_id=company.profile["open_id"],
            ),
            OrgMember(
                id=high,
                tenant_id=company.tenant,
                provider_id=company.provider,
                name="User",
                status="active",
                feishu_user_id=company.profile["user_id"],
            ),
        ],
    )
    import app.services.auth_provider as auth_module
    import app.services.identity_links as links

    entered, held, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_apply, original_lock = auth_module.apply_observed_subject_link, links._lock

    async def barrier(*args, **kwargs):
        if kwargs["member"].id == high and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_apply(*args, **kwargs)

    async def tracked_lock(session, keys):
        await original_lock(session, keys)
        if asyncio.current_task().get_name() == "manual-sso":
            held.set()

    monkeypatch.setattr(auth_module, "apply_observed_subject_link", barrier)
    monkeypatch.setattr(links, "_lock", tracked_lock)

    async def sign_in():
        async with company.app() as db:
            await pin_rls_tenant_context(db, company.tenant)
            user, _token = await feishu_auth_provider.authenticate_with_code(
                db, code="synthetic", tenant_id=company.tenant
            )
            await db.commit()
            return user.id

    async def manual():
        async with company.app() as db:
            await pin_rls_tenant_context(db, company.tenant)
            result = await create_manual_link(
                db,
                tenant_id=company.tenant,
                actor_id=company.admin,
                org_member_id=high,
                user_id=company.first,
                provider_id=company.provider,
                subject_kind="provider_user_id",
            )
            await db.commit()
            return result.code

    task = asyncio.create_task(sign_in(), name="sso-two")
    await asyncio.wait_for(entered.wait(), 10)
    repair = asyncio.create_task(manual(), name="manual-sso")
    try:
        await asyncio.wait_for(held.wait(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(task, repair, return_exceptions=True), 20)
    finally:
        release.set()
        for pending in (task, repair):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, repair, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    assert not failures, [(type(item).__name__, str(item)[:500]) for item in failures]
    assert results[0] == company.first


async def test_sso_rechecks_login_disabled_before_the_token(company, monkeypatch):
    import app.services.auth_provider as auth_module

    original = auth_module.apply_observed_subject_link
    changed = False

    async def disable_then_continue(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            async with company.owner() as db:
                provider = await db.get(IdentityProvider, company.provider)
                provider.sso_login_enabled = False
                await db.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(auth_module, "apply_observed_subject_link", disable_then_continue)
    async with company.app() as db:
        await pin_rls_tenant_context(db, company.tenant)
        with pytest.raises(FeishuIdentityConflict) as raised:
            try:
                await feishu_auth_provider.authenticate_with_code(db, code="synthetic", tenant_id=company.tenant)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    assert raised.value.code == "provider_disabled"
    async with company.owner() as db:
        links = (
            await db.execute(
                select(OrgMemberUserLink.id).where(OrgMemberUserLink.org_member_id == company.member_id)
            )
        ).all()
    assert links == []


async def test_sso_rechecks_login_disabled_without_a_directory_member(company, monkeypatch):
    import app.services.auth_provider as auth_module

    company.profile.update(
        open_id="ou-new-" + company.tag,
        user_id="uid-new-" + company.tag,
        email="new-" + company.tag + "@example.test",
        name="New",
    )
    original = auth_module.FeishuAuthProvider._persist_authenticated_identity

    async def disable_then_continue(self, db, provider, user, profile):
        async with company.owner() as owner:
            current = await owner.get(IdentityProvider, company.provider)
            current.sso_login_enabled = False
            await owner.commit()
        return await original(self, db, provider, user, profile)

    monkeypatch.setattr(auth_module.FeishuAuthProvider, "_persist_authenticated_identity", disable_then_continue)
    async with company.app() as db:
        await pin_rls_tenant_context(db, company.tenant)
        with pytest.raises(FeishuIdentityConflict) as raised:
            try:
                await feishu_auth_provider.authenticate_with_code(db, code="synthetic", tenant_id=company.tenant)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    assert raised.value.code == "provider_disabled"
    async with company.owner() as db:
        created = (
            await db.execute(select(User.id).where(User.email == "new-" + company.tag + "@example.test"))
        ).scalar_one_or_none()
        identities = (
            await db.execute(
                select(ExternalIdentity.id).where(ExternalIdentity.provider_open_id == "ou-new-" + company.tag)
            )
        ).all()
    assert created is None
    assert identities == []


async def test_incoming_email_candidate_recovers_after_account_bind(company, monkeypatch):
    incoming = {
        "open_id": "ou-in-" + company.tag,
        "user_id": "uid-in-" + company.tag,
        "union_id": "",
        "name": "Admin",
        "email": "admin-" + company.tag + "@example.test",
        "department_id": "0",
    }

    async def fetch(self, app_id, app_secret):
        return {"complete": True, "departments": [], "members": [incoming]}

    monkeypatch.setattr(type(org_sync_service), "_fetch_directory_readonly", fetch)
    skipped = await org_sync_service.full_sync(company.tenant, actor_id=company.admin)
    assert skipped["applied"] == 0
    assert skipped["rows"][0]["reason"] == "email_only_candidate"
    assert skipped["rows"][0]["object_id"] == incoming["open_id"]
    async with company.owner() as db:
        stored = (
            await db.execute(select(OrgMember.id).where(OrgMember.feishu_open_id == incoming["open_id"]))
        ).scalar_one_or_none()
    assert stored is None
    company.profile.update(
        open_id=incoming["open_id"],
        user_id=incoming["user_id"],
        union_id="",
        name="Admin",
        email=incoming["email"],
    )
    async with company.app() as db:
        await pin_rls_tenant_context(db, company.tenant)
        admin = await db.get(User, company.admin)
        await feishu_auth_provider.bind_with_code(db, user=admin, code="synthetic")
        await db.commit()
    applied = await org_sync_service.full_sync(company.tenant, actor_id=company.admin)
    assert applied["applied"] == 1
    async with company.app() as db:
        await pin_rls_tenant_context(db, company.tenant)
        member = (
            await db.execute(select(OrgMember).where(OrgMember.feishu_open_id == incoming["open_id"]))
        ).scalar_one()
        links = (
            await db.execute(
                select(OrgMemberUserLink).where(
                    OrgMemberUserLink.org_member_id == member.id,
                    OrgMemberUserLink.status == "active",
                )
            )
        ).scalars().all()
        assert {link.user_id for link in links} == {company.admin}
        users = list((await db.execute(select(User).where(User.tenant_id == company.tenant))).scalars().all())
        members = list((await db.execute(select(OrgMember).where(OrgMember.tenant_id == company.tenant))).scalars().all())
        audiences = await persistent_grant_audiences(db, company.tenant, users, members)
        assert str(company.admin) in {item["user_id"] for item in audiences["grantable_users"]}
        assert str(member.id) not in {item["org_member_id"] for item in audiences["visible_unlinked"]}
        await db.rollback()
