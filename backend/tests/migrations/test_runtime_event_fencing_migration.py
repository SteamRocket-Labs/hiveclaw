from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_runtime_event_fencing_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    table: {column["name"] for column in inspect(sync_connection).get_columns(table)}
                    for table in ("runtime_tasks", "chat_transcript_events", "invocation_spans")
                }
            )
        runtime_task_columns = schema["runtime_tasks"]
        assert {
            "claim_version",
            "root_idempotency_key",
            "config_snapshot_hash",
            "policy_snapshot_hash",
        } <= runtime_task_columns

        transcript_columns = schema["chat_transcript_events"]
        assert {
            "schema_version",
            "item_type",
            "item_status",
            "turn_id",
            "causation_id",
            "correlation_id",
            "projection_status",
            "projection_attempts",
            "projection_error",
            "projected_at",
        } <= transcript_columns

        span_columns = schema["invocation_spans"]
        assert {
            "decision_id",
            "input_hash",
            "claim_version",
            "idempotency_key",
            "side_effect_refs",
        } <= span_columns
    finally:
        await engine.dispose()
