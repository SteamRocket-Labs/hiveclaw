from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_tool_search_returns_structured_discovery_sections(monkeypatch, tmp_path):
    import app.services.agent_tools as agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    skill_dir = tmp_path / "skills" / "web-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Web Research\ndescription: Web evidence workflow\ntools:\n  - web_search\n---\n# Web Research\n",
        encoding="utf-8",
    )

    async def fake_discoverable_tool_names(_agent_id, _query):
        return ["firecrawl_fetch", "mcp_github_issue_search"]

    monkeypatch.setattr(agent_tools, "discoverable_tool_names_for_query", fake_discoverable_tool_names)

    text = await _tool_search(tmp_path, "web", agent_id=uuid4())

    assert "loaded_tool_schemas:" in text
    assert "skill_candidates:" in text
    assert "subagent_candidates:" in text
    assert "mcp_candidates:" in text
    assert "firecrawl_fetch" in text
    assert "mcp_github_issue_search" in text
    assert "Web Research" in text


def test_tasks_json_protected_write_points_to_work_ledger(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file

    result = _write_file(tmp_path, "tasks.json", "[]")

    assert "manage_tasks" not in result
    assert "track_todo" in result
    assert "read_ledger" in result


def test_soul_md_direct_write_and_edit_are_rejected(tmp_path):
    from app.services.agent_tool_domains.workspace import _edit_file, _write_file

    (tmp_path / "soul.md").write_text("# Soul\n\n<identity>stable</identity>\n", encoding="utf-8")

    write_result = _write_file(tmp_path, "soul.md", "# Soul\n\n<identity>mutated</identity>\n")
    edit_result = _edit_file(tmp_path, "soul.md", "stable", "mutated")

    for result in (write_result, edit_result):
        assert "soul.md is governed by Dream/Soul promotion" in result
        assert "soul.md.next" in result

    assert (tmp_path / "soul.md").read_text(encoding="utf-8") == "# Soul\n\n<identity>stable</identity>\n"


def test_root_writes_are_rejected_except_governed_entrypoints(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file

    for rel_path in ("report.md", "relationships.md", "HEARTBEAT.md", "DREAM.md", "state.json"):
        result = _write_file(tmp_path, rel_path, "raw bypass")

        assert "workspace/" in result
        assert not (tmp_path / rel_path).exists()


def test_skill_direct_write_edit_and_delete_are_rejected(tmp_path):
    from app.services.agent_tool_domains.workspace import _delete_file, _edit_file, _write_file

    skill_path = tmp_path / "skills" / "deploy-checklist" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: deploy-checklist\n---\n", encoding="utf-8")

    write_result = _write_file(tmp_path, "skills/deploy-checklist/SKILL.md", "---\nname: bypass\n---\n")
    edit_result = _edit_file(tmp_path, "skills/deploy-checklist/SKILL.md", "deploy-checklist", "bypass")
    delete_result = _delete_file(tmp_path, "skills/deploy-checklist/SKILL.md")

    for result in (write_result, edit_result, delete_result):
        assert "Active skill packages are governed by Skill promotion" in result
        assert "save_skill" in result
        assert "Platform Skill Gate" in result

    assert skill_path.read_text(encoding="utf-8") == "---\nname: deploy-checklist\n---\n"


def test_enterprise_asset_direct_write_edit_and_delete_are_rejected(tmp_path):
    from app.services.agent_tool_domains.workspace import _delete_file, _edit_file, _write_file

    for rel_path in ("subagents/reviewer.md", "enterprise_info/company_profile.md"):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("platform-owned", encoding="utf-8")

        write_result = _write_file(tmp_path, rel_path, "raw bypass")
        edit_result = _edit_file(tmp_path, rel_path, "platform-owned", "bypass")
        delete_result = _delete_file(tmp_path, rel_path)

        for result in (write_result, edit_result, delete_result):
            assert "auth_or_permission" in result

        assert target.exists()
        assert target.read_text(encoding="utf-8") == "platform-owned"


def test_evolution_write_guard_points_to_platform_bookkeeping_not_missing_tool(tmp_path):
    from app.services.agent_tool_domains.workspace import _edit_file, _write_file

    write_result = _write_file(tmp_path, "evolution/lineage.md", "raw bypass")
    edit_result = _edit_file(tmp_path, "evolution/scorecard.md", "old", "new")

    for result in (write_result, edit_result):
        assert "evolution/ is managed by platform services" in result
        assert "Return the outcome summary instead" in result
        assert "Skill Candidate Packages plus Skill Gate" in result
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


def test_list_files_and_read_file_results_carry_provenance_hint(tmp_path):
    from app.services.agent_tool_domains.workspace import _list_files, _read_file
    from app.services.chat_artifact_delivery import build_session_artifact_parts

    agent_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    target = tmp_path / "workspace" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Report\n\nCurrent contents.\n", encoding="utf-8")

    parts = build_session_artifact_parts(
        agent_id=agent_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        paths=["workspace/report.md"],
        workspace_root=tmp_path,
        source="workspace_write",
        action="created",
    )

    assert parts
    listing = _list_files(tmp_path, "workspace")
    content = _read_file(tmp_path, "workspace/report.md")

    for result in (listing, content):
        assert "Provenance hint" in result
        assert "may belong to another session" in result
        assert str(session_id) in result
        assert str(runtime_task_id) in result


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


def test_load_skill_allows_only_matching_session_extension_overlay(tmp_path):
    from app.services.agent_tool_domains.workspace import _load_skill

    ws = tmp_path / "agent"
    skill_file = ws / "session_extensions" / "session-1" / "skills" / "trial" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: Trial Session Skill\ndescription: Only here\n---\n\nSession-only body.",
        encoding="utf-8",
    )

    assert "Session-only body" not in _load_skill(ws, "Trial Session Skill")
    assert "Session-only body" not in _load_skill(ws, "Trial Session Skill", session_id="session-2")
    assert "Session-only body" in _load_skill(ws, "Trial Session Skill", session_id="session-1")
    assert "Access denied" in _load_skill(ws, "session_extensions/session-1/skills/trial/SKILL.md")


@pytest.mark.asyncio
async def test_upload_image_rejects_sibling_prefix_escape(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import image_upload

    agent_id = uuid4()
    ws = tmp_path / "11111111-1111-1111-1111-111111111111"
    ws.mkdir()
    sibling = tmp_path / f"{ws.name}-evil"
    sibling.mkdir()
    (sibling / "secret.png").write_bytes(b"png")

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

    async def fake_resolve_tool_config(_tool_name, *, agent_id=None):
        assert agent_id == agent_id_arg
        return {"private_key": "private", "url_endpoint": "https://img.example"}

    agent_id_arg = agent_id
    monkeypatch.setattr("app.services.tool_config_service.resolve_tool_config", fake_resolve_tool_config)
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
    tenant_id = uuid4()
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

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def fake_sync_tasks(agent_id_arg, workspace):
        sync_calls.append((agent_id_arg, workspace))

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    workspace = await ensure_workspace(agent_id, tenant_id=str(tenant_id))

    assert workspace == tmp_path / str(agent_id)
    assert (workspace / "skills").is_dir()
    assert (workspace / "workspace").is_dir()
    assert (workspace / "workspace" / "knowledge_base").is_dir()
    assert (workspace / "memory").is_dir()
    for plane_dir in ("self", "profiles", "knowledge", "milestones"):
        assert (workspace / "memory" / plane_dir).is_dir()
    assert (workspace / "memory" / "explicit" / "MEMORY.md").exists()
    assert (workspace / "memory" / "indexes" / "wiki_map.md").exists()
    assert not (workspace / "memory" / "wiki_map.md").exists()
    assert not (workspace / "memory" / "learnings").exists()
    assert not (workspace / "evolution" / "skill_candidates.md").exists()
    assert (workspace / "evolution").is_dir()
    assert (workspace / "evolution" / "skill_review.md").exists()
    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "knowledge.md").exists()
    assert not (workspace / "memory" / "feedback.md").exists()
    assert not (workspace / "memory" / "learnings" / "LEARNINGS.md").exists()
    soul_content = (workspace / "soul.md").read_text(encoding="utf-8")
    assert "# Soul — 投后助手" in soul_content
    assert "负责投后分析" in soul_content
    enterprise_dir = tmp_path / f"enterprise_info_{tenant_id}"
    assert not (enterprise_dir / "knowledge_base").exists()
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


def test_agent_workspace_exposes_only_canonical_company_context(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import workspace as workspace_domain

    tenant_id = uuid4()
    agent_workspace = tmp_path / "agent"
    agent_workspace.mkdir()
    company_dir = tmp_path / f"enterprise_info_{tenant_id}"
    company_dir.mkdir()
    (company_dir / "company_profile.md").write_text("# Company\n", encoding="utf-8")
    (company_dir / "org_structure.md").write_text("# Org\n", encoding="utf-8")
    (company_dir / "knowledge_base").mkdir()
    (company_dir / "knowledge_base" / "policy.md").write_text("legacy secret\n", encoding="utf-8")
    (company_dir / "legacy-upload.md").write_text("legacy upload\n", encoding="utf-8")
    monkeypatch.setattr(workspace_domain, "WORKSPACE_ROOT", tmp_path)

    listing = workspace_domain._list_files(agent_workspace, "enterprise_info", str(tenant_id))
    blocked_file = workspace_domain._read_file(
        agent_workspace,
        "enterprise_info/knowledge_base/policy.md",
        str(tenant_id),
    )
    blocked_legacy_root = workspace_domain._read_file(
        agent_workspace,
        "enterprise_info/legacy-upload.md",
        str(tenant_id),
    )

    assert "company_profile.md" in listing
    assert "org_structure.md" in listing
    assert "knowledge_base" not in listing
    assert "legacy-upload.md" not in listing
    assert "auth_or_permission" in blocked_file
    assert "auth_or_permission" in blocked_legacy_root


@pytest.mark.asyncio
async def test_ensure_workspace_rebuilds_canonical_t3_index_without_legacy_index(monkeypatch, tmp_path):
    from app.tools.workspace import ensure_workspace

    agent_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / str(agent_id)
    legacy_index = workspace / "memory" / "INDEX.md"
    legacy_index.parent.mkdir(parents=True)
    legacy_index.write_text("# Legacy Index\n", encoding="utf-8")

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(name="索引测试", role_description="验证派生索引")

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult()

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def fake_sync_tasks(_agent_id_arg, _workspace):
        return None

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    await ensure_workspace(agent_id, tenant_id=str(tenant_id))

    wiki_map = workspace / "memory" / "indexes" / "wiki_map.md"
    assert wiki_map.exists()
    assert "Memory Wiki Map" in wiki_map.read_text(encoding="utf-8")
    assert not legacy_index.exists()
    assert not (workspace / "memory" / "index.md").exists()
    assert not (workspace / "memory" / ".derived" / "t3_index.md").exists()


@pytest.mark.asyncio
async def test_ensure_workspace_does_not_precreate_legacy_learnings_files(monkeypatch, tmp_path):
    from app.tools.workspace import ensure_workspace

    agent_id = uuid4()
    tenant_id = uuid4()

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(name="学习测试", role_description="验证 legacy memory cleanup")

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _query):
            return _FakeScalarResult()

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def fake_sync_tasks(_agent_id_arg, _workspace):
        return None

    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.tools.workspace._sync_tasks_to_file", fake_sync_tasks)

    await ensure_workspace(agent_id, tenant_id=str(tenant_id))

    workspace = tmp_path / str(agent_id)
    assert not (workspace / "memory" / "learnings").exists()
    assert not (workspace / "memory" / "learnings" / "insights.md").exists()
    assert not (workspace / "memory" / "learnings" / "errors.md").exists()
    assert not (workspace / "memory" / "learnings" / "requests.md").exists()


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

    # C7 cutover: legacy single-file memories are ARCHIVED (moved, never
    # deleted); content reorganization is the two-plane migration tool's job.
    assert not (workspace / "memory" / "memory.md").exists()
    assert not (workspace / "memory" / "learnings").exists()
    archive = workspace / "memory" / ".archive" / "legacy_import"
    assert "Keep the architecture md-first" in (archive / "memory.md").read_text(encoding="utf-8")
    assert "Prefer weighted promotion over rigid layer upgrades." in (archive / "learnings" / "LEARNINGS.md").read_text(
        encoding="utf-8"
    )


def test_migrate_all_workspaces_repairs_memory_hygiene(monkeypatch, tmp_path):
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

    # C7 cutover: legacy feedback.md is archived intact (no flat-T3 rewrite)
    assert not (mem / "feedback.md").exists()
    archived = (mem / ".archive" / "legacy_import" / "feedback.md").read_text(encoding="utf-8")
    assert "keep vendor contacts private" in archived
    assert not (workspace / "memory.sqlite3").exists()
    assert not (workspace / "reflections.md").exists()
    assert (workspace / "memory" / "retired_artifacts" / "memory.sqlite3").exists()
    assert (workspace / "memory" / "retired_artifacts" / "reflections.md").exists()

    # flat-T3 prose backfill retired: no lifecycle registration for legacy prose


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
    assert (workspace / "runtime_artifacts" / "traces" / "invocation_spans.jsonl").read_text(encoding="utf-8") == "{}\n"
    assert (workspace / "runtime_artifacts" / "agent_state.json").read_text(encoding="utf-8") == '{"status": "idle"}'
    assert (workspace / "memory" / "retired_artifacts" / "focus.md").read_text(encoding="utf-8") == "# Legacy Focus\n"
    assert not (workspace / "legacy_report.md").exists()
    assert (workspace / "workspace" / "archived" / "legacy-root-files" / "legacy_report.md").read_text(
        encoding="utf-8"
    ) == "# Legacy Report\n"


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
    monkeypatch.setattr("app.services.capability_gate.check_capability", _fake_check_capability)

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
    monkeypatch.setattr("app.services.capability_gate.check_capability", _fake_check_capability)

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
    assert not (tmp_path / "evolution" / "skill_activation_candidates.md").exists()
    packages = sorted((tmp_path / "evolution" / "skill_candidates").iterdir())
    assert len(packages) == 1
    candidate_text = (packages[0] / "SKILL.md.draft").read_text(encoding="utf-8")
    manifest = (packages[0] / "manifest.json").read_text(encoding="utf-8")
    assert "skills/zombie-skill/SKILL.md" in manifest
    assert "pending_behavior_verification" in manifest
    assert "web_pack" in manifest
    assert "packs:" not in candidate_text
    assert "tools:" not in candidate_text


@pytest.mark.asyncio
async def test_save_skill_candidate_is_recorded_in_evolution_ledger(monkeypatch, tmp_path):
    import uuid

    from app.core.execution_context import set_tool_tenant_id
    from app.services.evolution_ledger import load_evolution_ledger
    from app.tools.handlers.skills import save_skill

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    set_tool_tenant_id(tenant_id)
    try:
        result = await save_skill(
            agent_id,
            tmp_path,
            {
                "name": "Deploy Verification",
                "description": "Verify a deployment using health checks and logs.",
                "instructions": "Read current deploy output, inspect health endpoint, then summarize failures.",
                "tools": ["read_file"],
            },
        )
    finally:
        set_tool_tenant_id(None)

    assert "submitted for review" in result
    packages = sorted((tmp_path / "evolution" / "skill_candidates").iterdir())
    assert len(packages) == 1
    manifest = json.loads((packages[0] / "manifest.json").read_text(encoding="utf-8"))
    ledger_entries = load_evolution_ledger(tmp_path)
    skill_candidates = [
        entry
        for entry in ledger_entries
        if entry.get("schema") == "evolution_candidate.v1"
        and entry.get("target_type") == "skill"
        and entry.get("metadata", {}).get("lane") == "save_skill"
    ]
    assert len(skill_candidates) == 1
    assert skill_candidates[0]["candidate_id"] == manifest["candidate_id"]
    assert skill_candidates[0]["target_id"] == "skills/deploy-verification/SKILL.md"
    assert skill_candidates[0]["metadata"]["package_manifest_path"] == manifest["manifest_path"]
