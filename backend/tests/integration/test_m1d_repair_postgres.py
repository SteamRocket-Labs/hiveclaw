"""Regressions for the M1-D review: stale user, department conflicts, audit, rollback."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.identity import IdentityProvider
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import Department, User
from app.services.org_sync_service import OrgSyncService
from tests.integration.test_m1d_department_cutover_postgres import _app, _rows, _user


@pytest.fixture
async def case(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    import app.database as database

    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    tag = uuid.uuid4().hex[:8]
    tenant, foreign, admin_id, bob_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add_all([
            Tenant(id=tenant, name=f"Repair {tag}", slug=f"repair-{tag}"),
            Tenant(id=foreign, name=f"Other {tag}", slug=f"other-{tag}"),
        ])
        await db.flush()
        provider = IdentityProvider(
            provider_type="feishu", tenant_id=tenant, name="Fixture", is_active=True, config={},
        )
        other = Department(tenant_id=tenant, name=f"Owner assignment C {tag}")
        orgs = [
            OrgDepartment(tenant_id=tenant, name=f"Directory {i} {tag}", feishu_id=f"od-repair-{i}-{tag}")
            for i in range(2)
        ]
        db.add_all([provider, other, *orgs])
        await db.flush()
        alice = _user(
            db, id=admin_id, username=f"alice-{tag}", display_name=f"Alice {tag}",
            email=f"alice-{tag}@example.test", tenant_id=tenant, role="org_admin",
            feishu_open_id=f"ou-alice-{tag}", feishu_user_id=f"u-alice-{tag}",
        )
        bob = _user(
            db, id=bob_id, username=f"bob-{tag}", display_name=f"Bob {tag}",
            email=f"bob-{tag}@example.test", tenant_id=tenant, feishu_open_id=f"ou-bob-{tag}",
        )
        members = [
            OrgMember(
                tenant_id=tenant, provider_id=provider.id, name=user.display_name,
                feishu_open_id=user.feishu_open_id, feishu_user_id=user.feishu_user_id,
                department_id=orgs[0].id, status="active",
            )
            for user in (alice, bob)
        ]
        db.add_all(members)
        db.add(TenantSetting(
            tenant_id=tenant, key="feishu_org_sync",
            value={"app_id": f"fixture-{tag}", "app_secret": "synthetic-review-secret"},
        ))
        await db.commit()
        stored = SimpleNamespace(
            tenant=tenant, provider=provider.id, admin=admin_id, bob=bob_id, other=other.id,
            orgs=[org.id for org in orgs], feishu_departments=[org.feishu_id for org in orgs],
            members=[member.id for member in members], names=[alice.display_name, bob.display_name],
            subjects=[alice.feishu_open_id, bob.feishu_open_id],
        )
    app = _app(app_user_sessionmaker, SimpleNamespace(id=admin_id, tenant_id=tenant, role="org_admin"))

    async def authenticated_user(db=Depends(get_db)):
        await pin_rls_tenant_context(db, tenant)
        return await db.get(User, admin_id)

    app.dependency_overrides[get_current_user] = authenticated_user
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://repair",
    ) as client:
        stored.client = client
        stored.params = {"tenant_id": str(tenant)}
        mapped = await client.post(
            "/api/enterprise/org/department-links", params=stored.params,
            json={"selections": [{"org_department_id": str(org), "action": "create"} for org in stored.orgs]},
        )
        assert mapped.status_code == 200, mapped.text
        assert mapped.json()["counts"]["department_mappings"]["created"] == 2
        yield stored


async def _backfill(stored):
    result = await stored.client.post(
        "/api/enterprise/org/identity-links/backfill", params=stored.params,
        json={"org_member_ids": [str(member) for member in stored.members]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["counts"]["link_backfill"]["applied"] == 3, result.json()
    return result.json()


def _reasons(value):
    if isinstance(value, dict):
        return [value.get("reason"), *[reason for child in value.values() for reason in _reasons(child)]]
    if isinstance(value, list):
        return [reason for child in value for reason in _reasons(child)]
    return []


@pytest.mark.parametrize("self_assignment", [False, True])
async def test_row_lock_rechecks_owner_department(case, owner_sessionmaker, self_assignment):
    await _backfill(case)
    user_id = case.admin if self_assignment else case.bob
    member_id = case.members[0 if self_assignment else 1]
    async with owner_sessionmaker() as holder:
        await holder.execute(select(User).where(User.id == user_id).with_for_update())
        waiting = asyncio.create_task(case.client.post(
            "/api/enterprise/org/department-cutover", params=case.params,
            json={"org_member_ids": [str(member_id)]},
        ))
        saw_wait = False
        try:
            for _ in range(100):
                rows = await _rows(
                    owner_sessionmaker,
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE '%users%'",
                )
                if rows[0][0]:
                    saw_wait = True
                    break
                await asyncio.sleep(0.02)
            await holder.execute(
                text("UPDATE users SET department_id=:department WHERE id=:user"),
                {"department": case.other, "user": user_id},
            )
            await holder.commit()
        finally:
            await holder.rollback()
        result = await asyncio.wait_for(waiting, 10)
    assert saw_wait, "The actual User row lock must have blocked cutover."
    stored = await _rows(owner_sessionmaker, "SELECT department_id::text FROM users WHERE id=:user", user=user_id)
    assert result.status_code == 200, result.text
    assert stored == [(str(case.other),)], {"self_assignment": self_assignment, "response": result.json()}
    assert result.json()["counts"]["user_assignments"]["changed"] == 0
    assert "department_id_points_elsewhere" in _reasons(result.json())


@pytest.mark.parametrize("conflict", ["multiple_directory_departments", "department_id_points_elsewhere"])
async def test_real_sync_preserves_and_reports_department_conflict(case, owner_sessionmaker, monkeypatch, conflict):
    await _backfill(case)
    if conflict == "department_id_points_elsewhere":
        async with owner_sessionmaker() as db:
            await db.execute(
                text("UPDATE users SET department_id=:department WHERE id=:user"),
                {"department": case.other, "user": case.bob},
            )
            await db.commit()
    chosen = case.feishu_departments if conflict == "multiple_directory_departments" else case.feishu_departments[:1]

    async def fetch(_self, _app_id, _app_secret):
        return {"complete": True, "departments": [], "members": [
            {"open_id": case.subjects[1], "name": case.names[1], "open_department_ids": chosen},
        ]}

    async def no_projection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", fetch)
    monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", no_projection)
    monkeypatch.setattr("app.services.workspace_sync_dirty.mark_tenant_dirty", lambda *_args, **_kwargs: None)
    response = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["user_assignments"], int)
    stored = await _rows(owner_sessionmaker, "SELECT department_id::text FROM users WHERE id=:user", user=case.bob)
    expected = None if conflict == "multiple_directory_departments" else str(case.other)
    assert stored == [(expected,)], {"conflict": conflict, "response": body}
    assert conflict in _reasons(body), body
    row = next(item for item in body["assignment_rows"] if item.get("reason") == conflict)
    assert row.get("person_label")
    if conflict == "department_id_points_elsewhere":
        assert row.get("department_name") and row.get("mapped_department_name")


async def test_rolled_back_backfill_does_not_commit_success_audit(case, owner_sessionmaker, monkeypatch):
    import app.services.identity_cutover as cutover

    original = cutover.apply_observed_subject_link
    calls = 0

    async def disable_between_subjects(*args, **kwargs):
        nonlocal calls
        outcome = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            async with owner_sessionmaker() as db:
                await db.execute(text("UPDATE identity_providers SET is_active=false WHERE id=:provider"), {"provider": case.provider})
                await db.commit()
        return outcome

    monkeypatch.setattr(cutover, "apply_observed_subject_link", disable_between_subjects)
    response = await case.client.post(
        "/api/enterprise/org/identity-links/backfill", params=case.params,
        json={"org_member_ids": [str(case.members[0])]},
    )
    assert response.status_code == 200, response.text
    assert calls == 2
    links = await _rows(
        owner_sessionmaker, "SELECT count(*) FROM org_member_user_links WHERE tenant_id=:tenant", tenant=case.tenant,
    )
    journal = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant AND object_kind='org_member_user_link'",
        tenant=case.tenant,
    )
    audits = await _rows(
        owner_sessionmaker,
        "SELECT action, resource_id::text FROM security_audit_events "
        "WHERE tenant_id=:tenant AND event_type='org.identity_link'",
        tenant=case.tenant,
    )
    assert links == [(0,)] and journal == [(0,)], (links, journal, response.json())
    assert audits == [], {"response": response.json(), "audit_of_rolled_back_link": audits}


async def test_rollback_with_active_replacement_returns_typed_conflict(case, owner_sessionmaker):
    await _backfill(case)
    listed = await case.client.get("/api/enterprise/org/identity-links", params=case.params)
    assert listed.status_code == 200, listed.text
    person = next(row for row in listed.json()["members"] if row["org_member_id"] == str(case.members[1]))
    link = next(row for row in person["links"] if row["status"] == "active")
    body = {
        "expected_version": link["version"],
        **{f"expected_{key}": link[key] for key in ("user_id", "provider_id", "subject_kind", "status")},
    }
    revoked = await case.client.post(
        f"/api/enterprise/org/identity-links/{link['id']}/revoke", params=case.params, json=body,
    )
    assert revoked.status_code == 200, revoked.text
    recreated = await case.client.post(
        "/api/enterprise/org/identity-links", params=case.params,
        json={
            "org_member_id": str(case.members[1]), "user_id": str(case.bob),
            "provider_id": str(case.provider), "subject_kind": "provider_open_id",
        },
    )
    assert recreated.status_code == 200 and recreated.json()["applied"], recreated.text
    journals = (await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)).json()["rows"]
    undo = next(row for row in journals if row["object_id"] == link["id"] and row["after"]["status"] == "revoked")
    before = await _rows(
        owner_sessionmaker, "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant", tenant=case.tenant,
    )
    response = await case.client.post(
        "/api/enterprise/org/cutover-journal/rollback", params=case.params, json={"journal_ids": [undo["id"]]},
    )
    active = await _rows(
        owner_sessionmaker,
        "SELECT id::text FROM org_member_user_links WHERE org_member_id=:member AND status='active'",
        member=case.members[1],
    )
    after = await _rows(
        owner_sessionmaker, "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant", tenant=case.tenant,
    )
    assert active == [(recreated.json()["link"]["id"],)]
    assert response.status_code == 200, response.text
    assert "conflict" in response.text
    assert before == after


async def test_department_rollback_over_active_replacement_returns_typed_conflict(case, owner_sessionmaker):
    listed = await case.client.get("/api/enterprise/org/department-links", params=case.params)
    assert listed.status_code == 200, listed.text
    first, second = listed.json()["org_departments"]
    revoked = await case.client.post(
        "/api/enterprise/org/department-links", params=case.params,
        json={"selections": [{"action": "revoke", "link_id": first["link"]["id"]}]},
    )
    assert revoked.status_code == 200, revoked.text
    repaired = await case.client.post(
        "/api/enterprise/org/department-links", params=case.params,
        json={"selections": [{
            "action": "repair",
            "link_id": second["link"]["id"],
            "department_id": first["link"]["department_id"],
        }]},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["counts"]["department_mappings"]["conflict"] == 0, repaired.json()
    journals = (await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)).json()["rows"]
    undo = next(
        row for row in journals
        if row["object_id"] == first["link"]["id"] and (row.get("after") or {}).get("status") == "revoked"
    )
    before = await _rows(
        owner_sessionmaker, "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant", tenant=case.tenant,
    )
    response = await case.client.post(
        "/api/enterprise/org/cutover-journal/rollback", params=case.params, json={"journal_ids": [undo["id"]]},
    )
    active = await _rows(
        owner_sessionmaker,
        "SELECT id::text, department_id::text FROM department_directory_links "
        "WHERE org_department_id=:org AND status='active'",
        org=second["org_department_id"],
    )
    old = await _rows(
        owner_sessionmaker,
        "SELECT status FROM department_directory_links WHERE id=:link",
        link=first["link"]["id"],
    )
    after = await _rows(
        owner_sessionmaker, "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant", tenant=case.tenant,
    )
    assert response.status_code == 200, response.text
    assert "conflict" in response.text
    assert active == [(second["link"]["id"], first["link"]["department_id"])]
    assert old == [("revoked",)]
    assert before == after


async def test_capture_repaired_wires_for_the_existing_page(case, owner_sessionmaker, monkeypatch):
    """Save the mounted responses the organization page renders. Synthetic provider only."""

    import json
    import os
    from pathlib import Path

    await _backfill(case)
    async with owner_sessionmaker() as db:
        await db.execute(
            text("UPDATE users SET department_id=:department WHERE id=:user"),
            {"department": case.other, "user": case.bob},
        )
        await db.commit()

    async def fetch(_self, _app_id, _app_secret):
        return {"complete": True, "departments": [], "members": [
            {"open_id": case.subjects[1], "name": case.names[1], "open_department_ids": case.feishu_departments[:1]},
        ]}

    async def no_projection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", fetch)
    monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", no_projection)
    monkeypatch.setattr("app.services.workspace_sync_dirty.mark_tenant_dirty", lambda *_args, **_kwargs: None)
    synced = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert synced.status_code == 200, synced.text
    assigned = await case.client.post(
        "/api/enterprise/org/department-cutover", params=case.params,
        json={"org_member_ids": [str(member) for member in case.members]},
    )
    assert assigned.status_code == 200, assigned.text
    conflict = next(row for row in assigned.json()["rows"] if row.get("reason") == "department_id_points_elsewhere")
    repaired = await case.client.post(
        "/api/enterprise/org/department-assignments/repair", params=case.params,
        json={"user_id": str(case.bob), "department_id": conflict["mapped_department_id"]},
    )
    assert repaired.status_code == 200, repaired.text
    listed = await case.client.get("/api/enterprise/org/identity-links", params=case.params)
    person = next(row for row in listed.json()["members"] if row["org_member_id"] == str(case.members[1]))
    active_link = next(row for row in person["links"] if row["status"] == "active" and row["subject_kind"] == "provider_open_id")
    body = {
        "expected_version": active_link["version"],
        **{f"expected_{key}": active_link[key] for key in ("user_id", "provider_id", "subject_kind", "status")},
    }
    revoked = await case.client.post(
        f"/api/enterprise/org/identity-links/{active_link['id']}/revoke", params=case.params, json=body,
    )
    assert revoked.status_code == 200, revoked.text
    recreated = await case.client.post(
        "/api/enterprise/org/identity-links", params=case.params,
        json={
            "org_member_id": str(case.members[1]), "user_id": str(case.bob),
            "provider_id": str(case.provider), "subject_kind": "provider_open_id",
        },
    )
    assert recreated.status_code == 200 and recreated.json()["applied"], recreated.text
    journals = (await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)).json()["rows"]
    undo = next(row for row in journals if row["object_id"] == active_link["id"] and row["after"]["status"] == "revoked")
    refused = await case.client.post(
        "/api/enterprise/org/cutover-journal/rollback", params=case.params, json={"journal_ids": [undo["id"]]},
    )
    assert refused.status_code == 200 and "conflict" in refused.text
    paths = [
        "/api/enterprise/org/departments",
        "/api/enterprise/org/members",
        "/api/enterprise/org/identity-links",
        "/api/enterprise/org/department-links",
        "/api/enterprise/org/cutover-journal",
    ]
    wires = {}
    for path in paths:
        response = await case.client.get(path, params=case.params)
        assert response.status_code == 200, (path, response.text)
        wires[path] = response.json()
    preview = await case.client.post("/api/enterprise/org/identity-links/backfill/preview", params=case.params)
    assert preview.status_code == 200, preview.text
    evidence = os.environ.get("M1D_REPAIR_EVIDENCE")
    if not evidence:
        return
    payload = {
        "tenant_id": str(case.tenant),
        "names": case.names,
        "wires": wires,
        "preview": preview.json(),
        "sync_result": synced.json(),
        "cutover_result": assigned.json(),
        "repair_result": repaired.json(),
        "rollback_result": refused.json(),
    }
    Path(evidence).mkdir(parents=True, exist_ok=True)
    (Path(evidence) / "actual-api-wire.json").write_text(json.dumps(payload, indent=2) + "\n")


def _patch_directory(monkeypatch, members):
    async def fetch(_self, _app_id, _app_secret):
        return {"complete": True, "departments": [], "members": members}

    async def no_projection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(OrgSyncService, "_fetch_directory_readonly", fetch)
    monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", no_projection)
    monkeypatch.setattr("app.services.workspace_sync_dirty.mark_tenant_dirty", lambda *_args, **_kwargs: None)


async def _user_department(owner_sessionmaker, user_id):
    rows = await _rows(owner_sessionmaker, "SELECT department_id::text FROM users WHERE id=:id", id=user_id)
    return rows[0][0]


async def _member_observation(owner_sessionmaker, member_id):
    rows = await _rows(
        owner_sessionmaker,
        "SELECT name, department_id::text, observed_directory_department_ids, "
        "(SELECT count(*) FROM org_member_user_links AS link "
        "WHERE link.org_member_id = org_members.id AND link.status = 'active') "
        "FROM org_members WHERE id=:id",
        id=member_id,
    )
    return rows[0]


@pytest.mark.parametrize("next_action", ["selected_cutover", "later_sync"])
async def test_multi_department_observation_survives_next_request(case, owner_sessionmaker, monkeypatch, next_action):
    await _backfill(case)
    _patch_directory(monkeypatch, [{
        "open_id": case.subjects[1], "name": case.names[1], "open_department_ids": case.feishu_departments,
    }])
    synced = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert synced.status_code == 200 and "multiple_directory_departments" in synced.text, synced.text
    assert await _user_department(owner_sessionmaker, case.bob) is None
    if next_action == "selected_cutover":
        result = await case.client.post(
            "/api/enterprise/org/department-cutover", params=case.params,
            json={"org_member_ids": [str(case.members[1])]},
        )
        again = await case.client.post(
            "/api/enterprise/org/department-cutover", params=case.params,
            json={"org_member_ids": [str(case.members[1])]},
        )
        assert again.status_code == 200 and "multiple_directory_departments" in again.text, again.text
    else:
        _patch_directory(monkeypatch, [{
            "open_id": case.subjects[0], "name": case.names[0], "open_department_ids": case.feishu_departments[:1],
        }])
        result = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert result.status_code == 200, result.text
    assert await _user_department(owner_sessionmaker, case.bob) is None, result.text
    assert "multiple_directory_departments" in result.text, result.text
    name, primary, observation, links = await _member_observation(owner_sessionmaker, case.members[1])
    assert (name, primary, observation, links >= 1) == (case.names[1], str(case.orgs[0]), case.feishu_departments, True)
    preview = await case.client.post("/api/enterprise/org/identity-links/backfill/preview", params=case.params)
    assert preview.status_code == 200, preview.text
    selectable = next(row for row in preview.json()["rows"] if row["org_member_id"] == str(case.members[1]))
    assert selectable["selectable"] is True


async def test_unambiguous_directory_observation_replaces_ambiguity(case, owner_sessionmaker, monkeypatch):
    await _backfill(case)
    _patch_directory(monkeypatch, [{
        "open_id": case.subjects[1], "name": case.names[1], "open_department_ids": case.feishu_departments,
    }])
    first = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert first.status_code == 200 and "multiple_directory_departments" in first.text, first.text
    _patch_directory(monkeypatch, [{
        "open_id": case.subjects[1], "name": case.names[1], "open_department_ids": case.feishu_departments[:1],
    }])
    second = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert second.status_code == 200, second.text
    assert "multiple_directory_departments" not in _reasons(second.json()), second.text
    mapped = await _rows(
        owner_sessionmaker,
        "SELECT department_id::text FROM department_directory_links "
        "WHERE org_department_id=:org AND status='active'",
        org=case.orgs[0],
    )
    assert await _user_department(owner_sessionmaker, case.bob) == mapped[0][0]
    _name, primary, observation, _links = await _member_observation(owner_sessionmaker, case.members[1])
    assert primary == str(case.orgs[0])
    assert observation == case.feishu_departments[:1]


async def test_explicit_repair_replaces_department_ambiguity(case, owner_sessionmaker, monkeypatch):
    await _backfill(case)
    _patch_directory(monkeypatch, [{
        "open_id": case.subjects[1], "name": case.names[1], "open_department_ids": case.feishu_departments,
    }])
    synced = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert synced.status_code == 200 and "multiple_directory_departments" in synced.text, synced.text
    refused = await case.client.post(
        "/api/enterprise/org/department-assignments/repair", params=case.params,
        json={"user_id": str(case.bob), "department_id": str(uuid.uuid4())},
    )
    assert refused.status_code == 200 and refused.json()["applied"] is False, refused.text
    assert "department_not_in_tenant" in _reasons(refused.json())
    assert await _user_department(owner_sessionmaker, case.bob) is None
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] == case.feishu_departments
    repaired = await case.client.post(
        "/api/enterprise/org/department-assignments/repair", params=case.params,
        json={"user_id": str(case.bob), "department_id": str(case.other)},
    )
    assert repaired.status_code == 200 and repaired.json()["applied"] is True, repaired.text
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    name, primary, observation, links = await _member_observation(owner_sessionmaker, case.members[1])
    assert (name, primary, observation, links >= 1) == (case.names[1], str(case.orgs[0]), None, True)
    again = await case.client.post(
        "/api/enterprise/org/department-assignments/repair", params=case.params,
        json={"user_id": str(case.bob), "department_id": str(case.other)},
    )
    assert again.status_code == 200, again.text
    assert again.json()["counts"]["user_assignments"]["noop"] == 1
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] is None
    cutover = await case.client.post(
        "/api/enterprise/org/department-cutover", params=case.params,
        json={"org_member_ids": [str(case.members[1])]},
    )
    assert cutover.status_code == 200 and "department_id_points_elsewhere" in cutover.text, cutover.text
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)


async def test_journal_actor_is_exact_not_nearest_timestamp(case, owner_sessionmaker, app_user_sessionmaker):
    async with owner_sessionmaker() as db:
        await db.execute(text("UPDATE users SET role='org_admin' WHERE id=:id"), {"id": case.bob})
        await db.commit()
    mapped = await _rows(
        owner_sessionmaker,
        "SELECT department_id::text FROM department_directory_links "
        "WHERE org_department_id=:org AND status='active'",
        org=case.orgs[0],
    )
    target = mapped[0][0]
    bob_app = _app(app_user_sessionmaker, SimpleNamespace(id=case.bob, tenant_id=case.tenant, role="org_admin"))
    bob_authenticated = asyncio.Event()
    continue_bob = asyncio.Event()

    async def bob_user(db=Depends(get_db)):
        await pin_rls_tenant_context(db, case.tenant)
        user = await db.get(User, case.bob)
        bob_authenticated.set()
        await continue_bob.wait()
        return user

    bob_app.dependency_overrides[get_current_user] = bob_user
    async with AsyncClient(
        transport=ASGITransport(app=bob_app, raise_app_exceptions=False), base_url="http://repair",
    ) as bob_client:
        pending = asyncio.create_task(bob_client.post(
            "/api/enterprise/org/department-assignments/repair", params=case.params,
            json={"user_id": str(case.bob), "department_id": target},
        ))
        await asyncio.wait_for(bob_authenticated.wait(), 5)
        try:
            alice = await case.client.post(
                "/api/enterprise/org/department-assignments/repair", params=case.params,
                json={"user_id": str(case.bob), "department_id": str(case.other)},
            )
            assert alice.status_code == 200 and alice.json()["applied"], alice.text
        finally:
            continue_bob.set()
        bob = await asyncio.wait_for(pending, 10)
        assert bob.status_code == 200 and bob.json()["applied"], bob.text
    listed = await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)
    assert listed.status_code == 200, listed.text
    rows = [
        row for row in listed.json()["rows"]
        if row["object_kind"] == "user_department" and row["object_id"] == str(case.bob)
    ]
    actual = {row["after"]["department_id"]: row.get("actor_id") for row in rows}
    assert actual == {str(case.other): str(case.admin), target: str(case.bob)}, actual


async def test_old_journal_without_exact_actor_stays_unknown(case, owner_sessionmaker):
    from datetime import datetime, timezone

    from app.models.organization_identity import IdentityCutoverJournal
    from app.models.security_audit import SecurityAuditEvent

    await _backfill(case)
    listed = await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)
    assert listed.status_code == 200, listed.text
    link_rows = [row for row in listed.json()["rows"] if row["object_kind"] == "org_member_user_link"]
    assert link_rows
    assert {row["actor_id"] for row in link_rows} == {str(case.admin)}
    repaired = await case.client.post(
        "/api/enterprise/org/department-assignments/repair", params=case.params,
        json={"user_id": str(case.bob), "department_id": str(case.other)},
    )
    assert repaired.status_code == 200 and repaired.json()["applied"], repaired.text
    orphan = uuid.uuid4()
    moment = datetime.now(timezone.utc)
    async with owner_sessionmaker() as db:
        db.add(IdentityCutoverJournal(
            id=orphan, tenant_id=case.tenant, object_kind="user_department", object_id=case.bob,
            before_json={"department_id": None}, after_json={"department_id": str(case.other)}, applied_at=moment,
        ))
        db.add(SecurityAuditEvent(
            event_type="org.identity_cutover", severity="info", actor_type="user", actor_id=case.admin,
            tenant_id=case.tenant, resource_type="user_department", resource_id=case.bob,
            action="user_department.repair", details={"user_id": str(case.bob)},
            prev_hash="", event_hash="0" * 64, created_at=moment,
        ))
        await db.commit()
    again = await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)
    assert again.status_code == 200, again.text
    rows = {
        row["id"]: row for row in again.json()["rows"]
        if row["object_kind"] == "user_department" and row["object_id"] == str(case.bob)
    }
    assert rows[str(orphan)]["actor_id"] is None and rows[str(orphan)]["actor_label"] is None
    real = [row for row in rows.values() if row["id"] != str(orphan)]
    assert real and {row["actor_id"] for row in real} == {str(case.admin)}


async def _journal_rows(case):
    listed = await case.client.get("/api/enterprise/org/cutover-journal", params=case.params)
    assert listed.status_code == 200, listed.text
    return listed.json()["rows"]


async def _journal_count(owner_sessionmaker, tenant_id):
    rows = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM identity_cutover_journal WHERE tenant_id=:tenant",
        tenant=tenant_id,
    )
    return rows[0][0]


def _assignment_rows(rows, user_id):
    return [
        row for row in rows
        if row["object_kind"] == "user_department" and row["object_id"] == str(user_id)
    ]


async def _sync_bob(case, monkeypatch, department_ids):
    _patch_directory(monkeypatch, [{
        "open_id": case.subjects[1],
        "name": case.names[1],
        "open_department_ids": department_ids,
    }])
    synced = await case.client.post("/api/enterprise/org/sync", params=case.params)
    assert synced.status_code == 200, synced.text
    return synced


async def _repair_bob(case, department_id):
    repaired = await case.client.post(
        "/api/enterprise/org/department-assignments/repair",
        params=case.params,
        json={"user_id": str(case.bob), "department_id": str(department_id)},
    )
    assert repaired.status_code == 200, repaired.text
    return repaired


async def _rollback(case, journal_ids):
    undone = await case.client.post(
        "/api/enterprise/org/cutover-journal/rollback",
        params=case.params,
        json={"journal_ids": [str(journal_id) for journal_id in journal_ids]},
    )
    assert undone.status_code == 200, undone.text
    return undone


async def test_repair_rollback_restores_ambiguity_and_retry_writes_nothing(case, owner_sessionmaker, monkeypatch):
    await _backfill(case)
    synced = await _sync_bob(case, monkeypatch, case.feishu_departments)
    assert "multiple_directory_departments" in synced.text, synced.text
    repaired = await _repair_bob(case, case.other)
    assert repaired.json()["applied"] is True, repaired.text
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] is None
    original = next(
        row for row in _assignment_rows(await _journal_rows(case), case.bob)
        if row["after"]["department_id"] == str(case.other)
    )
    assert original["actor_id"] == str(case.admin)
    undone = await _rollback(case, [original["id"]])
    assert undone.json()["applied"] is True, undone.text
    assert await _user_department(owner_sessionmaker, case.bob) is None
    name, primary, observation, links = await _member_observation(owner_sessionmaker, case.members[1])
    assert (name, primary, observation, links >= 1) == (
        case.names[1], str(case.orgs[0]), case.feishu_departments, True,
    )
    selected = await case.client.post(
        "/api/enterprise/org/department-cutover",
        params=case.params,
        json={"org_member_ids": [str(case.members[1])]},
    )
    assert selected.status_code == 200, selected.text
    assert await _user_department(owner_sessionmaker, case.bob) is None, selected.text
    assert "multiple_directory_departments" in selected.text, selected.text
    count = await _journal_count(owner_sessionmaker, case.tenant)
    repeated = await _rollback(case, [original["id"]])
    assert all(row["outcome"] in {"conflict", "noop"} for row in repeated.json()["rows"]), repeated.text
    assert await _journal_count(owner_sessionmaker, case.tenant) == count
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] == case.feishu_departments
    listed = await _journal_rows(case)
    assert next(row for row in listed if row["id"] == original["id"])["actor_id"] == str(case.admin)
    produced = [
        row for row in _assignment_rows(listed, case.bob)
        if row["id"] != original["id"] and row["before"]["department_id"] == str(case.other)
    ]
    assert [row["actor_id"] for row in produced] == [str(case.admin)]


async def test_repair_rollback_keeps_a_newer_directory_observation(case, owner_sessionmaker, monkeypatch):
    await _backfill(case)
    await _sync_bob(case, monkeypatch, case.feishu_departments)
    repaired = await _repair_bob(case, case.other)
    assert repaired.json()["applied"] is True, repaired.text
    await _sync_bob(case, monkeypatch, case.feishu_departments[:1])
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    name, primary, observation, links = await _member_observation(owner_sessionmaker, case.members[1])
    assert (name, primary, observation, links >= 1) == (
        case.names[1], str(case.orgs[0]), case.feishu_departments[:1], True,
    )
    original = next(
        row for row in _assignment_rows(await _journal_rows(case), case.bob)
        if row["before"]["department_id"] is None and row["after"]["department_id"] == str(case.other)
    )
    undone = await _rollback(case, [original["id"]])
    assert undone.json()["applied"] is True, undone.text
    assert await _user_department(owner_sessionmaker, case.bob) is None
    name, primary, observation, links = await _member_observation(owner_sessionmaker, case.members[1])
    assert (name, primary, observation, links >= 1) == (
        case.names[1], str(case.orgs[0]), case.feishu_departments[:1], True,
    )
    mapped = await _rows(
        owner_sessionmaker,
        "SELECT department_id::text FROM department_directory_links "
        "WHERE org_department_id=:org AND status='active'",
        org=case.orgs[0],
    )
    assigned = await case.client.post(
        "/api/enterprise/org/department-cutover",
        params=case.params,
        json={"org_member_ids": [str(case.members[1])]},
    )
    assert assigned.status_code == 200, assigned.text
    assert "multiple_directory_departments" not in assigned.text, assigned.text
    assert await _user_department(owner_sessionmaker, case.bob) == mapped[0][0]
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] == case.feishu_departments[:1]


async def test_noop_department_repair_journals_resolved_ambiguity(case, owner_sessionmaker, monkeypatch):
    await _backfill(case)
    await _sync_bob(case, monkeypatch, case.feishu_departments)
    first = await _repair_bob(case, case.other)
    assert first.json()["applied"] is True, first.text
    await _sync_bob(case, monkeypatch, case.feishu_departments)
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] == case.feishu_departments
    before = await _journal_count(owner_sessionmaker, case.tenant)
    second = await _repair_bob(case, case.other)
    assert second.json()["applied"] is True, second.text
    assert second.json()["counts"]["user_assignments"]["changed"] == 1
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] is None
    assert await _journal_count(owner_sessionmaker, case.tenant) == before + 1
    resolution = next(
        row for row in _assignment_rows(await _journal_rows(case), case.bob)
        if row["before"]["department_id"] == row["after"]["department_id"] == str(case.other)
    )
    before_fact = next(
        item for item in resolution["before"]["directory_observations"]
        if item["org_member_id"] == str(case.members[1])
    )
    after_fact = next(
        item for item in resolution["after"]["directory_observations"]
        if item["org_member_id"] == str(case.members[1])
    )
    assert before_fact["observed_directory_department_ids"] == case.feishu_departments
    assert after_fact["observed_directory_department_ids"] is None
    third = await _repair_bob(case, case.other)
    assert third.json()["applied"] is False, third.text
    assert third.json()["counts"]["user_assignments"]["noop"] == 1
    assert await _journal_count(owner_sessionmaker, case.tenant) == before + 1
    undone = await _rollback(case, [resolution["id"]])
    assert undone.json()["applied"] is True, undone.text
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other)
    assert (await _member_observation(owner_sessionmaker, case.members[1]))[2] == case.feishu_departments
    selected = await case.client.post(
        "/api/enterprise/org/department-cutover",
        params=case.params,
        json={"org_member_ids": [str(case.members[1])]},
    )
    assert selected.status_code == 200, selected.text
    assert await _user_department(owner_sessionmaker, case.bob) == str(case.other), selected.text
    assert "multiple_directory_departments" in selected.text, selected.text
    count = await _journal_count(owner_sessionmaker, case.tenant)
    repeated = await _rollback(case, [resolution["id"]])
    assert all(row["outcome"] in {"conflict", "noop"} for row in repeated.json()["rows"]), repeated.text
    assert await _journal_count(owner_sessionmaker, case.tenant) == count


async def test_link_rollback_keeps_the_original_actor(case):
    await _backfill(case)
    rows = await _journal_rows(case)
    department = next(row for row in rows if row["object_kind"] == "department_directory_link")
    identity = next(row for row in rows if row["object_kind"] == "org_member_user_link")
    assert department["actor_id"] == identity["actor_id"] == str(case.admin)
    undone = await _rollback(case, [department["id"], identity["id"]])
    by_journal = {row["journal_id"]: row for row in undone.json()["rows"]}
    assert by_journal[department["id"]]["outcome"] == "restored", undone.text
    assert by_journal[identity["id"]]["outcome"] == "restored", undone.text
    listed = await _journal_rows(case)
    by_id = {row["id"]: row for row in listed}
    assert by_id[department["id"]]["actor_id"] == str(case.admin)
    assert by_id[identity["id"]]["actor_id"] == str(case.admin)

    def produced(original):
        return [
            row for row in listed
            if row["object_kind"] == original["object_kind"]
            and row["object_id"] == original["object_id"]
            and row["id"] != original["id"]
        ]

    assert [row["actor_id"] for row in produced(department)] == [str(case.admin)]
    assert [row["actor_id"] for row in produced(identity)] == [str(case.admin)]
