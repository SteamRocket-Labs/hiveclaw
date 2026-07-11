from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_resource_authority_upgrade_contract(chain_migrated_pg_url: str) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "workspace_columns": {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("workspace_resource_manifests")
                    },
                    "artifact_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("chat_artifacts")
                    },
                    "task_columns": {column["name"] for column in inspect(sync_connection).get_columns("tasks")},
                    "activity_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("agent_activity_logs")
                    },
                    "workspace_uniques": {
                        item["name"]
                        for item in inspect(sync_connection).get_unique_constraints("workspace_resource_manifests")
                    },
                }
            )
        assert {
            "tenant_id",
            "agent_id",
            "path",
            "owner_user_id",
            "root_session_id",
            "authority_state",
            "source",
            "content_hash",
            "deleted_at",
        } <= schema["workspace_columns"]
        assert {"owner_user_id", "root_session_id", "authority_state"} <= schema["artifact_columns"]
        assert {"root_session_id", "authority_state"} <= schema["task_columns"]
        assert {"owner_user_id", "root_session_id", "authority_state"} <= schema["activity_columns"]
        assert "uq_workspace_resource_manifest_agent_path" in schema["workspace_uniques"]
    finally:
        await engine.dispose()


def test_resource_authority_migration_backfills_known_owners_and_quarantines_unknown_rows() -> None:
    migration = (Path(__file__).resolve().parents[2] / "alembic" / "versions" / "resource_authority_0711.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "resource_authority_0711"' in migration
    assert 'down_revision = "ai_asset_usage_events_0711"' in migration
    assert "chat_sessions" in migration
    assert "owner_user_id" in migration
    assert "root_session_id" in migration
    assert "quarantined" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_isolation_workspace_resource_manifests" in migration
    assert "BOOL_AND(artifact.owner_user_id IS NOT NULL)" in migration
    assert "COUNT(DISTINCT artifact.owner_user_id) = 1" in migration
    assert "artifact_authority" in migration
