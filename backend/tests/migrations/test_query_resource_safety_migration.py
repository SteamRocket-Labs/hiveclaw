from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "query_resource_safety_0721.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("query_resource_safety_0721", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_resource_safety_migration_is_current_and_online() -> None:
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "query_resource_safety_0721"
    assert module.down_revision == "im_unverified_transport_0719"
    assert "autocommit_block" in source
    assert "NOT i.indisvalid" in source
    assert "_drop_invalid_index(name)" in source
    assert source.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") == 9
    for index_name in (
        "ix_agent_activity_logs_agent_created_at",
        "ix_agent_activity_logs_agent_action_created_at",
        "ix_agent_activity_logs_tenant_created_at",
        "ix_agent_activity_logs_tenant_action_created_at",
        "ix_chat_sessions_dashboard_recent",
        "ix_chat_messages_conversation_created_at",
        "ix_runtime_notification_outbox_source_lookup",
        "ix_resource_permissions_principal_resource",
        "ix_runtime_tasks_notification_reconcile",
    ):
        assert index_name in source


def test_query_resource_safety_migration_has_reversible_index_cleanup() -> None:
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert len(module._INDEXES) == 9
    assert "for name, _statement in reversed(_INDEXES)" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS {name}" in source


@pytest.mark.asyncio
async def test_query_resource_safety_real_downgrade_upgrade_and_stale_receipt_recovery(
    revision_parent_migrated_pg_url: str,
) -> None:
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    module = _load_module()
    expected = {name for name, _statement in module._INDEXES}

    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    engine = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT index_class.relname,index_row.indisvalid,index_row.indisready "
                    "FROM pg_catalog.pg_index AS index_row "
                    "JOIN pg_catalog.pg_class AS index_class ON index_class.oid=index_row.indexrelid "
                    "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=index_class.relnamespace "
                    "WHERE namespace.nspname='public' AND index_class.relname=ANY(:names)"
                ),
                {"names": list(expected)},
            )
            assert result.all() == []
            versions = set((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            assert module.down_revision in versions
    finally:
        await engine.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    engine = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT index_class.relname,index_row.indisvalid,index_row.indisready "
                        "FROM pg_catalog.pg_index AS index_row "
                        "JOIN pg_catalog.pg_class AS index_class ON index_class.oid=index_row.indexrelid "
                        "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=index_class.relnamespace "
                        "WHERE namespace.nspname='public' AND index_class.relname=ANY(:names)"
                    ),
                    {"names": list(expected)},
                )
            ).all()
            assert {row.relname for row in rows} == expected
            assert all(row.indisvalid and row.indisready for row in rows)

        # Concurrent DDL commits before Alembic records the revision. Simulate
        # a process death in that narrow window and prove a retry is harmless.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num=:parent WHERE version_num=:revision"),
                {"parent": module.down_revision, "revision": module.revision},
            )
    finally:
        await engine.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    verify = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with verify.connect() as connection:
            versions = set((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            assert module.revision in versions
            invalid = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_index AS index_row "
                    "JOIN pg_catalog.pg_class AS index_class ON index_class.oid=index_row.indexrelid "
                    "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=index_class.relnamespace "
                    "WHERE namespace.nspname='public' AND index_class.relname=ANY(:names) "
                    "AND (NOT index_row.indisvalid OR NOT index_row.indisready)"
                ),
                {"names": list(expected)},
            )
            assert invalid == 0
    finally:
        await verify.dispose()

    # This fixture is shared by the migration suite; restore the actual head.
    _alembic_upgrade(revision_parent_migrated_pg_url, "head")
