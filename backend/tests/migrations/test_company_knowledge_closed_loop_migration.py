from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "company_knowledge_closed_loop_0724.py"

COMPANY_KNOWLEDGE_TABLES = {
    "company_knowledge_source_contracts",
    "company_knowledge_sources",
    "company_knowledge_evidence",
    "company_knowledge_proposals",
    "company_knowledge_reviews",
    "company_knowledge_publications",
    "company_knowledge_events",
    "company_knowledge_outbox",
}
COMPANY_ONTOLOGY_TABLES = {
    "company_ontology_packages",
    "company_ontology_package_versions",
    "company_ontology_package_installations",
    "company_ontology_activations",
    "company_ontology_curation_runs",
    "company_ontology_releases",
    "company_ontology_object_types",
    "company_ontology_property_types",
    "company_ontology_link_types",
    "company_ontology_event_types",
    "company_ontology_rule_definitions",
    "company_ontology_action_types",
    "company_ontology_objects",
    "company_ontology_object_identities",
    "company_ontology_assertions",
    "company_ontology_links",
    "company_ontology_events",
    "company_ontology_evidence_bindings",
    "company_ontology_release_items",
}
TENANT_TABLES = COMPANY_KNOWLEDGE_TABLES | COMPANY_ONTOLOGY_TABLES


def _migration_module():
    spec = importlib.util.spec_from_file_location("company_knowledge_closed_loop_0724", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_company_knowledge_migration_contract_and_bootstrap_rls_inventory() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    module = _migration_module()
    bootstrap = (BACKEND_ROOT / "app" / "db_bootstrap.py").read_text(encoding="utf-8")

    assert module.revision == "company_knowledge_closed_loop_0724"
    assert module.down_revision == "im_unverified_transport_0719"
    assert set(module.TENANT_TABLES) == TENANT_TABLES
    assert "ALTER TABLE resource_permissions ALTER COLUMN principal_id DROP NOT NULL" in source
    assert "ALTER TABLE resource_permissions ALTER COLUMN resource_id DROP NOT NULL" in source
    for column in (
        "principal_key",
        "resource_key",
        "effect",
        "sensitivity_ceiling",
        "purposes",
        "source_acl_snapshot_hash",
        "expires_at",
        "revoked_at",
        "created_by_user_id",
        "revoked_by_user_id",
    ):
        assert f'"{column}"' in source or f"'{column}'" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_tenant_id', true)" in source
    for table in TENANT_TABLES:
        assert f'"{table}"' in bootstrap


@pytest.mark.asyncio
async def test_fresh_bootstrap_has_company_tables_columns_constraints_and_forced_rls(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:tables)"
                        ),
                        {"tables": sorted(TENANT_TABLES)},
                    )
                ).scalars()
            )
            permission_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'resource_permissions'"
                        )
                    )
                ).scalars()
            )
            rls_rows = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {"tables": sorted(TENANT_TABLES)},
                )
            ).all()
            tenant_nullable = (
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
                            "AND table_name = ANY(:tables) AND is_nullable <> 'NO'"
                        ),
                        {"tables": sorted(TENANT_TABLES)},
                    )
                )
                .scalars()
                .all()
            )

        assert tables == TENANT_TABLES
        assert {
            "principal_key",
            "resource_key",
            "effect",
            "sensitivity_ceiling",
            "purposes",
            "source_acl_snapshot_hash",
            "expires_at",
            "revoked_at",
            "created_by_user_id",
            "revoked_by_user_id",
        } <= permission_columns
        assert {row.relname for row in rls_rows} == TENANT_TABLES
        assert all(row.relrowsecurity and row.relforcerowsecurity for row in rls_rows)
        assert tenant_nullable == []
    finally:
        await engine.dispose()
