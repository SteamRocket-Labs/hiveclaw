"""Prepared worker claims must not scan completed task history each tick."""

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import dialect

from app.services.runtime_task_claim_service import (
    build_runtime_task_claim_candidate_statement,
    build_runtime_task_claim_statement,
)
from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task


async def test_generic_worker_claims_keep_active_status_indexes(owner_sessionmaker, tmp_path):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="R19a claim history")
    async with owner_sessionmaker() as db:
        await db.execute(text("""
            INSERT INTO runtime_tasks (id, tenant_id, task_type, status, depth, prompt,
                terminal_boundary_generation, terminal_boundary_enqueued_at, completion_outbox_settled_at,
                root_idempotency_key, config_snapshot_hash, policy_snapshot_hash)
            SELECT md5('r19a-plan-' || :identity || n)::uuid, t.tenant_id, 'trigger', 'completed', 1,
                repeat('completed work ', 30), 1, now(), now(),
                'r19a-plan-' || :identity || n, t.config_snapshot_hash, t.policy_snapshot_hash
            FROM generate_series(1,500000) n CROSS JOIN runtime_tasks t WHERE t.id = :task_id
        """), {"identity": seed.task_id.hex, "task_id": seed.task_id})
        await db.commit()
        await db.execute(text("ANALYZE runtime_tasks"))
        await db.commit()
        count = await db.scalar(text("SELECT count(*) FROM runtime_tasks"))
        assert count >= 500000
        connection = await db.connection()
        raw = await connection.get_raw_connection()
        await db.execute(text("SET LOCAL plan_cache_mode=force_generic_plan"))
        plans = []
        for build in (build_runtime_task_claim_candidate_statement, build_runtime_task_claim_statement):
            compiled = build(now=datetime.now(UTC)).compile(dialect=dialect(),
                compile_kwargs={"render_postcompile": True})
            prepared = await raw.driver_connection.prepare(str(compiled))

            def sql_value(value):
                if isinstance(value, (int, float)):
                    return str(value)
                return "'" + str(value).replace("'", "''") + "'"

            values = ",".join(sql_value(compiled.params[key]) for key in compiled.positiontup)
            plan = await raw.driver_connection.fetchval('EXPLAIN (FORMAT JSON) EXECUTE "'
                + prepared.get_name() + '"(' + values + ')')
            plans.append(json.loads(plan) if isinstance(plan, str) else plan)
        (tmp_path / "runtime-claims-generic.json").write_text(json.dumps(plans, indent=2))

        def nodes(plan):
            yield plan
            for child in plan.get("Plans", []):
                yield from nodes(child)

        scans = [node for plan in plans for node in nodes(plan[0]["Plan"])
            if node.get("Relation Name") == "runtime_tasks"]
        print("R19A_CLAIM_PLAN", count, [(node["Node Type"], node.get("Index Name")) for node in scans])
        assert not any(node["Node Type"] == "Seq Scan" for node in scans), scans
