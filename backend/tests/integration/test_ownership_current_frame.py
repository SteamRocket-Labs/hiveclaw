"""Ownership mutations consume current authority after an actual Agent lock wait."""

import asyncio
import json
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import admin, agents
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.machine_credential import MachineCredential
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration import test_m3h_machine_identity_postgres as machine

machine_api = machine.machine_api
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('change', [None, 'issuer_role', 'issuer_disabled', 'issuer_moved', 'company_disabled'])
async def test_machine_issuance_uses_owner_authority_current_after_the_employee_lock(
    machine_api, owner_sessionmaker, app_user_sessionmaker, tmp_path, change,
):
    seed, _issuer = machine_api
    issuer_id, other_tenant_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed['owner'])
        owner.role = 'platform_admin'
        agent = await db.get(Agent, seed['agent'])
        agent.status = 'stopped'
        db.add(Tenant(id=other_tenant_id, name='Other issuer company', slug=f'issuer-{other_tenant_id.hex}'))
        db.add(User(id=issuer_id, tenant_id=seed['tenant'], username=f'issuer-{issuer_id.hex}',
            email=f'{issuer_id.hex}@issuer.example.test', password_hash='synthetic-unused-password',
            display_name='Independent company issuer', role='org_admin', is_active=True))
        await db.commit()
    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def observe(self, statement):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is Agent for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                waiting.set()

        async def execute(self, statement, *args, **kwargs):
            await self.observe(statement)
            return await super().execute(statement, *args, **kwargs)

        async def scalar(self, statement, *args, **kwargs):
            await self.observe(statement)
            return await super().scalar(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)
    app = FastAPI()
    app.include_router(agents.router)
    app.include_router(admin.router)

    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=factory) as db:
            yield db

    app.dependency_overrides[get_db] = database
    issuer_token = create_access_token(str(issuer_id), 'org_admin', str(seed['tenant']))
    owner_token = create_access_token(str(seed['owner']), 'platform_admin', str(seed['tenant']))
    body = {**machine._issue(seed), 'expected_user_id': str(issuer_id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ownership-control') as api:
        async with owner_sessionmaker() as blocker:
            await blocker.scalar(select(Agent).where(Agent.id == seed['agent']).with_for_update())
            blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
            request = asyncio.create_task(api.post(f"/agents/{seed['agent']}/machine-identities", json=body,
                headers={'Authorization': 'Bearer ' + issuer_token}))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=10)
                async with owner_sessionmaker() as observer:
                    await observer.execute(text(f"""DO $test$
                        DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                        BEGIN
                            WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                                IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Issuer did not reach employee lock'; END IF;
                                PERFORM pg_sleep(0.01);
                            END LOOP;
                        END $test$"""))
                    assert blocker_pid in await observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if change == 'company_disabled':
                    disabled = await api.put(f"/admin/companies/{seed['tenant']}/toggle",
                        headers={'Authorization': 'Bearer ' + owner_token})
                    assert disabled.status_code == 200 and disabled.json()['is_active'] is False, disabled.text
                elif change:
                    async with owner_sessionmaker() as observer:
                        issuer = await observer.get(User, issuer_id)
                        if change == 'issuer_role':
                            issuer.role = 'member'
                        elif change == 'issuer_disabled':
                            issuer.is_active = False
                        else:
                            issuer.tenant_id = other_tenant_id
                        await observer.commit()
                await blocker.commit()
                response = await asyncio.wait_for(request, timeout=10)
            finally:
                await blocker.rollback()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
    async with owner_sessionmaker() as db:
        count = await db.scalar(select(func.count(MachineCredential.id)).where(MachineCredential.agent_id == seed['agent']))
    # Do not retain even synthetic bearer secrets in regression evidence.
    observed.update(change=change, status_code=response.status_code, credential_count=count)
    (tmp_path / 'ownership-current-frame.json').write_text(json.dumps(observed, indent=2))
    if change:
        assert response.status_code in {401, 403, 404}, observed
        assert count == 0, observed
    else:
        assert response.status_code == 201 and response.json()['secret_status'] == 'available_once', observed
        assert count == 1, observed
