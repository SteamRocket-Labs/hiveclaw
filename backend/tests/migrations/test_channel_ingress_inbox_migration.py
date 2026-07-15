from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_fresh_bootstrap_forces_channel_ingress_rls():
    from app.db_bootstrap import RLS_FORCED_TENANT_TABLES, STRICT_TENANT_RLS_TABLES

    assert "channel_ingress_events" in set(RLS_FORCED_TENANT_TABLES)
    assert "channel_ingress_events" in set(STRICT_TENANT_RLS_TABLES)


async def test_channel_ingress_upgrade_contract(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("channel_ingress_events")
                    },
                    "indexes": {
                        index["name"] for index in inspect(sync_connection).get_indexes("channel_ingress_events")
                    },
                    "chat_message_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("chat_messages")
                    },
                    "chat_message_indexes": {
                        index["name"] for index in inspect(sync_connection).get_indexes("chat_messages")
                    },
                    "uniques": {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_unique_constraints("channel_ingress_events")
                    },
                }
            )
            rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE oid = 'channel_ingress_events'::regclass"
                    )
                )
            ).one()
        assert "channel_ingress_events" in schema["tables"]
        assert {
            "tenant_id",
            "agent_id",
            "provider",
            "installation_ref",
            "provider_event_id",
            "handler_key",
            "payload_digest",
            "payload_json",
            "status",
            "attempt_count",
            "available_at",
            "locked_by",
            "locked_at",
            "result_runtime_task_id",
            "result_session_id",
            "last_error",
            "processed_at",
        } <= schema["columns"]
        assert "ix_channel_ingress_claim" in schema["indexes"]
        assert "uq_channel_ingress_provider_event" in schema["uniques"]
        assert "source_ingress_event_id" in schema["chat_message_columns"]
        assert "uq_chat_messages_ingress_user" in schema["chat_message_indexes"]
        assert rls == (True, True)
    finally:
        await engine.dispose()
