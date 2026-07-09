from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.subagent_definition import parse_subagent_definition
from app.services.external_capabilities import activation as activation_mod
from app.services.external_capabilities.activation import (
    activate_external_extension_for_agent,
    deactivate_external_extension_for_agent,
    try_external_extension_in_chat,
)


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ActivationSession:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.snapshot)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_activate_external_skill_snapshot_installs_existing_skill_runtime(tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="external_skill_url:demo:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "demo",
                    "qualified_name": "skill:demo",
                    "metadata": {
                        "files": [
                            {"path": "SKILL.md", "content": "# Demo Skill"},
                            {"path": "notes.md", "content": "hello"},
                        ]
                    },
                    "runtime_projection": {"folder_name": "demo"},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    assert result["status"] == "active"
    assert result["activated_components"] == [{"component_type": "skill", "name": "demo", "files_written": 2}]
    assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "# Demo Skill"
    assert db.added[0].tenant_id == tenant_id
    assert db.added[0].agent_id == agent_id
    assert db.added[0].snapshot_id == snapshot_id
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_activate_external_mcp_snapshot_uses_agent_scoped_existing_runtime(monkeypatch, tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    calls = []

    async def fake_import_mcp_for_agent_and_register(import_agent_id, **kwargs):
        calls.append((import_agent_id, kwargs))
        return "Imported Docs MCP"

    monkeypatch.setattr(activation_mod, "import_mcp_for_agent_and_register", fake_import_mcp_for_agent_and_register)
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="mcp_server:docs:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "mcp_server",
                    "local_name": "docs",
                    "qualified_name": "mcp:docs",
                    "runtime_projection": {
                        "server_id": "docs/server",
                        "server_name": "Docs MCP",
                        "mcp_url": "https://mcp.example/sse",
                        "config": {"transport": "sse"},
                    },
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    assert result["activated_components"] == [
        {
            "component_type": "mcp_server",
            "name": "Docs MCP",
            "status": "activated",
            "message": "Imported Docs MCP",
        }
    ]
    assert calls == [
        (
            agent_id,
            {
                "server_id": "docs/server",
                "mcp_url": "https://mcp.example/sse",
                "server_name": "Docs MCP",
                "config": {"transport": "sse"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_activate_external_snapshot_selects_components_and_requires_credential_handles(monkeypatch, tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    calls = []

    async def fake_import_mcp_for_agent_and_register(import_agent_id, **kwargs):
        calls.append((import_agent_id, kwargs))
        return "Imported Docs MCP"

    monkeypatch.setattr(activation_mod, "import_mcp_for_agent_and_register", fake_import_mcp_for_agent_and_register)
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="cc_plugin:docs-pack:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "audit",
                    "qualified_name": "docs-pack:skill:audit",
                    "metadata": {
                        "files": [
                            {"path": "SKILL.md", "content": "# Audit Skill"},
                        ]
                    },
                    "runtime_projection": {"folder_name": "audit"},
                },
                {
                    "component_type": "mcp_server",
                    "local_name": "docs",
                    "qualified_name": "docs-pack:mcp:docs",
                    "runtime_projection": {
                        "server_id": "docs/server",
                        "server_name": "Docs MCP",
                        "mcp_url": "https://mcp.example/sse",
                        "config": {"transport": "sse"},
                        "credential_requirements": [{"key": "docs_api_key", "label": "Docs API key"}],
                    },
                },
                {
                    "component_type": "hook",
                    "local_name": "pre-bash",
                    "qualified_name": "docs-pack:hook:pre-bash",
                    "runtime_projection": {"event": "PreToolUse", "handler": "deny-dangerous-command"},
                },
            ]
        },
    )

    skill_db = _ActivationSession(snapshot)
    skill_result = await activate_external_extension_for_agent(
        skill_db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
        component_qualified_names=["docs-pack:skill:audit"],
        credential_handles={},
    )

    assert skill_result["activated_components"] == [{"component_type": "skill", "name": "audit", "files_written": 1}]
    assert calls == []
    activation = skill_db.added[0]
    assert activation.selected_components_json == ["docs-pack:skill:audit"]
    assert activation.credential_handles_json == {}

    missing_credential_db = _ActivationSession(snapshot)
    with pytest.raises(ValueError, match="missing credential handle"):
        await activate_external_extension_for_agent(
            missing_credential_db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            snapshot_id=snapshot_id,
            workspace=tmp_path,
            activated_by_user_id=uuid4(),
            component_qualified_names=["docs-pack:mcp:docs"],
            credential_handles={},
        )

    mcp_db = _ActivationSession(snapshot)
    mcp_result = await activate_external_extension_for_agent(
        mcp_db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
        component_qualified_names=["docs-pack:mcp:docs", "docs-pack:hook:pre-bash"],
        credential_handles={"docs_api_key": "credential-handle-123"},
    )

    assert mcp_result["activated_components"] == [
        {
            "component_type": "mcp_server",
            "name": "Docs MCP",
            "status": "activated",
            "message": "Imported Docs MCP",
            "credential_handles": {"docs_api_key": "credential-handle-123"},
        },
        {
            "component_type": "hook",
            "name": "docs-pack:hook:pre-bash",
            "status": "pending_hook_approval",
            "event": "PreToolUse",
            "runtime_execution": "governance_preflight",
        },
    ]
    assert calls == [
        (
            agent_id,
            {
                "server_id": "docs/server",
                "mcp_url": "https://mcp.example/sse",
                "server_name": "Docs MCP",
                "config": {"transport": "sse", "credential_handles": {"docs_api_key": "credential-handle-123"}},
            },
        )
    ]
    assert mcp_db.added[0].selected_components_json == ["docs-pack:mcp:docs", "docs-pack:hook:pre-bash"]
    assert mcp_db.added[0].credential_handles_json == {"docs_api_key": "credential-handle-123"}


@pytest.mark.asyncio
async def test_try_external_extension_in_chat_creates_session_activation_overlay(tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    session_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="cc_plugin:trial-pack:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "trial",
                    "qualified_name": "trial-pack:skill:trial",
                    "metadata": {
                        "files": [
                            {"path": "SKILL.md", "content": "---\nname: Trial\n---\n\nTrial only."},
                        ]
                    },
                    "runtime_projection": {"folder_name": "trial"},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await try_external_extension_in_chat(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        session_id=session_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
        component_qualified_names=["trial-pack:skill:trial"],
        credential_handles={},
        expires_in_minutes=30,
    )

    assert result["status"] == "active"
    assert result["activation_scope"] == "session"
    assert result["session_id"] == str(session_id)
    assert result["expires_at"]
    assert (tmp_path / "session_extensions" / str(session_id) / "skills" / "trial" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "trial" / "SKILL.md").exists()
    activation = db.added[0]
    assert activation.activation_scope == "session"
    assert activation.session_id == session_id
    assert activation.selected_components_json == ["trial-pack:skill:trial"]
    assert activation.expires_at is not None


@pytest.mark.asyncio
async def test_activate_substitutes_plugin_root_and_materializes_plugin_body(tmp_path):
    # C3: ${CLAUDE_PLUGIN_ROOT} in projected skill content is replaced with the
    # real materialized path, and the plugin file body lands under
    # workspace/plugins/<name>.
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        normalized_name="toolkit",
        snapshot_key="cc_plugin:toolkit:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "skill",
                    "local_name": "audit",
                    "qualified_name": "toolkit:audit",
                    "source_path": "skills/audit/SKILL.md",
                    "metadata": {
                        "files": [
                            {"path": "SKILL.md", "content": "Run ${CLAUDE_PLUGIN_ROOT}/scripts/run.py"},
                        ]
                    },
                    "runtime_projection": {"folder_name": "audit"},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    assert result["status"] == "active"
    plugin_root = (tmp_path / "plugins" / "toolkit").resolve()
    # Projected (model-facing) skill content has the real path, no literal var.
    projected = (tmp_path / "skills" / "audit" / "SKILL.md").read_text(encoding="utf-8")
    assert projected == f"Run {plugin_root}/scripts/run.py"
    assert "${CLAUDE_PLUGIN_ROOT}" not in projected
    # Raw plugin body is materialized under workspace/plugins/<name> (var intact).
    raw = (plugin_root / "skills" / "audit" / "SKILL.md").read_text(encoding="utf-8")
    assert raw == "Run ${CLAUDE_PLUGIN_ROOT}/scripts/run.py"


@pytest.mark.asyncio
async def test_activate_external_subagent_snapshot_writes_sanitized_agent_definition(tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    raw_definition = """---
name: reviewer
description: Review code
permissionMode: bypassPermissions
hooks:
  PreToolUse: []
mcpServers:
  github: {}
tools:
  - read_file
skills:
  - audit
model: claude-sonnet
---

Review code carefully.
"""
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        snapshot_key="cc_plugin:review-pack:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "subagent",
                    "local_name": "reviewer",
                    "qualified_name": "review-pack:reviewer",
                    "metadata": {"definition": raw_definition},
                    "runtime_projection": {
                        "description": "Review code",
                        "tools": ["read_file"],
                        "skills": ["audit"],
                        "model": "claude-sonnet",
                    },
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    definition_path = tmp_path / "subagents" / "reviewer.md"
    spec = parse_subagent_definition(definition_path.read_text(encoding="utf-8"))
    assert result["activated_components"] == [{"component_type": "subagent", "name": "reviewer", "status": "activated"}]
    assert spec.allowed_tools == ("read_file",)
    assert spec.skills == ("audit",)
    assert spec.model == "claude-sonnet"
    assert spec.permission_mode is None
    assert spec.hooks is None
    assert spec.mcp_servers == ()


@pytest.mark.asyncio
async def test_activate_slash_command_projects_command_as_skill_package(tmp_path):
    # C6-command: a slash_command component projects into a single-file skill
    # package (SKILL.md), which get_agent_extensions -> _dynamic_skill_commands
    # then surfaces as an agent / command with zero new infra.
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        normalized_name="ops",
        snapshot_key="cc_plugin:ops:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "slash_command",
                    "local_name": "deploy",
                    "qualified_name": "ops:deploy",
                    "source_path": "commands/deploy.md",
                    "metadata": {"content": "---\ndescription: Deploy the app\n---\nRun the deploy steps."},
                    "runtime_projection": {"description": "Deploy the app", "allowed_tools": []},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    assert result["activated_components"] == [
        {"component_type": "slash_command", "name": "deploy", "files_written": 1, "status": "activated"}
    ]
    skill_md = (tmp_path / "skills" / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: deploy" in skill_md
    assert "Run the deploy steps." in skill_md
    # The command's own frontmatter is stripped — exactly one skill frontmatter block.
    assert skill_md.count("---") == 2


@pytest.mark.asyncio
async def test_activate_hook_component_reports_governance_preflight_wiring(tmp_path):
    # C6-hook closure (§1 2026-07-09): approved PreToolUse registrations feed
    # run_tool_governance's hook lanes; activation reports the wiring while
    # keeping the fail-closed pending_hook_approval status.
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        tenant_id=tenant_id,
        status="approved",
        normalized_name="guard",
        snapshot_key="cc_plugin:guard:abc",
        component_manifest_json={
            "components": [
                {
                    "component_type": "hook",
                    "local_name": "pre-bash",
                    "qualified_name": "guard:hook:PreToolUse:*:0",
                    "runtime_projection": {"event": "PreToolUse", "spec": {}},
                }
            ]
        },
    )
    db = _ActivationSession(snapshot)

    result = await activate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        activated_by_user_id=uuid4(),
    )

    hook = result["activated_components"][0]
    assert hook["component_type"] == "hook"
    assert hook["status"] == "pending_hook_approval"
    assert hook["runtime_execution"] == "governance_preflight"


@pytest.mark.asyncio
async def test_deactivate_external_extension_removes_files_and_marks_mcp_manual_revoke(tmp_path):
    tenant_id = uuid4()
    agent_id = uuid4()
    snapshot_id = uuid4()
    activation_id = uuid4()
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo", encoding="utf-8")
    subagent_file = tmp_path / "subagents" / "reviewer.md"
    subagent_file.parent.mkdir(parents=True)
    subagent_file.write_text("---\nname: reviewer\n---\n\nReview.", encoding="utf-8")
    activation = SimpleNamespace(
        id=activation_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        status="active",
        activation_result_json={
            "components": [
                {"component_type": "skill", "name": "demo"},
                {"component_type": "subagent", "name": "reviewer"},
                {"component_type": "mcp_server", "name": "Docs MCP"},
            ]
        },
    )
    db = _ActivationSession(activation)

    result = await deactivate_external_extension_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        workspace=tmp_path,
        deactivated_by_user_id=uuid4(),
    )

    assert result["status"] == "inactive"
    assert activation.status == "inactive"
    assert not skill_dir.exists()
    assert not subagent_file.exists()
    assert result["deactivated_components"] == [
        {"component_type": "skill", "name": "demo", "status": "removed"},
        {"component_type": "subagent", "name": "reviewer", "status": "removed"},
        {"component_type": "mcp_server", "name": "Docs MCP", "status": "manual_revoke_required"},
    ]
    assert activation.activation_result_json["deactivation"]["components"] == result["deactivated_components"]
    assert db.commit_calls == 1
