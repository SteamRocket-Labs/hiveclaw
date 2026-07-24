from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "completion_outbox_index_0721.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("completion_outbox_index_0721", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completion_outbox_index_migration_replaces_the_production_ineligible_predicate() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_PENDING_SQL

    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "completion_outbox_index_0721"
    assert module.down_revision == "completion_reconcile_0721"
    assert "tenant_id IS NOT NULL" in module._LEGACY_PENDING_PREDICATE
    assert "tenant_id IS NOT NULL" not in module._PENDING_PREDICATE
    assert module._PENDING_PREDICATE == COMPLETION_OUTBOX_PENDING_SQL
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "ALTER INDEX" in source
    assert "NOT i.indisvalid OR NOT i.indisready" in source


@pytest.mark.asyncio
async def test_completion_outbox_index_real_upgrade_is_planner_eligible_and_retry_safe(
    revision_parent_migrated_pg_url: str,
) -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_PENDING_SQL
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    module = _load_module()
    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    parent = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with parent.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='runtime_tasks' "
                        "AND column_name='tenant_id'"
                    )
                )
                == "NO"
            )
            legacy_predicate = await connection.scalar(
                text(
                    "SELECT pg_get_expr(i.indpred,i.indrelid) FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                )
            )
            assert "tenant_id IS NOT NULL" in legacy_predicate
    finally:
        await parent.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    upgraded = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with upgraded.connect() as connection:
            index_row = (
                await connection.execute(
                    text(
                        "SELECT i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) AS predicate "
                        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                        "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                    )
                )
            ).one()
            assert index_row.indisvalid is True
            assert index_row.indisready is True
            assert "tenant_id IS NOT NULL" not in index_row.predicate
            assert (
                await connection.scalar(
                    text("SELECT to_regclass('public.ix_runtime_tasks_completion_outbox_pending_replacement')")
                )
                is None
            )
            await connection.execute(text("SET LOCAL enable_seqscan=off"))
            plan = "\n".join(
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "EXPLAIN (ANALYZE,BUFFERS,COSTS OFF) "
                            "SELECT id FROM runtime_tasks "
                            f"WHERE {COMPLETION_OUTBOX_PENDING_SQL} "
                            "AND (completion_outbox_attempted_at IS NULL "
                            "OR completion_outbox_attempted_at <= now() - interval '30 seconds') "
                            "ORDER BY (completion_outbox_attempted_at IS NOT NULL) ASC, "
                            "completion_outbox_attempted_at ASC, created_at ASC "
                            "LIMIT 100 FOR UPDATE SKIP LOCKED"
                        )
                    )
                ).all()
            )
            assert "ix_runtime_tasks_completion_outbox_pending" in plan
            assert "Sort" not in plan
            assert "temp read=" not in plan
            assert "temp written=" not in plan

        # Simulate death after concurrent DDL commits but before Alembic writes
        # its receipt. Re-running must replace the official index safely.
        async with upgraded.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num=:parent WHERE version_num=:revision"),
                {"parent": module.down_revision, "revision": module.revision},
            )
    finally:
        await upgraded.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    verify = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with verify.connect() as connection:
            versions = set((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            assert module.revision in versions
            assert (
                await connection.scalar(
                    text("SELECT to_regclass('public.ix_runtime_tasks_completion_outbox_pending_replacement')")
                )
                is None
            )
    finally:
        await verify.dispose()

    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    downgraded = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with downgraded.connect() as connection:
            legacy_predicate = await connection.scalar(
                text(
                    "SELECT pg_get_expr(i.indpred,i.indrelid) FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                )
            )
            assert "tenant_id IS NOT NULL" in legacy_predicate
    finally:
        await downgraded.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, "head")
