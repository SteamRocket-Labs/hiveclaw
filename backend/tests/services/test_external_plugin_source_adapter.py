from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.external_capability import ExternalCapabilitySnapshot
from app.services.external_capabilities import activation as activation_mod
from app.services.external_capabilities.activation import activate_external_extension_for_agent
from app.services.external_capabilities.plugin_source_adapter import stage_cc_plugin_import
from app.services.external_capabilities.trust_gate import approve_external_capability_snapshot


class _ScalarResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value or []))


class _StubSession:
    """Stubs only the DB I/O boundary; the fetch/parse/stage/approve/activate
    domain logic all runs for real (no mocked main chain)."""

    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.added = []
        self.commit_calls = 0

    async def execute(self, _stmt):
        value = self.execute_values.pop(0) if self.execute_values else None
        return _ScalarResult(value)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        pass


def _write_cc_plugin_fixture(root):
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "ops-pack", "version": "1.0.0", "description": "Ops toolkit"}', encoding="utf-8"
    )
    (root / "commands").mkdir()
    (root / "commands" / "deploy.md").write_text(
        "---\ndescription: Deploy the app\n---\nRun ${CLAUDE_PLUGIN_ROOT}/scripts/deploy.sh", encoding="utf-8"
    )
    (root / "skills" / "audit").mkdir(parents=True)
    (root / "skills" / "audit" / "SKILL.md").write_text(
        "---\nname: audit\ndescription: Audit code\n---\nAudit the selected code.", encoding="utf-8"
    )
    (root / ".mcp.json").write_text(
        '{"mcpServers": {"browser": {"command": "npx", "args": ["@acme/browser"]}}}', encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_cc_plugin_directory_import_stage_approve_activate_e2e(tmp_path, monkeypatch):
    # C4 E2E: real CC-format plugin dir → directory import → stage → approve →
    # activate, exercising the whole chain (C4) plus the C3 ${CLAUDE_PLUGIN_ROOT}
    # and C6 slash_command faces. load_cc_plugin_bundle gets a production caller.
    plugin = tmp_path / "src" / "ops-pack"
    _write_cc_plugin_fixture(plugin)
    quarantine = tmp_path / "q"
    tenant_id = uuid4()
    agent_id = uuid4()

    async def fake_import_mcp(import_agent_id, **kwargs):
        return "Imported browser MCP"

    monkeypatch.setattr(activation_mod, "import_mcp_for_agent_and_register", fake_import_mcp)

    # 1. import (directory) → quarantine → load_cc_plugin_bundle → stage
    stage_db = _StubSession()
    import_result = await stage_cc_plugin_import(
        stage_db,
        tenant_id=tenant_id,
        created_by_user_id=None,
        source_kind="directory",
        source_ref=str(plugin),
        quarantine_root=quarantine,
        allowed_roots=[tmp_path],
    )

    assert import_result["status"] == "review_required"
    assert import_result["plugin_name"] == "ops-pack"
    review_row = stage_db.added[0]
    manifest = review_row.normalized_manifest_json
    component_types = {component["component_type"] for component in manifest["components"]}
    assert {"slash_command", "skill", "mcp_server"} <= component_types
    assert manifest["description"] == "Ops toolkit"

    # 2. approve (admin) — real supersede logic, empty superseded set
    approve_db = _StubSession(execute_values=[review_row, []])
    await approve_external_capability_snapshot(
        approve_db, tenant_id=tenant_id, review_id=review_row.id, approved_by_user_id=uuid4()
    )
    snapshot_row = next(row for row in approve_db.added if isinstance(row, ExternalCapabilitySnapshot))
    assert snapshot_row.status == "approved"
    assert snapshot_row.normalized_name == "ops-pack"

    # 3. activate (agent)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    activate_db = _StubSession(execute_values=[snapshot_row])
    activation = await activate_external_extension_for_agent(
        activate_db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        snapshot_id=snapshot_row.id,
        workspace=workspace,
        activated_by_user_id=uuid4(),
    )

    activated = {component["component_type"]: component for component in activation["activated_components"]}
    assert activated["skill"]["files_written"] == 1
    assert activated["slash_command"]["status"] == "activated"
    assert activated["mcp_server"]["status"] == "activated"

    # C3 face: plugin body materialized under workspace/plugins/ops-pack; the
    # ${CLAUDE_PLUGIN_ROOT} in the command is substituted in the projected skill.
    plugin_root = (workspace / "plugins" / "ops-pack").resolve()
    assert (plugin_root / "commands" / "deploy.md").exists()
    assert (workspace / "skills" / "audit" / "SKILL.md").exists()  # skill projection
    deploy_skill = (workspace / "skills" / "deploy" / "SKILL.md").read_text(encoding="utf-8")  # C6 slash_command
    assert str(plugin_root) in deploy_skill
    assert "${CLAUDE_PLUGIN_ROOT}" not in deploy_skill


@pytest.mark.asyncio
async def test_cc_plugin_import_blocks_traversal_source_without_parsing(tmp_path):
    # A blocked fetch never reaches load_cc_plugin_bundle; it returns the
    # materialization report as review evidence.
    stage_db = _StubSession()
    result = await stage_cc_plugin_import(
        stage_db,
        tenant_id=uuid4(),
        created_by_user_id=None,
        source_kind="directory",
        source_ref=str(tmp_path / "does-not-exist"),
        quarantine_root=tmp_path / "q",
        allowed_roots=[tmp_path],
    )

    assert result["status"] == "blocked"
    assert result["review"] is None
    assert result["materialization"]["blocking_notes"][0]["code"] == "local_source_not_found"
    assert stage_db.added == []  # nothing staged
