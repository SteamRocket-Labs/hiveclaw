from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.a2a as a2a_mod
from app.api.a2a import router as a2a_router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


class _MutationDB(_FakeDB):
    def __init__(self, *, group=None, member=None):
        self.group = group
        self.member = member
        self.committed = False

    async def get(self, model, object_id):
        if model is a2a_mod.AgentCollaborationGroup and self.group is not None and self.group.id == object_id:
            return self.group
        if model is a2a_mod.AgentCollaborationGroupMember and self.member is not None and self.member.id == object_id:
            return self.member
        return None

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _WriteDB(_MutationDB):
    def __init__(self, *, group=None, member=None, execute_values=()):
        super().__init__(group=group, member=member)
        self.execute_values = list(execute_values)
        self.added = []

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid4()
        self.added.append(value)

    async def execute(self, _stmt):
        if not self.execute_values:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self.execute_values.pop(0))


def _client(*, db=None, user=None):
    app = FastAPI()
    app.include_router(a2a_router)
    current_user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)
    fake_db = db or _FakeDB()

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_get_a2a_collaborators_uses_canonical_read_model(monkeypatch):
    client, fake_db, current_user = _client()
    agent_id = uuid4()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    async def fake_read_model(db_session, target_agent_id):
        assert db_session is fake_db
        assert target_agent_id == agent_id
        return {
            "agent_id": str(agent_id),
            "same_owner_agents": [{"id": str(uuid4()), "name": "Same Owner", "relation": "same_owner"}],
            "public_agents": [{"id": str(uuid4()), "name": "Public Agent", "relation": "public_agent"}],
            "collaboration_groups": [
                {
                    "group_id": str(uuid4()),
                    "group_name": "Research Pod",
                    "members": [{"id": str(uuid4()), "name": "Group Peer", "relation": "group_member"}],
                }
            ],
        }

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "build_a2a_collaboration_read_model", fake_read_model)

    response = client.get(f"/agents/{agent_id}/a2a/collaborators")

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == str(agent_id)
    assert body["same_owner_agents"][0]["name"] == "Same Owner"
    assert body["public_agents"][0]["name"] == "Public Agent"
    assert body["collaboration_groups"][0]["members"][0]["name"] == "Group Peer"


def test_system_hr_cannot_create_a2a_group(monkeypatch):
    client, fake_db, current_user = _client()
    agent_id = uuid4()

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id, name="__system_hr__", agent_class="internal_system"), "manage"

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)

    response = client.post(f"/agents/{agent_id}/a2a/groups", json={"name": "HR should not A2A"})

    assert response.status_code == 403
    assert response.json()["detail"] == "System HR cannot create A2A groups"


def test_invitation_rejects_unrecognized_group_role_before_database_access():
    client, _fake_db, _current_user = _client()

    response = client.post(
        f"/agents/{uuid4()}/a2a/groups/{uuid4()}/members",
        json={
            "target_agent_id": str(uuid4()),
            "role": "owner",
            "invitation_reason": "A caller cannot mint another group owner",
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_group_create_writes_audit_in_the_request_transaction(monkeypatch):
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source",
        tenant_id=tenant_id,
        owner_user_id=user.id,
        creator_id=user.id,
        agent_class="internal_tenant",
    )
    db = _WriteDB()
    audits = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    async def fake_audit(_db, **payload):
        audits.append(payload)

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "write_audit_event", fake_audit)
    monkeypatch.setattr(a2a_mod, "mark_agent_dirty", lambda _agent_id: None)

    response = await a2a_mod.create_a2a_group(
        source_agent.id,
        a2a_mod.CollaborationGroupCreateIn(name="Launch room", purpose="Ship together"),
        user,
        db,
    )

    assert response["status"] == "ok"
    assert db.committed is True
    assert audits[0]["event_type"] == "a2a_group_created"
    assert audits[0]["resource_id"] == db.added[0].id
    assert audits[0]["details"]["owner_membership_id"] == str(db.added[1].id)


