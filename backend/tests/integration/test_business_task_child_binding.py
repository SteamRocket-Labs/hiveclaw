"""Business child-session authority expands and rolls back without losing receipts."""

import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


def _migrate(url, action, target, tmp_path):
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic', action, target],
        env={**os.environ, 'DATABASE_URL': url}, capture_output=True, text=True, timeout=60)
    (tmp_path / f'{action}-{target}.log').write_text(result.stdout + result.stderr)
    return result


async def test_empty_business_child_revision_restores_exact_previous_and_current_guards(
    migrated_pg_url, owner_sessionmaker, tmp_path,
):
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT count(*) FROM runtime_tasks WHERE task_type='business_task'")) == 0
        before = await db.scalar(text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)"))
    assert "run.task_type='business_task'" in before
    previous = _migrate(migrated_pg_url, 'downgrade', 'original_blob_generations_1021', tmp_path)
    assert previous.returncode == 0, previous.stderr
    async with owner_sessionmaker() as db:
        restored = await db.scalar(text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)"))
        assert "run.task_type='business_task'" not in restored and "run.task_type='delegation'" in restored
        assert await db.scalar(text('SELECT version_num FROM alembic_version')) == 'original_blob_generations_1021'
    upgraded = _migrate(migrated_pg_url, 'upgrade', 'head', tmp_path)
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")) == before
        assert await db.scalar(text('SELECT version_num FROM alembic_version')) == 'delegation_permissions_1022'


async def test_business_model_receipts_require_exact_child_identity_and_block_lossy_downgrade(
    migrated_pg_url, owner_sessionmaker, app_user_sessionmaker, tmp_path,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    child_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed['run_id'])
        run.task_type, run.child_session_id = 'business_task', str(child_id)
        db.add(ChatSession(id=child_id, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'], user_id=seed['user_id'],
            source_channel='task', session_kind='user_task', runtime_task_id=run.id, listed_surface='task_updates'))
        await db.commit()

    async def insert_receipt():
        row_id = uuid.uuid4()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed['tenant_id'])
            db.add(SessionModelResult(id=row_id, tenant_id=seed['tenant_id'], session_id=child_id, run_id=seed['run_id'],
                turn_id=f"turn-{seed['run_id'].hex}", round_id=str(row_id), provider_request_id=str(row_id),
                model_request_hash='0' * 64, model_request_snapshot_json={}, bound_input_ids_json=[]))
            await db.commit()
        return row_id

    retained_id = await insert_receipt()
    observed = []
    for field, changed, original in [('user_id', None, seed['user_id']), ('session_kind', 'user_chat', 'user_task'),
        ('source_channel', 'web', 'task'), ('runtime_task_id', None, seed['run_id'])]:
        async with owner_sessionmaker() as db:
            setattr(await db.get(ChatSession, child_id), field, changed)
            await db.commit()
        with pytest.raises(DBAPIError, match='session_v2_authority_binding_mismatch'):
            await insert_receipt()
        async with owner_sessionmaker() as db:
            setattr(await db.get(ChatSession, child_id), field, original)
            await db.commit()
        observed.append(field)
    refused = _migrate(migrated_pg_url, 'downgrade', 'original_blob_generations_1021', tmp_path)
    assert refused.returncode != 0 and 'Retain native business model and tool receipts before downgrade' in refused.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text('SELECT version_num FROM alembic_version')) == 'business_task_child_1022'
        assert await db.get(SessionModelResult, retained_id) is not None
    (tmp_path / 'business-child-authority.json').write_text(json.dumps({'rejected_mutations': observed,
        'retained_model_result_id': str(retained_id), 'lossy_downgrade_refused': True}, indent=2))
