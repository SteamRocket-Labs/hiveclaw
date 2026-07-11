from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_fresh_bootstrap_forces_channel_delivery_outbox_rls():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    assert "channel_delivery_outbox" in RLS_FORCED_TENANT_TABLES
    assert "channel_delivery_outbox" in STRICT_TENANT_RLS_TABLES


async def test_channel_delivery_outbox_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("channel_delivery_outbox")
                    },
                    "unique": {
                        item["name"]
                        for item in inspect(sync_connection).get_unique_constraints("channel_delivery_outbox")
                    },
                }
            )
            rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE oid = 'channel_delivery_outbox'::regclass"
                    )
                )
            ).one()
        assert "channel_delivery_outbox" in schema["tables"]
        assert {
            "tenant_id",
            "runtime_task_id",
            "agent_id",
            "session_id",
            "user_id",
            "external_principal_id",
            "channel_config_id",
            "delivery_target_json",
            "text_content",
            "artifact_ids_json",
            "delivery_receipts_json",
            "status",
            "attempt_count",
            "available_at",
        } <= schema["columns"]
        assert "uq_channel_delivery_outbox_runtime_target" in schema["unique"]
        assert rls == (True, True)
    finally:
        await engine.dispose()
