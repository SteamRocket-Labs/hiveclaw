from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _HistoryDB:
    def __init__(self, messages=None):
        self.messages = messages or []

    async def execute(self, stmt):
        sql = str(stmt)
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
