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


def test_migrate_all_workspaces_handles_legacy_memory_file(monkeypatch, tmp_path):
    """Legacy migration runs at startup via migrate_all_workspaces, not per-call."""
    from app.tools.workspace import migrate_all_workspaces

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

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)

    migrate_all_workspaces()

    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "learnings" / "LEARNINGS.md").exists()
    assert "Keep the architecture md-first" in (workspace / "memory" / "knowledge.md").read_text(encoding="utf-8")
    assert (
        "Prefer weighted promotion over rigid layer upgrades."
        in (workspace / "memory" / "learnings" / "insights.md").read_text(encoding="utf-8")
    )
    assert (workspace / ".legacy_migrated").exists()


@pytest.mark.asyncio
async def test_check_declared_packs_authorized_skips_without_identity():
    """Conservative default — missing tenant/agent id permits write (e.g. distiller backfill)."""
    from app.services.agent_tool_domains.workspace import check_declared_packs_authorized

    ok, reason = await check_declared_packs_authorized(
        tenant_id=None,
        agent_id=None,
        declared_packs=("web_pack",),
    )
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_check_declared_packs_authorized_empty_packs_passes():
    from app.services.agent_tool_domains.workspace import check_declared_packs_authorized

    ok, reason = await check_declared_packs_authorized(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        declared_packs=(),
    )
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_check_declared_packs_authorized_unknown_pack_is_discovery_hint(monkeypatch):
    """C2: declared packs are discovery hints, not save-time existence gates."""
    from app.services.agent_tool_domains import workspace as workspace_mod

    class _NoopSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.database.async_session", lambda: _NoopSession())

    ok, reason = await workspace_mod.check_declared_packs_authorized(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        declared_packs=("totally_fake_pack",),
    )
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_check_declared_packs_authorized_denied_tool_still_allows_skill_save(monkeypatch):
    """C2: capability policy applies at tool call time, not when saving skill metadata."""
    from app.services.agent_tool_domains import workspace as workspace_mod
    from app.services.capability_gate import CapabilityCheckResult

    class _NoopSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _fake_check_capability(db, tenant_id, agent_id, tool_name):
        checked_tools.append(tool_name)
        if tool_name == "send_feishu_message":
            return CapabilityCheckResult(
                allowed=False,
                denied=True,
                capability="channel.feishu.message",
                reason="denied for this tenant",
            )
        return CapabilityCheckResult(allowed=True)

    checked_tools: list[str] = []
    monkeypatch.setattr("app.database.async_session", lambda: _NoopSession())
    monkeypatch.setattr(
        "app.services.capability_gate.check_capability", _fake_check_capability
    )

    ok, reason = await workspace_mod.check_declared_packs_authorized(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        declared_packs=("feishu_pack",),
    )
    assert ok is True
    assert reason == ""
    assert checked_tools == []


@pytest.mark.asyncio
async def test_save_skill_handler_keeps_denied_pack_as_discovery_hint(monkeypatch, tmp_path):
    """End-to-end: save_skill persists pack hints; call-time governance still owns access."""
    from app.core.execution_context import set_tool_tenant_id
    from app.services.capability_gate import CapabilityCheckResult
    from app.tools.handlers.skills import save_skill

    class _NoopSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _fake_check_capability(db, tenant_id, agent_id, tool_name):
        return CapabilityCheckResult(
            allowed=False,
            denied=True,
            capability="external.web.search",
            reason="tenant policy forbids web search",
        )

    monkeypatch.setattr("app.database.async_session", lambda: _NoopSession())
    monkeypatch.setattr(
        "app.services.capability_gate.check_capability", _fake_check_capability
    )

    tenant_id = uuid4()
    agent_id = uuid4()
    set_tool_tenant_id(tenant_id)

    try:
        result = await save_skill(
            agent_id,
            tmp_path,
            {
                "name": "Zombie Skill",
                "description": "would dangle if written",
                "instructions": "do web search then synthesize",
                "packs": ["web_pack"],
            },
        )
    finally:
        set_tool_tenant_id(None)

    assert "✅ Saved skill" in result
    assert "web_pack" in result
    saved = tmp_path / "skills" / "zombie-skill" / "SKILL.md"
    assert saved.exists()
    assert "packs:" in saved.read_text(encoding="utf-8")
    assert "web_pack" in saved.read_text(encoding="utf-8")
