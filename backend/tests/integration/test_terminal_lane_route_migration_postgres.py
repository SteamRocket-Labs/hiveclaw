"""Populated upgrades retain queue evidence and canonical, invoker-scoped routes."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
from tests.integration import conftest as integration
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


def migrate(url, action, target, root, step):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", action, target],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    (root / f"{step}-{action}.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr


async def test_terminal_lane_upgrade_preserves_old_queue_and_native_route_authority(
    migrated_pg_url,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Terminal route upgrade")
    delivered = await _seed_terminal_task(
        owner_sessionmaker, tenant_name="Delivered terminal history", task_type="delegation"
    )
    await _enqueue(owner_sessionmaker, seed, event_kind="runtime_terminal", binding=_binding())
    await _enqueue(owner_sessionmaker, delivered, event_kind="runtime_terminal", binding=_binding())
    async with owner_sessionmaker() as db:
        await db.execute(
            text(
                "UPDATE runtime_terminal_boundary_outbox SET status='delivered',delivered_at=now() "
                "WHERE runtime_task_id=:task"
            ),
            {"task": delivered.task_id},
        )
        await db.commit()
        original = (
            await db.execute(
                text("SELECT id,to_jsonb(q)-'task_type' AS body FROM runtime_terminal_boundary_outbox q ORDER BY id")
            )
        ).all()
        original = {str(row.id): row.body for row in original}

    migrate(migrated_pg_url, "downgrade", "terminal_queue_scan_1024", tmp_path, "previous")
    async with owner_sessionmaker() as db:
        assert not await db.scalar(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='runtime_terminal_boundary_outbox' AND column_name='task_type'"
            )
        )
        assert {
            str(row.id): row.body
            for row in (
                await db.execute(
                    text("SELECT id,to_jsonb(q) AS body FROM runtime_terminal_boundary_outbox q ORDER BY id")
                )
            ).all()
        } == original

    migrate(migrated_pg_url, "upgrade", "terminal_lane_route_1024", tmp_path, "populated")
    async with owner_sessionmaker() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id,task_type,to_jsonb(q)-'task_type' AS body "
                    "FROM runtime_terminal_boundary_outbox q ORDER BY id"
                )
            )
        ).all()
        assert {str(row.id): row.body for row in rows} == original
        active = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id)
        )
        old = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == delivered.task_id
            )
        )
        assert active.task_type == "business_task" and old.task_type is None
        assert (
            await db.execute(
                text(
                    "SELECT proname,prosecdef FROM pg_proc WHERE proname IN "
                    "('set_terminal_boundary_task_route','sync_terminal_boundary_task_route') ORDER BY proname"
                )
            )
        ).all() == [("set_terminal_boundary_task_route", False), ("sync_terminal_boundary_task_route", False)]
        indexes = (
            await db.execute(
                text(
                    "SELECT c.relname,i.indisvalid,i.indisready FROM pg_index i "
                    "JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname IN "
                    "('ix_runtime_terminal_boundary_active_lane','ix_runtime_tasks_terminal_discovery') ORDER BY c.relname"
                )
            )
        ).all()
        assert indexes == [
            ("ix_runtime_tasks_terminal_discovery", True, True),
            ("ix_runtime_terminal_boundary_active_lane", True, True),
        ]
        # Simulate the version marker not committing after a completed online index build.
        await db.execute(text("UPDATE alembic_version SET version_num='terminal_queue_scan_1024'"))
        await db.commit()
    migrate(migrated_pg_url, "upgrade", "terminal_lane_route_1024", tmp_path, "recovered")

    task_id, queue_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=seed.tenant_id,
                task_type="business_task",
                status="completed",
                terminal_boundary_enqueued_at=active.created_at,
                prompt="Native old-writer route",
            )
        )
        await db.commit()
        # An older writer omits the new routing column. The trigger reads the real task.
        await db.execute(
            text("""
            INSERT INTO runtime_terminal_boundary_outbox
                (id,tenant_id,runtime_task_id,agent_id,session_id,event_kind,terminal_status,
                 authority_ref,authority_id,binding_json,binding_sha256,idempotency_key)
            SELECT :id,tenant_id,CAST(:task AS uuid),agent_id,session_id,event_kind,terminal_status,
                authority_ref,CAST(CAST(:task AS uuid) AS text),binding_json,binding_sha256,repeat('d',64)
            FROM runtime_terminal_boundary_outbox WHERE id=:source
        """),
            {"id": queue_id, "task": task_id, "source": active.id},
        )
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        role = (
            await db.execute(text("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert role == ("rls_app_user", False, False)
        assert (
            await db.scalar(
                select(RuntimeTerminalBoundaryOutbox.task_type).where(RuntimeTerminalBoundaryOutbox.id == queue_id)
            )
            == "business_task"
        )
        await db.execute(
            text("UPDATE runtime_terminal_boundary_outbox SET task_type='delegation' WHERE id=:id"), {"id": queue_id}
        )
        assert (
            await db.scalar(
                select(RuntimeTerminalBoundaryOutbox.task_type).where(RuntimeTerminalBoundaryOutbox.id == queue_id)
            )
            == "business_task"
        )
        await db.execute(text("UPDATE runtime_tasks SET task_type='trigger' WHERE id=:id"), {"id": task_id})
        assert (
            await db.scalar(
                select(RuntimeTerminalBoundaryOutbox.task_type).where(RuntimeTerminalBoundaryOutbox.id == queue_id)
            )
            == "trigger"
        )
        assert not await db.scalar(
            select(RuntimeTerminalBoundaryOutbox.id).where(
                RuntimeTerminalBoundaryOutbox.runtime_task_id == delivered.task_id
            )
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        # Corrupted routing metadata still cannot override the canonical task at claim.
        await db.execute(
            text("ALTER TABLE runtime_terminal_boundary_outbox DISABLE TRIGGER trg_terminal_boundary_task_route")
        )
        await db.execute(
            text("UPDATE runtime_terminal_boundary_outbox SET task_type='delegation' WHERE id=:id"), {"id": queue_id}
        )
        await db.execute(
            text("ALTER TABLE runtime_terminal_boundary_outbox ENABLE TRIGGER trg_terminal_boundary_task_route")
        )
        await db.commit()
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    assert (
        await service.claim_batch(
            tenant_id=seed.tenant_id, worker_id="route-corruption-control", task_types=("delegation",)
        )
        == []
    )
    (tmp_path / "terminal-route-upgrade.json").write_text(
        json.dumps(
            {
                "old_active_route_backfilled": True,
                "delivered_payload_unchanged": True,
                "interrupted_version_marker_recovered": True,
                "old_writer_and_task_update_routed": True,
                "trigger_security_invoker": True,
                "cross_tenant_hidden": True,
                "corrupt_route_canonical_recheck": True,
                "role": "rls_app_user",
                "superuser": False,
                "bypassrls": False,
            },
            indent=2,
        )
    )


async def test_terminal_route_backfill_keeps_reads_available_and_recovers_committed_batches(
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Interrupted online route migration")
    await _enqueue(owner_sessionmaker, seed, event_kind="runtime_terminal", binding=_binding())
    async with owner_sessionmaker() as db:
        await db.execute(
            text("""
            INSERT INTO runtime_tasks (id,tenant_id,task_type,status,prompt,depth,
                root_idempotency_key,config_snapshot_hash,policy_snapshot_hash)
            SELECT md5(:prefix || n::text)::uuid,:tenant,'business_task','completed','Old queued task',0,
                :prefix || n::text,repeat('a',64),repeat('b',64)
            FROM generate_series(1,2001) n
        """),
            {"prefix": str(seed.task_id), "tenant": seed.tenant_id},
        )
        await db.execute(
            text("""
            INSERT INTO runtime_terminal_boundary_outbox
                (id,tenant_id,runtime_task_id,agent_id,session_id,event_kind,terminal_status,
                 authority_ref,authority_id,binding_json,binding_sha256,idempotency_key)
            SELECT lpad(to_hex(n),32,'0')::uuid,q.tenant_id,md5(:prefix || n::text)::uuid,
                q.agent_id,q.session_id,q.event_kind,q.terminal_status,q.authority_ref,
                md5(:prefix || n::text),q.binding_json,q.binding_sha256,repeat(md5(n::text),2)
            FROM generate_series(1,2001) n CROSS JOIN runtime_terminal_boundary_outbox q
            WHERE q.runtime_task_id=:task
        """),
            {"prefix": str(seed.task_id), "task": seed.task_id},
        )
        await db.commit()
        snapshot_sql = text(
            "SELECT count(*),md5(string_agg(md5((to_jsonb(q)-'task_type')::text),'' ORDER BY id)) "
            "FROM runtime_terminal_boundary_outbox q"
        )
        original = (await db.execute(snapshot_sql)).one()
    migrate(migrated_pg_url, "downgrade", "terminal_queue_scan_1024", tmp_path, "online-previous")
    async with owner_sessionmaker() as db:
        await db.execute(
            text("""
            CREATE FUNCTION public.test_route_backfill_barrier() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.id='00000000-0000-0000-0000-0000000003e9'::uuid THEN
                PERFORM pg_advisory_lock(761024,124);
                PERFORM pg_advisory_unlock(761024,124);
              END IF;
              RETURN NEW;
            END;
            $$
        """)
        )
        await db.execute(
            text(
                "CREATE TRIGGER test_route_backfill_barrier BEFORE UPDATE ON "
                "runtime_terminal_boundary_outbox FOR EACH ROW EXECUTE FUNCTION public.test_route_backfill_barrier()"
            )
        )
        await db.commit()

    waiter_sql = text(
        "SELECT a.pid FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid "
        "WHERE a.datname=current_database() AND a.pid<>pg_backend_pid() AND l.locktype='advisory' "
        "AND l.classid=761024 AND l.objid=124 AND NOT l.granted"
    )
    cancel_sql = text(
        "SELECT pg_cancel_backend(a.pid) FROM pg_stat_activity a WHERE a.pid=:pid "
        "AND a.datname=current_database() AND EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid=a.pid "
        "AND l.locktype='advisory' AND l.classid=761024 AND l.objid=124 AND NOT l.granted)"
    )
    process, migration_pid, committed = None, None, 0
    # A session advisory lock must not hold an old transaction that prevents
    # CREATE INDEX CONCURRENTLY from reaching the deliberately blocked batch.
    async with owner_engine.connect() as barrier:
        await barrier.execution_options(isolation_level="AUTOCOMMIT")
        await barrier.execute(text("SELECT pg_advisory_lock(761024,124)"))
        with (tmp_path / "online-interrupted-upgrade.log").open("w") as output:
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "alembic", "upgrade", "terminal_lane_route_1024"],
                    cwd=Path(__file__).resolve().parents[2],
                    env={**os.environ, "DATABASE_URL": migrated_pg_url},
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                async with asyncio.timeout(30):
                    while migration_pid is None:
                        assert process.poll() is None, "Migration exited before reaching the owned backfill barrier"
                        async with owner_sessionmaker() as observer:
                            migration_pid = await observer.scalar(waiter_sql)
                        if migration_pid is None:
                            await asyncio.sleep(0.05)
                # A different, real RLS connection must read while the second batch is blocked.
                async with app_user_sessionmaker() as reader:
                    await pin_rls_tenant_context(reader, seed.tenant_id)
                    assert (
                        await reader.execute(
                            text("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                        )
                    ).one() == ("rls_app_user", False, False)
                    await reader.execute(text("SET LOCAL lock_timeout='500ms'"))
                    assert await reader.scalar(
                        text("SELECT id FROM runtime_terminal_boundary_outbox WHERE id=:id"), {"id": UUID(int=1)}
                    ) == UUID(int=1)
                    committed = await reader.scalar(
                        text("SELECT count(*) FROM runtime_terminal_boundary_outbox WHERE task_type IS NOT NULL")
                    )
                    assert 0 < committed < original[0]
                # Lose the migration host, then cancel only its observed, test-owned advisory waiter.
                process.terminate()
                await asyncio.to_thread(process.wait, 10)
            finally:
                if migration_pid is not None:
                    async with owner_sessionmaker() as observer:
                        await observer.execute(cancel_sql, {"pid": migration_pid})
                        await observer.commit()
                await barrier.execute(text("SELECT pg_advisory_unlock(761024,124)"))
                if process is not None and process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 10)

    async with owner_sessionmaker() as db:
        assert not await db.scalar(waiter_sql)
        assert (await db.execute(snapshot_sql)).one() == original
        assert (
            await db.scalar(text("SELECT count(*) FROM runtime_terminal_boundary_outbox WHERE task_type IS NOT NULL"))
            == committed
        )
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "terminal_queue_scan_1024"
        await db.execute(text("DROP TRIGGER test_route_backfill_barrier ON runtime_terminal_boundary_outbox"))
        await db.execute(text("DROP FUNCTION public.test_route_backfill_barrier()"))
        await db.commit()
    process = None
    with (tmp_path / "online-recovered-upgrade.log").open("w") as output:
        try:
            async with app_user_sessionmaker() as reader:
                await pin_rls_tenant_context(reader, seed.tenant_id)
                # Resume the data phase under a real old-runtime reader. Concurrent
                # index completion may legitimately wait for this transaction to end.
                assert await reader.scalar(
                    text("SELECT id FROM runtime_terminal_boundary_outbox WHERE id=:id"), {"id": UUID(int=1)}
                ) == UUID(int=1)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "alembic", "upgrade", "terminal_lane_route_1024"],
                    cwd=Path(__file__).resolve().parents[2],
                    env={**os.environ, "DATABASE_URL": migrated_pg_url},
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                async with asyncio.timeout(30):
                    while True:
                        async with owner_sessionmaker() as observer:
                            recovered = await observer.scalar(
                                text(
                                    "SELECT count(*) FROM runtime_terminal_boundary_outbox WHERE task_type IS NOT NULL"
                                )
                            )
                        if recovered == original[0]:
                            break
                        assert process.poll() is None, "Migration exited before resuming its data backfill"
                        await asyncio.sleep(0.05)
                assert await reader.scalar(
                    text("SELECT id FROM runtime_terminal_boundary_outbox WHERE id=:id"), {"id": UUID(int=1)}
                ) == UUID(int=1)
            assert await asyncio.to_thread(process.wait, 20) == 0
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                # Only this test owns migration statements in its isolated database.
                async with owner_sessionmaker() as observer:
                    await observer.execute(
                        text(
                            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                            "WHERE datname=current_database() AND pid<>pg_backend_pid() AND ("
                            "query LIKE 'DROP INDEX CONCURRENTLY IF EXISTS ix_terminal_route_backfill_pending%' OR "
                            "query LIKE 'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_terminal_boundary_active_lane%' OR "
                            "query LIKE 'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_runtime_tasks_terminal_discovery%' OR "
                            "query LIKE '%WITH page AS MATERIALIZED (%UPDATE runtime_terminal_boundary_outbox q SET task_type=t.task_type%')"
                        )
                    )
                    await observer.commit()
                await asyncio.to_thread(process.wait, 10)
    async with owner_sessionmaker() as db:
        assert (await db.execute(snapshot_sql)).one() == original
        assert (
            await db.scalar(
                text("SELECT count(*) FROM runtime_terminal_boundary_outbox WHERE task_type='business_task'")
            )
            == original[0]
            == 2002
        )
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "terminal_lane_route_1024"
        assert not await db.scalar(text("SELECT 1 FROM pg_class WHERE relname='ix_terminal_route_backfill_pending'"))
    (tmp_path / "online-terminal-route-recovery.json").write_text(
        json.dumps(
            {
                "ordinary_rls_reads_available_during_backfill": True,
                "committed_before_host_loss": committed,
                "rows_after_recovery": original[0],
                "non_routing_row_hash": original[1],
                "already_committed_batches_survived": True,
                "all_routes_recovered": True,
                "test_owned_waiter_cancelled": True,
                "temporary_backfill_index_removed": True,
                "resume_preserves_concurrent_original_reader": True,
            },
            indent=2,
        )
    )


async def test_fresh_bootstrap_installs_invoker_terminal_routes_for_legacy_writers(
    migrated_pg_url,
    owner_sessionmaker,
    app_user_sessionmaker,
):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Fresh terminal route")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert tuple(role) == (False, False)
        # A previous writer omits task_type; the database binds its canonical task.
        row = RuntimeTerminalBoundaryOutbox(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            runtime_task_id=seed.task_id,
            agent_id=seed.agent_id,
            session_id=str(seed.session_id),
            event_kind="runtime_terminal",
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=str(seed.task_id),
            binding_json=_binding(),
            binding_sha256="a" * 64,
            idempotency_key="b" * 64,
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        assert row.task_type == "business_task"
        row.task_type = "delegation"
        await db.flush()
        await db.refresh(row)
        assert row.task_type == "business_task"
        task = await db.get(RuntimeTask, seed.task_id)
        task.task_type = "trigger"
        await db.flush()
        await db.refresh(row)
        assert row.task_type == "trigger"
        functions = (
            await db.execute(
                text(
                    "SELECT proname,prosecdef,proconfig FROM pg_proc WHERE proname IN "
                    "('set_terminal_boundary_task_route','sync_terminal_boundary_task_route') ORDER BY proname"
                )
            )
        ).all()
        assert [r.proname for r in functions] == [
            "set_terminal_boundary_task_route",
            "sync_terminal_boundary_task_route",
        ]
        assert all(not r.prosecdef and r.proconfig == ["search_path=pg_catalog, public"] for r in functions)
        await db.commit()


async def _remove_routes_and_clear_owned_cache(db, task_id):
    for statement in (
        "DROP TRIGGER trg_terminal_boundary_task_route ON runtime_terminal_boundary_outbox",
        "DROP TRIGGER trg_sync_terminal_boundary_task_route ON runtime_tasks",
        "DROP FUNCTION public.set_terminal_boundary_task_route()",
        "DROP FUNCTION public.sync_terminal_boundary_task_route()",
    ):
        await db.execute(text(statement))
    await db.execute(
        text("UPDATE runtime_terminal_boundary_outbox SET task_type=NULL WHERE runtime_task_id=:task"),
        {"task": task_id},
    )


async def test_versioned_bootstrap_repair_preserves_queue_and_parent_contract(
    migrated_pg_url,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Versioned bootstrap repair")
    await _enqueue(owner_sessionmaker, seed, event_kind="runtime_terminal", binding=_binding())
    snapshot = text("SELECT id,to_jsonb(q)-'task_type' AS body FROM runtime_terminal_boundary_outbox q ORDER BY id")
    async with owner_sessionmaker() as db:
        before = {str(row.id): row.body for row in (await db.execute(snapshot)).all()}
        await _remove_routes_and_clear_owned_cache(db, seed.task_id)
        await db.execute(text("UPDATE alembic_version SET version_num='terminal_lane_route_1024'"))
        await db.commit()
    current_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    for step, action, target in (
        ("repair", "upgrade", "head"),
        ("parent-contract", "downgrade", "terminal_lane_route_1024"),
        ("replay-repair", "upgrade", "head"),
    ):
        migrate(migrated_pg_url, action, target, tmp_path, step)
        async with owner_sessionmaker() as db:
            assert {str(row.id): row.body for row in (await db.execute(snapshot)).all()} == before
            assert (
                await db.scalar(
                    select(RuntimeTerminalBoundaryOutbox.task_type).where(
                        RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id
                    )
                )
                == "business_task"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN "
                        "('set_terminal_boundary_task_route','sync_terminal_boundary_task_route') AND NOT prosecdef"
                    )
                )
                == 2
            )
            expected_head = target if action == "downgrade" else current_head
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == expected_head
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        await db.execute(
            text("UPDATE runtime_terminal_boundary_outbox SET task_type='delegation' WHERE runtime_task_id=:task"),
            {"task": seed.task_id},
        )
        assert (
            await db.scalar(
                select(RuntimeTerminalBoundaryOutbox.task_type).where(
                    RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id
                )
            )
            == "business_task"
        )
        await db.commit()


async def test_unversioned_runtime_prepare_backfills_routes_without_changing_queue_evidence(
    migrated_pg_url,
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from app.database import Base
    from app.db_bootstrap import prepare_runtime_schema
    from app.models import import_all_models

    import_all_models()
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Unversioned runtime route repair")
    await _enqueue(owner_sessionmaker, seed, event_kind="runtime_terminal", binding=_binding())
    snapshot = text("SELECT id,to_jsonb(q)-'task_type' AS body FROM runtime_terminal_boundary_outbox q ORDER BY id")
    async with owner_sessionmaker() as db:
        before = {str(row.id): row.body for row in (await db.execute(snapshot)).all()}
        await _remove_routes_and_clear_owned_cache(db, seed.task_id)
        await db.execute(text("DROP TABLE alembic_version"))
        await db.commit()
        connection = await db.connection()
        assert await connection.run_sync(lambda sync: prepare_runtime_schema(sync, Base.metadata)) is True
        await db.commit()
        assert {str(row.id): row.body for row in (await db.execute(snapshot)).all()} == before
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='alembic_version'"
                )
            )
            == 0
        )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        assert (
            await db.scalar(
                select(RuntimeTerminalBoundaryOutbox.task_type).where(
                    RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id
                )
            )
            == "business_task"
        )
