from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_fresh_bootstrap_forces_recent_durable_state_tables():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    tables = {
        "workflow_proposal_artifacts",
        "workflow_preview_artifacts",
        "runtime_notification_outbox",
    }
    assert tables <= set(RLS_FORCED_TENANT_TABLES)
    assert tables <= set(STRICT_TENANT_RLS_TABLES)


async def test_runtime_notification_outbox_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("runtime_notification_outbox")
                    },
                    "indexes": {
                        index["name"] for index in inspect(sync_connection).get_indexes("chat_transcript_events")
                    },
                }
            )
            rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert "runtime_notification_outbox" in schema["tables"]
        assert {
            "tenant_id",
            "source_kind",
            "source_run_id",
            "parent_session_id",
            "terminal_status",
            "delivery_mode",
            "payload_rank",
            "status",
            "attempt_count",
            "available_at",
            "locked_by",
            "delivery_receipt_json",
        } <= schema["columns"]
        assert "uq_chat_transcript_completion_causation" in schema["indexes"]
        assert rls == (True, True)
    finally:
        await engine.dispose()
