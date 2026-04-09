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
    def __init__(self, existing=None):
        self._existing = existing
        self.added = []

    async def execute(self, _stmt):
        return _ScalarResult(self._existing)

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_ensure_agent_tool_assignment_creates_new_row():
    from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment

    db = _FakeDB()
    agent_id = uuid4()
    tool_id = uuid4()

    assignment, created = await ensure_agent_tool_assignment(
        db,
        agent_id=agent_id,
        tool_id=tool_id,
        enabled=True,
        source="user_installed",
        installed_by_agent_id=agent_id,
        config={"api_key": "secret"},
    )

    assert created is True
    assert assignment.agent_id == agent_id
    assert assignment.tool_id == tool_id
    assert assignment.enabled is True
    assert assignment.source == "user_installed"
    assert assignment.installed_by_agent_id == agent_id
    assert assignment.config == {"api_key": "secret"}
    assert db.added == [assignment]


@pytest.mark.asyncio
async def test_ensure_agent_tool_assignment_merges_existing_config():
    from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment

    existing = SimpleNamespace(
        agent_id=uuid4(),
        tool_id=uuid4(),
        enabled=False,
        source="system",
        installed_by_agent_id=None,
        config={"smithery_namespace": "hive"},
    )
    db = _FakeDB(existing=existing)
    installer_id = uuid4()

    assignment, created = await ensure_agent_tool_assignment(
        db,
        agent_id=existing.agent_id,
        tool_id=existing.tool_id,
        enabled=True,
        source="user_installed",
        installed_by_agent_id=installer_id,
        config={"smithery_connection_id": "conn-1"},
    )

    assert created is False
    assert assignment is existing
    assert existing.enabled is True
    assert existing.source == "user_installed"
    assert existing.installed_by_agent_id == installer_id
    assert existing.config == {
        "smithery_namespace": "hive",
        "smithery_connection_id": "conn-1",
    }
    assert db.added == []
