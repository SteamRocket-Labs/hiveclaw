from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "completion_reconcile_0721.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("completion_reconcile_0721", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completion_reconcile_migration_is_online_recoverable_and_generation_scoped() -> None:
    from app.models.runtime_task import COMPLETION_OUTBOX_PENDING_SQL

    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "completion_reconcile_0721"
    assert module.down_revision == "query_resource_safety_0721"
    assert "autocommit_block" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "NOT i.indisvalid OR NOT i.indisready" in source
    assert "completion_outbox_generation IS NULL" in source
    assert "status IN ('pending', 'running', 'resumable', 'suspended')" in source
    assert "task_type = 'trigger' AND status = 'skipped'" in module._PENDING_PREDICATE
    assert "tenant_id IS NOT NULL" in module._PENDING_PREDICATE
    assert "tenant_id IS NOT NULL" not in COMPLETION_OUTBOX_PENDING_SQL


@pytest.mark.asyncio
async def test_completion_reconcile_real_upgrade_preserves_history_and_recovers_stale_receipt(
    revision_parent_migrated_pg_url: str,
) -> None:
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    module = _load_module()
    _alembic_downgrade(revision_parent_migrated_pg_url, module.down_revision)
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    historical_id = uuid.uuid4()
    active_id = uuid.uuid4()

    parent = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with parent.begin() as connection:
            columns = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name='runtime_tasks'"
                        )
                    )
                ).all()
            }
            assert not any(name.startswith("completion_outbox_") for name in columns)
            assert await connection.scalar(text("SELECT to_regclass('public.ix_runtime_tasks_notification_reconcile')"))
            await connection.execute(
                text(
                    "INSERT INTO tenants("
                    "id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,"
                    "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
                    "tokens_used_today,tokens_used_month,tokens_used_total,sync_version"
                    ") VALUES ("
                    ":id,'Completion migration',:slug,'web_only',true,45,'UTC',20,5,5,0,0,0,1"
                    ")"
                ),
                {"id": tenant_id, "slug": f"cm-{tenant_id.hex}"},
            )
            for task_id, status in ((historical_id, "completed"), (active_id, "running")):
                await connection.execute(
                    text(
                        "INSERT INTO runtime_tasks("
                        "id,task_type,parent_agent_id,tenant_id,status,delegation_chain_json,depth,priority,"
                        "attempt_count,claim_version,root_idempotency_key,config_snapshot_hash,policy_snapshot_hash"
                        ") VALUES ("
                        ":id,'workflow',:agent_id,:tenant_id,:status,'[]'::jsonb,1,0,0,0,:key,repeat('a',64),repeat('b',64)"
                        ")"
                    ),
                    {
                        "id": task_id,
                        "agent_id": parent_agent_id,
                        "tenant_id": tenant_id,
                        "status": status,
                        "key": f"completion-migration:{task_id}",
                    },
                )
    finally:
        await parent.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    upgraded = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with upgraded.connect() as connection:
            generations = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT id,completion_outbox_generation FROM runtime_tasks "
                            "WHERE id IN (:historical_id,:active_id)"
                        ),
                        {"historical_id": historical_id, "active_id": active_id},
                    )
                ).all()
            )
            assert generations == {historical_id: None, active_id: 1}
            index_row = (
                await connection.execute(
                    text(
                        "SELECT i.indisvalid,i.indisready,pg_get_expr(i.indpred,i.indrelid) AS predicate "
                        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                        "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                    )
                )
            ).one()
            assert index_row.indisvalid is True
            assert index_row.indisready is True
            assert "completion_outbox_generation IS NOT NULL" in index_row.predicate
            assert (
                await connection.scalar(text("SELECT to_regclass('public.ix_runtime_tasks_notification_reconcile')"))
                is None
            )
            await connection.execute(
                text("UPDATE runtime_tasks SET status='completed',completed_at=now() WHERE id=:active_id"),
                {"active_id": active_id},
            )
            await connection.execute(text("SET LOCAL enable_seqscan=off"))
            plan = "\n".join(
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "EXPLAIN (ANALYZE,BUFFERS,COSTS OFF) "
                            "SELECT id FROM runtime_tasks "
                            f"WHERE {module._PENDING_PREDICATE} "
                            "AND (completion_outbox_attempted_at IS NULL "
                            "OR completion_outbox_attempted_at <= now() - interval '30 seconds') "
                            "ORDER BY (completion_outbox_attempted_at IS NOT NULL) ASC, "
                            "completion_outbox_attempted_at ASC, created_at ASC "
                            "LIMIT 100 FOR UPDATE SKIP LOCKED"
                        )
                    )
                ).all()
            )
            assert "ix_runtime_tasks_completion_outbox_pending" in plan
            assert "Sort" not in plan
            assert "temp read=" not in plan
            assert "temp written=" not in plan

        # Concurrent index DDL commits before Alembic writes its receipt. A
        # retry from that exact state must not fail on already-added columns.
        async with upgraded.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num=:parent WHERE version_num=:revision"),
                {"parent": module.down_revision, "revision": module.revision},
            )
    finally:
        await upgraded.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, module.revision)
    verify = create_async_engine(revision_parent_migrated_pg_url)
    try:
        async with verify.connect() as connection:
            versions = set((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            assert module.revision in versions
            assert (
                await connection.scalar(
                    text(
                        "SELECT i.indisvalid AND i.indisready FROM pg_index i "
                        "JOIN pg_class c ON c.oid=i.indexrelid "
                        "WHERE c.relname='ix_runtime_tasks_completion_outbox_pending'"
                    )
                )
                is True
            )
    finally:
        await verify.dispose()

    _alembic_upgrade(revision_parent_migrated_pg_url, "head")
