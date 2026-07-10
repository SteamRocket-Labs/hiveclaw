from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, session):
        self.session = session

    async def execute(self, _statement):
        return _ScalarResult(self.session)


@pytest.mark.asyncio
async def test_authorize_session_action_binds_shared_agent_use_to_session_owner(monkeypatch):
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    user_id = uuid4()
    session = SimpleNamespace(id=uuid4(), agent_id=agent_id, user_id=user_id)
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(permissions_module, "check_agent_access", fake_agent_access)

    decision = await permissions_module.authorize_session_action(
        _DB(session),
        user,
        agent_id=agent_id,
        session_id=session.id,
        action="goal:start",
    )

    assert decision.agent is agent
    assert decision.session is session
    assert decision.access_level == "use"
    assert decision.authority_source == "session_owner"


@pytest.mark.asyncio
async def test_authorize_session_action_rejects_other_users_session(monkeypatch):
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    session = SimpleNamespace(id=uuid4(), agent_id=agent_id, user_id=uuid4())
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="member")

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(permissions_module, "check_agent_access", fake_agent_access)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.authorize_session_action(
            _DB(session),
            user,
            agent_id=agent_id,
            session_id=session.id,
            action="plan:confirm",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "This session belongs to a different user"


@pytest.mark.asyncio
async def test_authorize_session_action_manager_override_requires_reason_and_is_audited(monkeypatch):
    import app.core.permissions as permissions_module

    agent_id = uuid4()
    session = SimpleNamespace(id=uuid4(), agent_id=agent_id, user_id=uuid4())
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), role="org_admin")
    audit_calls = []

    async def fake_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_audit(action, details=None, agent_id=None, user_id=None):
        audit_calls.append((action, details, agent_id, user_id))

    monkeypatch.setattr(permissions_module, "check_agent_access", fake_agent_access)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_audit)

    with pytest.raises(HTTPException) as exc:
        await permissions_module.authorize_session_action(
            _DB(session),
            user,
            agent_id=agent_id,
            session_id=session.id,
            action="team:close",
            allow_manager_override=True,
        )
    assert exc.value.status_code == 403

    decision = await permissions_module.authorize_session_action(
        _DB(session),
        user,
        agent_id=agent_id,
        session_id=session.id,
        action="team:close",
        allow_manager_override=True,
        manager_override_reason="Incident response requested by the session owner",
    )

    assert decision.authority_source == "manager_override"
    assert audit_calls == [
        (
            "session_authority_override",
            {
                "session_id": str(session.id),
                "session_user_id": str(session.user_id),
                "action": "team:close",
                "reason": "Incident response requested by the session owner",
                "authority_source": "manager_override",
            },
            agent_id,
            user.id,
        )
    ]
