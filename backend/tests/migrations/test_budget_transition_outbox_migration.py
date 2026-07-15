from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_fresh_bootstrap_forces_budget_transition_outbox():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    assert "budget_transition_outbox" in RLS_FORCED_TENANT_TABLES
    assert "budget_transition_outbox" in STRICT_TENANT_RLS_TABLES


async def test_budget_transition_outbox_upgrade_contract(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("budget_transition_outbox")
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
                        "FROM pg_class WHERE oid = 'budget_transition_outbox'::regclass"
                    )
                )
            ).one()
        assert "budget_transition_outbox" in schema["tables"]
        assert {
            "tenant_id",
            "budget_run_id",
            "budget_event_id",
            "transition",
            "session_id",
            "agent_id",
            "content",
            "delivery_target_json",
            "delivery_receipts_json",
            "status",
            "attempt_count",
            "available_at",
            "locked_by",
        } <= schema["columns"]
        assert "uq_chat_transcript_budget_transition_causation" in schema["indexes"]
        assert rls == (True, True)
    finally:
        await engine.dispose()
