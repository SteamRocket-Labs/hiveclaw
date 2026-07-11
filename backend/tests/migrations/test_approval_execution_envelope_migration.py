from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_approval_execution_envelope_upgrade_adds_bound_columns(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("approval_requests")
                }
            )
        assert {"execution_envelope", "execution_envelope_hash"} <= columns
    finally:
        await engine.dispose()


def test_approval_execution_envelope_migration_invalidates_legacy_tool_tickets_reversibly() -> None:
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "approval_execution_envelope_0711.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.down_revision == "plan_authorization_lease_0711"
    assert migration.LEGACY_INVALIDATION_MARKER == "approval_execution_envelope_0711"
    source = path.read_text(encoding="utf-8")
    assert "execution_status = 'needs_reapproval'" in source
    assert "legacy_approval_previous_status" in source
    assert "legacy_approval_previous_execution_status" in source
    assert "legacy_approval_previous_resolved_at" in source
    assert "execution_envelope IS NULL" in source
    assert "legacy_approval_invalidated_by" in source
    assert "DROP COLUMN execution_envelope" not in source
    assert 'op.drop_column("approval_requests", "execution_envelope")' in source
