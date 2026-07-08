from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.external_capabilities.legacy_pack_adapter import (
    build_legacy_pack_migration_report,
    normalize_legacy_installed_plugin,
    sweep_legacy_pack_migration_dry_run,
)


class _ScalarRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _LegacySession:
    def __init__(self, rows):
        self.rows = list(rows)
        self.added = []
        self.commit_calls = 0

    async def execute(self, _stmt):
        return _ScalarRows(self.rows.pop(0))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commit_calls += 1


def test_legacy_pack_adapter_is_migration_only_normalized_report():
    tenant_id = uuid4()
    plugin = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        plugin_key="office_pack",
        version="1.2.3",
        source_kind="builtin",
        source_ref="backend/packs/office_pack/pack.yaml",
        status="enabled",
        config_json={"enabled_by": "admin"},
        lockfile_json={"content_sha256": "manifest-sha"},
    )

    bundle = normalize_legacy_installed_plugin(plugin)

    assert bundle.source_format == "legacy_pack"
    assert bundle.source_uri == "legacy-pack:office_pack"
    assert bundle.plugin_name == "office_pack"
    assert bundle.version == "1.2.3"
    assert bundle.components == []
    assert bundle.lockfile["content_sha256"] == "manifest-sha"
    assert bundle.unsupported_components[0]["component_type"] == "legacy_pack_projection"
    assert bundle.admission_notes[0]["migration_only"] is True


def test_legacy_pack_report_projects_catalog_and_agent_activation_without_runtime_write():
    tenant_id = uuid4()
    plugin_id = uuid4()
    agent_id = uuid4()
    plugin = SimpleNamespace(
        id=plugin_id,
        tenant_id=tenant_id,
        plugin_key="web_pack",
        version="0.4.0",
        source_kind="builtin",
        source_ref=None,
        status="enabled",
        config_json={},
        lockfile_json={},
    )
    assignment = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        installed_plugin_id=plugin_id,
        enabled=True,
    )

    report = build_legacy_pack_migration_report([plugin], [assignment])

    assert report["migration_only"] is True
    assert report["counts"] == {"plugins": 1, "assignments": 1, "enabled_assignments": 1}
    assert report["catalog_projections"][0]["plugin_key"] == "web_pack"
    assert report["catalog_projections"][0]["policy"] == "approved_available"
    assert report["activation_projections"][0]["agent_id"] == str(agent_id)
    assert report["activation_projections"][0]["activation_scope"] == "agent"
    assert report["runtime_writes"] == []


@pytest.mark.asyncio
async def test_legacy_pack_dry_run_sweep_does_not_write_database():
    tenant_id = uuid4()
    plugin = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        plugin_key="web_pack",
        version="0.4.0",
        source_kind="builtin",
        source_ref=None,
        status="enabled",
        config_json={},
        lockfile_json={},
    )
    db = _LegacySession([[plugin], []])

    report = await sweep_legacy_pack_migration_dry_run(db, tenant_id=tenant_id)

    assert report["counts"]["plugins"] == 1
    assert db.added == []
    assert db.commit_calls == 0
