from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _ReuseSession:
    def __init__(self, values):
        self._values = list(values)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        value = self._values.pop(0) if self._values else None
        return _ScalarResult(value)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_reuse_existing_skill_for_agent_copies_registry_files(tmp_path, monkeypatch):
    import app.services.capability_reuse_service as reuse_service

    agent_id = uuid4()
    skill = SimpleNamespace(
        id=uuid4(),
        name="Market Research",
        folder_name="market-research-agent",
        files=[
            SimpleNamespace(path="SKILL.md", content="# Skill"),
            SimpleNamespace(path="scripts/run.py", content="print('ok')"),
        ],
    )
    session = _ReuseSession([skill])
    monkeypatch.setattr(reuse_service, "async_session", lambda: session)
    monkeypatch.setattr(reuse_service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await reuse_service.reuse_existing_skill_for_agent(
        agent_id=agent_id,
        tenant_id=uuid4(),
        folder_name="market-research-agent",
    )

    assert result is not None
    assert result["status"] == "already_installed"
    assert result["folder_name"] == "market-research-agent"
    assert result["files_written"] == 2
    assert (tmp_path / str(agent_id) / "skills" / "market-research-agent" / "SKILL.md").exists()
    assert (tmp_path / str(agent_id) / "skills" / "market-research-agent" / "scripts" / "run.py").exists()


@pytest.mark.asyncio
async def test_reuse_existing_mcp_server_for_agent_reassigns_existing_tools(monkeypatch):
    import app.services.capability_reuse_service as reuse_service

    tool_one = SimpleNamespace(id=uuid4(), display_name="GitHub: repo_read")
    tool_two = SimpleNamespace(id=uuid4(), display_name="GitHub: issue_search")
    session = _ReuseSession([[tool_one, tool_two]])
    monkeypatch.setattr(reuse_service, "async_session", lambda: session)

    calls = []

    async def fake_ensure(db, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs), True

    monkeypatch.setattr(reuse_service, "ensure_agent_tool_assignment", fake_ensure)

    result = await reuse_service.reuse_existing_mcp_server_for_agent(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        server_id="smithery/github",
        config={"smithery_api_key": "secret"},
    )

    assert result is not None
    assert result["status"] == "already_installed"
    assert result["tool_count"] == 2
    assert len(calls) == 2
    assert session.committed is True
