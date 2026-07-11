from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_runtime_task_root_authority_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "columns": {column["name"] for column in inspect(sync_connection).get_columns("runtime_tasks")},
                    "indexes": {item["name"] for item in inspect(sync_connection).get_indexes("runtime_tasks")},
                }
            )
        assert {"root_user_id", "root_session_id", "delegation_chain_json"} <= schema["columns"]
        assert {
            "ix_runtime_tasks_root_user_id",
            "ix_runtime_tasks_root_session_id",
            "ix_runtime_tasks_root_authority",
        } <= schema["indexes"]
    finally:
        await engine.dispose()


def test_runtime_task_root_authority_migration_backfills_all_available_truth_sources() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_task_root_authority_0711.py"
    ).read_text(encoding="utf-8")
    assert "execution_principal" in migration
    assert "runtime_budget_runs" in migration
    assert "chat_sessions" in migration
    assert "parent_session_id" in migration
    assert "delegation_chain_json" in migration
