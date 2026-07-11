from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_business_task_atomic_state_columns_exist(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns, uniques, checks = await connection.run_sync(
                lambda sync_connection: (
                    {column["name"] for column in inspect(sync_connection).get_columns("tasks")},
                    {constraint["name"] for constraint in inspect(sync_connection).get_unique_constraints("tasks")},
                    {constraint["name"] for constraint in inspect(sync_connection).get_check_constraints("tasks")},
                )
            )
        assert {
            "request_id",
            "active_runtime_task_id",
            "execution_attempt",
            "last_execution_status",
            "last_error",
            "last_result",
        } <= columns
        assert "uq_tasks_tenant_agent_request_id" in uniques
        assert "ck_tasks_status" in checks
    finally:
        await engine.dispose()


def test_business_task_migration_backfills_pending_and_uncertain_legacy_runs() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "business_task_atomic_state_0711.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.down_revision == "approval_execution_envelope_0711"
    source = path.read_text(encoding="utf-8")
    assert "legacy-business-task:" in source
    assert "needs_reconciliation" in source
    assert "business_task_id" in source
    assert "active_runtime_task_id" in source
    assert "legacy_business_task_backfill" in source
