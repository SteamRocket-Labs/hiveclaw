from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        return _ScalarResult(self._agent)


@pytest.mark.asyncio
async def test_sync_agent_relationships_uses_shared_relationship_writer(monkeypatch):
    from app.services.workspace_sync import sync_agent_relationships

    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), name="关系助手")
    db = _FakeDB(agent)
    captured = {}

    async def fake_write_relationships_file(*, db, agent_id, include_owner):
        captured["db"] = db
        captured["agent_id"] = agent_id
        captured["include_owner"] = include_owner

    monkeypatch.setattr("app.services.workspace_sync.write_relationships_file", fake_write_relationships_file)

    await sync_agent_relationships(db, agent_id)

    assert captured == {
        "db": db,
        "agent_id": agent_id,
        "include_owner": True,
    }
