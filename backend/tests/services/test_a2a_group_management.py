from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, *, rows=None, scalars=None):
        self._rows = list(rows or [])
        self._scalars = list(scalars or [])

    def all(self):
        return list(self._rows)

    def scalars(self):
        return _ScalarRows(self._scalars)


class _QueueDB:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


def _agent(*, agent_id, owner_id, tenant_id, name):
    return SimpleNamespace(
        id=agent_id,
        owner_user_id=owner_id,
        creator_id=owner_id,
        tenant_id=tenant_id,
        name=name,
        role_description=f"{name} role",
        status="running",
        agent_type="native",
        agent_class="internal_tenant",
    )


def _member(*, member_id, group_id, agent, status, role="member"):
    return SimpleNamespace(
        id=member_id,
        group_id=group_id,
        agent_id=agent.id,
        agent_owner_user_id=agent.owner_user_id,
        role=role,
        status=status,
        invitation_reason="Need domain review",
        capability_scope={"workstream": "launch"},
    )


def test_member_capabilities_keep_target_owner_and_admin_reason_authoritative():
    from app.services.a2a_group_management import member_action_capabilities

    owner_id = uuid4()
    member = SimpleNamespace(agent_owner_user_id=owner_id, status="pending_owner_confirmation")

    owner = SimpleNamespace(id=owner_id, role="member")
    owner_capabilities = member_action_capabilities(owner, member)
    assert owner_capabilities == {
        "can_approve": True,
        "can_reject": True,
        "can_revoke": False,
        "moderation_reason_required": False,
    }

    admin = SimpleNamespace(id=uuid4(), role="org_admin")
    admin_capabilities = member_action_capabilities(admin, member)
    assert admin_capabilities == {
        "can_approve": True,
        "can_reject": True,
        "can_revoke": False,
        "moderation_reason_required": True,
    }

    unrelated = SimpleNamespace(id=uuid4(), role="member")
    assert member_action_capabilities(unrelated, member) == {
        "can_approve": False,
        "can_reject": False,
        "can_revoke": False,
        "moderation_reason_required": False,
    }

    group_owner_member = SimpleNamespace(
        agent_owner_user_id=owner_id,
        status="active",
        role="owner",
    )
    assert member_action_capabilities(owner, group_owner_member) == {
        "can_approve": False,
        "can_reject": False,
        "can_revoke": False,
        "moderation_reason_required": False,
    }

    approval_closed = member_action_capabilities(
        owner,
        member,
        group_accepting_approval=False,
    )
    assert approval_closed["can_approve"] is False
    assert approval_closed["can_reject"] is True


@pytest.mark.asyncio
async def test_management_projection_exposes_pending_members_only_to_the_control_plane():
    from app.services.a2a_group_management import build_a2a_group_management_read_model

    tenant_id = uuid4()
    group_id = uuid4()
    source_owner_id = uuid4()
    target_owner_id = uuid4()
    source = _agent(agent_id=uuid4(), owner_id=source_owner_id, tenant_id=tenant_id, name="Source")
    target = _agent(agent_id=uuid4(), owner_id=target_owner_id, tenant_id=tenant_id, name="Target")
    source_member = _member(
        member_id=uuid4(),
        group_id=group_id,
        agent=source,
        status="active",
        role="owner",
    )
    target_member = _member(
        member_id=uuid4(),
        group_id=group_id,
        agent=target,
        status="pending_owner_confirmation",
    )
    group = SimpleNamespace(
        id=group_id,
        name="Launch room",
        purpose="Cross-owner launch",
        status="active",
        visibility="group_members",
        expires_at=None,
        created_by_agent_id=source.id,
    )
    db = _QueueDB(
        _Result(scalars=[group]),
        _Result(
            rows=[
                (source, source_member, "Source Owner"),
                (target, target_member, "Target Owner"),
            ]
        ),
    )
    current_user = SimpleNamespace(id=target_owner_id, role="member")

    result = await build_a2a_group_management_read_model(db, source_agent=target, current_user=current_user)

    assert result["groups"][0]["can_invite"] is False
    pending = next(member for member in result["groups"][0]["members"] if member["agent_id"] == str(target.id))
    assert pending["member_id"] == str(target_member.id)
    assert pending["status"] == "pending_owner_confirmation"
    assert pending["owner_name"] == "Target Owner"
    assert pending["owner_relation"] == "you"
    assert pending["can_approve"] is True
    assert pending["can_reject"] is True
    assert pending["can_revoke"] is False
    assert "owner_user_id" not in pending


@pytest.mark.asyncio
async def test_management_projection_allows_active_source_member_to_invite_without_approving_other_owner():
    from app.services.a2a_group_management import build_a2a_group_management_read_model

    tenant_id = uuid4()
    group_id = uuid4()
    source_owner_id = uuid4()
    target_owner_id = uuid4()
    source = _agent(agent_id=uuid4(), owner_id=source_owner_id, tenant_id=tenant_id, name="Source")
    target = _agent(agent_id=uuid4(), owner_id=target_owner_id, tenant_id=tenant_id, name="Target")
    source_member = _member(
        member_id=uuid4(),
        group_id=group_id,
        agent=source,
        status="active",
        role="owner",
    )
    target_member = _member(
        member_id=uuid4(),
        group_id=group_id,
        agent=target,
        status="pending_owner_confirmation",
    )
    group = SimpleNamespace(
        id=group_id,
        name="Launch room",
        purpose="Cross-owner launch",
        status="active",
        visibility="group_members",
        expires_at=None,
        created_by_agent_id=source.id,
    )
    db = _QueueDB(
        _Result(scalars=[group]),
        _Result(
            rows=[
                (source, source_member, "Source Owner"),
                (target, target_member, "Target Owner"),
            ]
        ),
    )
    current_user = SimpleNamespace(id=source_owner_id, role="member")

    result = await build_a2a_group_management_read_model(db, source_agent=source, current_user=current_user)

    group_payload = result["groups"][0]
    assert group_payload["can_invite"] is True
    pending = next(member for member in group_payload["members"] if member["agent_id"] == str(target.id))
    assert pending["owner_relation"] == "another_owner"
    assert pending["can_approve"] is False
    assert pending["can_reject"] is False


@pytest.mark.asyncio
async def test_invite_candidate_search_is_bounded_and_marks_reinvite_state():
    from app.services.a2a_group_management import search_a2a_invite_candidates

    tenant_id = uuid4()
    owner_id = uuid4()
    source = _agent(agent_id=uuid4(), owner_id=owner_id, tenant_id=tenant_id, name="Source")
    candidate = _agent(agent_id=uuid4(), owner_id=uuid4(), tenant_id=tenant_id, name="Risk Reviewer")
    revoked_member = SimpleNamespace(id=uuid4(), status="revoked")
    db = _QueueDB(_Result(rows=[(candidate, "Risk Owner", revoked_member)]))

    result = await search_a2a_invite_candidates(
        db,
        source_agent=source,
        group_id=uuid4(),
        current_user=SimpleNamespace(id=owner_id, role="member"),
        query="risk",
        limit=20,
    )

    assert result == [
        {
            "agent_id": str(candidate.id),
            "name": "Risk Reviewer",
            "role_description": "Risk Reviewer role",
            "status": "running",
            "owner_name": "Risk Owner",
            "owner_relation": "another_owner",
            "membership_status": "revoked",
            "invite_action": "reinvite",
        }
    ]