@pytest.mark.asyncio
async def test_cross_owner_reinvite_clears_stale_approval_and_writes_audit(monkeypatch):
    tenant_id = uuid4()
    source_owner_id = uuid4()
    target_owner_id = uuid4()
    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source",
        tenant_id=tenant_id,
        owner_user_id=source_owner_id,
        creator_id=source_owner_id,
        agent_class="internal_tenant",
    )
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target",
        tenant_id=tenant_id,
        owner_user_id=target_owner_id,
        creator_id=target_owner_id,
        agent_class="internal_tenant",
        status="running",
        deleted_at=None,
    )
    group = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, status="active")
    source_membership = SimpleNamespace(status="active")
    stale_member = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        group_id=group.id,
        agent_id=target_agent.id,
        agent_owner_user_id=target_owner_id,
        role="member",
        status="revoked",
        invited_by_user_id=None,
        invited_by_agent_id=None,
        invitation_reason="",
        capability_scope={},
        approved_by_user_id=uuid4(),
        approved_at=datetime.now(timezone.utc),
        rejected_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc),
    )
    db = _WriteDB(
        group=group,
        execute_values=(source_membership, target_agent, stale_member),
    )
    user = SimpleNamespace(id=source_owner_id, role="member", tenant_id=tenant_id)
    audits = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    async def fake_audit(_db, **payload):
        audits.append(payload)

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "write_audit_event", fake_audit)
    monkeypatch.setattr(a2a_mod, "mark_agent_dirty", lambda _agent_id: None)

    response = await a2a_mod.invite_a2a_group_member(
        source_agent.id,
        group.id,
        a2a_mod.CollaborationGroupInviteIn(
            target_agent_id=str(target_agent.id),
            role="specialist",
            invitation_reason="Need risk review",
        ),
        user,
        db,
    )

    assert response["member_status"] == "pending_owner_confirmation"
    assert response["requires_owner_confirmation"] is True
    assert stale_member.approved_by_user_id is None
    assert stale_member.approved_at is None
    assert stale_member.rejected_at is None
    assert stale_member.revoked_at is None
    assert audits[0]["event_type"] == "a2a_group_member_invited"
    assert audits[0]["details"]["requires_owner_confirmation"] is True
    assert db.committed is True


def test_management_projection_is_separate_from_callable_collaborators(monkeypatch):
    client, fake_db, current_user = _client()
    agent_id = uuid4()
    source_agent = SimpleNamespace(
        id=agent_id,
        name="Managed Agent",
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        creator_id=current_user.id,
        agent_class="internal_tenant",
    )

    async def fake_check_agent_access(db_session, user, target_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert target_agent_id == agent_id
        return source_agent, "manage"

    async def fake_management(db_session, *, source_agent: object, current_user: object):
        assert db_session is fake_db
        assert source_agent is managed_source_agent
        assert current_user is managed_user
        return {"groups": [{"group_id": str(uuid4()), "members": []}]}

    managed_source_agent = source_agent
    managed_user = current_user
    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        a2a_mod,
        "build_a2a_group_management_read_model",
        fake_management,
        raising=False,
    )

    response = client.get(f"/agents/{agent_id}/a2a/management")

    assert response.status_code == 200
    assert len(response.json()["groups"]) == 1


def test_use_only_viewer_cannot_read_a2a_management_projection(monkeypatch):
    client, fake_db, current_user = _client()
    agent_id = uuid4()

    async def fake_check_agent_access(_db_session, _user, _target_agent_id):
        return SimpleNamespace(id=agent_id, agent_class="internal_tenant"), "use"

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)

    response = client.get(f"/agents/{agent_id}/a2a/management")

    assert response.status_code == 403
    assert response.json()["detail"] == "Manage access required"


