from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_plan_authorization_upgrade_adds_task_evidence_column(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("tasks")}
            )
        assert "plan_authorization" in columns
    finally:
        await engine.dispose()


def test_plan_authorization_migration_declares_safe_legacy_invalidation():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "plan_authorization_lease_0711.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "runtime_notification_outbox_0710"
    assert migration.LEGACY_INVALIDATION_MARKER == "plan_authorization_lease_0711"
    source = path.read_text(encoding="utf-8")
    assert "SET status = 'expired'" in source
    assert "NOT EXISTS" in source and "action_type = 'plan_authorization'" in source
    assert "legacy_plan_authorization_previous_expires_at" in source
    assert "SET status = 'confirmed'" in source
