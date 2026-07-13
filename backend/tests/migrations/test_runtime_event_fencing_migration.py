from __future__ import annotations

import ast
import json
from pathlib import Path
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_event_fencing_0710.py"


def test_runtime_event_fencing_snapshot_backfill_is_set_based() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    module = ast.parse(source)
    upgrade = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade")

    assert not any(isinstance(node, (ast.For, ast.AsyncFor)) for node in ast.walk(upgrade))
    assert "sha256(" in source
    assert "convert_to(" in source
    assert "jsonb_build_object(" in source


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


async def test_runtime_event_fencing_migrates_terminal_legacy_deep_research_tasks(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"runtime_fencing_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic_upgrade(database_url, "head")

    tenant_id = uuid.uuid4()
    task_ids = {"completed": uuid.uuid4(), "failed": uuid.uuid4()}
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # Empty-database bootstrap creates current metadata before stamping
            # head. Remove only this migration's additions and pin its parent so
            # the isolated database represents the real production upgrade.
            for statement in (
                "ALTER TABLE runtime_tasks DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE runtime_tasks DROP CONSTRAINT IF EXISTS ck_runtime_tasks_task_type",
                "ALTER TABLE runtime_tasks DROP CONSTRAINT IF EXISTS ck_runtime_tasks_status",
                "ALTER TABLE runtime_tasks DROP CONSTRAINT IF EXISTS uq_runtime_tasks_root_idempotency_key",
                "DROP INDEX IF EXISTS ix_chat_transcript_events_turn_id",
                "DROP INDEX IF EXISTS ix_chat_transcript_events_causation_id",
                "DROP INDEX IF EXISTS ix_chat_transcript_events_correlation_id",
                "DROP INDEX IF EXISTS ix_chat_transcript_events_projection_status",
                "DROP INDEX IF EXISTS ix_invocation_spans_decision_id",
                "DROP INDEX IF EXISTS ix_invocation_spans_idempotency_key",
            ):
                await connection.execute(text(statement))
            for column in (
                "claim_version",
                "root_idempotency_key",
                "config_snapshot_hash",
                "policy_snapshot_hash",
            ):
                await connection.execute(text(f"ALTER TABLE runtime_tasks DROP COLUMN IF EXISTS {column} CASCADE"))
            for column in (
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
            ):
                await connection.execute(
                    text(f"ALTER TABLE chat_transcript_events DROP COLUMN IF EXISTS {column} CASCADE")
                )
            for column in (
                "decision_id",
                "input_hash",
                "claim_version",
                "idempotency_key",
                "side_effect_refs",
            ):
                await connection.execute(text(f"ALTER TABLE invocation_spans DROP COLUMN IF EXISTS {column} CASCADE"))
            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'external_capability_strict_rls_0709'")
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO tenants (
                        id, name, slug, im_provider, is_active,
                        min_heartbeat_interval_minutes, timezone,
                        default_max_triggers, min_poll_interval_floor,
                        max_webhook_rate_ceiling, tokens_used_today,
                        tokens_used_month, tokens_used_total, sync_version
                    ) VALUES (
                        :tenant_id, 'Runtime Fencing Migration', :slug,
                        'web_only', true, 45, 'UTC', 20, 5, 5, 0, 0, 0, 1
                    )
                    """
                ),
                {"tenant_id": tenant_id, "slug": f"runtime-fencing-{tenant_id.hex[:8]}"},
            )
            for status, task_id in task_ids.items():
                await connection.execute(
                    text(
                        """
                        INSERT INTO runtime_tasks (
                            id, tenant_id, task_type, status, depth,
                            result_summary, metadata_json
                        ) VALUES (
                            :task_id, :tenant_id, 'deep_research', :status, 1, :summary,
                            CAST(:metadata_json AS json)
                        )
                        """
                    ),
                    {
                        "task_id": task_id,
                        "tenant_id": tenant_id,
                        "status": status,
                        "summary": f"legacy {status} result",
                        "metadata_json": json.dumps(
                            {
                                "deep_research": {"question": f"legacy {status} question"},
                                "artifact_refs": [f"reports/{status}.md"],
                            }
                        ),
                    },
                )
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "runtime_event_fencing_0710")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
            ) == "runtime_event_fencing_0710"
            rows = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT id, task_type, status, result_summary, metadata_json,
                               root_idempotency_key, config_snapshot_hash, policy_snapshot_hash
                        FROM runtime_tasks
                        WHERE id IN (:completed_id, :failed_id)
                        ORDER BY status
                        """
                        ),
                        {
                            "completed_id": task_ids["completed"],
                            "failed_id": task_ids["failed"],
                        },
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == 2
        assert {row["task_type"] for row in rows} == {"workflow"}
        assert {row["status"] for row in rows} == {"completed", "failed"}
        for row in rows:
            assert row["root_idempotency_key"] == f"workflow:{row['id']}"
            assert len(row["config_snapshot_hash"]) == 64
            assert len(row["policy_snapshot_hash"]) == 64
            int(row["config_snapshot_hash"], 16)
            int(row["policy_snapshot_hash"], 16)
            assert row["config_snapshot_hash"] != row["policy_snapshot_hash"]
            assert row["result_summary"] == f"legacy {row['status']} result"
            assert row["metadata_json"]["deep_research"]["question"] == (f"legacy {row['status']} question")
            assert row["metadata_json"]["artifact_refs"] == [f"reports/{row['status']}.md"]
            assert row["metadata_json"]["runtime_type_migration"] == {
                "execution_replayed": False,
                "migration_revision": "runtime_event_fencing_0710",
                "source_type": "deep_research",
                "target_type": "workflow",
            }
    finally:
        await engine.dispose()
