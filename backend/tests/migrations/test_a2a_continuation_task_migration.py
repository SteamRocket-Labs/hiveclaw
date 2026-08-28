from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "a2a_continuation_task_0828.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("a2a_continuation_task_0828", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a2a_continuation_migration_mirrors_the_current_model_contract() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_PENDING_SQL

    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "a2a_continuation_task_0828"
    assert module.down_revision == "merge_incident_kimi_0725"
    assert "a2a_continuation" in module._RUNTIME_TASK_TYPES
    assert "a2a_continuation" not in module._LEGACY_RUNTIME_TASK_TYPES
    assert module._OUTBOX_PENDING_PREDICATE == COMPLETION_OUTBOX_PENDING_SQL
    assert "a2a_continuation" not in module._OUTBOX_LEGACY_PENDING_PREDICATE
    assert "a2a_continuation" in module._OUTBOX_SOURCE_KINDS
    assert "a2a_continuation" not in module._OUTBOX_LEGACY_SOURCE_KINDS
    assert "a2a_continuation" in module._ACTIVE_RUN_TYPES
    assert "a2a_continuation" not in module._ACTIVE_RUN_LEGACY_TYPES
    assert "pg_get_constraintdef" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "ALTER INDEX" in source
    assert "NOT i.indisvalid OR NOT i.indisready" in source


@pytest.mark.asyncio
async def test_a2a_continuation_real_upgrade_is_planner_eligible_and_retry_safe(
    revision_parent_migrated_pg_url: str,
) -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_PENDING_SQL
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    module = _load_module()
    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    parent = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with parent.connect() as connection:
            legacy_constraint = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_tasks'::regclass AND conname='ck_runtime_tasks_task_type'"
                )
            )
            assert legacy_constraint is not None
            assert "a2a_continuation" not in legacy_constraint
            legacy_source_kinds = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_notification_outbox'::regclass "
                    "AND conname='ck_runtime_notification_outbox_source_kind'"
                )
            )
            assert legacy_source_kinds is not None
            assert "a2a_continuation" not in legacy_source_kinds
            for index_name in (
                "ix_runtime_tasks_completion_outbox_pending",
                "uq_runtime_tasks_active_web_chat_session",
            ):
                legacy_predicate = await connection.scalar(
                    text(
                        "SELECT pg_get_expr(i.indpred,i.indrelid) FROM pg_index i "
                        "JOIN pg_class c ON c.oid=i.indexrelid "
                        f"WHERE c.relname='{index_name}'"
                    )
                )
                assert legacy_predicate is not None
                assert "a2a_continuation" not in legacy_predicate
    finally:
        await parent.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    upgraded = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with upgraded.connect() as connection:
            constraint = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_tasks'::regclass AND conname='ck_runtime_tasks_task_type'"
                )
            )
            assert constraint is not None and "a2a_continuation" in constraint
            source_kinds = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_notification_outbox'::regclass "
                    "AND conname='ck_runtime_notification_outbox_source_kind'"
                )
            )
            assert source_kinds is not None and "a2a_continuation" in source_kinds
            outbox_row = (
                await connection.execute(
                    text(
                        "SELECT i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) AS predicate "
                        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                        "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                    )
                )
            ).one()
            assert outbox_row.indisvalid is True
            assert outbox_row.indisready is True
            assert "a2a_continuation" in outbox_row.predicate
            active_predicate = await connection.scalar(
                text(
                    "SELECT pg_get_expr(i.indpred,i.indrelid) FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE c.relname='uq_runtime_tasks_active_web_chat_session'"
                )
            )
            assert active_predicate is not None
            assert "a2a_continuation" in active_predicate
            assert "suspended" in active_predicate and "resumable" in active_predicate
            for leftover in (
                "ix_runtime_tasks_completion_outbox_pending_a2a_new",
                "ix_runtime_tasks_completion_outbox_pending_a2a_old",
            ):
                assert (await connection.scalar(text(f"SELECT to_regclass('public.{leftover}')"))) is None
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
                    text("SELECT to_regclass('public.ix_runtime_tasks_completion_outbox_pending_a2a_new')")
                )
                is None
            )
    finally:
        await verify.dispose()

    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    downgraded = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with downgraded.connect() as connection:
            legacy_constraint = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_tasks'::regclass AND conname='ck_runtime_tasks_task_type'"
                )
            )
            assert legacy_constraint is not None
            assert "a2a_continuation" not in legacy_constraint
            legacy_source_kinds = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='runtime_notification_outbox'::regclass "
                    "AND conname='ck_runtime_notification_outbox_source_kind'"
                )
            )
            assert legacy_source_kinds is not None
            assert "a2a_continuation" not in legacy_source_kinds
            for index_name in (
                "ix_runtime_tasks_completion_outbox_pending",
                "uq_runtime_tasks_active_web_chat_session",
            ):
                legacy_predicate = await connection.scalar(
                    text(
                        "SELECT pg_get_expr(i.indpred,i.indrelid) FROM pg_index i "
                        "JOIN pg_class c ON c.oid=i.indexrelid "
                        f"WHERE c.relname='{index_name}'"
                    )
                )
                assert legacy_predicate is not None
                assert "a2a_continuation" not in legacy_predicate
    finally:
        await downgraded.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, "head")
