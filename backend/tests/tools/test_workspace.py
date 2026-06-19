from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_tasks_json_protected_write_points_to_work_ledger(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file

    result = _write_file(tmp_path, "tasks.json", "[]")

    assert "manage_tasks" not in result
    assert "track_todo" in result
    assert "read_ledger" in result


def test_root_writes_are_rejected_except_governed_entrypoints(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file

    for rel_path in ("report.md", "relationships.md", "HEARTBEAT.md", "DREAM.md", "state.json"):
        result = _write_file(tmp_path, rel_path, "raw bypass")

        assert "workspace/" in result
        assert not (tmp_path / rel_path).exists()


def test_skill_writes_require_folder_skill_md_shape(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file

    for rel_path in ("skills/MCP_INSTALLER.md", "skills/.usage.json", "skills/flat-skill"):
        result = _write_file(tmp_path, rel_path, "raw bypass")

        assert "skills/<slug>/SKILL.md" in result
        assert not (tmp_path / rel_path).exists()

    ok = _write_file(tmp_path, "skills/deploy-checklist/SKILL.md", "---\nname: deploy-checklist\n---\n")

    assert "Written" in ok
    assert (tmp_path / "skills" / "deploy-checklist" / "SKILL.md").exists()


def test_evolution_write_guard_points_to_platform_bookkeeping_not_missing_tool(tmp_path):
    from app.services.agent_tool_domains.workspace import _edit_file, _write_file

    write_result = _write_file(tmp_path, "evolution/lineage.md", "raw bypass")
    edit_result = _edit_file(tmp_path, "evolution/scorecard.md", "old", "new")

    for result in (write_result, edit_result):
        assert "evolution/ is managed by platform services" in result
        assert "Return the outcome summary instead" in result
        assert "skill/evolution" not in result

    assert not (tmp_path / "evolution" / "lineage.md").exists()
    assert not (tmp_path / "evolution" / "scorecard.md").exists()


def test_workspace_tool_paths_reject_sibling_prefix_escape(tmp_path):
    from app.services.agent_tool_domains.workspace import (
        _delete_file,
        _edit_file,
        _glob_search,
        _grep_search,
        _list_files,
        _read_file,
        _write_file,
    )

    ws = tmp_path / "11111111-1111-1111-1111-111111111111"
    ws.mkdir()
    sibling = tmp_path / f"{ws.name}-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret token\n", encoding="utf-8")

    escaped_dir = f"workspace/../../{ws.name}-evil"
    escaped_file = f"{escaped_dir}/secret.txt"
    escaped_new_file = f"{escaped_dir}/pwn.txt"

    assert "Access denied" in _list_files(ws, escaped_dir)
    assert "Access denied" in _read_file(ws, escaped_file)
    assert "Access denied" in _write_file(ws, escaped_new_file, "bad")
    assert "Access denied" in _edit_file(ws, escaped_file, "secret", "changed")
    assert "Access denied" in _delete_file(ws, escaped_file)
    assert "Access denied" in _glob_search(ws, "*", root=escaped_dir)
    assert "Access denied" in _grep_search(ws, "secret", root=escaped_dir)

    assert not (sibling / "pwn.txt").exists()
    assert (sibling / "secret.txt").read_text(encoding="utf-8") == "secret token\n"


def test_load_skill_rejects_sibling_prefix_escape(tmp_path):
    from app.services.agent_tool_domains.workspace import _load_skill

    ws = tmp_path / "agent"
    (ws / "skills").mkdir(parents=True)
    sibling = ws / "skills-evil"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text("---\nname: stolen\n---\n\nsecret skill body\n", encoding="utf-8")

    result = _load_skill(ws, "skills/../skills-evil/SKILL.md")

    assert "Access denied" in result
    assert "secret skill body" not in result


@pytest.mark.asyncio
async def test_upload_image_rejects_sibling_prefix_escape(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import image_upload

    agent_id = uuid4()
    ws = tmp_path / "11111111-1111-1111-1111-111111111111"
    ws.mkdir()
    sibling = tmp_path / f"{ws.name}-evil"
    sibling.mkdir()
    (sibling / "secret.png").write_bytes(b"png")

    class _FakeResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=uuid4(), config={"private_key": "private", "url_endpoint": "https://img.example"})

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeResult()

    class _FakeResponse:
        status_code = 201

        def json(self):
            return {"url": "https://cdn.example/pwn.png", "fileId": "file-1", "size": 3}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(image_upload, "async_session", lambda: _FakeSession())
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    result = await image_upload._upload_image(
        agent_id,
        ws,
        {"file_path": f"../{ws.name}-evil/secret.png"},
    )

    assert "Access denied" in result
    assert "cdn.example" not in result


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
    assert (workspace / "memory" / "t3" / "episodes.md").exists()
    assert (workspace / "memory" / "t3" / "user.md").exists()
    assert (workspace / "memory" / "t3" / "worker.md").exists()
    assert (workspace / "memory" / "t3" / "capabilities.md").exists()
    assert (workspace / "memory" / "explicit" / "MEMORY.md").exists()
    assert (workspace / "memory" / "learnings" / "insights.md").exists()
    assert (workspace / "evolution" / "skill_candidates.md").exists()
    assert (workspace / "evolution" / "skill_review.md").exists()
    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "knowledge.md").exists()
    assert not (workspace / "memory" / "feedback.md").exists()
    assert not (workspace / "memory" / "learnings" / "LEARNINGS.md").exists()
    soul_content = (workspace / "soul.md").read_text(encoding="utf-8")
    assert "# Soul — 投后助手" in soul_content
    assert "负责投后分析" in soul_content

    enterprise_dir = tmp_path / "enterprise_info_tenant-1"
    assert (enterprise_dir / "knowledge_base").is_dir()
    assert (enterprise_dir / "company_profile.md").exists()
    assert sync_calls == [(agent_id, workspace)]

    # D10: reflections.md is a pre-spec scaffold stub. The §7 canonical T3 file
    # set never included it, and no writer/reader exists. A fresh workspace must
    # not materialize it anywhere — neither as a file nor as a directory.
    for reflections_path in (
        workspace / "reflections.md",
        workspace / "memory" / "reflections.md",
        workspace / "evolution" / "reflections.md",
    ):
        assert not reflections_path.exists(), f"dead stub created: {reflections_path}"


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
    assert "Keep the architecture md-first" in (
        workspace / "memory" / "t3" / "capabilities.md"
    ).read_text(encoding="utf-8")
    assert (
        "Prefer weighted promotion over rigid layer upgrades."
        in (workspace / "memory" / "learnings" / "insights.md").read_text(encoding="utf-8")
    )
    assert (workspace / ".legacy_migrated").exists()


def test_migrate_all_workspaces_repairs_memory_hygiene(monkeypatch, tmp_path):
    from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path
    from app.tools.workspace import migrate_all_workspaces

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    mem = workspace / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback.md").write_text(
        "# Feedback\n\n"
        "- [2026-06-04][entry_id=f1][sensitivity=PL2_pii][access_count=4]"
        "[last_accessed=2026-06-04T17:00:00+00:00] keep vendor contacts private\n",
        encoding="utf-8",
    )
    (workspace / "memory.sqlite3").write_text("retired sqlite store", encoding="utf-8")
    (workspace / "reflections.md").write_text("# dead stub\n", encoding="utf-8")

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)

    migrate_all_workspaces()

    assert not (mem / "feedback.md").exists()
    feedback = (mem / "t3" / "user.md").read_text(encoding="utf-8")
    assert feedback.strip().endswith("- [2026-06-04][entry_id=f1] keep vendor contacts private")
    assert "[sensitivity=" not in feedback
    assert not (workspace / "memory.sqlite3").exists()
    assert not (workspace / "reflections.md").exists()
    assert (workspace / "memory" / "retired_artifacts" / "memory.sqlite3").exists()
    assert (workspace / "memory" / "retired_artifacts" / "reflections.md").exists()

    lifecycle_entry = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("f1")
    assert lifecycle_entry.metadata["sensitivity"] == "PL2_pii"
    assert lifecycle_entry.access_count == 4


