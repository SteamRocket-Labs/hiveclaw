"""Machine authentication rechecks company authority after its real lock waits."""

import asyncio
import json
from uuid import UUID

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import Select, select, text
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


@pytest.mark.parametrize('lock_kind', ['agent', 'credential'])
@pytest.mark.parametrize('disable_company', [False, True])
async def test_machine_authentication_uses_company_current_after_its_lock_wait(
    machine_api, owner_sessionmaker, app_user_sessionmaker, tmp_path, lock_kind, disable_company,
):
    seed, issuer = machine_api
    issued = await issuer.post(f"/agents/{seed['agent']}/machine-identities", json=machine._issue(seed))
    assert issued.status_code == 201, issued.text
    credential_id = UUID(issued.json()['credential_id'])
    async with owner_sessionmaker() as db:
        actor = await db.get(User, seed['owner'])
        actor.role = 'platform_admin'
        # A stopped employee still exposes its authorized machine description.
        # Company disable need not update its Agent row while authentication holds it.
        agent = await db.get(Agent, seed['agent'])
        agent.status = 'stopped'
        await db.commit()
    waiting, observed = asyncio.Event(), {}
    locked_model, locked_id = (Agent, seed['agent']) if lock_kind == 'agent' else (MachineCredential, credential_id)

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is locked_model for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                waiting.set()
            return await super().scalar(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)
    app = FastAPI()
    app.include_router(agents.router)
    app.include_router(admin.router)

    async def database():
        async with tenant_scoped_session(seed['tenant'], session_factory=factory) as db:
            yield db

    app.dependency_overrides[get_db] = database
    admin_token = create_access_token(str(seed['owner']), 'platform_admin', str(seed['tenant']))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://machine-control') as api:
        async with owner_sessionmaker() as blocker:
            await blocker.scalar(select(locked_model).where(locked_model.id == locked_id).with_for_update())
            blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
            request = asyncio.create_task(api.get(f"/agents/{seed['agent']}/machine-description",
                headers={'Authorization': 'Bearer ' + issued.json()['token']}))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=10)
                async with owner_sessionmaker() as observer:
                    await observer.execute(text(f"""DO $test$
                        DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                        BEGIN
                            WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                                IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Machine did not reach authority lock'; END IF;
                                PERFORM pg_sleep(0.01);
                            END LOOP;
                        END $test$"""))
                    assert blocker_pid in await observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if disable_company:
                    disabled = await api.put(f"/admin/companies/{seed['tenant']}/toggle",
                        headers={'Authorization': 'Bearer ' + admin_token})
                    assert disabled.status_code == 200 and disabled.json()['is_active'] is False, disabled.text
                    async with owner_sessionmaker() as observer:
                        assert (await observer.get(Tenant, seed['tenant'])).is_active is False
                await blocker.commit()
                response = await asyncio.wait_for(request, timeout=10)
            finally:
                await blocker.rollback()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
    async with owner_sessionmaker() as db:
        credential = await db.get(MachineCredential, credential_id)
        observed.update(lock_kind=lock_kind, disable_company=disable_company,
            status_code=response.status_code, payload=response.json(), last_used_at=credential.last_used_at)
    (tmp_path / 'machine-current-frame.json').write_text(json.dumps(observed, default=str, indent=2))
    if disable_company:
        assert response.status_code == 401, observed
        assert credential.last_used_at is None, observed
    else:
        assert response.status_code == 200 and response.json()['agent']['id'] == str(seed['agent']), observed
        assert credential.last_used_at is not None, observed
