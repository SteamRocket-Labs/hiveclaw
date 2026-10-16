"""Mounted administrator backfill, department mapping, cutover, and rollback.

The provider transport is synthetic. Routes, transactions, stored links,
department reads, and the department permission consumer are real.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.enterprise import router as enterprise_router
from app.api.knowledge_company import router as knowledge_router
from app.api.organization import router as organization_router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import Department, User
from app.services.identity_links import lock_directory_sync
from app.services.org_sync_service import OrgSyncService

SECRET = "synthetic-m1d-app-secret"


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
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(enterprise_router, prefix="/api")
    app.include_router(organization_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = db_override
    return app


async def _rows(sessionmaker, sql: str, **params) -> list[tuple]:
    async with sessionmaker() as db:
        return [tuple(row) for row in (await db.execute(text(sql), params)).all()]


def _row(body: dict, object_id: str, reason: str | None = None) -> dict:
    found = [
        row for row in body["rows"]
        if str(row.get("object_id") or row.get("org_member_id") or "") == object_id
        and (reason is None or row.get("reason") == reason)
    ]
    assert found, body
    return found[0]


def _flat_departments(nodes: list[dict]) -> list[dict]:
    found = []
    for node in nodes:
        found.append(node)
        found.extend(_flat_departments(node.get("children") or []))
    return found


async def test_backfill_uses_current_stored_facts_only(owner_sessionmaker, app_user_sessionmaker):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    admin_id = uuid.uuid4()
    trusted_id = uuid.uuid4()
    foreign_name = f"secret-foreign-company-{tag}"
    foreign_member_name = f"secret-foreign-person-{tag}"
    trusted_open, trusted_user, trusted_union = f"ou-d-{tag}", f"u-d-{tag}", f"on-d-{tag}"

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1D A {tag}", slug=f"m1d-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=foreign_name, slug=f"m1d-b-{tag}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant_a, is_active=True, config={"source": "test"},
        )
        other = IdentityProvider(
            provider_type="oidc", name="Other", tenant_id=tenant_a, is_active=True, config={"source": "test"},
        )
        db.add_all([provider, other])
        await db.flush()
        _user(
            db, id=admin_id, username=f"admin-{tag}", email=f"admin-{tag}@example.test",
            tenant_id=tenant_a, role="org_admin",
        )
        _user(
            db, id=trusted_id, username=f"trusted-{tag}", email=f"trusted-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=trusted_open, feishu_user_id=trusted_user, feishu_union_id=trusted_union,
        )
        email_user = _user(
            db, username=f"email-{tag}", email=f"email-{tag}@example.test", tenant_id=tenant_a,
        )
        inactive = _user(
            db, username=f"inactive-{tag}", email=f"inactive-{tag}@example.test", tenant_id=tenant_a,
            feishu_open_id=f"ou-inactive-{tag}", is_active=False,
        )
        drift = _user(
            db, username=f"drift-{tag}", email=f"drift-{tag}@example.test", tenant_id=tenant_a,
            feishu_open_id=f"ou-drift-{tag}",
        )
        left = _user(
            db, username=f"left-{tag}", email=f"left-{tag}@example.test", tenant_id=tenant_a,
            feishu_open_id=f"ou-amb-a-{tag}",
        )
        right = _user(
            db, username=f"right-{tag}", email=f"right-{tag}@example.test", tenant_id=tenant_a,
            feishu_user_id=f"u-amb-b-{tag}",
        )
        revoked_user = _user(
            db, username=f"revoked-{tag}", email=f"revoked-{tag}@example.test", tenant_id=tenant_a,
            feishu_open_id=f"ou-revoked-{tag}",
        )
        wrong_user = _user(
            db, username=f"wrong-{tag}", email=f"wrong-{tag}@example.test", tenant_id=tenant_a,
            feishu_open_id=f"ou-wrong-{tag}",
        )
        foreign_user = _user(
            db, username=f"foreign-{tag}", email=f"foreign-{tag}@example.test", tenant_id=tenant_b,
            display_name=foreign_member_name, feishu_open_id=f"ou-foreign-{tag}",
        )
        dept = OrgDepartment(tenant_id=tenant_a, name=f"Directory {tag}", feishu_id=f"od-d-{tag}")
        foreign_dept = OrgDepartment(tenant_id=tenant_b, name=foreign_name, feishu_id=f"od-foreign-{tag}")
        db.add_all([dept, foreign_dept])
        await db.flush()
        members = {
            "trusted": OrgMember(
                tenant_id=tenant_a, name=f"Trusted {tag}", feishu_open_id=trusted_open,
                feishu_user_id=trusted_user, unionid=trusted_union, department_id=dept.id, status="active",
                provider_id=provider.id,
            ),
            "email": OrgMember(
                tenant_id=tenant_a, name=f"Email {tag}", email=email_user.email, department_id=dept.id,
            ),
            "inactive": OrgMember(
                tenant_id=tenant_a, name=f"Inactive {tag}", feishu_open_id=inactive.feishu_open_id, department_id=dept.id,
            ),
            "drift": OrgMember(
                tenant_id=tenant_a, name=f"Drift {tag}", feishu_open_id=drift.feishu_open_id, department_id=dept.id,
            ),
            "ambiguous": OrgMember(
                tenant_id=tenant_a, name=f"Ambiguous {tag}", feishu_open_id=left.feishu_open_id,
                feishu_user_id=right.feishu_user_id, department_id=dept.id,
            ),
            "revoked": OrgMember(
                tenant_id=tenant_a, name=f"Revoked {tag}", feishu_open_id=revoked_user.feishu_open_id, department_id=dept.id,
            ),
            "wrong": OrgMember(
                tenant_id=tenant_a, name=f"Wrong {tag}", feishu_open_id=wrong_user.feishu_open_id,
                provider_id=other.id, department_id=dept.id,
            ),
            "foreign": OrgMember(
                tenant_id=tenant_b, name=foreign_member_name, feishu_open_id=foreign_user.feishu_open_id,
            ),
        }
        db.add_all(members.values())
        await db.flush()
        db.add(OrgMemberUserLink(
            tenant_id=tenant_a, org_member_id=members["revoked"].id, user_id=revoked_user.id, provider_id=provider.id,
            subject_kind="provider_open_id", match_subject=revoked_user.feishu_open_id, status="revoked",
            evidence_json={"source": "manual", "observed_at": "2026-10-16T00:00:00+00:00"},
            created_by_user_id=admin_id,
        ))
        await db.commit()
        stored = {key: member.id for key, member in members.items()}

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_a, role="org_admin")
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1d") as client:
        preview = await client.post("/api/enterprise/org/identity-links/backfill/preview", params={"tenant_id": str(tenant_a)})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["applied"] is False and body["schema"] == "hive.identity_backfill_preview.v1"
        assert foreign_name not in preview.text and foreign_member_name not in preview.text
        selectable = {row["org_member_id"]: row for row in body["rows"]}
        assert selectable[str(stored["trusted"])]["selectable"] is True
        assert selectable[str(stored["email"])]["selectable"] is False
        assert selectable[str(stored["email"])]["reason"] == "email_only_candidate"
        assert selectable[str(stored["inactive"])]["reason"] == "authenticated_user_inactive"
        assert selectable[str(stored["ambiguous"])]["reason"] == "ambiguous_identity"
        assert selectable[str(stored["wrong"])]["reason"] == "provider_unproven"
        assert str(stored["foreign"]) not in selectable

        rejected = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant_a)},
            json={"org_member_ids": [str(stored["trusted"])], "trusted": True},
        )
        assert rejected.status_code == 422, rejected.text

        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant_a)})
        assert audiences.status_code == 200, audiences.text
        unlinked = {item["org_member_id"] for item in audiences.json()["visible_unlinked"]}
        assert str(stored["trusted"]) not in unlinked
        assert str(stored["email"]) in unlinked

        applied = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant_a)},
            json={"org_member_ids": [str(value) for value in stored.values()] + [str(uuid.uuid4())]},
        )
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert foreign_name not in applied.text and foreign_member_name not in applied.text
        assert result["counts"]["department_mappings"]["created"] == 0
        assert result["counts"]["user_assignments"]["changed"] == 0
        trusted_row = _row(result, str(stored["trusted"]))
        assert trusted_row["outcome"] == "applied"
        assert {item["subject_kind"] for item in trusted_row["links"]} == {
            "provider_open_id", "provider_user_id", "provider_union_id",
        }
        assert _row(result, str(stored["email"]))["reason"] == "email_only_candidate"
        assert _row(result, str(stored["inactive"]))["reason"] == "authenticated_user_inactive"
        assert _row(result, str(stored["ambiguous"]))["reason"] == "ambiguous_identity"
        assert _row(result, str(stored["wrong"]))["reason"] == "provider_unproven"
        assert _row(result, str(stored["revoked"]))["outcome"] == "conflict"
        assert _row(result, str(stored["foreign"]))["outcome"] == "unavailable"
        assert _row(result, str(stored["drift"]))["outcome"] == "applied"

        again = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant_a)},
            json={"org_member_ids": [str(stored["trusted"])]},
        )
        assert again.status_code == 200, again.text
        assert _row(again.json(), str(stored["trusted"]))["outcome"] == "noop"

        async with owner_sessionmaker() as db:
            drifting = await db.get(User, drift.id)
            drifting.is_active = False
            await db.commit()
        stale = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant_a)},
            json={"org_member_ids": [str(stored["drift"])]},
        )
        assert stale.status_code == 200, stale.text
        assert _row(stale.json(), str(stored["drift"]))["reason"] == "authenticated_user_inactive"

    links = await _rows(
        owner_sessionmaker,
        """
        SELECT subject_kind, status, evidence_json->>'source', created_by_user_id::text
        FROM org_member_user_links
        WHERE org_member_id = :member
        ORDER BY subject_kind
        """,
        member=stored["trusted"],
    )
    assert links == [
        ("provider_open_id", "active", "dry_run", str(admin_id)),
        ("provider_union_id", "active", "dry_run", str(admin_id)),
        ("provider_user_id", "active", "dry_run", str(admin_id)),
    ]
    revoked = await _rows(
        owner_sessionmaker,
        "SELECT status FROM org_member_user_links WHERE org_member_id = :member",
        member=stored["revoked"],
    )
    assert revoked == [("revoked",)]
    drift_links = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM org_member_user_links WHERE org_member_id = :member AND status = 'active'",
        member=stored["drift"],
    )
    assert drift_links == [(1,)]
    journals = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant AND object_kind = 'org_member_user_link'",
        tenant=tenant_a,
    )
    assert journals == [(4,)]


async def test_department_mapping_is_explicit_and_feeds_conflict_reports(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    admin_id, user_id = uuid.uuid4(), uuid.uuid4()
    foreign_name = f"secret-dept-company-{tag}"
    foreign_feishu = f"od-foreign-dept-{tag}"

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1D Dept A {tag}", slug=f"m1d-dept-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=foreign_name, slug=f"m1d-dept-b-{tag}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant_a, is_active=True, config={"source": "test"},
        )
        db.add(provider)
        await db.flush()
        same = Department(tenant_id=tenant_a, name=f"Finance {tag}")
        root = Department(tenant_id=tenant_a, name=f"Root internal {tag}")
        wrong_child = Department(tenant_id=tenant_a, name=f"Wrong child {tag}")
        good_child = Department(tenant_id=tenant_a, name=f"Good child {tag}")
        db.add_all([same, root, wrong_child, good_child])
        await db.flush()
        _user(
            db, id=admin_id, username=f"dept-admin-{tag}", email=f"dept-admin-{tag}@example.test",
            tenant_id=tenant_a, role="org_admin",
        )
        _user(
            db, id=user_id, username=f"mapped-{tag}", email=f"mapped-{tag}@example.test",
            tenant_id=tenant_a, feishu_open_id=f"ou-mapped-{tag}", department_id=root.id,
        )
        same_org = OrgDepartment(tenant_id=tenant_a, name=same.name, feishu_id=f"od-same-{tag}")
        root_org = OrgDepartment(tenant_id=tenant_a, name=f"Root directory {tag}", feishu_id=f"od-root-{tag}")
        blank = OrgDepartment(tenant_id=tenant_a, name=f"Blank {tag}", feishu_id=None)
        foreign = OrgDepartment(tenant_id=tenant_b, name=foreign_name, feishu_id=foreign_feishu)
        db.add_all([same_org, root_org, blank, foreign])
        await db.flush()
        child_org = OrgDepartment(
            tenant_id=tenant_a, name=f"Child directory {tag}", feishu_id=f"od-child-{tag}", parent_id=root_org.id,
        )
        cycle_org = OrgDepartment(tenant_id=tenant_a, name=f"Cycle directory {tag}", feishu_id=f"od-cycle-{tag}")
        db.add_all([child_org, cycle_org])
        member = OrgMember(
            tenant_id=tenant_a, name=f"Mapped {tag}", feishu_open_id=f"ou-mapped-{tag}", department_id=same_org.id,
        )
        db.add(member)
        db.add(TenantSetting(
            tenant_id=tenant_a, key="feishu_org_sync", value={"app_id": f"cli_{tag}", "app_secret": SECRET},
        ))
        await db.commit()
        ids = {
            "same": same.id, "root": root.id, "wrong": wrong_child.id, "good": good_child.id,
            "same_org": same_org.id, "root_org": root_org.id, "child_org": child_org.id, "blank": blank.id,
            "foreign": foreign.id, "cycle_org": cycle_org.id, "member": member.id,
        }

    async with owner_sessionmaker() as db:
        child = await db.get(Department, ids["good"])
        root_row = await db.get(Department, ids["root"])
        child.parent_id = root_row.id
        await db.commit()

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant_a, role="org_admin")

    async def fetch(self, app_id, app_secret):
        assert app_secret == SECRET
        return {
            "complete": True,
            "departments": [{"open_department_id": foreign_feishu, "name": "Incoming", "parent_department_id": "0"}],
            "members": [],
        }

    monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", fetch)
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1d") as client:
        listed = await client.get("/api/enterprise/org/department-links", params={"tenant_id": str(tenant_a)})
        assert listed.status_code == 200, listed.text
        catalog = listed.json()
        assert catalog["applied"] is False and foreign_name not in listed.text
        by_org = {row["org_department_id"]: row for row in catalog["org_departments"]}
        assert by_org[str(ids["same_org"])]["proposal"]["selectable"] is False
        assert by_org[str(ids["same_org"])]["proposal"]["reason"] == "name_collision"
        assert by_org[str(ids["root_org"])]["proposal"]["selectable"] is True
        assert by_org[str(ids["child_org"])]["proposal"]["reason"] == "missing_parent"

        blocked = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant_a)},
            json={"selections": [{"org_department_id": str(ids["same_org"]), "action": "create"}]},
        )
        assert blocked.status_code == 200, blocked.text
        assert _row(blocked.json(), str(ids["same_org"]))["reason"] == "name_collision"
        assert blocked.json()["counts"]["department_mappings"]["created"] == 0

        linked = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant_a)},
            json={"selections": [{
                "org_department_id": str(ids["same_org"]),
                "action": "link",
                "department_id": str(ids["same"]),
            }]},
        )
        assert linked.status_code == 200, linked.text
        linked_row = _row(linked.json(), str(ids["same_org"]))
        assert linked_row["outcome"] == "mapped"
        assert linked_row["provider_department_id"] == f"od-same-{tag}"

        created = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant_a)},
            json={"selections": [{"org_department_id": str(ids["root_org"]), "action": "create"}]},
        )
        assert created.status_code == 200, created.text
        assert _row(created.json(), str(ids["root_org"]))["outcome"] == "created"
        created_department = uuid.UUID(_row(created.json(), str(ids["root_org"]))["department_id"])
        root_parent = await _rows(
            owner_sessionmaker,
            "SELECT parent_id::text FROM departments WHERE id = :department",
            department=created_department,
        )
        assert root_parent == [(None,)]

        async with owner_sessionmaker() as db:
            good = await db.get(Department, ids["good"])
            good.parent_id = created_department
            await db.commit()
        child_link = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant_a)},
            json={"selections": [
                {"org_department_id": str(ids["child_org"]), "action": "link", "department_id": str(ids["wrong"])},
                {"org_department_id": str(ids["child_org"]), "action": "link", "department_id": str(ids["good"])},
                {"org_department_id": str(ids["blank"]), "action": "create"},
                {"org_department_id": str(ids["foreign"]), "action": "create"},
            ]},
        )
        assert child_link.status_code == 200, child_link.text
        assert foreign_name not in child_link.text
        reasons = {row["object_id"]: row["reason"] for row in child_link.json()["rows"]}
        assert reasons[str(ids["child_org"])] in {"parent_conflict", "link"}
        assert any(row["reason"] == "parent_conflict" and row["object_id"] == str(ids["child_org"]) for row in child_link.json()["rows"])
        assert any(row["reason"] == "link" and row["outcome"] == "mapped" for row in child_link.json()["rows"])
        assert reasons[str(ids["blank"])] == "missing_provider_department"
        assert reasons[str(ids["foreign"])] == "org_department_not_in_tenant"
        loop_id = uuid.uuid4()
        async with owner_sessionmaker() as db:
            loop = Department(id=loop_id, tenant_id=tenant_a, name=f"Loop {tag}", parent_id=created_department)
            db.add(loop)
            await db.flush()
            created_row = await db.get(Department, created_department)
            created_row.parent_id = loop.id
            cycle_org = await db.get(OrgDepartment, ids["cycle_org"])
            cycle_org.parent_id = ids["root_org"]
            await db.commit()
        cycled = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant_a)},
            json={"selections": [{
                "org_department_id": str(ids["cycle_org"]),
                "action": "link",
                "department_id": str(loop_id),
            }]},
        )
        assert cycled.status_code == 200, cycled.text
        assert _row(cycled.json(), str(ids["cycle_org"]))["reason"] == "cycle"

        dry = await client.post("/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_a)})
        assert dry.status_code == 200, dry.text
        report = dry.json()
        assert any(
            row["object_id"] == foreign_feishu and row["disposition"] == "cross_tenant"
            for row in report["rows"]
        )
        conflict_rows = [
            row for row in report["rows"]
            if row["object_id"] == str(ids["member"]) and row["disposition"] == "department_conflict"
        ]
        assert [(row["reason"]) for row in conflict_rows] == ["department_id_points_elsewhere"]

    teams = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM business_teams WHERE tenant_id = :tenant",
        tenant=tenant_a,
    )
    assert teams == [(0,)]


async def test_cutover_rollback_sync_and_permission_follow_the_mapping(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    tag = uuid.uuid4().hex[:8]
    tenant = uuid.uuid4()
    admin_id = uuid.uuid4()
    open_user, home_user, other_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    resource_id = uuid.uuid4()

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name=f"M1D Cut {tag}", slug=f"m1d-cut-{tag}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant, is_active=True, config={"source": "test"},
        )
        db.add(provider)
        await db.flush()
        home = Department(tenant_id=tenant, name=f"Home {tag}")
        other = Department(tenant_id=tenant, name=f"Other {tag}")
        db.add_all([home, other])
        await db.flush()
        _user(db, id=admin_id, username=f"cut-admin-{tag}", email=f"cut-admin-{tag}@example.test", tenant_id=tenant, role="org_admin")
        _user(db, id=open_user, username=f"open-{tag}", email=f"open-{tag}@example.test", tenant_id=tenant, feishu_open_id=f"ou-open-{tag}")
        _user(
            db, id=home_user, username=f"home-{tag}", email=f"home-{tag}@example.test", tenant_id=tenant,
            feishu_open_id=f"ou-home-{tag}", department_id=home.id,
        )
        _user(
            db, id=other_user, username=f"other-{tag}", email=f"other-{tag}@example.test", tenant_id=tenant,
            feishu_open_id=f"ou-other-{tag}", feishu_user_id=f"u-other-{tag}",
        )
        first = OrgDepartment(tenant_id=tenant, name=f"First {tag}", feishu_id=f"od-first-{tag}")
        second = OrgDepartment(tenant_id=tenant, name=f"Second {tag}", feishu_id=f"od-second-{tag}")
        db.add_all([first, second])
        await db.flush()
        open_member = OrgMember(tenant_id=tenant, name=f"Open {tag}", feishu_open_id=f"ou-open-{tag}", department_id=first.id, title="Kept")
        home_member = OrgMember(tenant_id=tenant, name=f"Home {tag}", feishu_open_id=f"ou-home-{tag}", department_id=first.id)
        split_open = OrgMember(tenant_id=tenant, name=f"Split open {tag}", feishu_open_id=f"ou-other-{tag}", department_id=first.id)
        split_user = OrgMember(tenant_id=tenant, name=f"Split user {tag}", feishu_user_id=f"u-other-{tag}", department_id=second.id)
        db.add_all([open_member, home_member, split_open, split_user])
        db.add(TenantSetting(tenant_id=tenant, key="feishu_org_sync", value={"app_id": f"cli_{tag}", "app_secret": SECRET}))
        await db.commit()
        ids = {
            "home": home.id, "other": other.id, "first": first.id, "second": second.id,
            "open_member": open_member.id, "home_member": home_member.id,
            "split_open": split_open.id, "split_user": split_user.id,
        }

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant, role="org_admin")
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1d") as client:
        backfill = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant)},
            json={"org_member_ids": [str(ids["open_member"]), str(ids["home_member"]), str(ids["split_open"]), str(ids["split_user"])]},
        )
        assert backfill.status_code == 200, backfill.text
        created = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant)},
            json={"selections": [
                {"org_department_id": str(ids["first"]), "action": "create"},
                {"org_department_id": str(ids["second"]), "action": "create"},
            ]},
        )
        assert created.status_code == 200, created.text
        mapped = {row["object_id"]: uuid.UUID(row["department_id"]) for row in created.json()["rows"]}
        grant_department = mapped[str(ids["first"])]
        async with owner_sessionmaker() as db:
            db.add(ResourcePermission(
                tenant_id=tenant, principal_type="department", principal_id=grant_department,
                resource_type="agent_activity", resource_id=resource_id, actions=["read"], conditions={},
                effect="allow", sensitivity_ceiling="PL1_public", purposes=[],
            ))
            await db.commit()

        before = await client.get("/api/org/users", params={"tenant_id": str(tenant), "department_id": str(grant_department)})
        assert before.status_code == 200, before.text
        assert str(open_user) not in {row["id"] for row in before.json()}

        assigned = await client.post(
            "/api/enterprise/org/department-cutover",
            params={"tenant_id": str(tenant)},
            json={"org_member_ids": [str(ids["open_member"]), str(ids["home_member"]), str(ids["split_open"])]},
        )
        assert assigned.status_code == 200, assigned.text
        assert _row(assigned.json(), str(open_user))["outcome"] == "changed"
        assert _row(assigned.json(), str(home_user))["reason"] == "department_id_points_elsewhere"
        assert _row(assigned.json(), str(other_user))["reason"] == "multiple_directory_departments"
        assert assigned.json()["counts"]["user_assignments"]["changed"] == 1
        assert assigned.json()["counts"]["link_backfill"]["applied"] == 0

        listed = await client.get("/api/org/users", params={"tenant_id": str(tenant), "department_id": str(grant_department)})
        assert str(open_user) in {row["id"] for row in listed.json()}
        tree = await client.get("/api/org/departments", params={"tenant_id": str(tenant)})
        assert tree.status_code == 200, tree.text
        flat = _flat_departments(tree.json())
        assert next(row for row in flat if row["id"] == str(grant_department))["member_count"] == 1

        from app.core.resource_authority import load_explicit_resource_grant_ids

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            account = await db.get(User, open_user)
            granted = await load_explicit_resource_grant_ids(db, user=account, resource_kind="agent_activity", action="read")
            assert resource_id in granted
            untouched = await db.get(User, home_user)
            assert untouched.department_id == ids["home"]

        repaired = await client.post(
            "/api/enterprise/org/department-assignments/repair",
            params={"tenant_id": str(tenant)},
            json={"user_id": str(open_user), "department_id": str(ids["other"])},
        )
        assert repaired.status_code == 200, repaired.text
        assert _row(repaired.json(), str(open_user))["outcome"] == "changed"
        retried = await client.post(
            "/api/enterprise/org/department-cutover",
            params={"tenant_id": str(tenant)},
            json={"org_member_ids": [str(ids["open_member"])]},
        )
        assert _row(retried.json(), str(open_user))["reason"] == "department_id_points_elsewhere"

        async def empty_fetch(self, app_id, app_secret):
            assert app_secret == SECRET
            return {"complete": True, "departments": [], "members": []}

        monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", empty_fetch)
        monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", lambda db, tenant_id: _async_none())
        monkeypatch.setattr("app.services.workspace_sync_dirty.mark_tenant_dirty", lambda tenant_id, broadcast=True: None)
        synced = await client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant)})
        assert synced.status_code == 200, synced.text
        assert synced.json()["status"] == "complete"
        assert synced.json()["user_assignments"] == 0
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            assert (await db.get(User, open_user)).department_id == ids["other"]
            assert (await db.get(User, home_user)).department_id == ids["home"]

        journal = await client.get("/api/enterprise/org/cutover-journal", params={"tenant_id": str(tenant)})
        assert journal.status_code == 200, journal.text
        rows = journal.json()["rows"]
        user_journal = next(row for row in rows if row["object_kind"] == "user_department" and row["after"]["department_id"] == str(grant_department))
        link_journal = next(
            row for row in rows
            if row["object_kind"] == "department_directory_link" and row["after"]["department_id"] == str(grant_department)
        )
        kept = await client.post(
            "/api/enterprise/org/cutover-journal/rollback",
            params={"tenant_id": str(tenant)},
            json={"journal_ids": [user_journal["id"], link_journal["id"], str(uuid.uuid4())]},
        )
        assert kept.status_code == 200, kept.text
        by_journal = {row["journal_id"]: row for row in kept.json()["rows"]}
        assert by_journal[user_journal["id"]]["reason"] == "rollback_conflict"
        assert by_journal[user_journal["id"]]["observed"]["department_id"] == str(ids["other"])
        assert by_journal[user_journal["id"]]["expected_after"]["department_id"] == str(grant_department)
        assert by_journal[link_journal["id"]]["reason"] == "dependent_link_retained"
        assert by_journal[link_journal["id"]]["observed"]["status"] == "active"
        assert any(row["reason"] == "journal_not_in_tenant" for row in kept.json()["rows"])

        async with owner_sessionmaker() as db:
            account = await db.get(User, open_user)
            account.department_id = grant_department
            await db.commit()
        restored = await client.post(
            "/api/enterprise/org/cutover-journal/rollback",
            params={"tenant_id": str(tenant)},
            json={"journal_ids": [user_journal["id"], link_journal["id"]]},
        )
        assert restored.status_code == 200, restored.text
        assert {row["journal_id"]: row["outcome"] for row in restored.json()["rows"]}[user_journal["id"]] == "restored"
        restored_count = await _rows(
            owner_sessionmaker,
            "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant",
            tenant=tenant,
        )
        repeated = await client.post(
            "/api/enterprise/org/cutover-journal/rollback",
            params={"tenant_id": str(tenant)},
            json={"journal_ids": [user_journal["id"], link_journal["id"]]},
        )
        assert repeated.status_code == 200, repeated.text
        assert all(row["outcome"] in {"conflict", "noop"} for row in repeated.json()["rows"])
        after_repeat = await _rows(
            owner_sessionmaker,
            "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant",
            tenant=tenant,
        )
        assert restored_count == after_repeat
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            assert (await db.get(User, open_user)).department_id is None
        link_status = await _rows(
            owner_sessionmaker,
            "SELECT status FROM department_directory_links WHERE department_id = :department",
            department=grant_department,
        )
        assert link_status == [("revoked",)]
        still_there = await _rows(
            owner_sessionmaker,
            "SELECT count(*) FROM departments WHERE id = :department",
            department=grant_department,
        )
        assert still_there == [(1,)]
        synced_again = await client.post("/api/enterprise/org/sync", params={"tenant_id": str(tenant)})
        assert synced_again.status_code == 200, synced_again.text
        assert synced_again.json()["status"] == "complete"
        # The revoked first mapping stays revoked. The still-active second mapping
        # assigns the user whose department is empty.
        assert synced_again.json()["user_assignments"] == 1
        link_status = await _rows(
            owner_sessionmaker,
            "SELECT status FROM department_directory_links WHERE department_id = :department",
            department=grant_department,
        )
        assert link_status == [("revoked",)]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            assert (await db.get(User, open_user)).department_id is None
            assert (await db.get(User, home_user)).department_id == ids["home"]
            assert (await db.get(User, other_user)).department_id == mapped[str(ids["second"])]
            profile = await db.get(OrgMember, ids["open_member"])
            assert profile.title == "Kept" and profile.name == f"Open {tag}"

        link_ids = [
            row["id"] for row in rows
            if row["object_kind"] == "org_member_user_link" and row["after"]["org_member_id"] == str(ids["open_member"])
        ]
        assert link_ids
        revoked_links = await client.post(
            "/api/enterprise/org/cutover-journal/rollback",
            params={"tenant_id": str(tenant)},
            json={"journal_ids": link_ids},
        )
        assert revoked_links.status_code == 200, revoked_links.text
        assert all(row["outcome"] == "restored" for row in revoked_links.json()["rows"])
        audiences = await client.get("/api/knowledge/company/permission-audiences", params={"tenant_id": str(tenant)})
        assert audiences.status_code == 200, audiences.text
        reasons = {item["org_member_id"]: item["reason"] for item in audiences.json()["visible_unlinked"]}
        assert reasons[str(ids["open_member"])] == "revoked"


def _async_none():
    async def _inner(*_args, **_kwargs):
        return None
    return _inner()


async def test_failed_cutover_transaction_writes_nothing(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    tenant, admin_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name=f"M1D Fail {tag}", slug=f"m1d-fail-{tag}"))
        await db.flush()
        _user(db, id=admin_id, username=f"fail-admin-{tag}", email=f"fail-admin-{tag}@example.test", tenant_id=tenant, role="org_admin")
        org = OrgDepartment(tenant_id=tenant, name=f"Fail directory {tag}", feishu_id=f"od-fail-{tag}")
        db.add(org)
        await db.commit()
        org_id = org.id

    import app.services.identity_cutover as cutover

    async def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic cutover failure")

    monkeypatch.setattr(cutover, "_journal", explode)
    actor = SimpleNamespace(id=admin_id, tenant_id=tenant, role="org_admin")
    async with AsyncClient(
        transport=ASGITransport(app=_app(app_user_sessionmaker, actor), raise_app_exceptions=False),
        base_url="http://m1d",
    ) as client:
        failed = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant)},
            json={"selections": [{"org_department_id": str(org_id), "action": "create"}]},
        )
        assert failed.status_code == 500, failed.text
    created = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM departments WHERE tenant_id = :tenant AND name = :name",
        tenant=tenant, name=f"Fail directory {tag}",
    )
    links = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM department_directory_links WHERE org_department_id = :org",
        org=org_id,
    )
    journals = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id = :tenant",
        tenant=tenant,
    )
    assert created == [(0,)] and links == [(0,)] and journals == [(0,)]


async def test_cutover_revalidates_after_the_directory_lock_waits(owner_sessionmaker, app_user_sessionmaker):
    tag = uuid.uuid4().hex[:8]
    tenant, admin_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name=f"M1D Lock {tag}", slug=f"m1d-lock-{tag}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="Feishu", tenant_id=tenant, is_active=True, config={"source": "test"},
        )
        db.add(provider)
        await db.flush()
        _user(db, id=admin_id, username=f"lock-admin-{tag}", email=f"lock-admin-{tag}@example.test", tenant_id=tenant, role="org_admin")
        account = _user(
            db, id=user_id, username=f"lock-user-{tag}", email=f"lock-user-{tag}@example.test",
            tenant_id=tenant, feishu_open_id=f"ou-lock-{tag}",
        )
        org = OrgDepartment(tenant_id=tenant, name=f"Lock directory {tag}", feishu_id=f"od-lock-{tag}")
        db.add(org)
        await db.flush()
        member = OrgMember(tenant_id=tenant, name=f"Lock {tag}", feishu_open_id=account.feishu_open_id, department_id=org.id)
        db.add(member)
        await db.commit()
        member_id = member.id

    actor = SimpleNamespace(id=admin_id, tenant_id=tenant, role="org_admin")
    async with AsyncClient(transport=ASGITransport(app=_app(app_user_sessionmaker, actor)), base_url="http://m1d") as client:
        prepared = await client.post(
            "/api/enterprise/org/identity-links/backfill",
            params={"tenant_id": str(tenant)},
            json={"org_member_ids": [str(member_id)]},
        )
        assert prepared.status_code == 200, prepared.text
        mapped = await client.post(
            "/api/enterprise/org/department-links",
            params={"tenant_id": str(tenant)},
            json={"selections": [{"org_department_id": str(org.id), "action": "create"}]},
        )
        assert mapped.status_code == 200, mapped.text

    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            await lock_directory_sync(db, tenant)
            account_row = await db.get(User, user_id)
            account_row.is_active = False
            await db.flush()
            ready.set()
            await release.wait()
            await db.commit()

    async def waiting_cutover():
        await ready.wait()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            from app.services.identity_cutover import apply_user_department_cutover

            return await apply_user_department_cutover(
                db, tenant_id=tenant, actor_id=admin_id, org_member_ids=[member_id],
            )

    holder = asyncio.create_task(hold())
    await ready.wait()
    waiter = asyncio.create_task(waiting_cutover())
    saw_wait = False
    for _ in range(50):
        waiting = await _rows(
            owner_sessionmaker,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted",
        )
        if waiting[0][0] > 0:
            saw_wait = True
            break
        await asyncio.sleep(0.05)
    release.set()
    result = await waiter
    await holder
    assert saw_wait is True
    assert result["counts"]["user_assignments"]["changed"] == 0
    assert result["rows"][0]["reason"] == "inactive_user"
    stored = await _rows(
        owner_sessionmaker,
        "SELECT is_active, department_id::text FROM users WHERE id = :user",
        user=user_id,
    )
    assert stored == [(False, None)]
