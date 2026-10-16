"""Mounted business-team creation, membership, employee use, and private work entry.

Routes, transactions, RLS, permission checks, session creation, and input
admission are real. The model dispatch after admission is synthetic_transport.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api.agents import router as agents_router
from app.api.business_teams import router as business_teams_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.mcp_servers import router as mcp_router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.identity import IdentityProvider
from app.models.mcp_server import MCPServer
from app.models.org import OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.participant import Participant
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import Department, User


def _user(db, **kwargs) -> User:
    kwargs.setdefault("password_hash", "stored-password-hash")
    kwargs.setdefault("role", "member")
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("display_name", kwargs["username"])
    row = User(**kwargs)
    db.add(row)
    return row


def _actor(**kwargs):
    kwargs.setdefault("is_active", True)
    kwargs.setdefault("department_id", None)
    return SimpleNamespace(**kwargs)


def _app(sessionmaker, holder):
    async def db_override():
        async with sessionmaker() as session:
            try:
                await pin_rls_tenant_context(session, getattr(holder["actor"], "tenant_id", None))
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(business_teams_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(chat_sessions_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: holder["actor"]
    app.dependency_overrides[get_db] = db_override
    return app


def _client(sessionmaker, holder) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(sessionmaker, holder)), base_url="http://m1e")


async def _rows(sessionmaker, sql: str, **params) -> list[tuple]:
    async with sessionmaker() as db:
        return [tuple(row) for row in (await db.execute(text(sql), params)).all()]


def _detail(response) -> dict:
    body = response.json()
    detail = body.get("detail")
    return detail if isinstance(detail, dict) else {"message": detail}


async def _agent(db, *, tenant_id, owner_id, name) -> Agent:
    agent_id = uuid.uuid4()
    participant = Participant(type="agent", ref_id=agent_id, display_name=name[:100])
    db.add(participant)
    await db.flush()
    agent = Agent(
        id=agent_id,
        name=name[:100],
        role_description="Research employee",
        creator_id=owner_id,
        sponsor_user_id=owner_id,
        owner_user_id=owner_id,
        participant_id=participant.id,
        tenant_id=tenant_id,
        status="idle",
    )
    db.add(agent)
    await db.flush()
    return agent


async def test_creation_membership_and_admin_boundaries(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    founder_id, peer_id, outsider_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admin_id, foreign_admin_id, inactive_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    platform_id, linked_id = uuid.uuid4(), uuid.uuid4()
    foreign_name = f"secret-foreign-person-{tag}"

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1E A {tag}", slug=f"m1e-a-{tag}"))
        db.add(Tenant(id=tenant_b, name=f"M1E B {tag}", slug=f"m1e-b-{tag}"))
        await db.flush()
        dept = Department(tenant_id=tenant_a, name=f"Research {tag}")
        other_dept = Department(tenant_id=tenant_a, name=f"Finance {tag}")
        foreign_dept = Department(tenant_id=tenant_b, name=f"Foreign {tag}")
        db.add_all([dept, other_dept, foreign_dept])
        await db.flush()
        _user(db, id=founder_id, username=f"founder-{tag}", email=f"founder-{tag}@example.test", tenant_id=tenant_a, department_id=dept.id, display_name=f"Founder {tag}", role="member")
        _user(db, id=peer_id, username=f"peer-{tag}", email=f"peer-{tag}@example.test", tenant_id=tenant_a, display_name=f"Peer {tag}")
        _user(db, id=outsider_id, username=f"outsider-{tag}", email=f"outsider-{tag}@example.test", tenant_id=tenant_a, department_id=other_dept.id, display_name=f"Outsider {tag}")
        _user(db, id=admin_id, username=f"admin-{tag}", email=f"admin-{tag}@example.test", tenant_id=tenant_a, role="org_admin", display_name=f"Admin {tag}")
        _user(db, id=foreign_admin_id, username=f"fadmin-{tag}", email=f"fadmin-{tag}@example.test", tenant_id=tenant_b, role="org_admin", display_name=f"Foreign Admin {tag}")
        _user(db, id=inactive_id, username=f"inactive-{tag}", email=f"inactive-{tag}@example.test", tenant_id=tenant_a, is_active=False, display_name=f"Inactive {tag}")
        _user(db, id=linked_id, username=f"linked-{tag}", email=f"linked-{tag}@example.test", tenant_id=tenant_a, display_name=f"Linked {tag}")
        _user(db, id=platform_id, username=f"platform-{tag}", email=f"platform-{tag}@example.test", tenant_id=None, role="platform_admin", display_name=f"Platform {tag}")
        _user(db, id=uuid.uuid4(), username=f"foreign-{tag}", email=f"foreign-{tag}@example.test", tenant_id=tenant_b, display_name=foreign_name)
        provider = IdentityProvider(provider_type="feishu", name="Feishu", tenant_id=tenant_a, is_active=True, config={"source": "test"})
        db.add(provider)
        await db.flush()
        directory = OrgMember(tenant_id=tenant_a, name=f"Directory {tag}", status="active")
        linked_member = OrgMember(tenant_id=tenant_a, name=f"Linked directory {tag}", status="active")
        db.add_all([directory, linked_member])
        await db.flush()
        db.add(OrgMemberUserLink(
            tenant_id=tenant_a, org_member_id=linked_member.id, user_id=linked_id, provider_id=provider.id,
            subject_kind="provider_open_id", match_subject=f"ou-linked-{tag}", status="active",
            evidence_json={"source": "manual", "observed_at": "2026-10-16T00:00:00+00:00"},
            created_by_user_id=admin_id,
        ))
        await db.commit()
        stored = {"dept": dept.id, "other_dept": other_dept.id, "foreign_dept": foreign_dept.id, "directory": directory.id, "linked_member": linked_member.id}

    holder = {"actor": _actor(id=founder_id, tenant_id=tenant_a, role="member", department_id=stored["dept"])}
    async with _client(app_user_sessionmaker, holder) as client:
        rejected_lead = await client.post("/api/business-teams", json={"name": f"Nope {tag}", "first_lead_user_id": str(peer_id)})
        assert rejected_lead.status_code == 422
        rejected_creator = await client.post("/api/business-teams", json={"name": f"Nope {tag}", "created_by_user_id": str(peer_id)})
        assert rejected_creator.status_code == 422
        wrong_dept = await client.post("/api/business-teams", json={"name": f"Nope {tag}", "department_id": str(stored["other_dept"])})
        assert wrong_dept.status_code == 422 and _detail(wrong_dept)["code"] == "department_mismatch"
        foreign_department = await client.post("/api/business-teams", json={"name": f"Nope {tag}", "department_id": str(stored["foreign_dept"])})
        assert foreign_department.status_code == 422 and foreign_name not in foreign_department.text

        created = await client.post("/api/business-teams", json={"name": f"Same Name {tag}", "department_id": str(stored["dept"])})
        assert created.status_code == 201, created.text
        team = created.json()
        assert team["my_role"] == "lead" and team["can_manage"] is True
        assert team["department_name"] == f"Research {tag}"
        assert team["created_by_display_name"] == f"Founder {tag}"
        assert team["members"][0]["authority"] is True and team["private_sessions"] is True
        duplicate_name = await client.post("/api/business-teams", json={"name": f"Same Name {tag}"})
        assert duplicate_name.status_code == 201 and duplicate_name.json()["id"] != team["id"]

        added = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(peer_id), "role": "member"})
        assert added.status_code == 201, added.text
        again = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(peer_id), "role": "lead"})
        assert again.status_code == 200 and again.json()["membership_id"] == added.json()["membership_id"]
        inactive = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(inactive_id)})
        assert inactive.status_code == 409 and _detail(inactive)["code"] == "inactive_user"
        foreign_user = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(foreign_admin_id)})
        assert foreign_user.status_code == 404 and _detail(foreign_user)["code"] == "user_not_found"
        assert foreign_name not in foreign_user.text
        directory_row = await client.post(f"/api/business-teams/{team['id']}/members", json={"org_member_id": str(stored["directory"])})
        assert directory_row.status_code == 201, directory_row.text
        unregistered = next(row for row in directory_row.json()["members"] if row["org_member_id"] == str(stored["directory"]))
        assert unregistered["registered"] is False and unregistered["authority"] is False
        assert unregistered["display_name"] == f"Directory {tag}"
        linked_only = await client.post(f"/api/business-teams/{team['id']}/members", json={"org_member_id": str(stored["linked_member"])})
        assert linked_only.status_code == 409 and _detail(linked_only)["code"] == "registered_user_required"
        mismatched = await client.post(
            f"/api/business-teams/{team['id']}/members",
            json={"user_id": str(peer_id), "org_member_id": str(stored["linked_member"])},
        )
        assert mismatched.status_code == 409 and _detail(mismatched)["code"] == "org_member_user_mismatch"

        holder["actor"] = _actor(id=peer_id, tenant_id=tenant_a, role="member")
        peer_manage = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(linked_id)})
        assert peer_manage.status_code == 403 and _detail(peer_manage)["code"] == "team_manage_forbidden"
        holder["actor"] = _actor(id=outsider_id, tenant_id=tenant_a, role="member", department_id=stored["other_dept"])
        outsider = await client.post("/api/business-teams", json={"name": f"Outsider {tag}"})
        assert outsider.status_code == 201
        outsider_team = outsider.json()["id"]
        stolen = await client.patch(f"/api/business-teams/{team['id']}", json={"name": "taken"})
        assert stolen.status_code == 404 and _detail(stolen)["code"] == "team_not_found"

        holder["actor"] = _actor(id=founder_id, tenant_id=tenant_a, role="member", department_id=stored["dept"])
        peer_membership = next(row["id"] for row in again.json()["members"] if row["user_id"] == str(peer_id))
        left = await client.patch(f"/api/business-teams/{team['id']}/members/{peer_membership}", json={"status": "left"})
        assert left.status_code == 200
        left_row = next(row for row in left.json()["members"] if row["id"] == peer_membership)
        assert left_row["status"] == "left" and left_row["left_at"]
        rejoined = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(peer_id)})
        assert rejoined.status_code == 201 and rejoined.json()["membership_id"] != peer_membership

        holder["actor"] = _actor(id=admin_id, tenant_id=tenant_a, role="org_admin")
        installed = await client.post("/api/enterprise/business-teams", json={"name": f"Installed {tag}", "first_lead_user_id": str(peer_id)})
        assert installed.status_code == 201, installed.text
        install_body = installed.json()
        assert install_body["my_role"] is None
        assert [row for row in install_body["members"] if row["user_id"] == str(admin_id)] == []
        assert any(row["user_id"] == str(peer_id) and row["role"] == "lead" for row in install_body["members"])
        assert install_body["created_by_display_name"] == f"Admin {tag}"
        foreign_lead = await client.post("/api/enterprise/business-teams", json={"name": f"Foreign lead {tag}", "first_lead_user_id": str(foreign_admin_id)})
        assert foreign_lead.status_code == 404 and foreign_name not in foreign_lead.text
        suspended = await client.patch(f"/api/business-teams/{team['id']}", json={"status": "suspended"})
        assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
        holder["actor"] = _actor(id=founder_id, tenant_id=tenant_a, role="member", department_id=stored["dept"])
        lead_blocked = await client.post(f"/api/business-teams/{team['id']}/members", json={"user_id": str(linked_id)})
        assert lead_blocked.status_code == 403
        holder["actor"] = _actor(id=admin_id, tenant_id=tenant_a, role="org_admin")
        recovered = await client.patch(f"/api/business-teams/{team['id']}", json={"status": "active"})
        assert recovered.status_code == 200 and recovered.json()["status"] == "active"

        holder["actor"] = _actor(id=foreign_admin_id, tenant_id=tenant_b, role="org_admin")
        hidden = await client.get(f"/api/business-teams/{team['id']}")
        assert hidden.status_code == 404 and f"Founder {tag}" not in hidden.text

        holder["actor"] = _actor(id=platform_id, tenant_id=tenant_a, role="platform_admin")
        platform_self = await client.post("/api/business-teams", json={"name": f"Platform self {tag}"})
        assert platform_self.status_code == 422 and _detail(platform_self)["code"] == "self_service_home_required"
        platform_team = await client.post("/api/enterprise/business-teams", json={"name": f"Platform install {tag}", "first_lead_user_id": str(founder_id)})
        assert platform_team.status_code == 201, platform_team.text
        assert platform_team.json()["my_role"] is None
        holder["actor"] = _actor(id=platform_id, tenant_id=tenant_b, role="platform_admin")
        wrong_selection = await client.get(f"/api/business-teams/{platform_team.json()['id']}")
        assert wrong_selection.status_code == 404
        wrong_lead = await client.post("/api/enterprise/business-teams", json={"name": f"Wrong selection {tag}", "first_lead_user_id": str(founder_id)})
        assert wrong_lead.status_code == 404 and f"Founder {tag}" not in wrong_lead.text
        holder["actor"] = _actor(id=platform_id, tenant_id=None, role="platform_admin")
        unselected = await client.post("/api/enterprise/business-teams", json={"name": f"No company {tag}", "first_lead_user_id": str(founder_id)})
        assert unselected.status_code == 400 and _detail(unselected)["code"] == "tenant_required"

    roles = await _rows(owner_sessionmaker, "SELECT id::text, role::text FROM users WHERE username LIKE :name", name=f"%-{tag}")
    assert dict(roles)[str(founder_id)] == "member"
    assert dict(roles)[str(peer_id)] == "member"
    assert dict(roles)[str(admin_id)] == "org_admin"
    memberships = await _rows(
        owner_sessionmaker,
        "SELECT user_id::text, status FROM business_team_memberships WHERE business_team_id = :team AND user_id = :user ORDER BY joined_at",
        team=team["id"], user=peer_id,
    )
    assert [row[1] for row in memberships] == ["left", "active"]
    audits = await _rows(
        owner_sessionmaker,
        "SELECT action FROM security_audit_events WHERE resource_id = :team AND event_type = 'business_team.changed'",
        team=team["id"],
    )
    assert "business_team.create_self_service" in {row[0] for row in audits}

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_a)
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                text("UPDATE business_teams SET created_by_user_id = :new WHERE id = :team"),
                {"new": peer_id, "team": team["id"]},
            )
            await db.commit()

    import app.services.business_teams as teams
    calls = {"n": 0}
    original_flush = teams.flush_business_team_write

    async def fail_after_both_writes(db):
        calls["n"] += 1
        await original_flush(db)
        if calls["n"] == 2:
            raise RuntimeError("flush failed after both team and lead writes")

    monkeypatch.setattr(teams, "flush_business_team_write", fail_after_both_writes)
    holder["actor"] = _actor(id=founder_id, tenant_id=tenant_a, role="member", department_id=stored["dept"])
    with pytest.raises(RuntimeError, match="flush failed after both team and lead writes"):
        async with _client(app_user_sessionmaker, holder) as client:
            await client.post("/api/business-teams", json={"name": f"Atomic {tag}"})
    assert calls["n"] == 2
    leftover = await _rows(owner_sessionmaker, "SELECT id::text FROM business_teams WHERE name = :name", name=f"Atomic {tag}")
    assert leftover == []
    outsider_rows = await _rows(owner_sessionmaker, "SELECT created_by_user_id::text FROM business_teams WHERE id = :team", team=outsider_team)
    assert outsider_rows == [(str(outsider_id),)]


async def test_employee_use_revocation_and_private_session(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    tenant_a = uuid.uuid4()
    owner_id, member_id, lead_id, company_id, personal_id = (uuid.uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_a, name=f"M1E use {tag}", slug=f"m1e-use-{tag}"))
        await db.flush()
        for user_id, name, role in (
            (owner_id, "owner", "member"),
            (member_id, "member", "member"),
            (lead_id, "lead", "member"),
            (company_id, "company", "member"),
            (personal_id, "personal", "member"),
        ):
            _user(db, id=user_id, username=f"{name}-{tag}", email=f"{name}-{tag}@example.test", tenant_id=tenant_a, display_name=f"{name.title()} {tag}", role=role)
        employee = await _agent(db, tenant_id=tenant_a, owner_id=owner_id, name=f"Analyst {tag}")
        company_employee = await _agent(db, tenant_id=tenant_a, owner_id=owner_id, name=f"Company Analyst {tag}")
        other = await _agent(db, tenant_id=tenant_a, owner_id=personal_id, name=f"Personal {tag}")
        db.add(AgentPermission(agent_id=employee.id, tenant_id=tenant_a, scope_type="user", scope_id=company_id, access_level="use"))
        db.add(AgentPermission(agent_id=company_employee.id, tenant_id=tenant_a, scope_type="company", scope_id=None, access_level="manage"))
        db.add(ResourcePermission(
            principal_type="user", principal_id=personal_id, resource_type="agent", resource_id=other.id,
            actions=["read", "execute"], effect="allow", tenant_id=tenant_a, created_by_user_id=personal_id,
            conditions={}, purposes=[],
        ))
        await db.commit()
        employee_id, company_employee_id, other_id = employee.id, company_employee.id, other.id
        owner_before = employee.owner_user_id
        creator_before = employee.creator_id
        sponsor_before = employee.sponsor_user_id

    holder = {"actor": _actor(id=owner_id, tenant_id=tenant_a, role="member")}
    captured: dict = {}
    async with _client(app_user_sessionmaker, holder) as client:
        own_team = await client.post("/api/business-teams", json={"name": f"Delivery {tag}"})
        assert own_team.status_code == 201, own_team.text
        captured["created_team"] = own_team.json()
        team_id = own_team.json()["id"]
        second = await client.post("/api/business-teams", json={"name": f"Review {tag}"})
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        for user_id in (member_id, lead_id, company_id):
            added = await client.post(f"/api/business-teams/{team_id}/members", json={"user_id": str(user_id), "role": "member"})
            assert added.status_code == 201, added.text
        lead_membership = next(row["id"] for row in (await client.get(f"/api/business-teams/{team_id}")).json()["members"] if row["user_id"] == str(lead_id))
        promoted = await client.patch(f"/api/business-teams/{team_id}/members/{lead_membership}", json={"role": "lead"})
        assert promoted.status_code == 200

        holder["actor"] = _actor(id=lead_id, tenant_id=tenant_a, role="member")
        takeover = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(employee_id)})
        assert takeover.status_code == 403
        holder["actor"] = _actor(id=company_id, tenant_id=tenant_a, role="member")
        company_takeover = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(employee_id)})
        assert company_takeover.status_code == 403
        holder["actor"] = _actor(id=personal_id, tenant_id=tenant_a, role="member")
        outsider_share = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(other_id)})
        assert outsider_share.status_code == 403 and _detail(outsider_share)["code"] == "team_membership_required"

        holder["actor"] = _actor(id=owner_id, tenant_id=tenant_a, role="member")
        shared = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(employee_id)})
        assert shared.status_code == 200, shared.text
        captured["shared_team"] = shared.json()
        affiliation = next(row for row in shared.json()["affiliations"] if row["agent_id"] == str(employee_id))
        assert affiliation["actions"] == ["read", "execute"]
        assert affiliation["use_access"] == "read_and_execute" and affiliation["execution"] == "allowed"
        assert affiliation["manage_granted"] is False and affiliation["owner_unchanged"] is True and affiliation["home_team"] is True
        second_share = await client.post(f"/api/business-teams/{second_id}/agents", json={"agent_id": str(employee_id)})
        assert second_share.status_code == 200
        assert next(row for row in second_share.json()["affiliations"] if row["agent_id"] == str(employee_id))["home_team"] is False
        company_share = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(company_employee_id)})
        assert company_share.status_code == 200, company_share.text

    facts = await _rows(
        owner_sessionmaker,
        "SELECT owner_user_id::text, creator_id::text, sponsor_user_id::text, home_business_team_id::text FROM agents WHERE id = :id",
        id=employee_id,
    )
    assert facts == [(str(owner_before), str(creator_before), str(sponsor_before), team_id)]

    holder["actor"] = _actor(id=member_id, tenant_id=tenant_a, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        listed = await client.get("/api/agents/")
        assert listed.status_code == 200, listed.text
        visible = next(row for row in listed.json() if row["id"] == str(employee_id))
        captured["listed_employee"] = visible
        assert visible["name"] == f"Analyst {tag}"
        assert visible["access_level"] == "use" and visible["action_capabilities"]["can_use"] is True
        assert visible["action_capabilities"]["can_manage"] is False
        assert visible["action_capabilities"]["can_transfer_ownership"] is False
        detail = await client.get(f"/api/agents/{employee_id}")
        assert detail.status_code == 200 and detail.json()["access_level"] == "use"
        opened = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": f"Private {tag}"})
        assert opened.status_code == 201, opened.text
        captured["session"] = opened.json()
        session_id = opened.json()["id"]
        member_ready = await client.get(f"/api/business-teams/{team_id}")
        assert member_ready.status_code == 200, member_ready.text
        captured["member_ready_team"] = member_ready.json()

    stored_session = await _rows(
        owner_sessionmaker,
        "SELECT user_id::text, audience_kind FROM chat_sessions WHERE id = :id",
        id=session_id,
    )
    assert stored_session == [(str(member_id), None)]

    dispatched = {"called": False}

    async def synthetic_dispatch(*_args, **_kwargs):
        dispatched["called"] = True
        return SimpleNamespace(state="synthetic_transport", receipt={"transport": "synthetic_transport"})

    monkeypatch.setattr("app.services.session_input_dispatch.dispatch_admitted_input_fast_path", synthetic_dispatch)
    async with _client(app_user_sessionmaker, holder) as client:
        turn = await client.post(
            f"/api/agents/{employee_id}/sessions/{session_id}/inputs",
            json={
                "kind": "start_turn",
                "input_id": str(uuid.uuid4()),
                "idempotency_key": f"m1e-{tag}",
                "content_parts": [{"type": "text", "text": "Start my private work"}],
            },
        )
        assert turn.status_code == 200, turn.text
        assert dispatched["called"] is True and "synthetic_transport" in turn.text
        holder["actor"] = _actor(id=lead_id, tenant_id=tenant_a, role="member")
        private = await client.get(f"/api/agents/{employee_id}/sessions/{session_id}/messages")
        assert private.status_code == 403 and "different user" in private.text

    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(
            principal_type="business_team", principal_id=uuid.UUID(team_id), resource_type="agent", resource_id=employee_id,
            actions=["execute"], effect="deny", tenant_id=tenant_a, created_by_user_id=owner_id, conditions={}, purposes=[],
        ))
        db.add(ResourcePermission(
            principal_type="business_team", principal_id=uuid.UUID(team_id), resource_type="agent", resource_id=company_employee_id,
            actions=["execute"], effect="deny", tenant_id=tenant_a, created_by_user_id=owner_id, conditions={}, purposes=[],
        ))
        await db.commit()

    submit_called = {"called": False}

    async def fail_if_called(**_kwargs):
        submit_called["called"] = True
        raise AssertionError("execute denial reached submit_live_human_input")

    monkeypatch.setattr("app.api.chat_sessions.submit_live_human_input", fail_if_called)
    holder["actor"] = _actor(id=lead_id, tenant_id=tenant_a, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        denied_list = await client.get("/api/agents/")
        denied_row = next(row for row in denied_list.json() if row["id"] == str(employee_id))
        assert denied_row["access_level"] == "read" and denied_row["action_capabilities"]["can_use"] is False
        denied = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": "blocked"})
        assert denied.status_code == 403 and _detail(denied)["code"] == "agent_execute_denied"
        captured["execution_denied"] = denied.json()
        captured["lead_denied_agent"] = denied_row
        holder["actor"] = _actor(id=member_id, tenant_id=tenant_a, role="member")
        member_denied = await client.get(f"/api/business-teams/{team_id}")
        assert member_denied.status_code == 200, member_denied.text
        captured["member_denied_team"] = member_denied.json()
        denied_affiliations = {row["agent_id"]: row for row in member_denied.json()["affiliations"]}
        assert denied_affiliations[str(employee_id)]["execution"] == "blocked"
        assert denied_affiliations[str(employee_id)]["caller_can_use"] is False
        assert denied_affiliations[str(company_employee_id)]["execution"] == "blocked"
        assert denied_affiliations[str(company_employee_id)]["caller_can_use"] is True
        holder["actor"] = _actor(id=company_id, tenant_id=tenant_a, role="member")
        company_session = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": "independent user"})
        assert company_session.status_code == 201, company_session.text
        holder["actor"] = _actor(id=member_id, tenant_id=tenant_a, role="member")
        company_wide = await client.post(f"/api/agents/{company_employee_id}/sessions", json={"title": "company wide"})
        assert company_wide.status_code == 201, company_wide.text
        company_detail = await client.get(f"/api/agents/{company_employee_id}")
        assert company_detail.status_code == 200 and company_detail.json()["access_level"] == "use"
    assert submit_called["called"] is False
    blocked_rows = await _rows(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE title = 'blocked'")
    assert blocked_rows == [(0,)]

    holder["actor"] = _actor(id=owner_id, tenant_id=tenant_a, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        removed = await client.delete(f"/api/business-teams/{team_id}/agents/{employee_id}")
        assert removed.status_code == 200, removed.text
        captured["revoked_team"] = removed.json()
        assert all(row["agent_id"] != str(employee_id) for row in removed.json()["affiliations"])
    holder["actor"] = _actor(id=member_id, tenant_id=tenant_a, role="member")
    async with _client(app_user_sessionmaker, holder) as client:
        after_revoke = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": "after revoke"})
        assert after_revoke.status_code == 403
    grants = await _rows(
        owner_sessionmaker,
        """
        SELECT principal_type, effect, revoked_at IS NULL
        FROM resource_permissions
        WHERE resource_id = :agent
        ORDER BY principal_type, effect
        """,
        agent=employee_id,
    )
    assert not any(row[0] == "user" and row[1] == "allow" for row in grants)
    assert ("business_team", "deny", True) in grants
    assert ("business_team", "allow", False) in grants
    home = await _rows(owner_sessionmaker, "SELECT home_business_team_id::text FROM agents WHERE id = :id", id=employee_id)
    assert home == [(None,)]
    second_grant = await _rows(
        owner_sessionmaker,
        """
        SELECT revoked_at IS NULL FROM resource_permissions
        WHERE principal_id = :team AND resource_id = :agent AND effect = 'allow'
        """,
        team=second_id, agent=employee_id,
    )
    assert second_grant == [(True,)]
    personal_grant = await _rows(
        owner_sessionmaker,
        "SELECT revoked_at IS NULL FROM resource_permissions WHERE principal_id = :user AND resource_id = :agent",
        user=personal_id, agent=other_id,
    )
    assert personal_grant == [(True,)]

    wire = {
        "transport": "synthetic_transport",
        "label": "synthetic_transport replay of mounted API responses; not a live model provider",
        "team": f"Delivery {tag}",
        "employee": f"Analyst {tag}",
        "owner": f"Owner {tag}",
        "member": f"Member {tag}",
        "session_id": session_id,
        "execution_denied_code": "agent_execute_denied",
        "private_session": True,
        "created_team": captured["created_team"],
        "shared_team": captured["shared_team"],
        "listed_employee": captured["listed_employee"],
        "member_ready_team": captured["member_ready_team"],
        "session": captured["session"],
        "execution_denied": captured["execution_denied"],
        "lead_denied_agent": captured["lead_denied_agent"],
        "member_denied_team": captured["member_denied_team"],
        "revoked_team": captured["revoked_team"],
    }
    destination = os.environ.get("M1E_WIRE")
    if destination:
        Path(destination).write_text(json.dumps(wire, indent=2), encoding="utf-8")


async def test_repair_execution_management_and_visibility(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    """Denied execution and team use cannot reach the side doors C1 still had open."""
    tag = uuid.uuid4().hex[:8]
    tenant_id, owner_id, member_id, admin_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1E repair {tag}", slug=f"m1e-repair-{tag}"))
        await db.flush()
        _user(db, id=owner_id, username=f"repair-owner-{tag}", email=f"repair-owner-{tag}@example.test", tenant_id=tenant_id, role="member")
        _user(db, id=member_id, username=f"repair-member-{tag}", email=f"repair-member-{tag}@example.test", tenant_id=tenant_id, role="member")
        _user(db, id=admin_id, username=f"repair-admin-{tag}", email=f"repair-admin-{tag}@example.test", tenant_id=tenant_id, role="org_admin")
        employee = await _agent(db, tenant_id=tenant_id, owner_id=owner_id, name=f"Repair analyst {tag}")
        server_id = uuid.uuid4()
        db.add(MCPServer(id=server_id, tenant_id=tenant_id, name="Synthetic local MCP", server_key=server_id.hex, status="disabled"))
        await db.commit()
        employee_id = employee.id

    holder = {"actor": _actor(id=owner_id, tenant_id=tenant_id, role="member")}
    app = _app(app_user_sessionmaker, holder)
    app.include_router(mcp_router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://m1e-repair") as client:
        created = await client.post("/api/business-teams", json={"name": f"Repair {tag}"})
        assert created.status_code == 201, created.text
        team = created.json()
        team_id = team["id"]
        owner_membership = next(row["id"] for row in team["members"] if row["user_id"] == str(owner_id))
        added = await client.post(f"/api/business-teams/{team_id}/members", json={"user_id": str(member_id)})
        assert added.status_code == 201, added.text
        shared = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(employee_id)})
        assert shared.status_code == 200, shared.text
        suspended_team = await client.post("/api/business-teams", json={"name": f"Repair suspended {tag}"})
        assert suspended_team.status_code == 201, suspended_team.text
        suspended_id = suspended_team.json()["id"]
        suspended_membership = next(row["id"] for row in suspended_team.json()["members"] if row["user_id"] == str(owner_id))

        assignment = f"/api/agents/{employee_id}/mcp-servers/{server_id}"
        owner_policy = await client.put(assignment, json={"enabled": False, "default_tool_mode": "deny"})
        assert owner_policy.status_code == 200, owner_policy.text
        holder["actor"] = _actor(id=member_id, tenant_id=tenant_id, role="member")
        visible = await client.get(f"/api/agents/{employee_id}")
        assert visible.status_code == 200 and visible.json()["action_capabilities"]["can_manage"] is False
        rejected = await client.put(assignment, json={"enabled": True, "default_tool_mode": "auto"})
        tool_rejected = await client.put(f"{assignment}/tools/synthetic_tool/policy", json={"mode": "auto"})
        stored = await _rows(owner_sessionmaker, "SELECT enabled, default_tool_mode FROM agent_mcp_server_assignments WHERE agent_id = :agent", agent=employee_id)
        assert rejected.status_code == 403 and tool_rejected.status_code == 403 and stored == [(False, "deny")], (rejected.text, tool_rejected.text, stored)

        opened = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": f"Repair private {tag}"})
        assert opened.status_code == 201, opened.text
        session_id = uuid.UUID(opened.json()["id"])

    anchor_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatTranscriptEvent(
            id=anchor_id, tenant_id=tenant_id, agent_id=employee_id, session_id=session_id,
            sequence=1, actor_type="user", event_type="user_message", content="Original request",
        ))
        db.add(ResourcePermission(
            tenant_id=tenant_id, principal_type="business_team", principal_id=uuid.UUID(team_id),
            resource_type="agent", resource_id=employee_id, actions=["execute"], effect="deny",
            conditions={}, purposes=[], created_by_user_id=owner_id,
        ))
        await db.commit()

    dispatched = []

    async def synthetic_dispatch(*_args, **_kwargs):
        dispatched.append("dispatch")
        return {"run_id": "synthetic"}

    async def synthetic_start(*_args, **_kwargs):
        dispatched.append("start")
        return {"run_id": "synthetic"}

    monkeypatch.setattr("app.services.session_input_dispatch.dispatch_admitted_input_fast_path", synthetic_dispatch)
    monkeypatch.setattr("app.api.chat_sessions.start_web_chat_run", synthetic_start)
    monkeypatch.setattr("app.api.chat_sessions.submit_live_human_input", synthetic_dispatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://m1e-repair") as client:
        denied = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": "blocked"})
        assert denied.status_code == 403 and _detail(denied)["code"] == "agent_execute_denied"
        denied_again = await client.put(assignment, json={"enabled": True, "default_tool_mode": "auto"})
        branch = await client.post(
            f"/api/agents/{employee_id}/sessions/{session_id}/branches",
            json={"mode": "reply", "anchor_event_id": str(anchor_id), "content": "Execute despite denial", "start_run": True},
        )
        regenerate = await client.post(
            f"/api/agents/{employee_id}/sessions/{session_id}/branches",
            json={"mode": "regenerate", "anchor_event_id": str(anchor_id), "start_run": True},
        )
        draft = await client.post(
            f"/api/agents/{employee_id}/sessions/{session_id}/branches",
            json={"mode": "reply", "anchor_event_id": str(anchor_id), "content": "Draft only", "start_run": False},
        )
        stored = await _rows(owner_sessionmaker, "SELECT enabled, default_tool_mode FROM agent_mcp_server_assignments WHERE agent_id = :agent", agent=employee_id)
        assert denied_again.status_code == 403 and stored == [(False, "deny")]
        assert branch.status_code == 403 and regenerate.status_code == 403 and not dispatched, (branch.text, regenerate.text, dispatched)
        assert draft.status_code == 201, draft.text
        branches = await _rows(owner_sessionmaker, "SELECT count(*) FROM chat_sessions WHERE agent_id = :agent AND id <> :session", agent=employee_id, session=session_id)
        assert branches == [(1,)]

        holder["actor"] = _actor(id=owner_id, tenant_id=tenant_id, role="member")
        suspended = await client.patch(
            f"/api/business-teams/{suspended_id}/members/{suspended_membership}",
            json={"status": "suspended"},
        )
        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["visible"] is False and suspended.json()["access"] == "lost"
        assert "members" not in suspended.json()
        left = await client.patch(
            f"/api/business-teams/{team_id}/members/{owner_membership}",
            json={"status": "left"},
        )
        assert left.status_code == 200, left.text
        assert left.json()["visible"] is False and left.json()["access"] == "lost" and "members" not in left.json()
        revoked = await client.delete(f"/api/business-teams/{team_id}/agents/{employee_id}")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["visible"] is False and revoked.json()["access"] == "lost"
        assert revoked.json()["removed_agent_id"] == str(employee_id) and "members" not in revoked.json()

        holder["actor"] = _actor(id=member_id, tenant_id=tenant_id, role="member")
        still_visible = await client.get(f"/api/business-teams/{team_id}")
        assert still_visible.status_code == 200, still_visible.text
        owner_row = next(row for row in still_visible.json()["members"] if row["user_id"] == str(owner_id))
        assert owner_row["status"] == "left"
        assert all(row["agent_id"] != str(employee_id) for row in still_visible.json()["affiliations"])

        holder["actor"] = _actor(id=admin_id, tenant_id=tenant_id, role="org_admin")
        recovered = await client.get(f"/api/business-teams/{team_id}")
        assert recovered.status_code == 200, recovered.text
        assert next(row for row in recovered.json()["members"] if row["user_id"] == str(owner_id))["status"] == "left"
        suspended_view = await client.get(f"/api/business-teams/{suspended_id}")
        assert suspended_view.status_code == 200
        assert next(row for row in suspended_view.json()["members"] if row["user_id"] == str(owner_id))["status"] == "suspended"

    statuses = await _rows(
        owner_sessionmaker,
        "SELECT status FROM business_team_memberships WHERE id = :left_id OR id = :suspended_id ORDER BY status",
        left_id=owner_membership,
        suspended_id=suspended_membership,
    )
    assert statuses == [("left",), ("suspended",)]
    home = await _rows(owner_sessionmaker, "SELECT home_business_team_id::text FROM agents WHERE id = :id", id=employee_id)
    assert home == [(None,)]
    grant = await _rows(
        owner_sessionmaker,
        "SELECT revoked_at IS NULL FROM resource_permissions WHERE principal_id = :team AND resource_id = :agent AND effect = 'allow'",
        team=team_id, agent=employee_id,
    )
    assert grant == [(False,)]


async def _admit_input(owner_sessionmaker, *, member_id, agent_id, session_id, text_value: str):
    from app.models.user import User
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, member_id)
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=agent_id, session_id=session_id, action="mutate_session_input",
        )
        await accept_human_input(db, authority=authority, intent={
            "kind": "start_turn",
            "input_id": str(input_id),
            "idempotency_key": str(input_id),
            "session_id": str(session_id),
            "content_parts": [{"type": "text", "text": text_value}],
        })
        await db.commit()
    async with owner_sessionmaker() as db:
        user = await db.get(User, member_id)
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=agent_id, session_id=session_id, action="mutate_session_input",
        )
        outcome = await run_user_prompt_admission(
            db, authority=authority, input_id=input_id, worker_id=f"m1e-admit:{input_id}",
        )
        assert outcome.state == "admitted", outcome
    return input_id


async def test_denied_execution_cannot_be_recovered_or_revised_into_a_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    """An authorized recovery still runs. A later execute deny does not."""
    tag = uuid.uuid4().hex[:8]
    tenant_id, owner_id, member_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"M1E recovery {tag}", slug=f"m1e-recovery-{tag}"))
        await db.flush()
        _user(db, id=owner_id, username=f"recovery-owner-{tag}", email=f"recovery-owner-{tag}@example.test", tenant_id=tenant_id)
        _user(db, id=member_id, username=f"recovery-member-{tag}", email=f"recovery-member-{tag}@example.test", tenant_id=tenant_id)
        employee = await _agent(db, tenant_id=tenant_id, owner_id=owner_id, name=f"Recovery analyst {tag}")
        await db.commit()
        employee_id = employee.id

    holder = {"actor": _actor(id=owner_id, tenant_id=tenant_id, role="member")}
    async with _client(app_user_sessionmaker, holder) as client:
        created = await client.post("/api/business-teams", json={"name": f"Recovery {tag}"})
        assert created.status_code == 201, created.text
        team_id = created.json()["id"]
        added = await client.post(f"/api/business-teams/{team_id}/members", json={"user_id": str(member_id)})
        assert added.status_code == 201, added.text
        shared = await client.post(f"/api/business-teams/{team_id}/agents", json={"agent_id": str(employee_id)})
        assert shared.status_code == 200, shared.text
        holder["actor"] = _actor(id=member_id, tenant_id=tenant_id, role="member")
        opened = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": f"Recovery work {tag}"})
        assert opened.status_code == 201, opened.text
        session_id = uuid.UUID(opened.json()["id"])

    started = []

    async def no_external_hook(*_args, **_kwargs):
        return None

    async def synthetic_runtime(**kwargs):
        started.append(kwargs["content"])
        return {"run_id": str(kwargs["run_id"]), "status": "synthetic_transport"}

    monkeypatch.setattr("app.services.session_input_admission.emit_hook", no_external_hook)
    monkeypatch.setattr("app.services.web_chat_runtime.start_web_chat_run", synthetic_runtime)

    from app.services.session_input_dispatch import recover_admitted_session_inputs_once

    authorized_id = await _admit_input(
        owner_sessionmaker, member_id=member_id, agent_id=employee_id, session_id=session_id, text_value="Authorized recovery",
    )
    async with owner_sessionmaker() as db:
        authorized = await recover_admitted_session_inputs_once(
            db, worker_id="authorized-recovery", tenant_id=tenant_id, limit=10,
        )
    assert authorized["dispatched"] == 1 and started == ["Authorized recovery"]

    queued_id = await _admit_input(
        owner_sessionmaker, member_id=member_id, agent_id=employee_id, session_id=session_id, text_value="Queued before denial",
    )
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(
            tenant_id=tenant_id, principal_type="business_team", principal_id=uuid.UUID(team_id),
            resource_type="agent", resource_id=employee_id, actions=["execute"], effect="deny",
            conditions={}, purposes=[], created_by_user_id=owner_id,
        ))
        await db.commit()
        started.clear()
        denied = await recover_admitted_session_inputs_once(
            db, worker_id="denied-recovery", tenant_id=tenant_id, limit=10,
        )
        repeated = await recover_admitted_session_inputs_once(
            db, worker_id="denied-recovery-again", tenant_id=tenant_id, limit=10,
        )
    queued = await _rows(
        owner_sessionmaker,
        """
        SELECT i.intent, i.status, a.state, a.dispatch_state, a.dispatch_receipt_json->>'code',
               i.content_parts_json->0->>'text'
        FROM session_turn_inputs i
        JOIN session_input_admissions a ON a.input_id = i.id AND a.input_revision = i.revision
        WHERE i.id = :id
        """,
        id=queued_id,
    )
    assert not started, started
    assert denied["claimed"] == 1 and denied["dispatched"] == 0 and denied["retried"] == 0
    assert repeated == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
    assert queued == [("start_turn", "accepted", "admitted", "pending", "agent_execute_denied", "Queued before denial")]

    draft_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        from app.models.user import User
        from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

        user = await db.get(User, member_id)
        authority = await resolve_session_mutation_authority(
            db, user=user, agent_id=employee_id, session_id=session_id, action="mutate_session_input",
        )
        await accept_human_input(db, authority=authority, intent={
            "kind": "start_turn", "input_id": str(draft_id), "idempotency_key": str(draft_id),
            "session_id": str(session_id), "content_parts": [{"type": "text", "text": "Draft after denial"}],
        })
        await db.commit()
    async with _client(app_user_sessionmaker, holder) as client:
        blocked = await client.post(f"/api/agents/{employee_id}/sessions", json={"title": "Denied control"})
        revised = await client.patch(
            f"/api/agents/{employee_id}/sessions/{session_id}/inputs/{draft_id}",
            headers={"If-Match": "1"},
            json={"content_parts": [{"type": "text", "text": "Run after denial"}]},
        )
        cancelled = await client.delete(
            f"/api/agents/{employee_id}/sessions/{session_id}/inputs/{draft_id}",
            headers={"If-Match": "1"},
        )
    async with owner_sessionmaker() as db:
        started.clear()
        after_revision = await recover_admitted_session_inputs_once(
            db, worker_id="revision-recovery", tenant_id=tenant_id, limit=10,
        )
    draft_input = await _rows(
        owner_sessionmaker,
        "SELECT status, revision FROM session_turn_inputs WHERE id = :id",
        id=draft_id,
    )
    draft_admission = await _rows(
        owner_sessionmaker,
        "SELECT state, input_revision FROM session_input_admissions WHERE input_id = :id ORDER BY input_revision",
        id=draft_id,
    )
    executed = await _rows(
        owner_sessionmaker,
        "SELECT count(*) FROM session_turn_inputs WHERE session_id = :session AND content_parts_json::text LIKE '%Run after denial%'",
        session=session_id,
    )
    assert blocked.status_code == 403 and _detail(blocked)["code"] == "agent_execute_denied"
    assert revised.status_code == 403 and _detail(revised)["code"] == "agent_execute_denied"
    assert cancelled.status_code == 200, cancelled.text
    assert not started and after_revision["dispatched"] == 0
    assert draft_input == [("cancelled", 2)]
    assert draft_admission == [("cancelled", 1)]
    assert executed == [(0,)]
    untouched = await _rows(
        owner_sessionmaker,
        "SELECT intent, status FROM session_turn_inputs WHERE id = :id",
        id=authorized_id,
    )
    assert untouched == [("start_turn", "accepted")]
