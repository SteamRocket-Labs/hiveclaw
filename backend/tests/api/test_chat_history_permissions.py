from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def scalar_one_or_none(self):
        if isinstance(self._values, list):
            return self._values[0] if self._values else None
        return self._values


class _HistoryDB:
    def __init__(self, messages=None, sessions=None):
        self.messages = messages or []
        self.sessions = sessions or []
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        sql = str(stmt)
        if "FROM chat_sessions" in sql:
            return _ListResult(self.sessions)
        if "FROM chat_messages" in sql:
            return _ListResult(self.messages)
        raise AssertionError(f"Unhandled SQL in fake DB: {sql}")


@pytest.mark.asyncio
async def test_get_chat_history_uses_check_agent_access(monkeypatch):
    import app.api.websocket as websocket_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    db = _HistoryDB(messages=[])
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(websocket_api, "check_agent_access", fake_check_agent_access)

    result = await websocket_api.get_chat_history(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert result == []
    assert called["args"] == (db, current_user, agent_id)


@pytest.mark.asyncio
async def test_get_chat_history_reads_messages_from_canonical_session_id(monkeypatch):
    import app.api.websocket as websocket_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), username="alice", role="member")
    db = _HistoryDB(messages=[])

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        return SimpleNamespace(id=agent_id), "use"

    async def fake_find_web_chat_session(db_arg, *, agent_id, user, requested_session_id=None):
        assert db_arg is db
        assert requested_session_id is None
        assert user is current_user
        return SimpleNamespace(id=session_id)

    monkeypatch.setattr(websocket_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(websocket_api, "find_web_chat_session", fake_find_web_chat_session)
    monkeypatch.setattr(
        "app.services.chat_message_parts.serialize_chat_message",
        lambda message: {"id": str(getattr(message, "id", "msg"))},
    )

    result = await websocket_api.get_chat_history(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert result == []
    assert len(db.statements) == 1
    params = db.statements[0].compile().params
    assert params["conversation_id_1"] == str(session_id)
    assert params["user_id_1"] == current_user.id
