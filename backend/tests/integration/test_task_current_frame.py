"""Task cancellation revalidates its actor and employee access after real lock waits."""

import asyncio
import json
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import admin, tasks
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent, AgentPermission
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services.business_task_runtime import stage_business_task_runtime
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('change', [None, 'actor_role', 'actor_disabled', 'actor_moved',
    'company_disabled', 'agent_access_revoked', 'task_access_revoked', 'agent_handover', 'actor_promoted_moved'])
async def test_cancel_task_uses_authority_current_after_its_task_lock(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    actor_id, other_tenant_id, task_id, agent_grant_id, task_grant_id = (uuid4() for _ in range(5))
    company_admin_id = uuid4()
    member = change in {'agent_access_revoked', 'task_access_revoked', 'agent_handover', 'actor_promoted_moved'}
    role = 'member' if member else 'org_admin'
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed['user_id'])
        owner.role = 'platform_admin'
        agent = await db.get(Agent, seed['agent_id'])
        agent.status = 'stopped'
        if change == 'agent_handover':
            agent.owner_user_id = actor_id
        db.add(Tenant(id=other_tenant_id, name='Other task company', slug=f'task-{other_tenant_id.hex}'))
        db.add(User(id=actor_id, tenant_id=seed['tenant_id'], username=f'control-{actor_id.hex}',
            email=f'{actor_id.hex}@task.example.test', display_name='Task controller',
            password_hash='synthetic-unused-password', role=role, is_active=True))
        if change == 'company_disabled':
            db.add(User(id=company_admin_id, username=f'global-{company_admin_id.hex}',
                email=f'{company_admin_id.hex}@task.example.test', display_name='Global company administrator',
                password_hash='synthetic-unused-password', role='platform_admin', is_active=True))
        await db.flush()
        task = Task(id=task_id, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
            created_by=seed['user_id'], responsible_user_id=seed['user_id'], root_session_id=seed['session_id'],
            title='Original task', description='Preserve this original task.', request_id=str(uuid4()),
            request_hash='a' * 64, authority_state='owned')
        db.add(task)
        await db.flush()
        run = await stage_business_task_runtime(db=db, task=task, requester_user_id=seed['user_id'],
            agent_name=agent.name, request_id=task.request_id, request_hash=task.request_hash,
            root_session_id=seed['session_id'])
        run_id = run.id
        if member:
            if change != 'agent_handover':
                db.add(AgentPermission(id=agent_grant_id, tenant_id=seed['tenant_id'], agent_id=seed['agent_id'],
                    scope_type='user', scope_id=actor_id, access_level='use'))
            db.add(ResourcePermission(id=task_grant_id, tenant_id=seed['tenant_id'], principal_type='user',
                principal_id=actor_id, resource_type='task', resource_id=task_id, actions=['execute']))
        await db.commit()
    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is Task for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                waiting.set()
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)
    app = FastAPI()
    app.include_router(tasks.router)
    app.include_router(admin.router)

    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=factory) as db:
            yield db

    async def no_remote_wakeup(**_kwargs):
        pass

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr('app.services.runtime_control_bus.publish_business_task_cancel', no_remote_wakeup)
    actor_token = create_access_token(str(actor_id), role, str(seed['tenant_id']))
    owner_token = create_access_token(str(company_admin_id), 'platform_admin', None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://task-control') as api:
        if change == 'company_disabled':
            # The admin transition serializes with the request's tenant FK lock.
            # Exercise that route separately before testing a committed nonkey update.
            disabled = await api.put(f"/admin/companies/{seed['tenant_id']}/toggle",
                headers={'Authorization': 'Bearer ' + owner_token})
            assert disabled.status_code == 200 and disabled.json()['is_active'] is False, disabled.text
            enabled = await api.put(f"/admin/companies/{seed['tenant_id']}/toggle",
                headers={'Authorization': 'Bearer ' + owner_token})
            assert enabled.status_code == 200 and enabled.json()['is_active'] is True, enabled.text
        async with owner_sessionmaker() as blocker:
            await blocker.scalar(select(Task).where(Task.id == task_id).with_for_update())
            blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
            request = asyncio.create_task(api.post(f"/agents/{seed['agent_id']}/tasks/{task_id}/cancel",
                json={'reason': 'Cancel the authorized original work.'}, headers={'Authorization': 'Bearer ' + actor_token}))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=10)
                async with owner_sessionmaker() as observer:
                    await observer.execute(text(f"""DO $test$
                        DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                        BEGIN
                            WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                                IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Cancel did not reach Task lock'; END IF;
                                PERFORM pg_sleep(0.01);
                            END LOOP;
                        END $test$"""))
                    assert blocker_pid in await observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if change:
                    async with owner_sessionmaker() as observer:
                        if change == 'company_disabled':
                            (await observer.get(Tenant, seed['tenant_id'])).is_active = False
                        elif change == 'agent_access_revoked':
                            await observer.delete(await observer.get(AgentPermission, agent_grant_id))
                        elif change == 'task_access_revoked':
                            await observer.delete(await observer.get(ResourcePermission, task_grant_id))
                        elif change == 'agent_handover':
                            (await observer.get(Agent, seed['agent_id'])).owner_user_id = seed['user_id']
                        elif change == 'actor_promoted_moved':
                            actor = await observer.get(User, actor_id)
                            actor.role, actor.tenant_id = 'platform_admin', other_tenant_id
                            await observer.delete(await observer.get(AgentPermission, agent_grant_id))
                        else:
                            actor = await observer.get(User, actor_id)
                            if change == 'actor_role':
                                actor.role = 'member'
                            elif change == 'actor_disabled':
                                actor.is_active = False
                            else:
                                actor.tenant_id = other_tenant_id
                        await asyncio.wait_for(observer.commit(), timeout=10)
                        if change == 'company_disabled':
                            assert await observer.scalar(select(Tenant.is_active).where(Tenant.id == seed['tenant_id'])) is False
                await blocker.commit()
                response = await asyncio.wait_for(request, timeout=10)
            finally:
                await blocker.rollback()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
    async with owner_sessionmaker() as db:
        task, run = await db.get(Task, task_id), await db.get(RuntimeTask, run_id)
        log_count = await db.scalar(select(func.count(TaskLog.id)).where(TaskLog.task_id == task_id))
        actor = await db.get(User, actor_id)
        observed.update(change=change, status_code=response.status_code, task_status=task.status,
            run_status=run.status, logs=log_count, actor_role=actor.role, actor_home=str(actor.tenant_id))
    (tmp_path / 'task-current-frame.json').write_text(json.dumps(observed, indent=2))
    if change and change != 'actor_promoted_moved':
        assert response.status_code in {401, 403, 404}, observed
        assert task.status == run.status == 'pending' and log_count == 0, observed
    else:
        assert response.status_code == 200, response.text
        assert task.status == 'cancelled' and run.status == 'killed' and log_count == 1, observed
        if change == 'actor_promoted_moved':
            assert response.json()['task']['authority_source'] == 'scoped_business_admin', response.text
            assert actor.role == 'platform_admin' and actor.tenant_id == other_tenant_id, observed
