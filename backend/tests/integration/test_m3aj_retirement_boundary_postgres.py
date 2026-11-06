"""Retirement reports actual authority state and releases its preparation locks."""

import pytest
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_retirement import prepare
from tests.integration import test_m3aj_employee_retirement_postgres as retirement


async def _seed_admin_agent(owner_sessionmaker, *, role: str = "org_admin", shared_tenant: bool = True):
    import uuid

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other_user_id = uuid.uuid4()
    other_tenant_id = tenant_id if shared_tenant else uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Boundary Fixture Tenant", slug=f"rb-{tenant_id.hex[:8]}", is_active=True))
        if not shared_tenant:
            db.add(Tenant(id=other_tenant_id, name="Foreign Tenant", slug=f"rbf-{other_tenant_id.hex[:8]}", is_active=True))
        await db.flush()
        db.add(User(id=user_id, username=f"rb-{user_id.hex[:8]}", email=f"{user_id.hex[:8]}@rb.test",
            password_hash="x", display_name="Boundary Owner", tenant_id=tenant_id, role="org_admin", is_active=True))
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Boundary Agent",
            creator_id=user_id, owner_user_id=user_id))
        await db.flush()
        db.add(User(id=other_user_id, username=f"rb2-{other_user_id.hex[:8]}", email=f"{other_user_id.hex[:8]}@rb2.test",
            password_hash="x", display_name="Boundary Admin", tenant_id=other_tenant_id, role=role, is_active=True))
        await db.commit()
    return tenant_id, agent_id, other_user_id


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


async def test_deactivate_reports_exactly_the_deactivation_it_committed(
    remote_work, app_user_sessionmaker,
):
    seed, api, session_id = remote_work
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        remote.reply = lambda _request: {"task": {"id": "still-live-external-work", "status": {"state": "TASK_STATE_WORKING"}}}
        created = await api.post(path + "/tasks", json=retirement.remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        await retirement.remote_tasks.execute(seed, created.json()["id"], app_user_sessionmaker)
        # The remote worker never stops, so the recall cannot settle. The
        # route commits the deactivation first, then reports the pending
        # recall truthfully: disabled new execution, unconfirmed external
        # execution, employee never deleted.
        refused = await api.post(f"/agents/{seed['agent']}/deactivate")
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "agent_retirement_pending", detail
        assert detail["new_execution"] == "disabled", detail
        assert detail["external_execution"] == "unknown", detail
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            assert agent.deactivated_at is not None and agent.status == "stopped"
            assert agent.deleted_at is None
