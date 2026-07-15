from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "storage_blob_lifecycle_0715.py"
    spec = importlib.util.spec_from_file_location("storage_blob_lifecycle_0715", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path.read_text(encoding="utf-8")


def test_storage_migration_is_on_current_head_and_creates_all_tables() -> None:
    module, source = _load()

    assert module.revision == "storage_blob_lifecycle_0715"
    assert module.down_revision == "agent_model_tenant_authority_0715"
    for table in ("storage_blobs", "storage_blob_refs", "storage_gc_runs"):
        assert f'"{table}"' in source


def test_storage_migration_forces_fail_closed_tenant_rls() -> None:
    _module, source = _load()

    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in source
    assert "tenant_id::text = current_setting('app.current_tenant_id', true)" in source
    assert "tenant_id IS NULL" not in source
