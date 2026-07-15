from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_approval_ticket_governance_upgrade_contract(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns, indexes, uniques, checks = await connection.run_sync(
                lambda sync_connection: (
                    {column["name"] for column in inspect(sync_connection).get_columns("approval_requests")},
                    {index["name"] for index in inspect(sync_connection).get_indexes("approval_requests")},
                    {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_unique_constraints("approval_requests")
                    },
                    {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_check_constraints("approval_requests")
                    },
                )
            )
        assert {
            "requested_by",
            "tool_name",
            "normalized_arguments",
            "input_hash",
            "policy_snapshot",
            "policy_snapshot_hash",
            "expires_at",
            "consumed_at",
            "execution_status",
            "execution_idempotency_key",
            "execution_result",
            "execution_receipt",
            "decision_id",
        } <= columns
        assert {
            "ix_approval_requests_requested_by",
            "ix_approval_requests_input_hash",
            "ix_approval_requests_expires_at",
            "ix_approval_requests_decision_id",
        } <= indexes
        assert "uq_approval_requests_execution_idempotency_key" in uniques
        assert "ck_approval_requests_execution_status" in checks
    finally:
        await engine.dispose()
