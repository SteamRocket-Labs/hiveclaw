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

    def scalar(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def scalar(self):
        return self._values


class _QueryAwareDB:
    def __init__(self, *, agent=None, sessions=None, messages=None, counts=None, users=None):
        self.agent = agent
        self.sessions = sessions or []
        self.messages = messages or []
        self.counts = list(counts or [])
        self.users = users or {}
        self.added = []
        self.deleted = []
        self.commits = 0

    async def execute(self, stmt):
        sql = str(stmt)
        if "count(chat_messages.id)" in sql:
            if not self.counts:
                raise AssertionError("No count prepared")
            return _ScalarResult(self.counts.pop(0))
        if "FROM chat_sessions" in sql:
            if "WHERE chat_sessions.id =" in sql:
                return _ScalarResult(self.sessions[0] if self.sessions else None)
            return _ListResult(self.sessions)
        if "FROM chat_messages" in sql:
            return _ListResult(self.messages)
        if "coalesce(users.display_name, users.username)" in sql:
            session = self.sessions[0]
            return _ScalarResult(self.users.get(session.user_id, "Unknown"))
        if "FROM agents" in sql:
            return _ScalarResult(self.agent)
        raise AssertionError(f"Unhandled SQL in fake DB: {sql}")

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


@pytest.mark.asyncio
async def test_list_sessions_uses_check_agent_access_for_mine_scope(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _QueryAwareDB(agent=agent, sessions=[])
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="mine",
        current_user=current_user,
        db=db,
    )

    assert result == []
    assert called["args"] == (db, current_user, agent_id)


@pytest.mark.asyncio
async def test_create_session_uses_check_agent_access(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4())
    db = _QueryAwareDB(agent=agent)
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.create_session(
        agent_id=agent_id,
        body=chat_sessions_api.CreateSessionIn(title="Manual Session"),
        current_user=current_user,
        db=db,
    )

    assert result.title == "Manual Session"
    assert called["args"] == (db, current_user, agent_id)


@pytest.mark.asyncio
async def test_list_sessions_all_scope_allows_manage_access_for_non_creator(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Ops Thread",
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:00:00+00:00"),
        last_message_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:10:00+00:00"),
        peer_agent_id=None,
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session], counts=[2], users={owner_id: "Owner"})

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0].id == str(session_id)


@pytest.mark.asyncio
async def test_get_session_messages_allows_manage_access_for_non_owner(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
    )
    message = SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="done",
        participant_id=None,
        thinking=None,
        created_at=None,
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session], messages=[message])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    result = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        db=db,
    )

    assert len(result) == 1
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "done"


@pytest.mark.asyncio
async def test_rename_session_rejects_use_access_for_non_owner(monkeypatch):
    import app.api.chat_sessions as chat_sessions_api

    agent_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=owner_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=owner_id,
        title="Before",
    )
    current_user = SimpleNamespace(id=viewer_id, role="member")
    db = _QueryAwareDB(agent=agent, sessions=[session])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)

    with pytest.raises(HTTPException) as exc:
        await chat_sessions_api.rename_session(
            agent_id=agent_id,
            session_id=session_id,
            body=chat_sessions_api.PatchSessionIn(title="After"),
            current_user=current_user,
            db=db,
        )

    assert exc.value.status_code == 403
