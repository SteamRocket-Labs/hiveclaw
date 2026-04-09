from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def fetchall(self):
        return self._values


class _MessagesDB:
    def __init__(self, *, created_agent_rows=None, permission_rows=None, sessions=None, messages=None, participants=None):
        self.created_agent_rows = created_agent_rows or []
        self.permission_rows = permission_rows or []
        self.sessions = sessions or []
        self.messages = messages or []
        self.participants = participants or {}

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM agent_permissions" in sql:
            return _ListResult(self.permission_rows)
        if "FROM agents" in sql and "agents.id" in sql:
            return _ListResult(self.created_agent_rows)
        if "FROM chat_sessions" in sql:
            return _ListResult(self.sessions)
        if "FROM chat_messages" in sql:
            return _ListResult(self.messages)
        if "FROM participants" in sql:
            participant_id = next(iter(self.participants))
            return _ScalarResult(self.participants[participant_id])
        raise AssertionError(f"Unhandled SQL in fake DB: {sql}")


@pytest.mark.asyncio
async def test_list_accessible_agent_ids_includes_permission_scopes():
    import app.api.messages as messages_api

    creator_agent_id = uuid4()
    permitted_agent_id = uuid4()
    current_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        department_id=uuid4(),
        role="member",
    )
    db = _MessagesDB(
        created_agent_rows=[(creator_agent_id,)],
        permission_rows=[
            SimpleNamespace(agent_id=permitted_agent_id, scope_type="company", scope_id=None),
        ],
    )

    result = await messages_api._list_accessible_agent_ids(db, current_user)

    assert result == [creator_agent_id, permitted_agent_id]


@pytest.mark.asyncio
async def test_get_inbox_uses_accessible_agent_helper(monkeypatch):
    import app.api.messages as messages_api

    agent_id = uuid4()
    session_id = uuid4()
    participant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), department_id=None, role="member")
    db = _MessagesDB(
        sessions=[
            SimpleNamespace(
                id=session_id,
                title="Agent Thread",
                agent_id=agent_id,
                peer_agent_id=None,
                source_channel="agent",
            )
        ],
        messages=[
            SimpleNamespace(
                id=uuid4(),
                participant_id=participant_id,
                content="hello",
                created_at=SimpleNamespace(isoformat=lambda: "2026-03-25T00:00:00+00:00"),
            )
        ],
        participants={participant_id: "Ops Agent"},
    )
    called = {}

    async def fake_list_accessible_agent_ids(db_arg, user_arg):
        called["args"] = (db_arg, user_arg)
        return [agent_id]

    monkeypatch.setattr(messages_api, "_list_accessible_agent_ids", fake_list_accessible_agent_ids, raising=False)

    result = await messages_api.get_inbox(limit=10, current_user=current_user, db=db)

    assert called["args"] == (db, current_user)
    assert result == [
        {
            "id": str(db.messages[0].id),
            "sender_type": "agent",
            "sender_name": "Ops Agent",
            "content": "hello",
            "session_title": "Agent Thread",
            "created_at": "2026-03-25T00:00:00+00:00",
        }
    ]
