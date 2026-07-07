from __future__ import annotations

import importlib.util
from pathlib import Path


_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "personal_knowledge_core_0707.py"

_KNOWLEDGE_TABLES = [
    "knowledge_documents",
    "knowledge_segments",
    "knowledge_entities",
    "knowledge_assertions",
    "knowledge_links",
    "knowledge_index_jobs",
    "knowledge_grants",
]


def _load_migration():
    spec = importlib.util.spec_from_file_location("_personal_knowledge_core_0707", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_personal_knowledge_migration_revises_current_plugin_head() -> None:
    module = _load_migration()

    assert module.revision == "personal_knowledge_core_0707"
    assert module.down_revision == "external_extension_catalog_entries_0707"


def test_personal_knowledge_migration_targets_all_core_tables_for_rls() -> None:
    assert _load_migration()._KNOWLEDGE_TABLES == _KNOWLEDGE_TABLES


def test_personal_knowledge_migration_uses_postgres_native_text_search_not_pgvector() -> None:
    src = _MIGRATION.read_text(encoding="utf-8")

    assert "postgresql.TSVECTOR" in src
    assert "USING gin (tsv)" in src
    assert "CREATE EXTENSION IF NOT EXISTS vector" not in src
    assert "pgvector" not in src.lower()


def test_personal_knowledge_migration_enables_force_rls_for_all_tables() -> None:
    src = _MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in src
    assert "ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in src
    assert "CREATE POLICY tenant_isolation_{table} ON {table}" in src
    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in src
    assert "tenant_id::text = current_setting('app.current_tenant_id', true)" in src
