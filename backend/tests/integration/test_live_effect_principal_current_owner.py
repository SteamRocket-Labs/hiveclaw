"""Trigger reclaim rechecks its actual responsible owner after a row-lock wait."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import triggers
from app.core.security import create_access_token
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.trigger import AgentTrigger
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("disable_owner", [False, True])
async def test_trigger_reclaim_uses_owner_current_after_its_row_lock_wait(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, disable_owner,
):
    tenant_id, user_id, agent_id, _session_id, _run_id = await _seed_session(owner_sessionmaker)
    trigger_id = uuid4()
    original_config = {"minutes": 30, "created_by": str(user_id), "authority_state": "quarantined",
        "authority_revoked_reason": "source_authority_revoked"}
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        agent = await db.get(Agent, agent_id)
        agent.owner_user_id = user_id
        user = await db.get(User, user_id)
        token = create_access_token(str(user_id), user.role, str(tenant_id))
        db.add(AgentTrigger(id=trigger_id, tenant_id=tenant_id, agent_id=agent_id, name="Held reclaim",
            type="interval", reason="Reconcile the current owner's paused work", config=original_config, is_enabled=False))
        await db.commit()

    waiting, observed = asyncio.Event(), {}

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (isinstance(statement, Select) and statement._for_update_arg is not None and not waiting.is_set()
                and any(column.get("entity") is AgentTrigger for column in statement.column_descriptions)):
                observed["pid"] = await super().scalar(text("SELECT pg_backend_pid()"))
                waiting.set()
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(app_user_sessionmaker.kw["bind"], class_=ObservedSession, expire_on_commit=False)
    app = FastAPI()
    app.include_router(triggers.router)

    async def database():
        async with tenant_scoped_session(tenant_id, session_factory=factory) as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owner-reclaim",
        headers={"Authorization": "Bearer " + token}) as api:
        async with owner_sessionmaker() as blocker:
            await blocker.scalar(select(AgentTrigger).where(AgentTrigger.id == trigger_id).with_for_update())
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            request = asyncio.create_task(api.post(f"/agents/{agent_id}/triggers/{trigger_id}/reclaim"))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=10)
                async with owner_sessionmaker() as observer:
                    await observer.execute(text(f"""DO $test$
                        DECLARE deadline timestamptz := clock_timestamp() + interval '3 seconds';
                        BEGIN
                            WHILE NOT {int(blocker_pid)} = ANY(pg_blocking_pids({int(observed['pid'])})) LOOP
                                IF clock_timestamp() > deadline THEN RAISE EXCEPTION 'Reclaim did not reach its lock'; END IF;
                                PERFORM pg_sleep(0.01);
                            END LOOP;
                        END $test$"""))
                    assert blocker_pid in await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": observed["pid"]})
                    if disable_owner:
                        (await observer.get(User, user_id)).is_active = False
                        await observer.commit()
                await blocker.commit()
                response = await asyncio.wait_for(request, timeout=10)
            finally:
                await blocker.rollback()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async with owner_sessionmaker() as db:
        trigger = await db.get(AgentTrigger, trigger_id)
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == agent_id,
            AuditLog.action == "trigger:authority_reclaimed")))
        observed.update(disable_owner=disable_owner, status_code=response.status_code, payload=response.json(),
            stored_config=trigger.config, stored_enabled=trigger.is_enabled, reclaim_audits=len(audits))
    (tmp_path / "owner-current-frame.json").write_text(json.dumps(observed, default=str, indent=2))
    assert trigger.is_enabled is False
    if disable_owner:
        assert response.status_code == 409, observed
        assert response.json()["detail"]["code"] == "responsible_principal_inactive"
        assert trigger.config == original_config and audits == []
    else:
        assert response.status_code == 200, observed
        assert trigger.config["created_by"] == str(user_id) and trigger.config["authority_state"] == "owned"
        assert len(audits) == 1
