from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        values = self._value if isinstance(self._value, list) else [self._value]
        return SimpleNamespace(all=lambda: values)


class _DB:
    def __init__(self, session=None):
        self.session = session
        self.added: list = []

    async def execute(self, _statement):
        return _ScalarResult(self.session)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_resource_authority_accepts_owner_session_and_explicit_grant(monkeypatch):
    import app.core.resource_authority as authority

    agent_id = uuid4()
    owner_id = uuid4()
    grantee_id = uuid4()
    session_id = uuid4()
    resource_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=grantee_id)

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "use"

    grant_actions: list[tuple[str, str]] = []

    async def fake_permission(_db, **kwargs):
        grant_actions.append((kwargs["resource_type"], kwargs["action"]))
        return kwargs["principal_id"] == grantee_id and kwargs["action"] == "read"

    monkeypatch.setattr(authority, "check_agent_access", fake_agent_access)
    monkeypatch.setattr(authority, "check_agent_operator_reachability", fake_agent_access)
    monkeypatch.setattr(authority, "check_permission", fake_permission)

    owner = SimpleNamespace(id=owner_id, role="member", tenant_id=agent.tenant_id, department_id=None)
    owner_decision = await authority.authorize_resource_action(
        _DB(),
        owner,
        agent_id=agent_id,
        resource_kind="task",
        resource_id=resource_id,
        action="read",
        owner_user_id=owner_id,
    )
    assert owner_decision.authority_source == "resource_owner"
    assert owner_decision.operator_view is False

    grantee = SimpleNamespace(id=grantee_id, role="member", tenant_id=agent.tenant_id, department_id=None)
    session_decision = await authority.authorize_resource_action(
        _DB(session),
        grantee,
        agent_id=agent_id,
        resource_kind="task",
        resource_id=resource_id,
        action="write",
        owner_user_id=owner_id,
        root_session_id=session_id,
    )
    assert session_decision.authority_source == "root_session_owner"

    grant_decision = await authority.authorize_resource_action(
        _DB(),
        grantee,
        agent_id=agent_id,
        resource_kind="task",
        resource_id=resource_id,
        action="read",
        owner_user_id=owner_id,
    )
    assert grant_decision.authority_source == "resource_grant"
    assert ("task", "read") in grant_actions


@pytest.mark.asyncio
async def test_resource_authority_quarantines_unknown_legacy_and_audits_explicit_operator(monkeypatch):
    import app.core.resource_authority as authority

    agent_id = uuid4()
    resource_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    manager = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id, department_id=None)
    inspection_calls: list[dict] = []

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_permission(*_args, **_kwargs):
        return False

    monkeypatch.setattr(authority, "check_agent_access", fake_agent_access)
    monkeypatch.setattr(authority, "check_agent_operator_reachability", fake_agent_access)
    monkeypatch.setattr(authority, "check_permission", fake_permission)

    # PDEC-013: a scoped business administrator resolves quarantined business
    # resources by role; an ordinary member stays fail-closed and needs the
    # audited operator lane.
    member = SimpleNamespace(id=uuid4(), role="member", tenant_id=agent.tenant_id, department_id=None)
    scoped_decision = await authority.authorize_resource_action(
        _DB(),
        manager,
        agent_id=agent_id,
        resource_kind="workspace_file",
        resource_id=resource_id,
        action="read",
        owner_user_id=None,
        authority_state="quarantined",
    )
    assert scoped_decision.authority_source == "scoped_business_admin"

    for kwargs in (
        {},
        {"allow_manager_override": True},
        {"allow_manager_override": True, "manager_override_reason": " "},
    ):
        with pytest.raises(HTTPException) as exc:
            await authority.authorize_resource_action(
                _DB(),
                member,
                agent_id=agent_id,
                resource_kind="workspace_file",
                resource_id=resource_id,
                action="read",
                owner_user_id=None,
                authority_state="quarantined",
                **kwargs,
            )
        assert exc.value.status_code == 403

    async def fake_operator_inspection(_db, **kwargs):
        inspection_calls.append(kwargs)
        return "operator_inspect_grant"

    monkeypatch.setattr(authority, "authorize_agent_operator_inspection", fake_operator_inspection)

    # Administrator authority takes precedence over the optional operator
    # projection (CC R1 / Codex item 3): an administrator who passes
    # operator-view inputs keeps scoped business authority — no 403, no grant
    # requirement — and the audit records the ignored operator request.
    admin_decision = await authority.authorize_resource_action(
        _DB(),
        manager,
        agent_id=agent_id,
        resource_kind="workspace_file",
        resource_id=resource_id,
        action="read",
        authority_state="quarantined",
        allow_manager_override=True,
        manager_override_reason="Incident export approved by the tenant owner",
    )
    assert admin_decision.authority_source == "scoped_business_admin"
    assert admin_decision.operator_view is False

    # The audited operator lane itself stays the ordinary-member inspection
    # path for cross-owner reads.
    decision = await authority.authorize_resource_action(
        _DB(),
        member,
        agent_id=agent_id,
        resource_kind="workspace_file",
        resource_id=resource_id,
        action="read",
        authority_state="quarantined",
        allow_manager_override=True,
        manager_override_reason="Incident export approved by the tenant owner",
    )

    assert decision.authority_source == "operator_inspect_grant"
    assert decision.operator_view is True
    assert inspection_calls[0]["resource_type"] == "workspace_file"
    assert inspection_calls[0]["details"]["authority_state"] == "quarantined"


