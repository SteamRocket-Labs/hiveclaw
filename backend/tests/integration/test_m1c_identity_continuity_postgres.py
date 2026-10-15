"""Mounted Feishu sync and authenticated identity continuity on PostgreSQL RLS.

The directory transport is synthetic. Sync, OAuth, callbacks, and inbound
resolution still run through the real services and the non-owner role.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.desktop_auth import _store_oauth_state
from app.api.desktop_auth import router as desktop_router
from app.api.enterprise import router as enterprise_router
from app.api.feishu import router as feishu_router
from app.api.knowledge_company import router as knowledge_router
from app.core.security import get_current_user, verify_password
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context

from app.models.org import OrgMember
from app.models.identity import ExternalIdentity, IdentityProvider, SSOScanSession
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import Department, User
from app.services.auth_provider import FeishuIdentityConflict, feishu_auth_provider
from app.services.channel_user_service import channel_user_service
from app.services.identity_links import create_manual_link, revoke_identity_link
from app.services.org_sync_service import OrgSyncService

SECRET = "synthetic-m1c-app-secret"
APP_ID = "cli_m1c_synthetic"
GLOBAL_APP_ID = "cli_m1c_global_not_for_tenants"


def _user(db, **kwargs) -> User:
    kwargs.setdefault("password_hash", "stored-password-hash")
    kwargs.setdefault("role", "member")
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("display_name", kwargs["username"])
    row = User(**kwargs)
    db.add(row)
    return row


def _app(sessionmaker, actor):
    async def db_override():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app = FastAPI()
    app.include_router(enterprise_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.include_router(feishu_router, prefix="/api")
    app.include_router(desktop_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = db_override
    return app


def _settings():
    return SimpleNamespace(
        FEISHU_APP_ID=GLOBAL_APP_ID,
        FEISHU_APP_SECRET="global-secret-not-for-tenants",
        FEISHU_REDIRECT_URI="http://127.0.0.1:9/api/auth/feishu/callback",
        FEISHU_PLATFORM_REGION="feishu_cn",
        FEISHU_OPEN_API_DOMAIN="",
        FEISHU_OAUTH_AUTHORIZE_URL="",
        PUBLIC_BASE_URL="",
        BASE_URL="",
    )


def _profile(**kwargs) -> dict:
    profile = {
        "user_id": "",
        "open_id": "",
        "union_id": "",
        "name": "Synthetic Person",
        "email": "",
        "avatar_url": "",
        "mobile": "",
        "raw_profile": {"source": "synthetic-oauth"},
    }
    profile.update(kwargs)
    return profile


async def _rows(sessionmaker, sql: str, **params) -> list[tuple]:
    async with sessionmaker() as db:
        return [tuple(row) for row in (await db.execute(text(sql), params)).all()]


async def _journal_count(sessionmaker, tenant_id: uuid.UUID) -> int:
    rows = await _rows(
        sessionmaker,
        "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant",
        tenant=tenant_id,
    )
    return int(rows[0][0])


@pytest.fixture
def route_app_user(monkeypatch, app_user_sessionmaker):
    import app.database as app_database

    monkeypatch.setattr(app_database, "async_session", app_user_sessionmaker)
    return app_user_sessionmaker


async def test_sync_oauth_and_inbound_identity_stay_continuous(
    route_app_user,
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b, tenant_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admin_id = uuid.uuid4()
    trusted_id, email_id, inactive_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    contra_open_id, contra_user_id = uuid.uuid4(), uuid.uuid4()
    manual_user_id, revoked_user_id = uuid.uuid4(), uuid.uuid4()
    foreign_id, global_id = uuid.uuid4(), uuid.uuid4()
    plain_id, race_user_id, race_other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    trusted_open = f"ou-trusted-{tag}"
    inactive_open = f"ou-inactive-{tag}"
    contra_open = f"ou-contra-{tag}"
    contra_uid = f"u-contra-{tag}"
    manual_open = f"ou-manual-{tag}"
    revoked_open = f"ou-revoked-{tag}"
    foreign_open = f"ou-foreign-{tag}"
    global_open = f"ou-global-{tag}"
    race_open = f"ou-race-{tag}"
    root_email = f"root-{tag}@example.test"
    multi_email = f"multi-{tag}@example.test"
    email_only = f"email-only-{tag}@example.test"
    trusted_email = f"trusted-{tag}@example.test"
    department_name = f"Engineering {tag}"
    eng_id = f"od-eng-{tag}"
    company_id = f"od-co-{tag}"
    published: list[tuple] = []

    async def fetch(self, app_id, app_secret):
        assert app_secret == SECRET
        if state["mode"] == "wait":
            state["started"].set()
            await state["release"].wait()
        if state["mode"] == "incomplete":
            return {"complete": False, "departments": [], "members": []}
        return state["snapshot"]

    async def record_projection(db, tenant_id):
        published.append(("sync_org", tenant_id))

    def record_dirty(tenant_id, broadcast=True):
        published.append(("dirty", tenant_id))

    state: dict = {"mode": "apply", "snapshot": {}}
    monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", fetch)
    monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", record_projection)
    monkeypatch.setattr("app.services.workspace_sync_dirty.mark_tenant_dirty", record_dirty)
    monkeypatch.setattr("app.services.auth_provider._get_runtime_settings", _settings)
    profiles: dict[str, dict] = {}

    async def exchange(self, config, code):
        if code not in profiles:
            raise RuntimeError("synthetic code was not prepared")
        return profiles[code]

    monkeypatch.setattr(type(feishu_auth_provider), "_exchange_code_for_user", exchange)

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1C A {tag}", slug=f"m1c-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=f"M1C B {tag}", slug=f"m1c-b-{tag}"))
        db.add(Tenant(id=tenant_c, name=f"M1C C {tag}", slug=f"m1c-c-{tag}"))
        await db.flush()
        home = Department(tenant_id=tenant_a, name=f"Platform {tag}")
        named = Department(tenant_id=tenant_a, name=department_name)
        db.add_all([home, named])
        provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant_a, is_active=True,
            sso_login_enabled=True, config={"source": "tenant_setting"},
        )
        disabled = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant_c, is_active=False,
            sso_login_enabled=False, config={"source": "tenant_setting"},
        )
        global_provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=None, is_active=True,
            sso_login_enabled=True, config={"source": "global_env"},
        )
        db.add_all([provider, disabled, global_provider])
        await db.flush()
        db.add(TenantSetting(tenant_id=tenant_a, key="feishu_org_sync", value={"app_id": APP_ID, "app_secret": SECRET}))
        db.add(TenantSetting(
            tenant_id=tenant_c, key="feishu_org_sync",
            value={"app_id": "cli_disabled", "app_secret": SECRET},
        ))
        _user(db, id=admin_id, username=f"admin-{tag}", email=f"admin-{tag}@example.test", tenant_id=tenant_a, role="org_admin")
        trusted = _user(
            db, id=trusted_id, username=f"trusted-{tag}", email=trusted_email, tenant_id=tenant_a,
            display_name="Trusted", avatar_url="https://avatar.test/keep", feishu_open_id=trusted_open,
            department_id=home.id, password_hash="trusted-password-hash", role="member",
        )
        _user(db, id=email_id, username=f"email-{tag}", email=email_only, tenant_id=tenant_a, display_name="Email Only")
        _user(
            db, id=inactive_id, username=f"inactive-{tag}", email=f"inactive-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=inactive_open, is_active=False,
        )
        _user(
            db, id=contra_open_id, username=f"contra-open-{tag}", email=f"contra-open-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=contra_open,
        )
        _user(
            db, id=contra_user_id, username=f"contra-user-{tag}", email=f"contra-user-{tag}@example.test",
            tenant_id=tenant_a, feishu_user_id=contra_uid,
        )
        manual_user = _user(
            db, id=manual_user_id, username=f"manual-{tag}", email=f"manual-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=manual_open, password_hash="manual-password-hash", role="org_admin",
        )
        revoked_user = _user(
            db, id=revoked_user_id, username=f"revoked-{tag}", email=f"revoked-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=revoked_open,
        )
        _user(
            db, id=foreign_id, username=f"foreign-{tag}", email=f"foreign-{tag}@example.test",
            tenant_id=tenant_b, feishu_open_id=foreign_open,
        )
        _user(
            db, id=global_id, username=f"global-{tag}", email=f"global-{tag}@example.test",
            tenant_id=None, feishu_open_id=global_open, password_hash="global-password-hash",
        )
        _user(db, id=plain_id, username=f"plain-{tag}", email=f"plain-{tag}@example.test", tenant_id=tenant_a, password_hash="plain-password-hash")
        _user(db, id=race_user_id, username=f"race-a-{tag}", email=f"race-a-{tag}@example.test", tenant_id=tenant_a, feishu_open_id=race_open, avatar_url="https://avatar.test/race")
        _user(db, id=race_other_id, username=f"race-b-{tag}", email=f"race-b-{tag}@example.test", tenant_id=tenant_a)
        await db.flush()
        manual_member = OrgMember(
            tenant_id=tenant_a, provider_id=provider.id, name="Manual Person", feishu_open_id=manual_open,
            avatar_url="https://avatar.test/manual-old", title="Manual Title", phone="555", status="active",
        )
        revoked_member = OrgMember(
            tenant_id=tenant_a, provider_id=provider.id, name="Revoked Person", feishu_open_id=revoked_open,
            phone="444", status="active",
        )
        db.add_all([manual_member, revoked_member])
        await db.commit()
        ids = {
            "provider": provider.id,
            "disabled": disabled.id,
            "global_provider": global_provider.id,
            "home": home.id,
            "manual_member": manual_member.id,
            "revoked_member": revoked_member.id,
            "trusted_hash": trusted.password_hash,
            "manual_hash": manual_user.password_hash,
            "revoked_hash": revoked_user.password_hash,
        }

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        manual = await create_manual_link(
            db, tenant_id=tenant_a, actor_id=admin_id, org_member_id=ids["manual_member"],
            user_id=manual_user_id, provider_id=ids["provider"], subject_kind="provider_open_id",
        )
        created = await create_manual_link(
            db, tenant_id=tenant_a, actor_id=admin_id, org_member_id=ids["revoked_member"],
            user_id=revoked_user_id, provider_id=ids["provider"], subject_kind="provider_open_id",
        )
        assert manual.applied and created.link
        revoked = await revoke_identity_link(
            db, tenant_id=tenant_a, actor_id=admin_id, link_id=uuid.UUID(created.link["id"]),
            expected_version=created.link["version"], expected_user_id=revoked_user_id,
            expected_provider_id=ids["provider"], expected_subject_kind="provider_open_id",
            expected_status=created.link["stored_status"],
        )
        assert revoked.applied
        await db.commit()

    state["snapshot"] = {
        "complete": True,
        "departments": [
            {"open_department_id": company_id, "name": f"Company {tag}", "parent_department_id": "0"},
            {"open_department_id": eng_id, "name": department_name, "parent_department_id": company_id},
        ],
        "members": [
            {"open_id": f"ou-root-{tag}", "user_id": f"u-root-{tag}", "union_id": f"on-root-{tag}", "name": "Root Person", "email": root_email, "department_id": "0", "avatar_url": "https://avatar.test/root", "title": "Root Title", "phone": "10001"},
            {"open_id": f"ou-multi-{tag}", "user_id": f"u-multi-{tag}", "union_id": f"on-multi-{tag}", "name": "Multi Person", "email": multi_email, "department_id": "0", "avatar_url": "https://avatar.test/multi", "title": "Engineer", "phone": "10002"},
            {"open_id": f"ou-multi-{tag}", "user_id": f"u-multi-{tag}", "union_id": f"on-multi-{tag}", "name": "Multi Person", "email": multi_email, "department_id": eng_id, "title": "Engineer", "phone": "10002"},
            {"open_id": trusted_open, "user_id": f"u-trusted-{tag}", "union_id": f"on-trusted-{tag}", "name": "Trusted Person", "email": trusted_email, "department_id": eng_id, "avatar_url": "https://avatar.test/new", "title": "Lead", "phone": "10003"},
            {"open_id": f"ou-email-{tag}", "user_id": f"u-email-{tag}", "union_id": f"on-email-{tag}", "name": "Email Person", "email": email_only, "department_id": eng_id},
            {"open_id": inactive_open, "user_id": f"u-inactive-{tag}", "name": "Inactive Person", "email": f"inactive-new-{tag}@example.test", "department_id": "0"},
            {"open_id": contra_open, "user_id": contra_uid, "name": "Contra", "email": f"contra-new-{tag}@example.test", "department_id": "0"},
            {"open_id": foreign_open, "user_id": f"u-foreign-{tag}", "name": "Foreign", "email": f"foreign-new-{tag}@example.test", "department_id": "0"},
            {"open_id": manual_open, "user_id": f"u-manual-{tag}", "name": "Manual Updated", "email": f"manual-{tag}@example.test", "department_id": eng_id, "title": "Manual Title"},
            {"open_id": revoked_open, "user_id": f"u-revoked-{tag}", "name": "Revoked Updated", "department_id": "0"},
        ],
    }
    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_a, role="org_admin")
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1c") as client:
        response = await client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant_a)})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "complete"
        assert SECRET not in response.text and "123456" not in response.text and email_only not in response.text
        assert published == [("sync_org", tenant_a), ("dirty", tenant_a)]
        root_row = next(item for item in body["rows"] if item["object_id"] == f"ou-root-{tag}")
        multi_row = next(item for item in body["rows"] if item["object_id"] == f"ou-multi-{tag}")
        assert root_row["applied"] is True and root_row["department_ids"] == ["0"]
        assert multi_row["applied"] is True and multi_row["department_ids"] == ["0", eng_id]
        assert any(item["disposition"] == "email_only_candidate" for item in body["rows"])
        assert any(item["disposition"] == "cross_tenant" for item in body["rows"])
        assert any(item["disposition"] == "inactive_user" for item in body["rows"])
        assert any(item["reason"] == "ambiguous_identity" for item in body["rows"])
        assert body["users_created"] == 2
        assert root_email not in response.text

        listed = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_a)})
        assert listed.status_code == 200, listed.text
        assert SECRET not in listed.text
        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_a)})
        assert audiences.status_code == 200, audiences.text

    root_user = await _rows(
        owner_sessionmaker,
        "SELECT id::text, password_hash, role::text, must_change_password, coalesce(department_id::text, '') FROM users WHERE email = :email",
        email=root_email,
    )
    assert len(root_user) == 1
    assert verify_password("123456", root_user[0][1]) is False
    assert root_user[0][2] == "member" and root_user[0][3] is False and root_user[0][4] == ""
    links = await _rows(
        owner_sessionmaker,
        """
        SELECT l.subject_kind, l.status, l.evidence_json->>'source', coalesce(l.evidence_json->>'observed_at', ''),
               l.user_id::text, coalesce(l.external_identity_id::text, ''), i.raw_profile::text
        FROM org_member_user_links l
        JOIN org_members m ON m.id = l.org_member_id
        LEFT JOIN external_identities i ON i.id = l.external_identity_id
        WHERE m.feishu_open_id = :open_id
        ORDER BY l.subject_kind
        """,
        open_id=f"ou-root-{tag}",
    )
    assert [row[0] for row in links] == ["provider_open_id", "provider_union_id", "provider_user_id"]
    assert {row[1] for row in links} == {"active"}
    assert {row[2] for row in links} == {"org_sync"}
    assert all(row[3] and row[4] == root_user[0][0] and row[5] for row in links)
    assert all(row[6] and "org_sync" in row[6] and SECRET not in row[6] for row in links)
    member_rows = await _rows(
        owner_sessionmaker,
        """
        SELECT m.name, m.title, m.phone, m.avatar_url, d.feishu_id
        FROM org_members m
        LEFT JOIN org_departments d ON d.id = m.department_id
        WHERE m.feishu_open_id = :open_id
        """,
        open_id=f"ou-multi-{tag}",
    )
    assert member_rows == [("Multi Person", "Engineer", "10002", "https://avatar.test/multi", eng_id)]
    trusted_after = await _rows(
        owner_sessionmaker,
        """
        SELECT password_hash, role::text, avatar_url, coalesce(department_id::text, ''), is_active
        FROM users WHERE id = :user_id
        """,
        user_id=trusted_id,
    )
    assert trusted_after == [(ids["trusted_hash"], "member", "https://avatar.test/keep", str(ids["home"]), True)]
    manual_link = await _rows(
        owner_sessionmaker,
        """
        SELECT subject_kind, status, evidence_json->>'source', user_id::text,
               external_identity_id IS NOT NULL
        FROM org_member_user_links
        WHERE org_member_id = :member AND status = 'active'
        ORDER BY subject_kind
        """,
        member=ids["manual_member"],
    )
    assert manual_link == [
        ("provider_open_id", "active", "manual", str(manual_user_id), True),
        ("provider_user_id", "active", "org_sync", str(manual_user_id), True),
    ]
    assert await _rows(
        owner_sessionmaker,
        "SELECT password_hash, role::text FROM users WHERE id = :user_id",
        user_id=manual_user_id,
    ) == [(ids["manual_hash"], "org_admin")]
    revoked_link = await _rows(
        owner_sessionmaker,
        "SELECT status, user_id::text FROM org_member_user_links WHERE org_member_id = :member",
        member=ids["revoked_member"],
    )
    assert revoked_link == [("revoked", str(revoked_user_id))]
    preserved = await _rows(
        owner_sessionmaker,
        """
        SELECT
          (SELECT avatar_url FROM org_members WHERE id = :manual),
          (SELECT phone FROM org_members WHERE id = :revoked),
          (SELECT feishu_open_id FROM users WHERE id = :email_user),
          (SELECT is_active FROM users WHERE id = :inactive_user),
          (SELECT feishu_open_id FROM users WHERE id = :foreign_user),
          (SELECT count(*) FROM users WHERE email = :email_only),
          (SELECT count(*) FROM departments WHERE tenant_id = :tenant)
        """,
        manual=ids["manual_member"],
        revoked=ids["revoked_member"],
        email_user=email_id,
        inactive_user=inactive_id,
        foreign_user=foreign_id,
        email_only=email_only,
        tenant=tenant_a,
    )
    assert preserved == [("https://avatar.test/manual-old", "444", None, False, foreign_open, 1, 2)]
    root_member = next(item for item in listed.json()["members"] if item["label"] == "Root Person")
    assert root_member["valid"] is True
    assert {link["source"] for link in root_member["links"]} == {"org_sync"}
    manual_listed = next(item for item in listed.json()["members"] if item["org_member_id"] == str(ids["manual_member"]))
    assert manual_listed["valid"] is True
    assert {link["source"] for link in manual_listed["links"]} == {"manual", "org_sync"}
    revoked_listed = next(item for item in listed.json()["members"] if item["org_member_id"] == str(ids["revoked_member"]))
    assert revoked_listed["valid"] is False
    assert {link["status"] for link in revoked_listed["links"]} == {"revoked"}
    audience = audiences.json()
    assert root_user[0][0] in {item["user_id"] for item in audience["grantable_users"]}
    assert root_member["org_member_id"] not in {item["org_member_id"] for item in audience["visible_unlinked"]}
    journals = await _journal_count(owner_sessionmaker, tenant_a)
    synced_at = (await _rows(
        owner_sessionmaker,
        "SELECT value->>'last_synced_at' FROM tenant_settings WHERE tenant_id = :tenant AND key = 'feishu_org_sync'",
        tenant=tenant_a,
    ))[0][0]
    assert synced_at

    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1c") as client:
        again = await client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant_a)})
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "complete" and again.json()["users_created"] == 0
        assert await _journal_count(owner_sessionmaker, tenant_a) == journals
        synced_at = (await _rows(
            owner_sessionmaker,
            "SELECT value->>'last_synced_at' FROM tenant_settings WHERE tenant_id = :tenant AND key = 'feishu_org_sync'",
            tenant=tenant_a,
        ))[0][0]
        assert synced_at
        state["mode"] = "incomplete"
        failed = await client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant_a)})
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "unavailable" and failed.json()["code"] == "incomplete"
        assert [item[0] for item in published].count("sync_org") == 2
        assert (await _rows(
            owner_sessionmaker,
            "SELECT value->>'last_synced_at' FROM tenant_settings WHERE tenant_id = :tenant AND key = 'feishu_org_sync'",
            tenant=tenant_a,
        ))[0][0] == synced_at

    from app.services.org_sync_service import org_sync_service

    state["mode"] = "apply"
    state["snapshot"] = {"complete": True, "departments": [], "members": []}
    disabled_result = await org_sync_service.full_sync(tenant_c, actor_id=admin_id)
    assert disabled_result["status"] == "unavailable" and disabled_result["code"] == "provider_disabled"
    assert await _rows(owner_sessionmaker, "SELECT is_active FROM identity_providers WHERE id = :provider", provider=ids["disabled"]) == [(False,)]
    assert await _rows(owner_sessionmaker, "SELECT count(*) FROM org_members WHERE tenant_id = :tenant", tenant=tenant_c) == [(0,)]

    async with owner_sessionmaker() as db:
        race_member = OrgMember(
            tenant_id=tenant_a, provider_id=ids["provider"], name="Race Person",
            feishu_open_id=race_open, status="active",
        )
        db.add(race_member)
        await db.commit()
        race_member_id = race_member.id
    state["mode"] = "wait"
    state["started"] = asyncio.Event()
    state["release"] = asyncio.Event()
    state["snapshot"] = {
        "complete": True,
        "departments": [{"open_department_id": company_id, "name": f"Company {tag}", "parent_department_id": "0"}],
        "members": [{"open_id": race_open, "user_id": f"u-race-{tag}", "name": "Race Renamed", "department_id": "0"}],
    }

    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1c") as client:
        request = asyncio.create_task(client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant_a)}))
        await asyncio.wait_for(state["started"].wait(), timeout=20)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_a)
            race_link = await create_manual_link(
                db, tenant_id=tenant_a, actor_id=admin_id, org_member_id=race_member_id,
                user_id=race_other_id, provider_id=ids["provider"], subject_kind="provider_open_id",
            )
            assert race_link.applied
            await db.commit()
        state["release"].set()
        raced = await asyncio.wait_for(request, timeout=30)
        assert raced.status_code == 200, raced.text
        assert raced.json()["status"] == "complete"
    assert await _rows(
        owner_sessionmaker,
        "SELECT user_id::text, status, evidence_json->>'source' FROM org_member_user_links WHERE org_member_id = :member",
        member=race_member_id,
    ) == [(str(race_other_id), "active", "manual")]
    assert await _rows(
        owner_sessionmaker,
        "SELECT name FROM org_members WHERE id = :member",
        member=race_member_id,
    ) == [("Race Person",)]
    assert await _rows(
        owner_sessionmaker,
        "SELECT avatar_url, feishu_user_id FROM users WHERE id = :user_id",
        user_id=race_user_id,
    ) == [("https://avatar.test/race", None)]

    sso_open = f"ou-sso-{tag}"
    async with owner_sessionmaker() as db:
        db.add(OrgMember(
            tenant_id=tenant_a, provider_id=ids["provider"], name="SSO Person",
            feishu_open_id=sso_open, status="active",
        ))
        await db.commit()
    before_members = (await _rows(owner_sessionmaker, "SELECT count(*) FROM org_members WHERE tenant_id = :tenant", tenant=tenant_a))[0][0]
    profiles["existing"] = _profile(open_id=trusted_open, user_id=f"u-trusted-{tag}", union_id=f"on-trusted-{tag}", name="Trusted", email=trusted_email)
    profiles["sso-new"] = _profile(open_id=sso_open, user_id=f"u-sso-{tag}", name="SSO Person", email=f"sso-{tag}@example.test")
    profiles["email-only"] = _profile(open_id=f"ou-email-login-{tag}", user_id=f"u-email-login-{tag}", name="Email Login", email=email_only)
    profiles["contra"] = _profile(open_id=contra_open, user_id=contra_uid, name="Contra", email=f"contra-login-{tag}@example.test")
    profiles["inactive"] = _profile(open_id=inactive_open, name="Inactive", email=f"inactive-login-{tag}@example.test")
    profiles["global"] = _profile(open_id=global_open, name="Global", email=f"global-{tag}@example.test")
    profiles["steal"] = _profile(open_id=trusted_open, name="Steal", email=f"steal-{tag}@example.test")
    journals_before_login = await _journal_count(owner_sessionmaker, tenant_a)

    async def login(code: str, tenant_id):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            try:
                user, _token = await feishu_auth_provider.authenticate_with_code(db, code=code, tenant_id=tenant_id)
                await db.commit()
                return user
            except Exception:
                await db.rollback()
                raise

    existing = await login("existing", tenant_a)
    assert existing.id == trusted_id
    assert await _rows(owner_sessionmaker, "SELECT password_hash, role::text FROM users WHERE id = :user_id", user_id=trusted_id) == [(ids["trusted_hash"], "member")]
    assert await _journal_count(owner_sessionmaker, tenant_a) == journals_before_login
    created_user = await login("sso-new", tenant_a)
    sso_links = await _rows(
        owner_sessionmaker,
        """
        SELECT l.evidence_json->>'source', l.user_id::text, m.name
        FROM org_member_user_links l JOIN org_members m ON m.id = l.org_member_id
        WHERE m.feishu_open_id = :open_id AND l.status = 'active'
        """,
        open_id=sso_open,
    )
    assert sso_links == [("sso", str(created_user.id), "SSO Person")]
    assert (await _rows(owner_sessionmaker, "SELECT count(*) FROM org_members WHERE tenant_id = :tenant", tenant=tenant_a))[0][0] == before_members
    with pytest.raises(FeishuIdentityConflict) as email_conflict:
        await login("email-only", tenant_a)
    assert email_conflict.value.code == "email_only_candidate"
    with pytest.raises(FeishuIdentityConflict) as contra_conflict:
        await login("contra", tenant_a)
    assert contra_conflict.value.code == "contradictory_subjects"
    with pytest.raises(ValueError, match="inactive"):
        await login("inactive", tenant_a)
    with pytest.raises(RuntimeError, match="not configured"):
        await login("steal", tenant_b)
    assert await _rows(owner_sessionmaker, "SELECT count(*) FROM users WHERE email = :email", email=f"steal-{tag}@example.test") == [(0,)]

    async with app_user_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="m1c global desktop subject filter"):
            global_user, _token = await feishu_auth_provider.authenticate_with_code(db, code="global", tenant_id=None)
            await db.commit()
    assert global_user.id == global_id
    assert await _rows(owner_sessionmaker, "SELECT password_hash FROM users WHERE id = :user_id", user_id=global_id) == [("global-password-hash",)]
    async with app_user_sessionmaker() as db:
        async with enter_rls_bypass(db, reason="m1c global desktop rejects another tenant subject"):
            with pytest.raises(Exception):
                await feishu_auth_provider.authenticate_with_code(db, code="steal", tenant_id=None)
            await db.rollback()
    assert await _rows(owner_sessionmaker, "SELECT feishu_open_id, password_hash FROM users WHERE id = :user_id", user_id=trusted_id) == [(trusted_open, ids["trusted_hash"])]

    profiles["plain"] = _profile(open_id=f"ou-plain-{tag}", user_id=f"u-plain-{tag}", name="Plain", email=f"plain-login-{tag}@example.test")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        plain = await db.get(User, plain_id)
        await feishu_auth_provider.bind_with_code(db, user=plain, code="plain")
        await db.commit()
    assert (await _rows(owner_sessionmaker, "SELECT count(*) FROM org_members WHERE feishu_open_id = :open_id", open_id=f"ou-plain-{tag}"))[0][0] == 0
    assert await _rows(owner_sessionmaker, "SELECT password_hash, role::text FROM users WHERE id = :user_id", user_id=plain_id) == [("plain-password-hash", "member")]
    with pytest.raises(FeishuIdentityConflict) as bind_conflict:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_a)
            other = await db.get(User, race_other_id)
            try:
                await feishu_auth_provider.bind_with_code(db, user=other, code="plain")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    assert bind_conflict.value.code == "external_identity_conflict"

    async def poison(db, *, code, tenant_id=None):
        _user(
            db, username=f"partial-{tag}", email=f"partial-{tag}@example.test", tenant_id=tenant_a,
            display_name="Partial",
        )
        await db.flush()
        raise FeishuIdentityConflict("email_only_candidate", "email only")

    monkeypatch.setattr(feishu_auth_provider, "authenticate_with_code", poison)
    scan_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(SSOScanSession(
            id=scan_id, status="pending", tenant_id=tenant_a,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1c") as client:
        callback = await client.get("/api/auth/feishu/callback", params={"code": "partial", "state": str(scan_id)})
        assert callback.status_code == 400
        assert SECRET not in callback.text and "partial-" not in callback.text
        polled = await client.get("/api/auth/feishu/sso/poll", params={"session_id": str(scan_id)})
        assert polled.status_code == 200, polled.text
        assert polled.json()["status"] == "error"
        assert polled.json()["code"] == "email_only_candidate"
        assert "email" in polled.json()["detail"].lower()
        await _store_oauth_state(f"nonce-{tag}", "device-m1c")
        desktop = await client.get(
            "/api/auth/feishu/callback-desktop",
            params={"code": "steal", "state": f"nonce-{tag}"},
            follow_redirects=False,
        )
        assert desktop.status_code == 302
        location = desktop.headers["location"]
        assert "auth/error" in location and str(trusted_id) not in location and SECRET not in location
    assert await _rows(owner_sessionmaker, "SELECT count(*) FROM users WHERE email = :email", email=f"partial-{tag}@example.test") == [(0,)]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        matched = await channel_user_service.resolve_feishu_user(
            db, tenant_id=tenant_a, provider_open_id=trusted_open, provider_user_id=f"missing-{tag}", email=email_only,
        )
        hidden = await channel_user_service.resolve_feishu_user(
            db, tenant_id=tenant_a, provider_open_id=contra_open, provider_user_id=contra_uid,
        )
        email_ignored = await channel_user_service.resolve_feishu_user(db, tenant_id=tenant_a, email=email_only)
        global_hidden = await channel_user_service.resolve_feishu_user(db, tenant_id=tenant_a, provider_open_id=global_open)
        assert matched.id == trusted_id and hidden is None and email_ignored is None and global_hidden is None
        await db.rollback()
    async with owner_sessionmaker() as db:
        db.add(ExternalIdentity(
            provider_id=ids["disabled"], user_id=foreign_id, provider_open_id=f"ou-disabled-{tag}",
            raw_profile={"source": "fixture"},
        ))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_c)
        unresolved = await channel_user_service.resolve_feishu_user(db, tenant_id=tenant_c, provider_open_id=f"ou-disabled-{tag}")
        assert unresolved is None
        await db.rollback()


