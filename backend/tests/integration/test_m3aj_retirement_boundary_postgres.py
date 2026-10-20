"""Retirement reports actual authority state and releases its preparation locks."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_identity_lifecycle import soft_delete_agent
from app.services.agent_retirement import prepare
from tests.integration import test_m3aj_employee_retirement_postgres as retirement
from tests.integration.test_agent_delete_conflict_recovery import _seed_admin_agent

app_user_engine = retirement.app_user_engine
app_user_sessionmaker = retirement.app_user_sessionmaker
machine_api = retirement.machine_api
migrated_pg_url = retirement.migrated_pg_url
owner_engine = retirement.owner_engine
owner_sessionmaker = retirement.owner_sessionmaker
pg_container = retirement.pg_container
remote_work = retirement.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_native_preparation_does_not_retain_an_agent_lock_before_session_writers(
    owner_sessionmaker, app_user_sessionmaker,
):
    tenant, agent_id, actor_id = await _seed_admin_agent(owner_sessionmaker)
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as prepared:
        await prepare(prepared, agent=await prepared.get(Agent, agent_id), actor=await prepared.get(User, actor_id))
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as concurrent:
            await concurrent.execute(text("SET LOCAL lock_timeout = '250ms'"))
            assert await concurrent.scalar(select(Agent.id).where(Agent.id == agent_id).with_for_update()) == agent_id


async def test_shared_delete_guard_does_not_report_deactivation_it_has_not_committed(
    remote_work, app_user_sessionmaker,
):
    seed, api, session_id = remote_work
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        remote.reply = lambda _request: {"task": {"id": "still-live-external-work", "status": {"state": "TASK_STATE_WORKING"}}}
        created = await api.post(path + "/tasks", json=retirement.remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        await retirement.remote_tasks.execute(seed, created.json()["id"], app_user_sessionmaker)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            with pytest.raises(HTTPException) as pending:
                await soft_delete_agent(db, agent, actor_id=seed["owner"])
            assert pending.value.detail["code"] == "agent_retirement_required"
            assert pending.value.detail["new_execution"] == "not_withdrawn"
            assert agent.deleted_at is None and agent.deactivated_at is None
            await db.rollback()
