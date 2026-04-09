from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SkillSession:
    def __init__(self, values):
        self._values = list(values)
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        value = self._values.pop(0) if self._values else None
        return _ScalarResult(value)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_save_skill_to_db_can_return_existing_skill(monkeypatch):
    import app.api.skills as skills_api

    existing = SimpleNamespace(id=uuid4(), name="Market Research", folder_name="market-research-agent")
    session = _SkillSession([existing])
    monkeypatch.setattr(skills_api, "async_session", lambda: session)

    result = await skills_api._save_skill_to_db(
        folder_name="market-research-agent",
        name="Market Research",
        description="already there",
        category="clawhub-tier1",
        icon="",
        files=[{"path": "SKILL.md", "content": "# Skill"}],
        tenant_id=None,
        on_conflict="return_existing",
    )

    assert result == {
        "id": str(existing.id),
        "name": "Market Research",
        "folder_name": "market-research-agent",
        "status": "already_installed",
    }
    assert session.added == []
    assert session.committed is False
