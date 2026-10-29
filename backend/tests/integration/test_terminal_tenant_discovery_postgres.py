"""Terminal tenant discovery must avoid historical and sibling-lane queue scans."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.dialects.postgresql.asyncpg import dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.services.runtime_task_worker import _discover_terminal_boundary_tenants
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_terminal_tenant_discovery_uses_active_lanes_before_historical_tasks(
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
    migrated_pg_url,
):
    now = datetime.now(UTC)
    unrelated = await _seed_terminal_task(
        owner_sessionmaker, tenant_name="Unrelated pending Web lane", task_type="web_chat_turn"
    )
    await _enqueue(owner_sessionmaker, unrelated, event_kind="turn_stop", binding=_binding())
    async with owner_sessionmaker() as db:
        initial_row_count = await db.scalar(text("SELECT count(*) FROM runtime_terminal_boundary_outbox"))
    queued = await _seed_terminal_task(owner_sessionmaker, tenant_name="Queued direct lane")
    reconcile = await _seed_terminal_task(
        owner_sessionmaker, tenant_name="Unqueued direct lane", task_type="delegation"
    )
    sibling = await _seed_terminal_task(owner_sessionmaker, tenant_name="Older sibling lane", task_type="web_chat_turn")
    await _enqueue(owner_sessionmaker, queued, event_kind="runtime_terminal", binding=_binding())
    await _enqueue(owner_sessionmaker, sibling, event_kind="turn_stop", binding=_binding())
    async with owner_sessionmaker() as db:
        for seeded, age in [(queued, 1), (sibling, 3)]:
            row = await db.scalar(
                select(RuntimeTerminalBoundaryOutbox).where(
                    RuntimeTerminalBoundaryOutbox.runtime_task_id == seeded.task_id
                )
            )
            row.available_at = now - timedelta(hours=age)
        task = await db.get(RuntimeTask, reconcile.task_id)
        task.created_at = now - timedelta(hours=2)
        await db.execute(
            text("""
            INSERT INTO runtime_tasks (id,tenant_id,task_type,status,root_idempotency_key,
                config_snapshot_hash,policy_snapshot_hash,terminal_boundary_enqueued_at,prompt,depth)
            SELECT md5(:prefix || n::text)::uuid,:tenant,
                CASE WHEN n<=32768 THEN 'web_chat_turn' ELSE 'delegation' END,'completed',
                :prefix || n::text,repeat('a',64),repeat('b',64),:now,repeat(md5(n::text),8),0
            FROM generate_series(1,65536) AS n
        """),
            {"prefix": str(sibling.task_id), "tenant": sibling.tenant_id, "now": now},
        )
        await db.execute(
            text("""
            INSERT INTO runtime_terminal_boundary_outbox (id,tenant_id,runtime_task_id,agent_id,session_id,
                event_kind,terminal_status,authority_ref,authority_id,binding_json,binding_sha256,idempotency_key,
                status,available_at,delivered_at)
            SELECT md5('queue:' || t.id::text)::uuid,t.tenant_id,t.id,:agent,:session,
                'runtime_terminal','completed','runtime_task',t.id::text,'{}'::jsonb,repeat('c',64),
                md5(t.id::text)||md5(t.id::text),
                CASE WHEN t.task_type='web_chat_turn' THEN 'pending' ELSE 'delivered' END,
                :ready,CASE WHEN t.task_type='delegation' THEN CAST(:now AS timestamptz) ELSE NULL END
            FROM runtime_tasks t WHERE t.tenant_id=:tenant AND t.id<>:original
        """),
            {
                "agent": sibling.agent_id,
                "session": str(sibling.session_id),
                "ready": now - timedelta(days=1),
                "now": now,
                "tenant": sibling.tenant_id,
                "original": sibling.task_id,
            },
        )
        await db.commit()
        await db.execute(text("ANALYZE runtime_tasks"))
        await db.execute(text("ANALYZE runtime_terminal_boundary_outbox"))
        await db.commit()
        snapshot_sql = text(
            "SELECT count(*),md5(string_agg(md5((to_jsonb(q)-'task_type')::text),'' ORDER BY id)) "
            "FROM runtime_terminal_boundary_outbox q"
        )
        original_rows = (await db.execute(snapshot_sql)).one()
        assert original_rows[0] == initial_row_count + 65538

    from tests.integration.test_terminal_lane_route_migration_postgres import migrate

    migrate(migrated_pg_url, "downgrade", "terminal_queue_scan_1024", tmp_path, "history-previous")
    migrate(migrated_pg_url, "upgrade", "terminal_lane_route_1024", tmp_path, "history-populated")
    async with owner_sessionmaker() as db:
        recovered_rows = (await db.execute(snapshot_sql)).one()
        assert recovered_rows == original_rows and recovered_rows[0] == initial_row_count + 65538
    (tmp_path / "populated-terminal-discovery-upgrade.json").write_text(
        json.dumps(
            {
                "rows": recovered_rows[0],
                "non_routing_row_hash": recovered_rows[1],
                "all_non_routing_queue_bytes_preserved": True,
                "new_column_statistics_refreshed_after_upgrade": False,
            },
            indent=2,
        )
    )

    plans = []

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Select) and "UNION ALL" in str(statement):
                role = (
                    await super().execute(
                        text("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                    )
                ).one()
                assert role == ("rls_app_user", False, False)
                compiled = statement.compile(dialect=dialect(), compile_kwargs={"render_postcompile": True})
                connection = await self.connection()
                raw = await connection.get_raw_connection()
                plan = await raw.driver_connection.fetchval(
                    "EXPLAIN (FORMAT JSON) " + str(compiled), *(compiled.params[key] for key in compiled.positiontup)
                )
                plans.extend(json.loads(plan) if isinstance(plan, str) else plan)
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw["bind"], class_=ObservedSession, expire_on_commit=False)
    tenants = await _discover_terminal_boundary_tenants(
        worker_id="native-terminal-discovery",
        task_types=("business_task", "trigger", "delegation"),
        limit=2,
        now=now,
        session_factory=factory,
    )
    (tmp_path / "terminal-tenant-discovery-plan.json").write_text(json.dumps(plans, indent=2))
    assert tenants == [reconcile.tenant_id, queued.tenant_id]
    assert sibling.tenant_id not in tenants
    assert unrelated.tenant_id not in tenants
    assert plans

    def nodes(plan):
        yield plan
        for child in plan.get("Plans", []):
            yield from nodes(child)

    scans = [
        node
        for node in nodes(plans[0]["Plan"])
        if node.get("Relation Name") in {"runtime_tasks", "runtime_terminal_boundary_outbox"}
        and node["Node Type"] == "Seq Scan"
    ]
    assert not scans, [(node["Relation Name"], node["Plan Rows"], node["Total Cost"]) for node in scans]
