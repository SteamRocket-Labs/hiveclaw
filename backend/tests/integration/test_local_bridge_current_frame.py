"""A queued runtime frame rechecks authority after acquiring its connection lock."""

import asyncio
from datetime import timedelta
import json
from uuid import UUID

from fastapi import FastAPI, HTTPException
import httpx
import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db, tenant_scoped_session
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.models.local_bridge import LocalAgentBridgeConnection
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel
from tests.integration import test_m3a_local_employee_control_postgres as routing

local_work = routing.local_work
pytestmark = pytest.mark.usefixtures('migrated_pg_url')


@pytest.mark.parametrize('change', [None, 'company_disabled', 'expired'])
async def test_runtime_frame_uses_authority_current_after_its_real_lock_wait(
    local_work, owner_sessionmaker, app_user_sessionmaker, tmp_path, change,
):
    from app.api import admin
    from app.core.security import create_access_token

    seed = local_work
    async with owner_sessionmaker() as db:
        actor = await db.get(User, seed['user_id'])
        actor.role = 'platform_admin'
        await db.commit()
    app = FastAPI()
    app.include_router(admin.router)

    async def database():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=app_user_sessionmaker) as db:
            yield db

    app.dependency_overrides[get_db] = database
    token = create_access_token(str(seed['user_id']), 'platform_admin', str(seed['tenant_id']))
    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get('entity') is LocalAgentBridgeConnection for column in statement.column_descriptions)):
                observed['pid'] = await super().scalar(text('SELECT pg_backend_pid()'))
                observed['statement_started_at'] = channel.utcnow()
                waiting.set()
            return await super().scalar(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw['bind'], class_=ObservedSession, expire_on_commit=False)

    async def poll():
        async with tenant_scoped_session(seed['tenant_id'], session_factory=factory) as db:
            try:
                messages = await channel.poll_pending_channel_messages(db, context=seed['selected'])
                return {'delivered': [message['id'] for message in messages]}
            except HTTPException as exc:
                await db.rollback()
                return {'denied': exc.status_code, 'detail': exc.detail}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://company-control',
        headers={'Authorization': 'Bearer ' + token}) as api:
        async with owner_sessionmaker() as blocker:
            connection = await blocker.scalar(select(LocalAgentBridgeConnection).where(
                LocalAgentBridgeConnection.id == seed['selected'].connection_id).with_for_update())
            expiry = channel.utcnow() + timedelta(seconds=2)
            if change == 'expired':
                connection.expires_at = expiry
                await blocker.flush()
            blocker_pid = await blocker.scalar(text('SELECT pg_backend_pid()'))
            consumer = asyncio.create_task(poll())
            try:
                await asyncio.wait_for(waiting.wait(), timeout=10)
                async with owner_sessionmaker() as observer:
                    await observer.execute(text(f"""DO $test$
                        DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                        BEGIN
                            WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                                IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Frame did not reach connection lock'; END IF;
                                PERFORM pg_sleep(0.01);
                            END LOOP;
                        END $test$"""))
                    assert blocker_pid in await observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['pid']})
                if change == 'company_disabled':
                    disabled = await api.put(f"/admin/companies/{seed['tenant_id']}/toggle")
                    assert disabled.status_code == 200 and disabled.json()['is_active'] is False, disabled.text
                    async with owner_sessionmaker() as observer:
                        assert (await observer.get(Tenant, seed['tenant_id'])).is_active is False
                elif change == 'expired':
                    assert observed['statement_started_at'] < expiry
                    async with owner_sessionmaker() as observer:
                        await observer.execute(text('SELECT pg_sleep(2.1)'))
                    assert channel.utcnow() > expiry
                await blocker.commit()
                outcome = await asyncio.wait_for(consumer, timeout=10)
            finally:
                await blocker.rollback()
                if not consumer.done():
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
    async with owner_sessionmaker() as db:
        message = await db.get(LocalAgentChannelMessage, UUID(seed['message_id']))
        observed.update(change=change, outcome=outcome, message_status=message.status,
            delivery_attempts=message.delivery_attempt_count)
    (tmp_path / 'current-frame.json').write_text(json.dumps(observed, default=str, indent=2))
    if change:
        assert outcome.get('denied') in {401, 403, 409}, observed
        assert observed['message_status'] == 'pending' and observed['delivery_attempts'] == 0, observed
    else:
        assert outcome == {'delivered': [seed['message_id']]}, observed
        assert observed['message_status'] == 'delivered' and observed['delivery_attempts'] == 1, observed
