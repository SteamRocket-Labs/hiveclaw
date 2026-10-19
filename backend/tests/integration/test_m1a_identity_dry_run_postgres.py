"""Mounted identity dry-run against two tenants and the non-owner RLS role."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.enterprise import identity_dry_run_fetcher, router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import Department, User
from app.services.identity_dry_run import COUNT_KEYS
from app.services.org_sync_service import org_sync_service

# This module seeds platform-global providers. Keep that independent identity
# universe out of the OAuth fixtures in other modules, using the shared fixtures.
from tests.integration import conftest as _database_fixtures

app_user_engine = _database_fixtures.app_user_engine
app_user_sessionmaker = _database_fixtures.app_user_sessionmaker
migrated_pg_url = _database_fixtures.migrated_pg_url
owner_engine = _database_fixtures.owner_engine
owner_sessionmaker = _database_fixtures.owner_sessionmaker
pg_container = _database_fixtures.pg_container

REVISION = "136332b2165f50e610ee07f509cc748cf42d653e"


def _user(db, **kwargs) -> User:
    kwargs.setdefault("password_hash", "x")
    kwargs.setdefault("role", "member")
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("display_name", kwargs["username"])
    row = User(**kwargs)
    db.add(row)
    return row


def _hidden_labels(blob: str, secret: dict[str, str]) -> list[str]:
    return [label for label, value in secret.items() if value and value in blob]


async def _fingerprint(sessionmaker) -> dict[str, list[tuple]]:
    statements = {
        "users": """
            SELECT id::text, coalesce(tenant_id::text, ''), email, coalesce(feishu_open_id, ''),
                   coalesce(feishu_user_id, ''), is_active, role::text, coalesce(department_id::text, ''),
                   display_name, md5(password_hash)
            FROM users ORDER BY id
        """,
        "departments": "SELECT id::text, tenant_id::text, name FROM departments ORDER BY id",
        "org_departments": """
            SELECT id::text, tenant_id::text, coalesce(feishu_id, ''), name FROM org_departments ORDER BY id
        """,
        "org_members": """
            SELECT id::text, tenant_id::text, coalesce(feishu_open_id, ''), coalesce(feishu_user_id, ''),
                   coalesce(email, ''), status, coalesce(department_id::text, ''), coalesce(provider_id::text, ''),
                   name
            FROM org_members ORDER BY id
        """,
        "identity_providers": """
            SELECT id::text, coalesce(tenant_id::text, ''), provider_type, is_active, md5(config::text)
            FROM identity_providers ORDER BY id
        """,
        "external_identities": """
            SELECT id::text, provider_id::text, user_id::text, coalesce(provider_open_id, ''),
                   coalesce(provider_user_id, ''), coalesce(provider_union_id, ''), md5(raw_profile::text)
            FROM external_identities ORDER BY id
        """,
        "tenant_settings": """
            SELECT tenant_id::text, key, md5(value::text) FROM tenant_settings ORDER BY 1, 2
        """,
        "runtime_tasks": """
            SELECT id::text, tenant_id::text, coalesce(root_user_id::text, ''), status, task_type,
                   config_snapshot_hash, policy_snapshot_hash
            FROM runtime_tasks ORDER BY id
        """,
        "audit_logs": "SELECT id::text, coalesce(action, '') FROM audit_logs ORDER BY id",
    }
    async with sessionmaker() as db:
        found = {}
        for name, sql in statements.items():
            found[name] = [tuple(row) for row in (await db.execute(text(sql))).all()]
        return found


async def _visible_names(sessionmaker, tenant_id: uuid.UUID) -> tuple[list[str], list[str], str]:
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        member_names = list((await db.execute(select(OrgMember.name))).scalars().all())
        emails = list((await db.execute(select(User.email))).scalars().all())
        current = (await db.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()
        return member_names, emails, current


async def test_identity_dry_run_assesses_two_tenants_without_writing(
    owner_sessionmaker,
    app_user_sessionmaker,
    app_user_engine,
    monkeypatch,
):
    suffix = uuid.uuid4().hex[:8]
    secret = {
        "app_secret": f"m1a-synthetic-app-secret-{suffix}",
        "other_secret": f"m1a-other-tenant-secret-{suffix}",
        "global_secret": f"m1a-global-provider-secret-{suffix}",
        "foreign_name": "M1A Foreign Company Name",
        "foreign_member": "M1A Foreign Person",
        "foreign_email": f"m1a-foreign-{suffix}@example.test",
        "platform_email": f"m1a-platform-{suffix}@example.test",
    }
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    tenant_c = uuid.uuid4()
    ada_id = uuid.uuid4()
    amb_open_id = uuid.uuid4()
    amb_user_id = uuid.uuid4()
    email_id = uuid.uuid4()
    inactive_id = uuid.uuid4()
    same_id = uuid.uuid4()
    same_name_id = uuid.uuid4()
    unproven_id = uuid.uuid4()
    ext_user_id = uuid.uuid4()
    ext_other_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    c_admin_id = uuid.uuid4()
    b_user_id = uuid.uuid4()
    platform_id = uuid.uuid4()
    dangling_root = uuid.uuid4()
    ids = {
        "ada_open": f"ou_ada_{suffix}",
        "amb_open": f"ou_amb_{suffix}",
        "amb_user": f"fu_amb_{suffix}",
        "inactive_open": f"ou_inactive_{suffix}",
        "unknown_open": f"ou_unknown_{suffix}",
        "uuid_user_open": f"ou_uuid_user_{suffix}",
        "uuid_member_open": f"ou_uuid_member_{suffix}",
        "name_user_open": f"ou_name_user_{suffix}",
        "name_member_open": f"ou_name_member_{suffix}",
        "unproven_open": f"ou_unproven_{suffix}",
        "ext_open": f"ou_ext_{suffix}",
        "foreign_open": f"ou_foreign_{suffix}",
        "platform_open": f"ou_platform_{suffix}",
        "email_open": f"ou_email_{suffix}",
        "eng": f"od_eng_{suffix}",
        "research": f"od_research_{suffix}",
        "foreign_dept": f"od_foreign_{suffix}",
        "blank_dept": f"od_blank_{suffix}",
        "b_user_open": f"ou_b_user_{suffix}",
    }
    calls: list[dict] = []
    behaviors: list[dict] = []

    async def fetcher(app_id, app_secret):
        calls.append({"app_id": app_id, "matched": app_secret == secret["app_secret"]})
        return behaviors.pop(0)

    async def explode(*_args, **_kwargs):
        raise AssertionError("full_sync called")

    monkeypatch.setattr(org_sync_service, "full_sync", explode)
    monkeypatch.setenv("HIVE_SOURCE_REVISION", REVISION)

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name="M1A Home", slug=f"m1a-a-{suffix}"))
        db.add(Tenant(id=tenant_b, name=secret["foreign_name"], slug=f"m1a-b-{suffix}"))
        db.add(Tenant(id=tenant_c, name="M1A Unconfigured", slug=f"m1a-c-{suffix}"))
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", name="M1A Feishu", tenant_id=tenant_a, is_active=True, config={}
        )
        global_provider = IdentityProvider(
            provider_type="feishu",
            name="M1A Global",
            tenant_id=None,
            is_active=True,
            config={"app_secret": secret["global_secret"]},
        )
        db.add(provider)
        db.add(global_provider)
        finance = Department(tenant_id=tenant_a, name="Finance")
        engineering = Department(tenant_id=tenant_a, name="Engineering")
        db.add(finance)
        db.add(engineering)
        await db.flush()
        ada = _user(
            db, id=ada_id, username=f"ada-{suffix}", email=f"ada-{suffix}@example.test",
            display_name="Ada M1A", tenant_id=tenant_a, feishu_open_id=ids["ada_open"], department_id=finance.id,
        )
        _user(
            db, id=amb_open_id, username=f"amb-open-{suffix}", email=f"amb-open-{suffix}@example.test",
            tenant_id=tenant_a, feishu_open_id=ids["amb_open"],
        )
        _user(
            db, id=amb_user_id, username=f"amb-user-{suffix}", email=f"amb-user-{suffix}@example.test",
            tenant_id=tenant_a, feishu_user_id=ids["amb_user"],
        )
        _user(
            db, id=email_id, username=f"email-{suffix}", email=f"email-only-{suffix}@example.test", tenant_id=tenant_a,
        )
        _user(
            db, id=inactive_id, username=f"inactive-{suffix}", email=f"inactive-{suffix}@example.test",
            tenant_id=tenant_a, feishu_open_id=ids["inactive_open"], is_active=False,
        )
        _user(
            db, id=same_id, username=f"same-{suffix}", email=f"same-{suffix}@example.test", tenant_id=tenant_a,
            feishu_open_id=ids["uuid_user_open"],
        )
        _user(
            db, id=same_name_id, username=f"pat-{suffix}", email=f"pat-{suffix}@example.test",
            display_name="Pat M1A", tenant_id=tenant_a, feishu_open_id=ids["name_user_open"],
        )
        _user(
            db, id=unproven_id, username=f"unproven-{suffix}", email=f"unproven-{suffix}@example.test",
            tenant_id=tenant_a, feishu_open_id=ids["unproven_open"],
        )
        _user(
            db, id=ext_user_id, username=f"ext-{suffix}", email=f"ext-{suffix}@example.test",
            tenant_id=tenant_a, feishu_open_id=ids["ext_open"],
        )
        _user(
            db, id=ext_other_id, username=f"ext-other-{suffix}", email=f"ext-other-{suffix}@example.test",
            tenant_id=tenant_a,
        )
        _user(
            db, id=admin_id, username=f"admin-{suffix}", email=f"admin-{suffix}@example.test",
            tenant_id=tenant_a, role="org_admin",
        )
        _user(
            db, id=c_admin_id, username=f"c-admin-{suffix}", email=f"c-admin-{suffix}@example.test",
            tenant_id=tenant_c, role="org_admin",
        )
        b_user = _user(
            db, id=b_user_id, username=f"foreign-{suffix}", email=secret["foreign_email"],
            display_name=secret["foreign_member"], tenant_id=tenant_b, feishu_open_id=ids["b_user_open"],
        )
        platform = _user(
            db, id=platform_id, username=f"platform-{suffix}", email=secret["platform_email"],
            tenant_id=None, role="platform_admin", feishu_open_id=ids["platform_open"],
        )
        eng = OrgDepartment(tenant_id=tenant_a, name="Engineering", feishu_id=ids["eng"])
        foreign_dept = OrgDepartment(
            tenant_id=tenant_b, name="Foreign Dept", feishu_id=ids["foreign_dept"]
        )
        db.add(eng)
        db.add(foreign_dept)
        await db.flush()
        ada_member = OrgMember(
            tenant_id=tenant_a, name="Ada M1A", feishu_open_id=ids["ada_open"], department_id=eng.id, status="active",
        )
        amb_member = OrgMember(
            tenant_id=tenant_a, name="Ambiguous", feishu_open_id=ids["amb_open"], feishu_user_id=ids["amb_user"],
        )
        email_member = OrgMember(
            tenant_id=tenant_a, name="Email Only", email=f"email-only-{suffix}@example.test",
        )
        inactive_member = OrgMember(
            tenant_id=tenant_a, name="Inactive", feishu_open_id=ids["inactive_open"],
        )
        unknown_member = OrgMember(
            tenant_id=tenant_a, name="Unknown", feishu_open_id=ids["unknown_open"],
        )
        same_member = OrgMember(
            id=same_id, tenant_id=tenant_a, name="Same UUID", feishu_open_id=ids["uuid_member_open"],
        )
        name_member = OrgMember(
            tenant_id=tenant_a, name="Pat M1A", feishu_open_id=ids["name_member_open"],
        )
        unproven_member = OrgMember(
            tenant_id=tenant_a, name="Unproven", feishu_open_id=ids["unproven_open"], provider_id=global_provider.id,
        )
        ext_member = OrgMember(
            tenant_id=tenant_a, name="External", feishu_open_id=ids["ext_open"],
        )
        local_member = OrgMember(
            tenant_id=tenant_a, name="Local Placeholder", email=f"local-{suffix}@feishu.local",
        )
        foreign_member = OrgMember(
            tenant_id=tenant_b, name=secret["foreign_member"], feishu_open_id=ids["foreign_open"],
        )
        db.add_all([
            ada_member, amb_member, email_member, inactive_member, unknown_member, same_member,
            name_member, unproven_member, ext_member, local_member, foreign_member,
        ])
        ada_identity = ExternalIdentity(
            provider_id=provider.id, user_id=ada.id, provider_open_id=ids["ada_open"], raw_profile={},
        )
        ext_identity = ExternalIdentity(
            provider_id=provider.id, user_id=ext_other_id, provider_open_id=ids["ext_open"], raw_profile={},
        )
        db.add(ada_identity)
        db.add(ext_identity)
        db.add(TenantSetting(
            tenant_id=tenant_a, key="feishu_org_sync",
            value={"app_id": f"cli_{suffix}", "app_secret": secret["app_secret"]},
        ))
        db.add(TenantSetting(
            tenant_id=tenant_b, key="feishu_org_sync",
            value={"app_id": f"cli_b_{suffix}", "app_secret": secret["other_secret"]},
        ))
        dangling = RuntimeTask(
            tenant_id=tenant_a, root_user_id=dangling_root, task_type="delegation", status="pending",
            metadata_json={"source": "m1a"},
        )
        control = RuntimeTask(
            tenant_id=tenant_a, root_user_id=ada.id, task_type="delegation", status="pending",
            metadata_json={"source": "m1a-control"},
        )
        db.add(dangling)
        db.add(control)
        await db.commit()
        stored = {
            "ada_member": ada_member.id,
            "amb_member": amb_member.id,
            "email_member": email_member.id,
            "inactive_member": inactive_member.id,
            "unknown_member": unknown_member.id,
            "same_member": same_member.id,
            "name_member": name_member.id,
            "unproven_member": unproven_member.id,
            "ext_member": ext_member.id,
            "local_member": local_member.id,
            "eng": eng.id,
            "ada_identity": ada_identity.id,
            "dangling": dangling.id,
            "control": control.id,
            "provider": provider.id,
        }
        secret["tenant_b"] = str(tenant_b)
        secret["b_user"] = str(b_user.id)
        secret["platform_user"] = str(platform.id)
        secret["foreign_member_id"] = str(foreign_member.id)

    before = await _fingerprint(owner_sessionmaker)
    member_names, emails, current = await _visible_names(app_user_sessionmaker, tenant_a)
    assert "Ada M1A" in member_names
    assert secret["foreign_member"] not in member_names
    assert secret["foreign_email"] not in emails
    assert current == str(tenant_a)

    snapshot = {
        "complete": True,
        "departments": [
            {"open_department_id": ids["eng"], "name": "Engineering", "parent_department_id": ""},
            {"open_department_id": ids["research"], "name": "Research", "parent_department_id": ""},
            {"open_department_id": ids["foreign_dept"], "name": "Foreign Dept", "parent_department_id": ""},
            {"open_department_id": ids["blank_dept"], "name": "", "parent_department_id": ""},
        ],
        "members": [
            {"open_id": ids["foreign_open"], "user_id": "", "union_id": "", "email": "", "department_id": ""},
            {"open_id": ids["platform_open"], "user_id": "", "union_id": "", "email": "", "department_id": ""},
            {
                "open_id": ids["email_open"], "user_id": "", "union_id": "",
                "email": secret["foreign_email"], "department_id": "",
            },
            {"open_id": "", "user_id": "", "union_id": "", "email": "", "department_id": ""},
        ],
    }
    behaviors.extend([
        {"complete": False, "departments": [], "members": []},
        snapshot,
    ])
    actor = SimpleNamespace(id=admin_id, role="org_admin", tenant_id=tenant_a)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_override():
        async with app_user_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[identity_dry_run_fetcher] = lambda: fetcher
    captured: list[str] = []
    loguru_messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())  # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    from loguru import logger as loguru_logger

    log_handle = loguru_logger.add(lambda message: loguru_messages.append(str(message)))
    bodies: list[str] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            failed = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_a)}
            )
            bodies.append(failed.text)
            assert failed.status_code == 503
            assert failed.json()["detail"]["code"] == "incomplete"
            assert failed.json()["detail"]["applied"] is False
            assert failed.json()["detail"]["retryable"] is True
            assert "schema" not in failed.json()
            assert await _fingerprint(owner_sessionmaker) == before

            ready = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_a)}
            )
            bodies.append(ready.text)
            assert ready.status_code == 200
            body = ready.json()
            assert body["schema"] == "hive.identity_dry_run.v1"
            assert body["tenant_id"] == str(tenant_a)
            assert body["source_revision"] == REVISION
            assert body["applied"] is False
            assert body["inputs"] == {"stored_rows": True, "incoming_provider_subjects": True}
            found = {(row["object_id"], row["disposition"], row["reason"]): row for row in body["rows"]}
            expected = {
                (str(stored["ada_member"]), "trusted", "provider_open_id"),
                (str(stored["amb_member"]), "conflict", "ambiguous_identity"),
                (str(stored["email_member"]), "email_only_candidate", "email_only_candidate"),
                (str(stored["inactive_member"]), "inactive_user", "authenticated_user_inactive"),
                (str(stored["unknown_member"]), "unregistered", "unregistered"),
                (str(stored["same_member"]), "unregistered", "unregistered"),
                (str(stored["name_member"]), "unregistered", "unregistered"),
                (str(stored["unproven_member"]), "conflict", "provider_unproven"),
                (str(stored["ext_member"]), "conflict", "external_identity_conflict"),
                (str(stored["local_member"]), "conflict", "missing_provider_subject"),
                (str(stored["eng"]), "name_collision", "name_collision"),
                (ids["foreign_dept"], "cross_tenant", "cross_tenant_subject"),
                (ids["blank_dept"], "conflict", "missing_department_name"),
                (ids["foreign_open"], "cross_tenant", "cross_tenant_subject"),
                (ids["platform_open"], "cross_tenant", "cross_tenant_subject"),
                (ids["email_open"], "cross_tenant", "cross_tenant_subject"),
                ("incoming-member-3", "conflict", "missing_provider_subject"),
                (str(stored["dangling"]), "conflict", "dangling_root_user_id"),
            }
            assert set(found) == expected
            assert not any(row["disposition"] == "department_conflict" for row in body["rows"])
            assert body["counts"] == {
                key: sum(1 for row in body["rows"] if row["disposition"] == key) for key in COUNT_KEYS
            }
            assert sum(body["counts"].values()) == len(body["rows"])
            trusted = found[(str(stored["ada_member"]), "trusted", "provider_open_id")]
            assert trusted["user_id"] == str(ada_id)
            assert trusted["global_unique_blocked"] is False
            assert trusted["provenance"]["match_subject"] == ids["ada_open"]
            assert trusted["provenance"]["external_identity_id"] == str(stored["ada_identity"])
            assert trusted["provenance"]["source"] == "dry_run"
            assert trusted["provenance"]["applied"] is False
            ambiguous = found[(str(stored["amb_member"]), "conflict", "ambiguous_identity")]
            assert ambiguous["user_id"] is None
            assert ambiguous["provenance"]["candidate_user_ids"] == sorted([str(amb_open_id), str(amb_user_id)])
            same = found[(str(stored["same_member"]), "unregistered", "unregistered")]
            assert same["object_id"] == str(same_id)
            assert same["user_id"] is None
            assert same["provenance"]["candidate_user_ids"] == []
            blocked = found[(ids["foreign_open"], "cross_tenant", "cross_tenant_subject")]
            assert blocked["user_id"] is None
            assert blocked["global_unique_blocked"] is True
            assert blocked["provenance"]["candidate_user_ids"] == []
            assert found[(ids["email_open"], "cross_tenant", "cross_tenant_subject")]["global_unique_blocked"] is True
            assert found[(str(stored["ext_member"]), "conflict", "external_identity_conflict")]["user_id"] is None
            assert found[(str(stored["ext_member"]), "conflict", "external_identity_conflict")]["global_unique_blocked"] is False
            unproven = found[(str(stored["unproven_member"]), "conflict", "provider_unproven")]
            assert unproven["user_id"] is None
            assert unproven["provenance"]["provider_id"] is None
            assert unproven["provenance"]["candidate_user_ids"] == [str(unproven_id)]
            dangling_row = found[(str(stored["dangling"]), "conflict", "dangling_root_user_id")]
            assert dangling_row["provenance"]["claimed_root_user_id"] == str(dangling_root)
            assert str(stored["control"]) not in {row["object_id"] for row in body["rows"]}
            assert body["department_proposals"] == [{
                "object_kind": "org_department",
                "object_id": ids["research"],
                "provider_department_id": ids["research"],
                "name": "Research",
                "parent_provider_department_id": None,
                "action": "create",
                "applied": False,
            }]
            assert calls == [{"app_id": f"cli_{suffix}", "matched": True}, {"app_id": f"cli_{suffix}", "matched": True}]
            assert await _fingerprint(owner_sessionmaker) == before

            actor.role = "member"
            member_denied = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_a)}
            )
            bodies.append(member_denied.text)
            assert member_denied.status_code == 403
            assert member_denied.json()["detail"] == "Admin access required"

            actor.role = "org_admin"
            actor.tenant_id = tenant_a
            cross = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_b)}
            )
            bodies.append(cross.text)
            assert cross.status_code == 403
            assert cross.json()["detail"] == "Access denied"

            actor.role = "platform_admin"
            platform_denied = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_b)}
            )
            bodies.append(platform_denied.text)
            assert platform_denied.status_code == 400
            assert "X-Tenant-Id" in platform_denied.json()["detail"]
            assert len(calls) == 2

            actor.id = c_admin_id
            actor.role = "org_admin"
            actor.tenant_id = tenant_c
            missing = await client.post(
                "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_c)}
            )
            bodies.append(missing.text)
            assert missing.status_code == 409
            assert missing.json()["detail"]["code"] == "missing_config"
            assert missing.json()["detail"]["applied"] is False
            assert missing.json()["detail"]["retryable"] is True
            assert len(calls) == 2
    finally:
        logging.getLogger().removeHandler(handler)
        loguru_logger.remove(log_handle)

    assert await _fingerprint(owner_sessionmaker) == before
    member_names, emails, current = await _visible_names(app_user_sessionmaker, tenant_a)
    assert secret["foreign_member"] not in member_names
    assert secret["foreign_email"] not in emails
    assert current == str(tenant_a)
    async with app_user_engine.connect() as conn:
        role = (await conn.execute(text(
            "SELECT rolsuper, rolbypassrls, current_user AS role_name FROM pg_roles WHERE rolname = current_user"
        ))).one()
    assert role.rolsuper is False
    assert role.rolbypassrls is False
    assert role.role_name == "rls_app_user"
    leaked = _hidden_labels("\n".join(bodies + captured + loguru_messages), secret)
    assert leaked == []


@pytest.mark.parametrize("case", ["positive", "external_kind", "org_unique"])
async def test_preflight_checks_every_input_subject(owner_sessionmaker, app_user_sessionmaker, case):
    tag = uuid.uuid4().hex
    tenant_id, user_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    open_id, incoming_user = f"ou_{tag}", f"fu_{tag}"
    target_id = open_id
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Repair tenant", slug=f"repair-{tag}"))
        await db.flush()
        provider = IdentityProvider(provider_type="feishu", tenant_id=tenant_id, name="Repair provider", config={})
        db.add(provider)
        await db.flush()
        _user(
            db, id=user_id, username=f"repair-a-{tag}", email=f"a-{tag}@example.test",
            tenant_id=tenant_id, role="org_admin", feishu_open_id=open_id,
            feishu_user_id=incoming_user if case == "org_unique" else None,
        )
        _user(db, id=other_id, username=f"repair-b-{tag}", email=f"b-{tag}@example.test", tenant_id=tenant_id)
        await db.flush()
        if case == "org_unique":
            first = OrgMember(
                tenant_id=tenant_id, provider_id=provider.id, name="First",
                feishu_open_id=open_id, feishu_user_id=incoming_user,
            )
            second = OrgMember(
                tenant_id=tenant_id, provider_id=provider.id, name="Second",
                feishu_open_id=f"taken_{tag}", feishu_user_id=f"other_{tag}",
            )
            db.add_all([first, second])
            await db.flush()
            target_id = str(first.id)
            open_id = second.feishu_open_id
        else:
            db.add(ExternalIdentity(
                provider_id=provider.id,
                user_id=other_id if case == "external_kind" else user_id,
                provider_user_id=incoming_user,
            ))
        db.add(TenantSetting(
            tenant_id=tenant_id, key="feishu_org_sync",
            value={"app_id": "synthetic", "app_secret": "synthetic"},
        ))
        await db.commit()
    before = await _fingerprint(owner_sessionmaker)

    async def fetcher(*_args):
        return {"complete": True, "departments": [], "members": [{"open_id": open_id, "user_id": incoming_user}]}

    async def db_override():
        async with app_user_sessionmaker() as session:
            yield session
            await session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, tenant_id=tenant_id, role="org_admin",
    )
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[identity_dry_run_fetcher] = lambda: fetcher
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://repair") as client:
        response = await client.post(
            "/api/enterprise/org/identity-links/dry-run", params={"tenant_id": str(tenant_id)},
        )
    assert response.status_code == 200, response.text
    assert await _fingerprint(owner_sessionmaker) == before
    row = next(item for item in response.json()["rows"] if item["object_id"] == target_id)
    if case == "org_unique":
        async with owner_sessionmaker() as db:
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        update(OrgMember).where(OrgMember.id == uuid.UUID(target_id)).values(feishu_open_id=open_id)
                    )
        assert await _fingerprint(owner_sessionmaker) == before
        assert row["global_unique_blocked"] is True
        assert row["disposition"] != "cross_tenant"
    if case == "positive":
        assert row["disposition"] == "trusted" and row["user_id"] == str(user_id)
    else:
        assert row["disposition"] == "conflict" and row["user_id"] is None
