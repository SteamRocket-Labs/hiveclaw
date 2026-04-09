from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_ensure_workspace_creates_standard_structure_and_profile(monkeypatch, tmp_path):
    from app.tools.workspace import ensure_workspace

    agent_id = uuid4()
    sync_calls = []

    class _FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult(SimpleNamespace(name="投后助手", role_description="负责投后分析"))

    async def fake_sync_tasks(agent_id_arg, workspace):
        sync_calls.append((agent_id_arg, workspace))

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    workspace = await ensure_workspace(agent_id, tenant_id="tenant-1")

    assert workspace == tmp_path / str(agent_id)
    assert (workspace / "skills").is_dir()
    assert (workspace / "workspace").is_dir()
    assert (workspace / "workspace" / "knowledge_base").is_dir()
    assert (workspace / "memory").is_dir()
    assert (workspace / "memory" / "knowledge.md").exists()
    assert (workspace / "memory" / "feedback.md").exists()
    assert (workspace / "memory" / "learnings" / "insights.md").exists()
    assert (workspace / "evolution" / "skill_candidates.md").exists()
    assert (workspace / "evolution" / "skill_review.md").exists()
    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "learnings" / "LEARNINGS.md").exists()
    soul_content = (workspace / "soul.md").read_text(encoding="utf-8")
    assert "# Soul — 投后助手" in soul_content
    assert "负责投后分析" in soul_content

    enterprise_dir = tmp_path / "enterprise_info_tenant-1"
    assert (enterprise_dir / "knowledge_base").is_dir()
    assert (enterprise_dir / "company_profile.md").exists()
    assert sync_calls == [(agent_id, workspace)]


@pytest.mark.asyncio
async def test_ensure_workspace_migrates_legacy_memory_file(monkeypatch, tmp_path):
    from app.tools.workspace import ensure_workspace

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    legacy_memory = workspace / "memory.md"
    legacy_memory.write_text("# Old Memory\n", encoding="utf-8")

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult()

    async def fake_sync_tasks(_agent_id, _workspace):
        return None

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    resolved_workspace = await ensure_workspace(agent_id)

    assert resolved_workspace == workspace
    assert not legacy_memory.exists()
    knowledge_content = (workspace / "memory" / "knowledge.md").read_text(encoding="utf-8")
    assert "# Old Memory" not in knowledge_content


@pytest.mark.asyncio
async def test_ensure_workspace_migrates_legacy_memory_into_canonical_files(monkeypatch, tmp_path):
    from app.tools.workspace import ensure_workspace

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    (workspace / "memory" / "learnings").mkdir(parents=True)
    (workspace / "memory" / "memory.md").write_text(
        "# Memory\n\n- Keep the architecture md-first\n",
        encoding="utf-8",
    )
    (workspace / "memory" / "learnings" / "LEARNINGS.md").write_text(
        "# Learnings\n\n- Prefer weighted promotion over rigid layer upgrades.\n",
        encoding="utf-8",
    )

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult()

    async def fake_sync_tasks(_agent_id, _workspace):
        return None

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    resolved_workspace = await ensure_workspace(agent_id)

    assert resolved_workspace == workspace
    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "learnings" / "LEARNINGS.md").exists()
    assert "Keep the architecture md-first" in (workspace / "memory" / "knowledge.md").read_text(encoding="utf-8")
    assert (
        "Prefer weighted promotion over rigid layer upgrades."
        in (workspace / "memory" / "learnings" / "insights.md").read_text(encoding="utf-8")
    )