def _moderation_fixture(
    *,
    member_status: str,
    user_role: str = "member",
    user_is_owner: bool = True,
    member_role: str = "member",
    expires_at=None,
):
    tenant_id = uuid4()
    user_id = uuid4()
    source_agent_id = uuid4()
    group_id = uuid4()
    member_id = uuid4()
    member_owner_id = user_id if user_is_owner else uuid4()
    source_agent = SimpleNamespace(
        id=source_agent_id,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        creator_id=user_id,
        agent_class="internal_tenant",
    )
    group = SimpleNamespace(
        id=group_id,
        tenant_id=tenant_id,
        status="active",
        expires_at=expires_at,
    )
    member = SimpleNamespace(
        id=member_id,
        tenant_id=tenant_id,
        group_id=group_id,
        agent_id=source_agent_id,
        agent_owner_user_id=member_owner_id,
        status=member_status,
        role=member_role,
        metadata_json=None,
        approved_by_user_id=None,
        approved_at=None,
        rejected_at=None,
        revoked_at=None,
    )
    user = SimpleNamespace(id=user_id, role=user_role, tenant_id=tenant_id, is_active=True)
    db = _MutationDB(group=group, member=member)
    client, _, _ = _client(db=db, user=user)
    return client, db, user, source_agent, group, member


def test_target_owner_approval_writes_canonical_audit_and_commits(monkeypatch):
    client, db, user, source_agent, group, member = _moderation_fixture(member_status="pending_owner_confirmation")
    audit_calls = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    async def fake_audit(_db, **payload):
        audit_calls.append(payload)

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "write_audit_event", fake_audit, raising=False)
    monkeypatch.setattr(a2a_mod, "mark_agent_dirty", lambda _agent_id: None)

    response = client.post(
        f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/approve",
        json={"reason": "Approved for launch"},
    )

    assert response.status_code == 200
    assert member.status == "active"
    assert db.committed is True
    assert audit_calls[0]["event_type"] == "a2a_group_member_approved"
    assert audit_calls[0]["actor_id"] == user.id
    assert audit_calls[0]["tenant_id"] == source_agent.tenant_id
    assert audit_calls[0]["resource_id"] == member.id


def test_admin_moderation_requires_an_audit_reason(monkeypatch):
    client, _db, _user, source_agent, group, member = _moderation_fixture(
        member_status="pending_owner_confirmation",
        user_role="org_admin",
        user_is_owner=False,
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)

    response = client.post(
        f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/approve",
        json={"reason": ""},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Admin moderation reason required"


def test_moderation_path_agent_must_be_the_target_member(monkeypatch):
    client, _db, _user, source_agent, group, member = _moderation_fixture(member_status="pending_owner_confirmation")
    source_agent.id = uuid4()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)

    response = client.post(
        f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/approve",
        json={"reason": "Wrong path"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "A2A group member not found"


def test_member_status_transitions_fail_closed(monkeypatch):
    scenarios = (
        ("approve", "active"),
        ("reject", "rejected"),
        ("revoke", "pending_owner_confirmation"),
    )

    for action, initial_status in scenarios:
        client, _db, _user, source_agent, group, member = _moderation_fixture(member_status=initial_status)

        async def fake_check_agent_access(_db, _user, _agent_id):
            return source_agent, "manage"

        monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
        response = client.post(
            f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/{action}",
            json={"reason": "Invalid transition"},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == f"Cannot {action} A2A member from status {initial_status}"


def test_expired_group_cannot_accept_a_pending_member(monkeypatch):
    client, _db, _user, source_agent, group, member = _moderation_fixture(
        member_status="pending_owner_confirmation",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    response = client.post(
        f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/approve",
        json={"reason": "Too late"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A2A Collaboration Group is not accepting approvals"


def test_group_owner_membership_cannot_be_revoked(monkeypatch):
    client, _db, _user, source_agent, group, member = _moderation_fixture(
        member_status="active",
        member_role="owner",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return source_agent, "manage"

    async def fake_audit(_db, **_payload):
        return None

    monkeypatch.setattr(a2a_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(a2a_mod, "write_audit_event", fake_audit)
    monkeypatch.setattr(a2a_mod, "mark_agent_dirty", lambda _agent_id: None)
    response = client.post(
        f"/agents/{source_agent.id}/a2a/groups/{group.id}/members/{member.id}/revoke",
        json={"reason": "Would orphan the group"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A2A group owner membership cannot be revoked"
