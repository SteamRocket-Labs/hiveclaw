from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_hr_provisioning_upgrade_contract(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "draft_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("hr_creation_drafts")
                    },
                    "step_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("hr_provisioning_steps")
                    },
                    "step_indexes": {
                        item["name"] for item in inspect(sync_connection).get_indexes("hr_provisioning_steps")
                    },
                }
            )
        assert {"claim_token", "claim_version", "claim_heartbeat_at"} <= schema["draft_columns"]
        assert {
            "tenant_id",
            "draft_id",
            "step_key",
            "step_kind",
            "required",
            "status",
            "input_hash",
            "attempt_count",
            "receipt_json",
            "error_code",
            "error_message",
        } <= schema["step_columns"]
        assert {
            "ix_hr_provisioning_steps_draft_order",
            "ix_hr_provisioning_steps_tenant_status",
        } <= schema["step_indexes"]
    finally:
        await engine.dispose()


def test_hr_provisioning_migration_contains_legacy_backfill_and_forced_rls() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hr_provisioning_steps_0711.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "hr_provisioning_steps_0711"' in migration
    assert 'down_revision = "runtime_task_root_authority_0711"' in migration
    assert "jsonb_array_elements_text" in migration
    assert "legacy_completed_draft" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_isolation_hr_provisioning_steps" in migration
