"""Mounted identity repair against PostgreSQL, RLS, and company-knowledge audiences."""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.enterprise import router as enterprise_router
from app.api.knowledge_company import router as knowledge_router
from app.core.permissions import SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE, authorize_loaded_session_access
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.organization_identity import (
    BusinessTeam,
    BusinessTeamMembership,
    ContentInputReceipt,
    DepartmentDirectoryLink,
    IdentityCutoverJournal,
    OrgMemberUserLink,
)
from app.models.tenant import Tenant
from app.models.user import Department, User

PARENT = "session_v2_delete_fk_indexes_0907"
TABLES = (
    "business_teams",
    "business_team_memberships",
    "org_member_user_links",
    "department_directory_links",
    "identity_cutover_journal",
    "content_input_receipts",
)
SHA = "ab" * 32


def _user(db, **kwargs) -> User:
    kwargs.setdefault("password_hash", "synthetic-hash")
    kwargs.setdefault("role", "member")
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("display_name", kwargs["username"])
    row = User(**kwargs)
    db.add(row)
    return row


def _detail(response) -> dict:
    body = response.json()
    detail = body.get("detail", body)
    assert isinstance(detail, dict)
    return detail


def _app(sessionmaker, actor):
    async def db_override():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app = FastAPI()
    app.include_router(enterprise_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = db_override
    return app


async def _fingerprint(sessionmaker, tenant_id: uuid.UUID) -> dict:
    statements = {
        "users": """
            SELECT id::text, role::text, md5(password_hash), coalesce(department_id::text, ''),
                   coalesce(feishu_open_id, ''), coalesce(feishu_union_id, ''), coalesce(feishu_user_id, ''),
                   is_active
            FROM users WHERE tenant_id = :tenant OR id IN (
                SELECT user_id FROM external_identities WHERE provider_id IN (
                    SELECT id FROM identity_providers WHERE tenant_id = :tenant OR tenant_id IS NULL
                )
            )
            ORDER BY id
        """,
        "identities": """
            SELECT id::text, user_id::text, coalesce(provider_open_id, ''), coalesce(provider_union_id, ''),
                   coalesce(provider_user_id, ''), md5(raw_profile::text)
            FROM external_identities ORDER BY id
        """,
        "grants": "SELECT count(*) FROM knowledge_grants",
        "permissions": "SELECT count(*) FROM resource_permissions",
        "principals": "SELECT count(*) FROM external_principals",
    }
    async with sessionmaker() as db:
        found = {}
        for name, sql in statements.items():
            params = {"tenant": tenant_id} if ":tenant" in sql else {}
            found[name] = [tuple(row) for row in (await db.execute(text(sql), params)).all()]
        return found


async def _links(sessionmaker, tenant_id: uuid.UUID) -> list[tuple]:
    async with sessionmaker() as db:
        return [
            tuple(row)
            for row in (
                await db.execute(
                    text(
                        """
                        SELECT id::text, status, subject_kind, match_subject, user_id::text,
                               coalesce(external_identity_id::text, ''), evidence_json->>'source',
                               created_by_user_id::text
                        FROM org_member_user_links
                        WHERE tenant_id = :tenant
                        ORDER BY created_by_user_id, match_subject, status
                        """
                    ),
                    {"tenant": tenant_id},
                )
            ).all()
        ]


async def test_six_new_tables_isolate_reads_and_writes(owner_sessionmaker, app_user_sessionmaker, owner_engine):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1B A {tag}", slug=f"m1b-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=f"M1B secret company {tag}", slug=f"m1b-b-{tag}"))
        await db.flush()
        _user(db, id=user_a, username=f"m1b-a-{tag}", email=f"a-{tag}@example.test", tenant_id=tenant_a, role="org_admin")
        _user(db, id=user_b, username=f"m1b-b-{tag}", email=f"secret-{tag}@example.test", tenant_id=tenant_b)
        dept_a = Department(tenant_id=tenant_a, name="A")
        dept_b = Department(tenant_id=tenant_b, name="B")
        db.add(dept_a)
        db.add(dept_b)
        provider_a = IdentityProvider(provider_type="feishu", name="A", tenant_id=tenant_a, is_active=True, config={})
        provider_b = IdentityProvider(provider_type="feishu", name="B", tenant_id=tenant_b, is_active=True, config={})
        db.add(provider_a)
        db.add(provider_b)
        await db.flush()
        org_a = OrgDepartment(tenant_id=tenant_a, name="A", feishu_id=f"od-a-{tag}")
        org_b = OrgDepartment(tenant_id=tenant_b, name="B", feishu_id=f"od-b-{tag}")
        db.add(org_a)
        db.add(org_b)
        member_a = OrgMember(tenant_id=tenant_a, name="Person A", feishu_open_id=f"ou-a-{tag}", provider_id=provider_a.id)
        member_b = OrgMember(tenant_id=tenant_b, name="Secret Person", feishu_open_id=f"ou-b-{tag}", provider_id=provider_b.id)
        db.add(member_a)
        db.add(member_b)
        await db.commit()
        parent = {
            "a": (user_a, provider_a.id, org_a.id, member_a.id, dept_a.id),
            "b": (user_b, provider_b.id, org_b.id, member_b.id, dept_b.id),
        }

    def rows(tenant_id, user_id, provider_id, org_department_id, member_id, department_id):
        team_id = uuid.uuid4()
        return team_id, [
            BusinessTeam(id=team_id, tenant_id=tenant_id, name=f"Team {tenant_id.hex[:8]}", status="active", created_by_user_id=user_id, department_id=department_id),
            BusinessTeamMembership(tenant_id=tenant_id, business_team_id=team_id, user_id=user_id, role="member", status="active"),
            OrgMemberUserLink(
                tenant_id=tenant_id, org_member_id=member_id, user_id=user_id, provider_id=provider_id,
                subject_kind="provider_open_id", match_subject=f"subject-{tenant_id.hex[:8]}", status="active",
                evidence_json={"source": "manual", "observed_at": "2026-10-15T00:00:00+00:00"},
                created_by_user_id=user_id,
            ),
            DepartmentDirectoryLink(
                tenant_id=tenant_id, org_department_id=org_department_id, department_id=department_id,
                provider="feishu", provider_department_id=f"pd-{tenant_id.hex[:8]}", status="active",
            ),
            IdentityCutoverJournal(
                tenant_id=tenant_id, object_kind="org_member_user_link", object_id=uuid.uuid4(),
                before_json=None, after_json={"status": "active"},
            ),
            ContentInputReceipt(
                tenant_id=tenant_id, carrier_kind="team_memory", carrier_ref=f"memory-{tenant_id.hex[:8]}",
                revision=1, content_sha256=SHA, dependencies_json=[],
            ),
        ]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        team_a, records_a = rows(tenant_a, *parent["a"])
        db.add_all(records_a)
        await db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        visible = list((await db.execute(select(BusinessTeam.name))).scalars().all())
        assert any(str(team_a)[:8] in name or name.startswith("Team ") for name in visible)
        assert all("secret" not in name.lower() for name in visible)
        emails = list((await db.execute(select(User.email))).scalars().all())
        assert f"secret-{tag}@example.test" not in emails
        for model in (
            BusinessTeam, BusinessTeamMembership, OrgMemberUserLink, DepartmentDirectoryLink,
            IdentityCutoverJournal, ContentInputReceipt,
        ):
            count = len(list((await db.execute(select(model.id))).scalars().all()))
            assert count == 1
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                db.add(BusinessTeam(
                    tenant_id=tenant_b, name="Cross", status="active", created_by_user_id=user_a,
                ))
                await db.flush()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_b)
        hidden = list((await db.execute(select(OrgMemberUserLink.id))).scalars().all())
        assert hidden == []
        _team_b, records_b = rows(tenant_b, *parent["b"])
        db.add_all(records_b)
        await db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_b)
        renamed = await db.execute(text("UPDATE business_teams SET name = 'stolen' WHERE tenant_id = :tenant"), {"tenant": tenant_a})
        assert renamed.rowcount == 0
        await db.commit()

    async with owner_engine.connect() as conn:
        forced = (await conn.execute(text(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, p.polname AS policyname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_policy p ON p.polrelid = c.oid
            WHERE n.nspname = 'public' AND c.relname = ANY(:names)
            ORDER BY c.relname
            """
        ), {"names": list(TABLES)})).all()
    assert {row.relname for row in forced} == set(TABLES)
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in forced)
    assert {row.policyname for row in forced} == {f"tenant_isolation_{name}" for name in TABLES}


async def test_manual_repair_persists_and_is_consumed(
    owner_sessionmaker, app_user_sessionmaker,
):
    tag = uuid.uuid4().hex[:8]
    tenant_id, other_tenant = uuid.uuid4(), uuid.uuid4()
    admin_id, ada_id, bea_id, cam_id, dee_id, eve_id = (uuid.uuid4() for _ in range(6))
    subject = f"ou-ada-{tag}"
    union = f"on-ada-{tag}"
    dee_subject = f"ou-dee-{tag}"
    bea_subject = f"ou-bea-{tag}"
    secret = {
        "config": f"m1b-config-secret-{tag}",
        "profile": f"m1b-profile-secret-{tag}",
        "foreign": f"M1B Foreign {tag}",
        "email": f"foreign-{tag}@example.test",
    }
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1B Home {tag}", slug=f"m1b-home-{tag}"))
        db.add(Tenant(id=other_tenant, name=secret["foreign"], slug=f"m1b-foreign-{tag}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="Home Feishu", tenant_id=tenant_id, is_active=True,
            config={"app_secret": secret["config"]},
        )
        db.add(provider)
        _user(db, id=admin_id, username=f"admin-{tag}", email=f"admin-{tag}@example.test", tenant_id=tenant_id, role="org_admin")
        _user(db, id=ada_id, username=f"ada-{tag}", email=f"ada-{tag}@example.test", display_name="Ada", tenant_id=tenant_id, feishu_open_id=subject)
        _user(db, id=bea_id, username=f"bea-{tag}", email=f"bea-{tag}@example.test", display_name="Bea", tenant_id=tenant_id, feishu_open_id=bea_subject)
        _user(db, id=cam_id, username=f"cam-{tag}", email=f"cam-{tag}@example.test", display_name="Cam", tenant_id=tenant_id, feishu_open_id=f"ou-cam-{tag}")
        _user(db, id=dee_id, username=f"dee-{tag}", email=f"dee-{tag}@example.test", display_name="Dee", tenant_id=tenant_id)
        _user(db, id=eve_id, username=f"eve-{tag}", email=f"eve-{tag}@example.test", display_name="Eve", tenant_id=tenant_id)
        _user(db, id=uuid.uuid4(), username=f"foreign-{tag}", email=secret["email"], tenant_id=other_tenant)
        await db.flush()
        identity = ExternalIdentity(
            provider_id=provider.id, user_id=ada_id, provider_open_id=subject, provider_union_id=union,
            raw_profile={"token": secret["profile"]},
        )
        db.add(identity)
        ada = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Ada Person", feishu_open_id=subject, unionid=union)
        cam = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Cam Person", feishu_open_id=f"ou-cam-{tag}")
        blank = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Blank Person")
        dee = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Dee Person", feishu_open_id=dee_subject)
        db.add_all([ada, cam, blank, dee])
        await db.commit()
        ids = {
            "provider": provider.id, "identity": identity.id, "ada": ada.id, "cam": cam.id,
            "blank": blank.id, "dee": dee.id,
        }
    before = await _fingerprint(owner_sessionmaker, tenant_id)
    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_id, role="org_admin")
    captured: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())  # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    try:
        async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1b") as client:
            listed = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)})
            assert listed.status_code == 200, listed.text
            body = listed.text
            assert subject not in body and union not in body
            assert secret["config"] not in body and secret["profile"] not in body
            assert secret["foreign"] not in body and secret["email"] not in body
            payload = listed.json()
            ada_row = next(item for item in payload["members"] if item["org_member_id"] == str(ids["ada"]))
            assert ada_row["reason"] == "unregistered"
            assert ada_row["subject_kinds"] == ["provider_open_id", "provider_union_id"]
            assert ada_row["links"] == []
            blank_row = next(item for item in payload["members"] if item["org_member_id"] == str(ids["blank"]))
            assert blank_row["reason"] == "missing_provider_subject"
            assert secret["config"] not in str(payload["providers"])
            assert all("email" not in account for account in payload["accounts"])

            create_body = {
                "org_member_id": str(ids["ada"]),
                "user_id": str(ada_id),
                "provider_id": str(ids["provider"]),
                "subject_kind": "provider_union_id",
            }
            created = await client.post("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)}, json=create_body)
            assert created.status_code == 200, created.text
            created_body = created.json()
            assert created_body["applied"] is True and created_body["code"] == "active"
            assert created_body["link"]["subject_kind"] == "provider_union_id"
            link_id = created_body["link"]["id"]
            version = created_body["link"]["version"]
            assert subject not in created.text and union not in created.text
            repeated = await client.post("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)}, json=create_body)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["link"]["id"] == link_id
            assert repeated.json()["message"] == "This link is already stored."

            audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_id)})
            assert audiences.status_code == 200, audiences.text
            audience_body = audiences.json()
            assert str(ids["ada"]) not in {item["org_member_id"] for item in audience_body["visible_unlinked"]}
            assert str(ids["cam"]) not in {item["org_member_id"] for item in audience_body["visible_unlinked"]}
            assert {item["org_member_id"]: item["reason"] for item in audience_body["visible_unlinked"]}[str(ids["blank"])] == "unregistered"
            assert str(ada_id) in {item["user_id"] for item in audience_body["grantable_users"]}

            forged = await client.post(
                "/api/enterprise/org/identity-links",
                params={"tenant_id": str(tenant_id)},
                json={**create_body, "match_subject": "forged"},
            )
            assert forged.status_code == 422

            dee_body = {
                "org_member_id": str(ids["dee"]), "user_id": str(dee_id),
                "provider_id": str(ids["provider"]), "subject_kind": "provider_open_id",
            }
            dee_created = await client.post("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)}, json=dee_body)
            assert dee_created.status_code == 200 and dee_created.json()["applied"] is True
            dee_link = dee_created.json()["link"]
            stale = await client.post(
                f"/api/enterprise/org/identity-links/{dee_link['id']}/replace",
                params={"tenant_id": str(tenant_id)},
                json={
                    "expected_version": "not-the-version",
                    "expected_user_id": dee_link["user_id"],
                    "expected_provider_id": dee_link["provider_id"],
                    "expected_subject_kind": dee_link["subject_kind"],
                    "expected_status": dee_link["stored_status"],
                    "user_id": str(eve_id),
                    "provider_id": str(ids["provider"]),
                    "subject_kind": "provider_open_id",
                },
            )
            assert stale.status_code == 409 and _detail(stale)["code"] == "stale_mapping"
            assert _detail(stale)["retryable"] is True
            replaced = await client.post(
                f"/api/enterprise/org/identity-links/{dee_link['id']}/replace",
                params={"tenant_id": str(tenant_id)},
                json={
                    "expected_version": dee_link["version"],
                    "expected_user_id": dee_link["user_id"],
                    "expected_provider_id": dee_link["provider_id"],
                    "expected_subject_kind": dee_link["subject_kind"],
                    "expected_status": dee_link["stored_status"],
                    "user_id": str(eve_id),
                    "provider_id": str(ids["provider"]),
                    "subject_kind": "provider_open_id",
                },
            )
            assert replaced.status_code == 200, replaced.text
            assert replaced.json()["applied"] is True
            assert replaced.json()["link"]["user_id"] == str(eve_id)
            assert replaced.json()["link"]["id"] != dee_link["id"]

            async with owner_sessionmaker() as db:
                await db.execute(
                    text("UPDATE org_members SET unionid = :union WHERE id = :member"),
                    {"union": f"on-stale-{tag}", "member": ids["ada"]},
                )
                await db.commit()
            stale_audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_id)})
            stale_reasons = {item["org_member_id"]: item["reason"] for item in stale_audiences.json()["visible_unlinked"]}
            assert stale_reasons[str(ids["ada"])] == "stale_subject"

            revoked = await client.post(
                f"/api/enterprise/org/identity-links/{link_id}/revoke",
                params={"tenant_id": str(tenant_id)},
                json={
                    "expected_version": version,
                    "expected_user_id": str(ada_id),
                    "expected_provider_id": str(ids["provider"]),
                    "expected_subject_kind": "provider_union_id",
                    "expected_status": "active",
                },
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["applied"] is True and revoked.json()["code"] == "revoked"
            again = await client.post(
                f"/api/enterprise/org/identity-links/{link_id}/revoke",
                params={"tenant_id": str(tenant_id)},
                json={
                    "expected_version": revoked.json()["link"]["version"],
                    "expected_user_id": str(ada_id),
                    "expected_provider_id": str(ids["provider"]),
                    "expected_subject_kind": "provider_union_id",
                    "expected_status": "revoked",
                },
            )
            assert again.status_code == 200 and again.json()["message"] == "This link is already revoked."
            after_revoke = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_id)})
            reasons = {item["org_member_id"]: item["reason"] for item in after_revoke.json()["visible_unlinked"]}
            assert reasons[str(ids["ada"])] == "revoked"
            assert str(ids["dee"]) not in reasons
            assert str(ada_id) in {item["user_id"] for item in after_revoke.json()["grantable_users"]}
            assert str(eve_id) in {item["user_id"] for item in after_revoke.json()["grantable_users"]}
    finally:
        logging.getLogger().removeHandler(handler)

    assert await _fingerprint(owner_sessionmaker, tenant_id) == before
    async with owner_sessionmaker() as db:
        stored = (await db.execute(text(
            """
            SELECT status, external_identity_id::text, evidence_json->>'source', created_by_user_id::text
            FROM org_member_user_links WHERE id = :link
            """
        ), {"link": link_id})).one()
        assert stored == ("revoked", str(ids["identity"]), "manual", str(admin_id))
        journals = (await db.execute(text(
            """
            SELECT before_json IS NULL, after_json->>'match_subject', after_json->>'status'
            FROM identity_cutover_journal
            WHERE tenant_id = :tenant AND object_id = :link
            ORDER BY applied_at, after_json->>'status'
            """
        ), {"tenant": tenant_id, "link": link_id})).all()
        assert list(journals) == [(True, union, "active"), (False, union, "revoked")]
        audits = (await db.execute(text(
            """
            SELECT action, details::text FROM security_audit_events
            WHERE tenant_id = :tenant AND event_type = 'org.identity_link' AND resource_id = :link
            ORDER BY created_at
            """
        ), {"tenant": tenant_id, "link": link_id})).all()
    assert [row.action for row in audits] == ["identity_link.create", "identity_link.revoke"]
    assert all(subject not in row.details for row in audits)
    leaked = [item for item in (secret.values()) if any(item in line for line in captured)]
    assert leaked == []


async def test_repair_rejects_unsafe_targets_without_resurrecting_them(owner_sessionmaker, app_user_sessionmaker):
    tag = uuid.uuid4().hex[:8]
    tenant_id, other_tenant = uuid.uuid4(), uuid.uuid4()
    admin_id, active_id, inactive_id, other_user_id, foreign_id = (uuid.uuid4() for _ in range(5))
    open_id = f"ou-conflict-{tag}"
    secret = f"m1b-foreign-secret-{tag}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1B Reject {tag}", slug=f"m1b-reject-{tag}"))
        db.add(Tenant(id=other_tenant, name=f"M1B Other {tag}", slug=f"m1b-other-{tag}"))
        await db.flush()
        provider = IdentityProvider(provider_type="feishu", name="Home", tenant_id=tenant_id, is_active=True, config={})
        quiet = IdentityProvider(provider_type="oidc", name="Quiet", tenant_id=tenant_id, is_active=False, config={})
        foreign = IdentityProvider(
            provider_type="feishu", name="Foreign", tenant_id=other_tenant, is_active=True, config={"app_secret": secret},
        )
        db.add_all([provider, quiet, foreign])
        _user(db, id=admin_id, username=f"rej-admin-{tag}", email=f"rej-admin-{tag}@example.test", tenant_id=tenant_id, role="org_admin")
        _user(db, id=active_id, username=f"rej-active-{tag}", email=f"rej-active-{tag}@example.test", display_name="Active", tenant_id=tenant_id)
        _user(db, id=inactive_id, username=f"rej-inactive-{tag}", email=f"rej-inactive-{tag}@example.test", display_name="Inactive", tenant_id=tenant_id, is_active=False)
        other = _user(db, id=other_user_id, username=f"rej-other-{tag}", email=f"rej-other-{tag}@example.test", display_name="Other", tenant_id=tenant_id, feishu_open_id=open_id)
        _user(db, id=foreign_id, username=f"rej-foreign-{tag}", email=f"rej-foreign-{tag}@example.test", display_name="Foreign User", tenant_id=other_tenant)
        await db.flush()
        identity = ExternalIdentity(provider_id=provider.id, user_id=other.id, provider_open_id=open_id, raw_profile={"token": secret})
        db.add(identity)
        conflict = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Conflict Person", feishu_open_id=open_id)
        inactive = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Inactive Person", feishu_open_id=f"ou-inactive-{tag}")
        ambiguous = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Ambiguous Person", feishu_open_id=f"ou-left-{tag}", open_id=f"ou-right-{tag}")
        blank = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="No Subject")
        foreign_member = OrgMember(tenant_id=other_tenant, name="Foreign Person", feishu_open_id=f"ou-foreign-{tag}")
        db.add_all([conflict, inactive, ambiguous, blank, foreign_member])
        await db.commit()
        ids = {
            "provider": provider.id, "quiet": quiet.id, "foreign_provider": foreign.id, "identity": identity.id,
            "conflict": conflict.id, "inactive": inactive.id, "ambiguous": ambiguous.id, "blank": blank.id,
            "foreign_member": foreign_member.id,
        }

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_id, role="org_admin")
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1b") as client:
        async def post(member_id, user_id, provider_id, kind="provider_open_id", extra=None):
            body = {"org_member_id": str(member_id), "user_id": str(user_id), "provider_id": str(provider_id), "subject_kind": kind}
            if extra:
                body.update(extra)
            return await client.post("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)}, json=body)

        extra = await post(ids["blank"], active_id, ids["provider"], extra={"tenant_id": str(other_tenant), "role": "platform_admin"})
        assert extra.status_code == 422
        cross_user = await post(ids["inactive"], foreign_id, ids["provider"])
        assert cross_user.status_code == 409 and _detail(cross_user)["code"] == "cross_tenant"
        assert secret not in cross_user.text and "Foreign User" not in cross_user.text
        cross_member = await post(ids["foreign_member"], active_id, ids["provider"])
        assert cross_member.status_code == 409 and _detail(cross_member)["code"] == "cross_tenant"
        wrong_provider = await post(ids["inactive"], active_id, ids["foreign_provider"])
        assert wrong_provider.status_code == 409 and _detail(wrong_provider)["code"] == "unproven_provider"
        assert secret not in wrong_provider.text
        quiet_provider = await post(ids["inactive"], active_id, ids["quiet"])
        assert quiet_provider.status_code == 409 and _detail(quiet_provider)["code"] == "unproven_provider"
        missing = await post(ids["blank"], active_id, ids["provider"])
        assert missing.status_code == 409 and _detail(missing)["code"] == "missing_provider_subject"
        disagree = await post(ids["ambiguous"], active_id, ids["provider"])
        assert disagree.status_code == 409 and _detail(disagree)["code"] == "ambiguous_subject"
        wrong_kind = await post(ids["inactive"], active_id, ids["provider"], kind="provider_union_id")
        assert wrong_kind.status_code == 409 and _detail(wrong_kind)["code"] == "missing_provider_subject"
        inactive = await post(ids["inactive"], inactive_id, ids["provider"])
        assert inactive.status_code == 200, inactive.text
        assert inactive.json()["applied"] is False and inactive.json()["code"] == "inactive_user"
        conflict = await post(ids["conflict"], active_id, ids["provider"])
        assert conflict.status_code == 200, conflict.text
        assert conflict.json()["applied"] is False and conflict.json()["code"] == "conflict"
        assert open_id not in conflict.text and secret not in conflict.text
        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_id)})
        reasons = {item["org_member_id"]: item["reason"] for item in audiences.json()["visible_unlinked"]}
        assert reasons[str(ids["conflict"])] == "conflict"
        assert reasons[str(ids["inactive"])] == "authenticated_user_inactive"
        assert str(ids["blank"]) in reasons and reasons[str(ids["blank"])] == "unregistered"
        assert str(other_user_id) in {item["user_id"] for item in audiences.json()["grantable_users"]}
        assert str(inactive_id) not in {item["user_id"] for item in audiences.json()["grantable_users"]}
        actor.role = "member"
        denied = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(tenant_id)})
        assert denied.status_code == 403
        actor.role = "org_admin"
        actor.tenant_id = tenant_id
        foreign_scope = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(other_tenant)})
        assert foreign_scope.status_code == 403

    rows = await _links(owner_sessionmaker, tenant_id)
    assert {row[1] for row in rows} == {"inactive_user", "conflict"}
    async with owner_sessionmaker() as db:
        identity = (await db.execute(text(
            "SELECT user_id::text, md5(raw_profile::text) FROM external_identities WHERE id = :identity"
        ), {"identity": ids["identity"]})).one()
        pointer = (await db.execute(text(
            "SELECT coalesce(external_identity_id::text, '') FROM org_member_user_links WHERE org_member_id = :member"
        ), {"member": ids["conflict"]})).scalar_one()
    assert identity.user_id == str(other_user_id)
    assert pointer == ""


async def test_duplicate_and_concurrent_repairs_keep_one_active_row(owner_sessionmaker, app_user_sessionmaker):
    tag = uuid.uuid4().hex[:8]
    tenant_id = uuid.uuid4()
    admin_id, first_id, second_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1B Race {tag}", slug=f"m1b-race-{tag}"))
        await db.flush()
        provider = IdentityProvider(provider_type="feishu", name="Race", tenant_id=tenant_id, is_active=True, config={})
        db.add(provider)
        _user(db, id=admin_id, username=f"race-admin-{tag}", email=f"race-admin-{tag}@example.test", tenant_id=tenant_id, role="org_admin")
        _user(db, id=first_id, username=f"race-a-{tag}", email=f"race-a-{tag}@example.test", tenant_id=tenant_id)
        _user(db, id=second_id, username=f"race-b-{tag}", email=f"race-b-{tag}@example.test", tenant_id=tenant_id)
        await db.flush()
        same = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Same", feishu_open_id=f"ou-same-{tag}")
        clash = OrgMember(tenant_id=tenant_id, provider_id=provider.id, name="Clash", feishu_open_id=f"ou-clash-{tag}")
        db.add_all([same, clash])
        await db.commit()
        provider_id, same_id, clash_id = provider.id, same.id, clash.id

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_id, role="org_admin")

    async def post(member_id, user_id):
        async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1b") as client:
            return await client.post(
                "/api/enterprise/org/identity-links",
                params={"tenant_id": str(tenant_id)},
                json={
                    "org_member_id": str(member_id),
                    "user_id": str(user_id),
                    "provider_id": str(provider_id),
                    "subject_kind": "provider_open_id",
                },
            )

    same_results = await asyncio.gather(post(same_id, first_id), post(same_id, first_id))
    assert {item.status_code for item in same_results} <= {200, 409}
    assert any(item.status_code == 200 and item.json()["applied"] is True for item in same_results)
    for item in same_results:
        if item.status_code == 409:
            assert _detail(item)["code"] == "active_link_conflict" and _detail(item)["retryable"] is True
    clash_results = await asyncio.gather(post(clash_id, first_id), post(clash_id, second_id))
    assert sorted(item.status_code for item in clash_results) == [200, 409]
    winner = next(item for item in clash_results if item.status_code == 200)
    loser = next(item for item in clash_results if item.status_code == 409)
    assert winner.json()["applied"] is True
    assert _detail(loser)["retryable"] is True and _detail(loser)["code"] == "active_link_conflict"
    async with owner_sessionmaker() as db:
        active = (await db.execute(text(
            "SELECT org_member_id::text, count(*) FROM org_member_user_links WHERE tenant_id = :tenant AND status = 'active' GROUP BY org_member_id"
        ), {"tenant": tenant_id})).all()
        journals = (await db.execute(text(
            "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant AND object_kind = 'org_member_user_link'"
        ), {"tenant": tenant_id})).scalar_one()
    assert {row[0]: row[1] for row in active} == {str(same_id): 1, str(clash_id): 1}
    assert journals == 2


async def _seed_repair(factory, *, legacy=False, second=False, oidc_provider=False):
    tag = uuid.uuid4().hex[:10]
    tenant_id, admin_id, user_id, other_id, cam_id = (uuid.uuid4() for _ in range(5))
    async with factory() as db:
        db.add(Tenant(id=tenant_id, name=f"M1B Repair {tag}", slug=f"m1b-repair-{tag}"))
        await db.flush()
        _user(db, id=admin_id, username=f"repair-admin-{tag}", email=f"repair-admin-{tag}@example.test", tenant_id=tenant_id, role="org_admin")
        _user(db, id=user_id, username=f"repair-user-{tag}", email=f"repair-user-{tag}@example.test", display_name="Ada", tenant_id=tenant_id)
        _user(db, id=other_id, username=f"repair-other-{tag}", email=f"repair-other-{tag}@example.test", display_name="Bea", tenant_id=tenant_id)
        _user(db, id=cam_id, username=f"repair-cam-{tag}", email=f"repair-cam-{tag}@example.test", display_name="Cam", tenant_id=tenant_id)
        provider = IdentityProvider(provider_type="feishu", name="Feishu", tenant_id=tenant_id, is_active=True, config={})
        db.add(provider)
        oidc = None
        if oidc_provider:
            oidc = IdentityProvider(provider_type="oidc", name="OIDC", tenant_id=tenant_id, is_active=True, config={})
            db.add(oidc)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant_id,
            provider_id=None if legacy else provider.id,
            name="First",
            feishu_open_id=f"open-{tag}",
            unionid=f"union-{tag}",
        )
        db.add(member)
        second_member = None
        if second:
            second_member = OrgMember(
                tenant_id=tenant_id,
                provider_id=provider.id,
                name="Second",
                feishu_open_id=f"second-{tag}",
                unionid=f"union-{tag}",
            )
            db.add(second_member)
        assigned = None
        if oidc is not None:
            assigned = OrgMember(
                tenant_id=tenant_id,
                provider_id=oidc.id,
                name="Assigned OIDC",
                feishu_open_id=f"oidc-open-{tag}",
            )
            db.add(assigned)
        await db.flush()
        await db.commit()
        return SimpleNamespace(
            tenant=tenant_id,
            admin=admin_id,
            user=user_id,
            other=other_id,
            cam=cam_id,
            member=member.id,
            second=second_member.id if second_member is not None else None,
            assigned=assigned.id if assigned is not None else None,
            provider=provider.id,
            oidc=oidc.id if oidc is not None else None,
            tag=tag,
            actor=SimpleNamespace(id=admin_id, tenant_id=tenant_id, role="org_admin"),
        )


def _repair_client(factory, actor):
    return AsyncClient(transport=ASGITransport(app=_app(factory, actor)), base_url="http://m1b")


def _create_body(world, **extra):
    body = {
        "org_member_id": str(world.member),
        "user_id": str(world.user),
        "provider_id": str(world.provider),
        "subject_kind": "provider_open_id",
    }
    body.update(extra)
    return body


def _expected_body(link, **extra):
    body = {
        "expected_version": link["version"],
        "expected_user_id": link["user_id"],
        "expected_provider_id": link["provider_id"],
        "expected_subject_kind": link["subject_kind"],
        "expected_status": link["stored_status"],
    }
    body.update(extra)
    return body


async def test_replace_accepts_the_adapter_body_and_reloads(owner_sessionmaker, app_user_sessionmaker):
    world = await _seed_repair(owner_sessionmaker)
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        created = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world),
        )
        assert created.status_code == 200, created.text
        link = created.json()["link"]
        rejected = await client.post(
            f"/api/enterprise/org/identity-links/{link['id']}/replace",
            params={"tenant_id": str(world.tenant)},
            json=_expected_body(
                link,
                org_member_id=str(world.member),
                user_id=str(world.other),
                provider_id=str(world.provider),
                subject_kind="provider_open_id",
            ),
        )
        assert rejected.status_code == 422, rejected.text
        adapter_body = _expected_body(
            link,
            user_id=str(world.other),
            provider_id=str(world.provider),
            subject_kind="provider_open_id",
        )
        assert "org_member_id" not in adapter_body
        replaced = await client.post(
            f"/api/enterprise/org/identity-links/{link['id']}/replace",
            params={"tenant_id": str(world.tenant)},
            json=adapter_body,
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["applied"] is True
        new_link = replaced.json()["link"]
        assert new_link["user_id"] == str(world.other)
        assert new_link["org_member_id"] == str(world.member)
        reloaded = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(world.tenant)})
        assert reloaded.status_code == 200, reloaded.text
        member = next(item for item in reloaded.json()["members"] if item["org_member_id"] == str(world.member))
        stored = next(item for item in member["links"] if item["id"] == new_link["id"])
        assert stored["user_id"] == str(world.other) and stored["valid"] is True
    async with owner_sessionmaker() as db:
        rows = (await db.execute(text(
            """
            SELECT id::text, status, user_id::text
            FROM org_member_user_links
            WHERE tenant_id = :tenant
            ORDER BY status, id
            """
        ), {"tenant": world.tenant})).all()
    assert (new_link["id"], "active", str(world.other)) in {tuple(row) for row in rows}
    assert (link["id"], "revoked", str(world.user)) in {tuple(row) for row in rows}


async def test_shared_subject_conflicts_and_legacy_namespace_stays_feishu(owner_sessionmaker, app_user_sessionmaker):
    world = await _seed_repair(owner_sessionmaker, legacy=True, second=True, oidc_provider=True)
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        created = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, subject_kind="provider_union_id"),
        )
        assert created.status_code == 200, created.text
        link = created.json()["link"]
        repeated = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, subject_kind="provider_union_id"),
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["applied"] is True and repeated.json()["link"]["id"] == link["id"]
        shared = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, org_member_id=str(world.second), subject_kind="provider_union_id"),
        )
        assert shared.status_code == 409, shared.text
        assert _detail(shared)["code"] == "active_link_conflict"
        assert _detail(shared)["applied"] is False
        assert _detail(shared)["link"] is None
        oidc_create = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, provider_id=str(world.oidc)),
        )
        assert oidc_create.status_code == 409, oidc_create.text
        assert _detail(oidc_create)["code"] == "unproven_provider"
        moved = await client.post(
            f"/api/enterprise/org/identity-links/{link['id']}/replace",
            params={"tenant_id": str(world.tenant)},
            json=_expected_body(
                link,
                user_id=str(world.user),
                provider_id=str(world.oidc),
                subject_kind="provider_union_id",
            ),
        )
        assert moved.status_code == 409, moved.text
        assert _detail(moved)["code"] == "unproven_provider"
        assigned = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(
                world,
                org_member_id=str(world.assigned),
                provider_id=str(world.oidc),
            ),
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["applied"] is True
        assert assigned.json()["link"]["org_member_id"] == str(world.assigned)
    async with owner_sessionmaker() as db:
        unread_id = uuid.uuid4()
        db.add(OrgMember(
            id=unread_id, tenant_id=world.tenant, provider_id=None, name="Unread",
            feishu_open_id=f"unread-{world.tag}",
        ))
        await db.flush()
        db.add(OrgMemberUserLink(
            tenant_id=world.tenant, org_member_id=unread_id, user_id=world.user, provider_id=world.oidc,
            subject_kind="provider_open_id", match_subject=f"unread-{world.tag}", status="active",
            evidence_json={"source": "manual", "observed_at": "2026-10-15T00:00:00+00:00"},
            created_by_user_id=world.admin,
        ))
        await db.commit()
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        listed = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(world.tenant)})
        assert listed.status_code == 200, listed.text
        rows = {item["org_member_id"]: item for item in listed.json()["members"]}
        assert rows[str(world.member)]["valid"] is True and rows[str(world.member)]["reason"] == "active"
        assert rows[str(world.member)]["links"][0]["id"] == link["id"]
        unread = rows[str(unread_id)]
        assert unread["valid"] is False and unread["reason"] == "unproven_provider"
        assert unread["links"][0]["valid"] is False and unread["links"][0]["reason"] == "unproven_provider"
        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(world.tenant)})
        assert audiences.status_code == 200, audiences.text
        reasons = {item["org_member_id"]: item["reason"] for item in audiences.json()["visible_unlinked"]}
        assert str(world.member) not in reasons
        assert reasons[str(unread_id)] == "unproven_provider"
    async with owner_sessionmaker() as db:
        active = (await db.execute(text(
            """
            SELECT org_member_id::text FROM org_member_user_links
            WHERE tenant_id = :tenant AND status = 'active' AND org_member_id = :member
            """
        ), {"tenant": world.tenant, "member": world.member})).scalars().all()
    assert list(active) == [str(world.member)]


async def test_revoke_compares_the_locked_row(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.services import identity_links

    world = await _seed_repair(owner_sessionmaker)
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        created = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world),
        )
        assert created.status_code == 200, created.text
        link = created.json()["link"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = identity_links._lock
        state = {"first": True}

        async def barrier(session, keys):
            if state["first"]:
                state["first"] = False
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            await original(session, keys)

        monkeypatch.setattr(identity_links, "_lock", barrier)
        url = f"/api/enterprise/org/identity-links/{link['id']}/revoke"
        body = _expected_body(link)
        stale_request = asyncio.create_task(client.post(url, params={"tenant_id": str(world.tenant)}, json=body))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            winner = await client.post(url, params={"tenant_id": str(world.tenant)}, json=body)
            assert winner.status_code == 200, winner.text
            assert winner.json()["applied"] is True
        finally:
            release.set()
        stale = await asyncio.wait_for(stale_request, 5)
    assert stale.status_code == 409, stale.text
    assert _detail(stale)["code"] == "stale_mapping" and _detail(stale)["retryable"] is True
    async with owner_sessionmaker() as db:
        count = await db.scalar(text(
            "SELECT count(*) FROM identity_cutover_journal WHERE object_id = :link"
        ), {"link": uuid.UUID(link["id"])})
    assert count == 2


@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.parametrize("change", ["member_namespace", "user_inactive", "provider_inactive"])
async def test_target_change_during_lock_wait_is_not_reported_active(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, operation, change,
):
    from app.services import identity_links

    world = await _seed_repair(owner_sessionmaker, oidc_provider=True)
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        link = None
        if operation == "replace":
            created = await client.post(
                "/api/enterprise/org/identity-links",
                params={"tenant_id": str(world.tenant)},
                json=_create_body(world),
            )
            assert created.status_code == 200, created.text
            link = created.json()["link"]
        target_user = world.other if link else world.user
        entered, release = asyncio.Event(), asyncio.Event()
        original = identity_links._lock

        async def barrier(session, keys):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            await original(session, keys)

        monkeypatch.setattr(identity_links, "_lock", barrier)
        body = _create_body(world, user_id=str(target_user))
        url = "/api/enterprise/org/identity-links"
        if link:
            body.pop("org_member_id")
            body.update(_expected_body(
                link,
                user_id=str(target_user),
                provider_id=str(world.provider),
                subject_kind="provider_open_id",
            ))
            url += f"/{link['id']}/replace"
        pending = asyncio.create_task(client.post(url, params={"tenant_id": str(world.tenant)}, json=body))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with owner_sessionmaker() as db:
                if change == "member_namespace":
                    await db.execute(
                        text("UPDATE org_members SET provider_id=:provider WHERE id=:id"),
                        {"provider": world.oidc, "id": world.member},
                    )
                elif change == "user_inactive":
                    await db.execute(
                        text("UPDATE users SET is_active=false WHERE id=:id"),
                        {"id": target_user},
                    )
                else:
                    await db.execute(
                        text("UPDATE identity_providers SET is_active=false WHERE id=:id"),
                        {"id": world.provider},
                    )
                await db.commit()
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 5)
        payload = response.json()
        assert not (
            response.status_code == 200 and payload.get("applied") is True and payload.get("code") == "active"
        ), payload
        if operation == "create" and change == "user_inactive":
            assert response.status_code == 200, response.text
            assert payload["applied"] is False and payload["code"] == "inactive_user"
            assert payload["link"]["status"] == "inactive_user"
        else:
            assert response.status_code == 409, response.text
            detail = _detail(response)
            expected = {
                "member_namespace": "provider_namespace",
                "user_inactive": "inactive_user",
                "provider_inactive": "unproven_provider",
            }[change]
            assert detail["code"] == expected and detail["applied"] is False and detail["link"] is None
        listed = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(world.tenant)})
        member = next(item for item in listed.json()["members"] if item["org_member_id"] == str(world.member))
        if link:
            assert [item["id"] for item in member["links"]] == [link["id"]]
            assert member["links"][0]["status"] == "active"
            if change == "user_inactive":
                assert member["valid"] is True and member["reason"] == "active"
            else:
                assert member["valid"] is False and member["reason"] == "unproven_provider"
                link_reason = "provider_namespace" if change == "member_namespace" else "unproven_provider"
                assert member["links"][0]["reason"] == link_reason
        elif change == "user_inactive":
            assert member["valid"] is False and member["reason"] == "authenticated_user_inactive"
        else:
            assert member["links"] == []
    async with owner_sessionmaker() as db:
        if link:
            status = await db.scalar(
                text("SELECT status FROM org_member_user_links WHERE id=:id"),
                {"id": uuid.UUID(link["id"])},
            )
            assert status == "active"
        rows = (await db.execute(
            text("SELECT status FROM org_member_user_links WHERE tenant_id=:tenant AND status='active'"),
            {"tenant": world.tenant},
        )).scalars().all()
    if link:
        assert rows == ["active"]
    elif change == "user_inactive":
        assert rows == []
    else:
        assert rows == []


async def test_same_account_subjects_stay_one_mapping(owner_sessionmaker, app_user_sessionmaker):
    world = await _seed_repair(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        extra = OrgMember(
            tenant_id=world.tenant, provider_id=world.provider, name="Extra",
            feishu_open_id=f"extra-open-{world.tag}", unionid=f"extra-union-{world.tag}",
        )
        db.add(extra)
        await db.commit()
        extra_id = extra.id
    async with _repair_client(app_user_sessionmaker, world.actor) as client:
        opened = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world),
        )
        assert opened.status_code == 200, opened.text
        union = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, subject_kind="provider_union_id"),
        )
        assert union.status_code == 200, union.text
        other_open = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(world, org_member_id=str(extra_id), user_id=str(world.user)),
        )
        assert other_open.status_code == 200, other_open.text
        other_union = await client.post(
            "/api/enterprise/org/identity-links",
            params={"tenant_id": str(world.tenant)},
            json=_create_body(
                world,
                org_member_id=str(extra_id),
                user_id=str(world.other),
                subject_kind="provider_union_id",
            ),
        )
        assert other_union.status_code == 200, other_union.text
        listed = await client.get("/api/enterprise/org/identity-links", params={"tenant_id": str(world.tenant)})
        assert listed.status_code == 200, listed.text
        rows = {item["org_member_id"]: item for item in listed.json()["members"]}
        assert rows[str(world.member)]["valid"] is True and rows[str(world.member)]["reason"] == "active"
        assert len(rows[str(world.member)]["links"]) == 2
        assert rows[str(extra_id)]["valid"] is False and rows[str(extra_id)]["reason"] == "ambiguous_identity"
        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(world.tenant)})
        assert audiences.status_code == 200, audiences.text
        reasons = {item["org_member_id"]: item["reason"] for item in audiences.json()["visible_unlinked"]}
        assert str(world.member) not in reasons
        assert reasons[str(extra_id)] == "ambiguous_identity"
        grantable = {item["user_id"] for item in audiences.json()["grantable_users"]}
        assert str(world.user) not in grantable and str(world.other) not in grantable
        assert str(world.cam) in grantable


async def test_link_evidence_requires_source_observation_and_manual_actor(owner_sessionmaker, app_user_sessionmaker):
    world = await _seed_repair(owner_sessionmaker)
    observed = "2026-10-15T00:00:00+00:00"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant)
        for evidence in ({}, {"source": "manual"}, {"source": None, "observed_at": observed}):
            with pytest.raises(IntegrityError) as failure:
                async with db.begin_nested():
                    db.add(OrgMemberUserLink(
                        tenant_id=world.tenant, org_member_id=world.member, user_id=world.user,
                        provider_id=world.provider, subject_kind="provider_open_id",
                        match_subject=f"bad-{uuid.uuid4().hex[:8]}", status="revoked",
                        evidence_json=evidence, created_by_user_id=world.admin,
                    ))
                    await db.flush()
            assert "ck_org_member_user_links_evidence" in str(failure.value)
        db.add(OrgMemberUserLink(
            tenant_id=world.tenant, org_member_id=world.member, user_id=world.user,
            provider_id=world.provider, subject_kind="provider_open_id",
            match_subject=f"sync-{world.tag}", status="revoked",
            evidence_json={"source": "org_sync", "observed_at": observed},
            created_by_user_id=None,
        ))
        await db.flush()
        with pytest.raises(IntegrityError) as manual_failure:
            async with db.begin_nested():
                db.add(OrgMemberUserLink(
                    tenant_id=world.tenant, org_member_id=world.member, user_id=world.user,
                    provider_id=world.provider, subject_kind="provider_open_id",
                    match_subject=f"manual-{world.tag}", status="revoked",
                    evidence_json={"source": "manual", "observed_at": observed},
                    created_by_user_id=None,
                ))
                await db.flush()
        assert "ck_org_member_user_links_evidence" in str(manual_failure.value)
        await db.commit()


async def _required_columns(db: AsyncSession, table_name: str) -> list[str]:
    rows = (await db.execute(text(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table
          AND is_nullable = 'NO' AND column_default IS NULL
        ORDER BY ordinal_position
        """
    ), {"table": table_name})).scalars().all()
    return list(rows)


