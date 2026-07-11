from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_ai_asset_usage_event_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("ai_asset_usage_events")
                    },
                    "indexes": {
                        item["name"] for item in inspect(sync_connection).get_indexes("ai_asset_usage_events")
                    },
                    "uniques": {
                        item["name"] for item in inspect(sync_connection).get_unique_constraints("ai_asset_usage_events")
                    },
                }
            )
        assert {
            "tenant_id",
            "asset_id",
            "asset_revision_id",
            "revision_version",
            "content_hash",
            "native_key",
            "source_ref",
            "usage_kind",
            "usage_units",
            "idempotency_key",
            "runtime_task_id",
            "session_id",
            "trace_id",
            "span_id",
            "tool_call_id",
            "evidence_json",
        } <= schema["columns"]
        assert "uq_ai_asset_usage_event_idempotency" in schema["uniques"]
        assert "ix_ai_asset_usage_events_asset_created" in schema["indexes"]
        assert "ix_ai_asset_usage_events_tenant_kind" in schema["indexes"]
    finally:
        await engine.dispose()


def test_ai_asset_usage_event_migration_backfills_residual_counts_and_forces_rls() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "ai_asset_usage_events_0711.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "ai_asset_usage_events_0711"' in migration
    assert 'down_revision = "hr_provisioning_steps_0711"' in migration
    assert "jsonb_array_elements" in migration
    assert "legacy_residual" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_isolation_ai_asset_usage_events" in migration
    assert "needs_reapproval" in migration
    assert "status = 'rejected'" in migration