def test_migrate_all_workspaces_rehomes_legacy_runtime_and_root_files(monkeypatch, tmp_path):
    from app.tools.workspace import migrate_all_workspaces

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    (workspace / "traces").mkdir(parents=True)
    (workspace / "traces" / "invocation_spans.jsonl").write_text("{}\n", encoding="utf-8")
    (workspace / "state.json").write_text('{"status": "idle"}', encoding="utf-8")
    (workspace / "focus.md").write_text("# Legacy Focus\n", encoding="utf-8")
    (workspace / "legacy_report.md").write_text("# Legacy Report\n", encoding="utf-8")

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)

    migrate_all_workspaces()

    assert not (workspace / "traces").exists()
    assert not (workspace / "state.json").exists()
    assert not (workspace / "focus.md").exists()
    assert (workspace / "runtime_artifacts" / "traces" / "invocation_spans.jsonl").read_text(
        encoding="utf-8"
    ) == "{}\n"
    assert (workspace / "runtime_artifacts" / "agent_state.json").read_text(encoding="utf-8") == '{"status": "idle"}'
    assert (workspace / "memory" / "retired_artifacts" / "focus.md").read_text(encoding="utf-8") == "# Legacy Focus\n"
    assert not (workspace / "legacy_report.md").exists()
    assert (
        workspace / "workspace" / "archived" / "legacy-root-files" / "legacy_report.md"
    ).read_text(encoding="utf-8") == "# Legacy Report\n"


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
    """End-to-end: save_skill records a candidate; promotion remains externally gated."""
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

    assert "submitted for review" in result
    assert "active_skill_created: false" in result
    assert "web_pack" in result
    saved = tmp_path / "skills" / "zombie-skill" / "SKILL.md"
    assert not saved.exists()
    candidate = tmp_path / "evolution" / "skill_activation_candidates.md"
    assert candidate.exists()
    candidate_text = candidate.read_text(encoding="utf-8")
    assert "target: skills/zombie-skill/SKILL.md" in candidate_text
    assert "status: pending_behavior_verification" in candidate_text
    assert "web_pack" in candidate_text