def test_workspace_resource_id_is_stable_and_path_normalized():
    from app.core.resource_authority import workspace_resource_id

    agent_id = uuid4()
    assert workspace_resource_id(agent_id, "/workspace\\reports/./final.md") == workspace_resource_id(
        agent_id, "workspace/reports/final.md"
    )


@pytest.mark.asyncio
async def test_explicit_resource_grants_are_loaded_once_with_canonical_conditions():
    from app.core.resource_authority import load_explicit_resource_grant_ids

    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, department_id=uuid4())
    readable_id = uuid4()
    denied_id = uuid4()
    environment_blocked_id = uuid4()
    wrong_action_id = uuid4()
    permissions = [
        SimpleNamespace(
            resource_id=readable_id,
            actions=["read"],
            conditions={},
            effect="allow",
            revoked_at=None,
            expires_at=None,
        ),
        SimpleNamespace(
            resource_id=readable_id,
            actions=["read"],
            conditions={},
            effect="deny",
            revoked_at=None,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        SimpleNamespace(
            resource_id=denied_id,
            actions=["read"],
            conditions={},
            effect="allow",
            revoked_at=None,
            expires_at=None,
        ),
        SimpleNamespace(
            resource_id=denied_id,
            actions=["read"],
            conditions={},
            effect="deny",
            revoked_at=None,
            expires_at=None,
        ),
        SimpleNamespace(
            resource_id=environment_blocked_id,
            actions=["read"],
            conditions={"environment": "production"},
        ),
        SimpleNamespace(resource_id=wrong_action_id, actions=["write"], conditions={}),
    ]

    class _GrantDB:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return _ScalarResult(permissions)

    db = _GrantDB()
    granted = await load_explicit_resource_grant_ids(
        db,
        user=user,
        resource_kind="agent_activity",
        action="read",
    )

    assert db.calls == 1
    assert granted == {readable_id}


@pytest.mark.asyncio
async def test_platform_admin_resource_grants_use_only_the_exact_user_principal():
    from app.core.resource_authority import load_explicit_resource_grant_ids

    tenant_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        department_id=uuid4(),
        role="platform_admin",
    )

    class _GrantDB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _ScalarResult([])

    db = _GrantDB()
    await load_explicit_resource_grant_ids(
        db,
        user=user,
        resource_kind="agent_activity",
        action="read",
    )

    sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "resource_permissions.principal_type = 'user'" in sql
    assert "resource_permissions.principal_type = 'department'" not in sql


@pytest.mark.asyncio
async def test_filter_authorized_resources_hides_foreign_rows_and_operator_view_is_explicit(monkeypatch):
    import app.core.resource_authority as authority

    agent_id = uuid4()
    user_id = uuid4()
    other_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member", tenant_id=agent.tenant_id, department_id=None)
    rows = [
        SimpleNamespace(id=uuid4(), owner_user_id=user_id, root_session_id=None, authority_state="owned"),
        SimpleNamespace(id=uuid4(), owner_user_id=other_id, root_session_id=None, authority_state="owned"),
        SimpleNamespace(id=uuid4(), owner_user_id=None, root_session_id=None, authority_state="quarantined"),
    ]

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_permission(*_args, **_kwargs):
        return False

    monkeypatch.setattr(authority, "check_agent_access", fake_agent_access)
    monkeypatch.setattr(authority, "check_permission", fake_permission)

    visible = await authority.filter_authorized_resources(
        _DB(),
        user,
        agent_id=agent_id,
        resource_kind="task",
        action="read",
        resources=rows,
        agent_access=(agent, "use"),
    )
    assert [row.id for row, _decision in visible] == [rows[0].id]
    assert visible[0][1].authority_source == "resource_owner"

    manager = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=agent.tenant_id, department_id=None)
    inspection_calls = []

    async def fake_operator_inspection(_db, **kwargs):
        inspection_calls.append(kwargs)
        return "operator_inspect_grant"

    monkeypatch.setattr(authority, "authorize_agent_operator_inspection", fake_operator_inspection)

    # Administrator precedence (CC R1 / Codex item 3): an administrator's
    # explicit operator-view request still exposes the managed inventory, but
    # every decision carries the real scoped administrator authority and the
    # access is audited once per collection, not per row.
    admin_operator_db = _DB()
    operator_rows = await authority.filter_authorized_resources(
        admin_operator_db,
        manager,
        agent_id=agent_id,
        resource_kind="task",
        action="read",
        resources=rows,
        operator_view=True,
        operator_reason="Tenant task administration",
        agent_access=(agent, "manage"),
    )
    assert [row.id for row, _decision in operator_rows] == [row.id for row in rows]
    assert all(decision.authority_source == "scoped_business_admin" for _row, decision in operator_rows)
    assert all(decision.operator_view is False for _row, decision in operator_rows)
    assert len(admin_operator_db.added) == 1
    assert len(inspection_calls) == 0

    # The operator projection itself stays the ordinary-member inspection lane.
    member_operator_rows = await authority.filter_authorized_resources(
        _DB(),
        user,
        agent_id=agent_id,
        resource_kind="task",
        action="read",
        resources=rows,
        operator_view=True,
        operator_reason="Tenant task administration",
        agent_access=(agent, "use"),
    )
    assert [row.id for row, _decision in member_operator_rows] == [row.id for row in rows]
    assert all(decision.operator_view for _row, decision in member_operator_rows)
    assert len(inspection_calls) == 1