async def _insert_reflected(db: AsyncSession, table_name: str, values: dict) -> None:
    required = await _required_columns(db, table_name)
    missing = [name for name in required if name not in values]
    assert missing == [], f"{table_name} requires {missing}"
    connection = await db.connection()

    def reflect(sync_connection):
        return Table(table_name, MetaData(), autoload_with=sync_connection)

    table = await connection.run_sync(reflect)
    await db.execute(table.insert().values(**{key: value for key, value in values.items() if key in table.c}))


async def test_populated_legacy_rows_survive_upgrade_and_downgrade_is_not_a_restore():
    """Old rows keep their original columns. Downgrade drops new link rows."""

    from pathlib import Path

    from testcontainers.postgres import PostgresContainer
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    if not Path("/var/run/docker.sock").exists():
        desktop = Path.home() / ".docker" / "run" / "docker.sock"
        if desktop.exists():
            import os
            os.environ.setdefault("DOCKER_HOST", f"unix://{desktop}")
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    engine = None
    try:
        url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        # An empty database bootstraps current metadata and stamps head. Downgrade
        # to the parent, then upgrade again, so the populated rows cross the migration.
        _alembic_upgrade(url, "head")
        _alembic_downgrade(url, PARENT)
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        tenant_id, user_id, admin_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        agent_id, session_id, task_id, manifest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        member_id, department_id = uuid.uuid4(), uuid.uuid4()
        tag = uuid.uuid4().hex[:8]
        open_id, feishu_department = f"ou-legacy-{tag}", f"od-legacy-{tag}"
        async with factory() as db:
            db.add(Tenant(id=tenant_id, name=f"Legacy {tag}", slug=f"legacy-{tag}"))
            await db.flush()
            _user(
                db, id=user_id, username=f"legacy-{tag}", email=f"legacy-{tag}@example.test",
                tenant_id=tenant_id, role="member",
            )
            _user(
                db, id=admin_id, username=f"legacy-admin-{tag}", email=f"legacy-admin-{tag}@example.test",
                tenant_id=tenant_id, role="org_admin",
            )
            await db.flush()
            from tests.migrations.conftest import insert_agent_at_schema_revision

            await insert_agent_at_schema_revision(
                db, agent_id=agent_id, tenant_id=tenant_id, creator_id=user_id, name=f"Legacy {tag}",
            )
            await _insert_reflected(db, "chat_sessions", {
                "id": session_id, "agent_id": agent_id, "tenant_id": tenant_id, "user_id": user_id,
                "title": "Legacy session", "source_channel": "web", "session_kind": "human_chat",
                "actor_type": "user", "runtime_source": "web_chat", "visibility_scope": "direct_user",
                "listed_surface": "chat",
            })
            await _insert_reflected(db, "tasks", {
                "id": task_id, "agent_id": agent_id, "tenant_id": tenant_id, "title": "Legacy task",
                "type": "todo", "status": "pending", "priority": "medium", "assignee": "self", "created_by": user_id,
                "request_id": f"legacy-{tag}", "request_hash": SHA, "authority_state": "owned",
                "execution_attempt": 0,
            })
            await _insert_reflected(db, "workspace_resource_manifests", {
                "id": manifest_id, "tenant_id": tenant_id, "agent_id": agent_id,
                "path": f"workspace/{tag}.md", "authority_state": "owned", "source": "workspace_api",
            })
            await _insert_reflected(db, "org_departments", {
                "id": department_id, "tenant_id": tenant_id, "name": "Legacy", "feishu_id": feishu_department,
                "path": "", "member_count": 0,
            })
            await db.flush()
            # The downgraded table has no observation column. Insert the historical
            # row through the reflected schema instead of the current ORM metadata.
            await _insert_reflected(db, "org_members", {
                "id": member_id,
                "tenant_id": tenant_id,
                "name": "Legacy Person",
                "feishu_open_id": open_id,
                "title": "",
                "department_path": "",
                "status": "active",
            })
            await db.commit()

        _alembic_upgrade(url, "head")
        async with factory() as db:
            agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
            session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one()
            assert agent.creator_id == user_id
            assert agent.owner_user_id is None
            assert agent.home_business_team_id is None
            assert session.user_id == user_id
            assert session.audience_kind is None and session.audience_ref is None
            assert session.group_installation_ref is None and session.group_subject_id is None
            task = (await db.execute(text(
                "SELECT responsible_user_id IS NULL, business_team_id IS NULL, audience_kind IS NULL, created_by::text FROM tasks WHERE id = :id"
            ), {"id": task_id})).one()
            assert task == (True, True, True, str(user_id))
            manifest = (await db.execute(text(
                "SELECT owner_user_id IS NULL, audience_kind IS NULL, audience_ref IS NULL FROM workspace_resource_manifests WHERE id = :id"
            ), {"id": manifest_id})).one()
            assert manifest == (True, True, True)
            member = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
            with pytest.raises(HTTPException) as denied:
                await authorize_loaded_session_access(
                    db, member, agent=agent, session=session, access_level="use", action="read", audit=False,
                )
            assert denied.value.status_code == 403
            session.audience_kind = "group"
            await db.flush()
            with pytest.raises(HTTPException):
                await authorize_loaded_session_access(
                    db, member, agent=agent, session=session, access_level="use", action="read", audit=False,
                )
            admin = SimpleNamespace(id=admin_id, role="org_admin", tenant_id=tenant_id)
            assert await authorize_loaded_session_access(
                db, admin, agent=agent, session=session, access_level="use", action="read", audit=False,
            ) == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
            link_id = uuid.uuid4()
            db.add(OrgMemberUserLink(
                id=link_id, tenant_id=tenant_id, org_member_id=member_id, user_id=user_id,
                subject_kind=None, match_subject=open_id, status="revoked",
                evidence_json={"source": "manual", "observed_at": "2026-10-15T00:00:00+00:00"},
                created_by_user_id=admin_id,
            ))
            await db.commit()
            db.add(OrgMemberUserLink(
                tenant_id=tenant_id, org_member_id=member_id, user_id=user_id,
                subject_kind=None, match_subject=f"{open_id}-sync", status="revoked",
                evidence_json={"source": "org_sync", "observed_at": "2026-10-15T00:00:00+00:00"},
                created_by_user_id=None,
            ))
            await db.flush()
            for evidence in ({}, {"source": "manual"}, {"source": None, "observed_at": "2026-10-15T00:00:00Z"}):
                with pytest.raises(IntegrityError) as evidence_failure:
                    async with db.begin_nested():
                        db.add(OrgMemberUserLink(
                            tenant_id=tenant_id, org_member_id=member_id, user_id=user_id,
                            subject_kind=None, match_subject=f"bad-{uuid.uuid4().hex[:8]}", status="revoked",
                            evidence_json=evidence, created_by_user_id=admin_id,
                        ))
                        await db.flush()
                assert "ck_org_member_user_links_evidence" in str(evidence_failure.value)
            found = (await db.execute(text(
                """
                SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE contype = 'f'
                  AND conrelid::regclass::text IN ('agents', 'tasks', 'chat_sessions')
                  AND (
                    pg_get_constraintdef(oid) ILIKE '%business_team%'
                    OR pg_get_constraintdef(oid) ILIKE '%responsible_user%'
                    OR pg_get_constraintdef(oid) ILIKE '%audience_ref%'
                  )
                ORDER BY 1, 2
                """
            ))).all()
            assert [row.conname for row in found] == [
                "fk_agents_home_business_team_id",
                "fk_chat_sessions_audience_ref",
                "fk_tasks_business_team_id",
                "fk_tasks_responsible_user_id",
            ], found
        await engine.dispose()
        engine = None
        _alembic_downgrade(url, PARENT)
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            assert (await db.execute(text("SELECT to_regclass('public.org_member_user_links')"))).scalar_one() is None
            survived = (await db.execute(text(
                """
                SELECT a.creator_id::text, a.owner_user_id IS NULL, s.user_id::text, s.title,
                       m.feishu_open_id, d.feishu_id
                FROM agents a
                JOIN chat_sessions s ON s.agent_id = a.id
                JOIN org_members m ON m.tenant_id = a.tenant_id
                JOIN org_departments d ON d.tenant_id = a.tenant_id
                WHERE a.id = :agent
                """
            ), {"agent": agent_id})).one()
            assert survived == (str(user_id), True, str(user_id), "Legacy session", open_id, feishu_department)
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(text(
                        "INSERT INTO org_members (id, name, tenant_id, feishu_open_id) VALUES (:id, 'again', :tenant, :open)"
                    ), {"id": uuid.uuid4(), "tenant": tenant_id, "open": open_id})
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(text(
                        "INSERT INTO org_departments (id, name, tenant_id, feishu_id) VALUES (:id, 'again', :tenant, :feishu)"
                    ), {"id": uuid.uuid4(), "tenant": tenant_id, "feishu": feishu_department})
    finally:
        if engine is not None:
            await engine.dispose()
        container.stop()
