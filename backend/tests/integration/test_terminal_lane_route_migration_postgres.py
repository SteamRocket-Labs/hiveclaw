"""Populated upgrades retain queue evidence and canonical, invoker-scoped routes."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
from tests.integration import conftest as integration
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.fixture()
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


def migrate(url, action, target, root, step):
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic', action, target],
        cwd=Path(__file__).resolve().parents[2], env={**os.environ, 'DATABASE_URL': url},
        capture_output=True, text=True, timeout=60)
    (root / f'{step}-{action}.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr


async def test_terminal_lane_upgrade_preserves_old_queue_and_native_route_authority(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name='Terminal route upgrade')
    delivered = await _seed_terminal_task(owner_sessionmaker, tenant_name='Delivered terminal history', task_type='delegation')
    await _enqueue(owner_sessionmaker, seed, event_kind='runtime_terminal', binding=_binding())
    await _enqueue(owner_sessionmaker, delivered, event_kind='runtime_terminal', binding=_binding())
    async with owner_sessionmaker() as db:
        await db.execute(text("UPDATE runtime_terminal_boundary_outbox SET status='delivered',delivered_at=now() "
            'WHERE runtime_task_id=:task'), {'task': delivered.task_id})
        await db.commit()
        original = (await db.execute(text("SELECT id,to_jsonb(q)-'task_type' AS body FROM runtime_terminal_boundary_outbox q ORDER BY id"))).all()
        original = {str(row.id): row.body for row in original}

    migrate(migrated_pg_url, 'downgrade', 'terminal_queue_scan_1024', tmp_path, 'previous')
    async with owner_sessionmaker() as db:
        assert not await db.scalar(text("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='runtime_terminal_boundary_outbox' AND column_name='task_type'"))
        assert {str(row.id): row.body for row in (await db.execute(text(
            'SELECT id,to_jsonb(q) AS body FROM runtime_terminal_boundary_outbox q ORDER BY id'))).all()} == original

    migrate(migrated_pg_url, 'upgrade', 'terminal_lane_route_1024', tmp_path, 'populated')
    async with owner_sessionmaker() as db:
        rows = (await db.execute(text("SELECT id,task_type,to_jsonb(q)-'task_type' AS body "
            'FROM runtime_terminal_boundary_outbox q ORDER BY id'))).all()
        assert {str(row.id): row.body for row in rows} == original
        active = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id == seed.task_id))
        old = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id == delivered.task_id))
        assert active.task_type == 'business_task' and old.task_type is None
        assert (await db.execute(text("SELECT proname,prosecdef FROM pg_proc WHERE proname IN "
            "('set_terminal_boundary_task_route','sync_terminal_boundary_task_route') ORDER BY proname"))).all() == [
                ('set_terminal_boundary_task_route', False), ('sync_terminal_boundary_task_route', False)]
        indexes = (await db.execute(text("SELECT c.relname,i.indisvalid,i.indisready FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname IN "
            "('ix_runtime_terminal_boundary_active_lane','ix_runtime_tasks_terminal_discovery') ORDER BY c.relname"))).all()
        assert indexes == [('ix_runtime_tasks_terminal_discovery', True, True),
            ('ix_runtime_terminal_boundary_active_lane', True, True)]
        # Simulate the version marker not committing after a completed online index build.
        await db.execute(text("UPDATE alembic_version SET version_num='terminal_queue_scan_1024'"))
        await db.commit()
    migrate(migrated_pg_url, 'upgrade', 'terminal_lane_route_1024', tmp_path, 'recovered')

    task_id, queue_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=task_id, tenant_id=seed.tenant_id, task_type='business_task', status='completed',
            terminal_boundary_enqueued_at=active.created_at, prompt='Native old-writer route'))
        await db.commit()
        # An older writer omits the new routing column. The trigger reads the real task.
        await db.execute(text("""
            INSERT INTO runtime_terminal_boundary_outbox
                (id,tenant_id,runtime_task_id,agent_id,session_id,event_kind,terminal_status,
                 authority_ref,authority_id,binding_json,binding_sha256,idempotency_key)
            SELECT :id,tenant_id,CAST(:task AS uuid),agent_id,session_id,event_kind,terminal_status,
                authority_ref,CAST(CAST(:task AS uuid) AS text),binding_json,binding_sha256,repeat('d',64)
            FROM runtime_terminal_boundary_outbox WHERE id=:source
        """), {'id': queue_id, 'task': task_id, 'source': active.id})
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        role = (await db.execute(text('SELECT current_user,rolsuper,rolbypassrls FROM pg_roles '
            'WHERE rolname=current_user'))).one()
        assert role == ('rls_app_user', False, False)
        assert await db.scalar(select(RuntimeTerminalBoundaryOutbox.task_type).where(
            RuntimeTerminalBoundaryOutbox.id == queue_id)) == 'business_task'
        await db.execute(text("UPDATE runtime_terminal_boundary_outbox SET task_type='delegation' WHERE id=:id"), {'id': queue_id})
        assert await db.scalar(select(RuntimeTerminalBoundaryOutbox.task_type).where(
            RuntimeTerminalBoundaryOutbox.id == queue_id)) == 'business_task'
        await db.execute(text("UPDATE runtime_tasks SET task_type='trigger' WHERE id=:id"), {'id': task_id})
        assert await db.scalar(select(RuntimeTerminalBoundaryOutbox.task_type).where(
            RuntimeTerminalBoundaryOutbox.id == queue_id)) == 'trigger'
        assert not await db.scalar(select(RuntimeTerminalBoundaryOutbox.id).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id == delivered.task_id))
        await db.commit()
    async with owner_sessionmaker() as db:
        # Corrupted routing metadata still cannot override the canonical task at claim.
        await db.execute(text('ALTER TABLE runtime_terminal_boundary_outbox DISABLE TRIGGER trg_terminal_boundary_task_route'))
        await db.execute(text("UPDATE runtime_terminal_boundary_outbox SET task_type='delegation' WHERE id=:id"), {'id': queue_id})
        await db.execute(text('ALTER TABLE runtime_terminal_boundary_outbox ENABLE TRIGGER trg_terminal_boundary_task_route'))
        await db.commit()
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    assert await service.claim_batch(tenant_id=seed.tenant_id, worker_id='route-corruption-control', task_types=('delegation',)) == []
    (tmp_path / 'terminal-route-upgrade.json').write_text(json.dumps({
        'old_active_route_backfilled': True, 'delivered_payload_unchanged': True,
        'interrupted_version_marker_recovered': True, 'old_writer_and_task_update_routed': True,
        'trigger_security_invoker': True, 'cross_tenant_hidden': True, 'corrupt_route_canonical_recheck': True,
        'role': 'rls_app_user', 'superuser': False, 'bypassrls': False}, indent=2))
