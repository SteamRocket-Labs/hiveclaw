from __future__ import annotations

import importlib.util
from pathlib import Path


_MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "external_capability_rls_0709.py"
_STRICT_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "external_capability_strict_rls_0709.py"
)


_STRICT_EXTERNAL_TABLES = (
    "capability_factor_reviews",
    "capability_factors",
    "capability_promotion_proposals",
    "external_capability_reviews",
    "external_capability_snapshots",
    "external_extension_activations",
    "external_extension_catalog_entries",
    "external_extension_components",
    "external_extension_hook_registrations",
    "external_marketplace_entries",
    "external_marketplace_sources",
)


def _load_migration(path: Path = _MIGRATION):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_capability_rls_targets_all_strict_tenant_tables() -> None:
    module = _load_migration()

    assert module._EXTERNAL_CAPABILITY_TABLES == _STRICT_EXTERNAL_TABLES


def test_external_capability_rls_predicate_does_not_allow_null_tenant_rows() -> None:
    module = _load_migration()
    predicate = module._tenant_predicate("external_capability_reviews")

    assert "current_setting('app.current_tenant_id', true) = 'BYPASS'" in predicate
    assert (
        "external_capability_reviews.tenant_id::text = current_setting('app.current_tenant_id', true)"
        in predicate
    )
    assert "tenant_id IS NULL" not in predicate


def test_external_capability_strict_rls_repairs_already_applied_policy_names() -> None:
    module = _load_migration(_STRICT_MIGRATION)
    src = _STRICT_MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "external_capability_strict_rls_0709"
    assert module.down_revision == "runtime_budget_run_metadata_0709"
    assert module._EXTERNAL_CAPABILITY_TABLES == _STRICT_EXTERNAL_TABLES
    assert "DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}" in src
    assert "DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}" in src
    assert "CREATE POLICY {policy_name} ON {table}" in src
    assert "tenant_id IS NULL" not in module._tenant_predicate("external_capability_reviews")
