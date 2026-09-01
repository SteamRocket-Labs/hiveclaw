from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, session) -> None:
        self.session = session

    async def execute(self, _statement):
        return _ScalarResult(self.session)


@pytest.mark.asyncio
async def test_delegation_run_rejects_server_side_mutation_for_owner(monkeypatch) -> None:
    from app.core.permissions import authorize_session_action

    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4())
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user.id,
        session_kind="delegation_run",
        runtime_source="delegation",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await authorize_session_action(
            _DB(session),
            user,
            agent_id=agent.id,
            session_id=session.id,
            action="start_session_run",
            require_writable=True,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "session_read_only",
        "session_kind": "delegation_run",
        "action": "start_session_run",
    }


@pytest.mark.asyncio
async def test_delegation_run_remains_readable_and_normal_session_remains_writable(monkeypatch) -> None:
    from app.core.permissions import authorize_session_action

    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4())
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr("app.core.permissions.check_agent_access", fake_check_agent_access)

    read_only_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user.id,
        session_kind="delegation_run",
        runtime_source="delegation",
    )
    read_decision = await authorize_session_action(
        _DB(read_only_session),
        user,
        agent_id=agent.id,
        session_id=read_only_session.id,
        action="read_session_transcript",
        require_writable=False,
    )
    assert read_decision.session is read_only_session

    normal_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=user.id,
        session_kind="chat",
        runtime_source="web_chat",
    )
    write_decision = await authorize_session_action(
        _DB(normal_session),
        user,
        agent_id=agent.id,
        session_id=normal_session.id,
        action="start_session_run",
        require_writable=True,
    )
    assert write_decision.session is normal_session


@pytest.mark.asyncio
async def test_manager_override_cannot_turn_peer_a2a_session_into_a_writable_session(monkeypatch) -> None:
    from app.core.permissions import authorize_session_action

    tenant_id = uuid4()
    session_owner_id = uuid4()
    manager = SimpleNamespace(id=uuid4())
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=session_owner_id,
        session_kind="delegation_run",
        runtime_source="delegation",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr("app.core.permissions.check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await authorize_session_action(
            _DB(session),
            manager,
            agent_id=agent.id,
            session_id=session.id,
            action="delete_session",
            allow_manager_override=True,
            manager_override_reason="incident inspection",
            require_writable=True,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "This session belongs to a different user"


@pytest.mark.asyncio
async def test_read_only_kind_is_not_disclosed_before_session_authority(monkeypatch) -> None:
    from app.core.permissions import authorize_session_action

    tenant_id = uuid4()
    owner_id = uuid4()
    outsider = SimpleNamespace(id=uuid4())
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=owner_id,
        session_kind="delegation_run",
        runtime_source="delegation",
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr("app.core.permissions.check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc_info:
        await authorize_session_action(
            _DB(session),
            outsider,
            agent_id=agent.id,
            session_id=session.id,
            action="start_session_run",
            require_writable=True,
        )

    assert exc_info.value.status_code == 403
