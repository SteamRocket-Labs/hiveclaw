"""PDEC-013 administrator session inventory: ``scope=all`` without operator ritual.

A scoped business administrator lists the managed session inventory as
themselves — no manual operator reason, no ``operator.inspect`` grant — with
one audited collection decision recording the real actor and scope. Employees
keep the exact audited operator lane.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests.api.test_chat_sessions_permissions import _QueryAwareDB


def _session_row(session_id, agent_id, owner_id):
    return SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Employee Thread",
        created_at=SimpleNamespace(isoformat=lambda: "2026-09-05T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-09-05T00:10:00+00:00"),
        peer_agent_id=None,
    )


@pytest.mark.parametrize("admin_role", ["org_admin", "platform_admin"])
@pytest.mark.asyncio
async def test_scoped_admin_lists_all_sessions_without_operator_reason(monkeypatch, admin_role):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    other_owner_id = uuid4()
    admin_id = uuid4()
    session_id = uuid4()
    empty_session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id, tenant_id=tenant_id)
    session = _session_row(session_id, agent_id, owner_id)
    # A zero-message session owned by another employee is hidden from the
    # listing, so its owner must not leak into the audited target set either:
    # the collection event names the sessions actually exposed.
    empty_session = _session_row(empty_session_id, agent_id, other_owner_id)
    admin = SimpleNamespace(id=admin_id, role=admin_role, tenant_id=tenant_id)
    db = _QueryAwareDB(
        agent=agent,
        sessions=[session, empty_session],
        message_counts={session_id: 2},
        users={owner_id: "Owner"},
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(
        chat_sessions_api,
        "check_agent_operator_reachability",
        fake_check_agent_access,
        raising=False,
    )
    audit_calls = []

    async def fake_write_audit_event(_db, **kwargs):
        audit_calls.append(kwargs)
        return None

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=admin,
        db=db,
    )

    # Only the non-empty employee session is exposed, as a normal business
    # view: no operator projection, no manual operator reason.
    assert len(result) == 1
    assert result[0].id == str(session_id)
    assert result[0].authority_source == "scoped_business_admin"
    assert result[0].operator_view is False

    # One collection event per request with the same schema the per-session
    # writer and the other collection writers already use: real actor,
    # selected tenant, Agent, explicit outcome, and the deduplicated real
    # target set of the sessions actually exposed by scope=all.
    assert [call["event_type"] for call in audit_calls] == ["session.scoped_business_admin_access"]
    event = audit_calls[0]
    assert str(event["actor_id"]) == str(admin_id)
    assert str(event["tenant_id"]) == str(tenant_id)
    assert event["action"] == "chat_session_collection:read"
    details = event["details"]
    assert details["agent_id"] == str(agent_id)
    assert details["actor_role"] == admin_role
    assert details["authority_source"] == "scoped_business_admin"
    assert details["outcome"] == "allowed"
    assert details["target_user_ids"] == [str(owner_id)]
    assert details["target_count"] == 1
    assert details["session_user_id"] == str(owner_id)
    assert "operator_reason" not in details


@pytest.mark.asyncio
async def test_platform_admin_scope_requires_the_selected_company():
    tenant_id = uuid4()
    # The exact resource-scope predicate itself: an organization administrator
    # is scoped inside their own company; a platform administrator is scoped
    # only inside the authenticated selected company (PDEC-013). A tenantless
    # platform identity — no ``X-Tenant-Id`` selection, no home company equal
    # to the resource tenant — has no business scope at all and must select a
    # company first.
    from app.core.permissions import is_scoped_business_admin

    assert (
        is_scoped_business_admin(SimpleNamespace(role="org_admin", tenant_id=uuid4()), resource_tenant_id=tenant_id)
        is False
    )
    assert (
        is_scoped_business_admin(SimpleNamespace(role="member", tenant_id=tenant_id), resource_tenant_id=tenant_id)
        is False
    )
    # Unselected/home-company-foreign platform administrator: no scope.
    assert (
        is_scoped_business_admin(SimpleNamespace(role="platform_admin", tenant_id=None), resource_tenant_id=tenant_id)
        is False
    )
    assert (
        is_scoped_business_admin(
            SimpleNamespace(role="platform_admin", tenant_id=uuid4()), resource_tenant_id=tenant_id
        )
        is False
    )
    # The authenticated selected company is the one scope a platform admin holds.
    assert (
        is_scoped_business_admin(
            SimpleNamespace(role="platform_admin", tenant_id=tenant_id), resource_tenant_id=tenant_id
        )
        is True
    )
    assert (
        is_scoped_business_admin(SimpleNamespace(role="org_admin", tenant_id=tenant_id), resource_tenant_id=tenant_id)
        is True
    )


@pytest.mark.asyncio
async def test_employee_all_scope_still_requires_audited_operator_view(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id, tenant_id=tenant_id)
    session = _session_row(session_id, agent_id, owner_id)
    viewer = SimpleNamespace(id=viewer_id, role="member", tenant_id=tenant_id)
    db = _QueryAwareDB(agent=agent, sessions=[session], message_counts={session_id: 2}, users={owner_id: "Owner"})

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(
        chat_sessions_api,
        "check_agent_operator_reachability",
        fake_check_agent_access,
        raising=False,
    )

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.list_sessions(
            agent_id=agent_id,
            scope="all",
            operator_reason=None,
            current_user=viewer,
            db=db,
        )
    assert exc.value.status_code == 403
