"""Retire a company with its production-created, otherwise protected system HR."""

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import admin, agents, tenants
from app.core.security import get_current_user
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.schedule import AgentSchedule
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
from app.services.agent_manager import settings


@pytest.fixture
async def hr_company(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    tenant_id, home_id, owner_id, member_id, other_id = (uuid.uuid4() for _ in range(5))
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(tmp_path))
    async with owner_sessionmaker() as db:
        db.add_all([
            Tenant(id=home_id, name="Platform home", slug=f"g1b-home-{home_id.hex}"),
            Tenant(id=tenant_id, name="HR company", slug=f"g1b-company-{tenant_id.hex}"),
        ])
        await db.flush()
        db.add_all([
            User(id=owner_id, tenant_id=tenant_id, role="platform_admin", is_active=True,
                 username=f"g1b-owner-{owner_id.hex}", email=f"{owner_id}@g1b.test",
                 password_hash="x", display_name="Platform owner"),
            User(id=member_id, tenant_id=tenant_id, role="member", is_active=True,
                 username=f"g1b-member-{member_id.hex}", email=f"{member_id}@g1b.test",
                 password_hash="x", display_name="Company member"),
        ])
        await db.flush()
        db.add(Agent(id=other_id, tenant_id=tenant_id, creator_id=owner_id,
                     owner_user_id=owner_id, sponsor_user_id=owner_id, name="Stopped employee",
                     status="stopped", deactivated_at=datetime.now(timezone.utc)))
        await db.commit()

    async def database():
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            yield db

    async def current_user():
        async with owner_sessionmaker() as db:
            return await db.get(User, owner_id)

    app = FastAPI()
    for router in (tenants.router, agents.router, admin.router):
        app.include_router(router)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g1b") as api:
        created = await api.get("/agents/system/hr", params={"tenant_id": str(tenant_id)})
        assert created.status_code == 200, created.text
        hr_id = uuid.UUID(created.json()["id"])
        workspace = tmp_path / str(hr_id)
        assert (workspace / ".hr_template_version").is_file()
        session_id, task_id = uuid.uuid4(), uuid.uuid4()
        async with owner_sessionmaker() as db:
            hr = await db.get(Agent, hr_id)
            assert hr.agent_class == "internal_system" and hr.name == "__system_hr__"
            assert hr.owner_user_id == owner_id and hr.deactivated_at is None
            db.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=hr_id, user_id=owner_id))
            db.add(AgentTrigger(tenant_id=tenant_id, agent_id=hr_id, name="HR reminder",
                                type="interval", config={"minutes": 60}, is_enabled=True))
            db.add(AgentSchedule(tenant_id=tenant_id, agent_id=hr_id, name="HR schedule",
                                 instruction="Draft a summary", cron_expr="0 9 * * *",
                                 created_by=owner_id, is_enabled=True))
            await db.flush()
            db.add(RuntimeTask(id=task_id, tenant_id=tenant_id, parent_agent_id=hr_id,
                               parent_session_id=str(session_id), root_user_id=owner_id,
                               task_type="heartbeat", status="running", metadata_json={}))
            await db.commit()
        yield dict(api=api, tenant=tenant_id, home=home_id, owner=owner_id, member=member_id,
                   hr=hr_id, other=other_id, task=task_id, session=session_id, workspace=workspace)


def retirement(seed):
    return {"expected_user_ids": [str(seed["member"])], "reason": "Retire fixture company",
            "request_id": f"g1b-{seed['tenant']}"}


async def test_retirement_deactivates_system_hr_and_settles_its_work(hr_company, owner_sessionmaker):
    seed = hr_company
    retained = seed["workspace"] / "retained.txt"
    retained.write_text("Retain employee files")
    result = await seed["api"].request("DELETE", f"/tenants/{seed['tenant']}", json=retirement(seed))
    assert result.status_code == 200, result.text
    assert result.json()["retirement_status"] == "retired"
    async with owner_sessionmaker() as db:
        assert (await db.get(Tenant, seed["tenant"])).is_active is False
        owner = await db.get(User, seed["owner"])
        assert owner.is_active and owner.role == "platform_admin" and owner.tenant_id != seed["tenant"]
        assert (await db.get(User, seed["member"])).is_active is False
        hr = await db.get(Agent, seed["hr"])
        assert hr.deactivated_at is not None and hr.deleted_at is None and hr.status == "stopped"
        assert (await db.get(Agent, seed["other"])).deleted_at is None
        assert (await db.get(ChatSession, seed["session"])) is not None
        assert (await db.get(RuntimeTask, seed["task"])).status == "killed"
        assert not list(await db.scalars(select(Agent).where(
            Agent.tenant_id == seed["tenant"], Agent.deactivated_at.is_(None))))
        assert not (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == seed["hr"]))).one().is_enabled
        assert not (await db.scalars(select(AgentSchedule).where(AgentSchedule.agent_id == seed["hr"]))).one().is_enabled
        assert await db.scalar(select(AuditLog.id).where(
            AuditLog.agent_id == seed["hr"], AuditLog.action == "agent:retirement_requested"))
    assert retained.read_text() == "Retain employee files"
    profile = await seed["api"].get(f"/agents/{seed['hr']}")
    assert profile.status_code == 404, profile.text
    replay = await seed["api"].request("DELETE", f"/tenants/{seed['tenant']}", json=retirement(seed))
    assert replay.status_code == 200 and replay.json()["retirement_status"] == "already_retired", replay.text
    restored = await seed["api"].put(f"/admin/companies/{seed['tenant']}/toggle")
    assert restored.status_code == 200 and restored.json()["is_active"] is True, restored.text
    async with owner_sessionmaker() as db:
        assert (await db.get(Agent, seed["hr"])).deactivated_at is not None
        assert not list(await db.scalars(select(Agent).where(
            Agent.id == seed["hr"], agent_lifecycle_active_clause())))


async def test_other_active_employee_blocks_before_hr_is_deactivated(hr_company, owner_sessionmaker):
    seed = hr_company
    async with owner_sessionmaker() as db:
        other = await db.get(Agent, seed["other"])
        other.deactivated_at, other.status = None, "idle"
        await db.commit()
    result = await seed["api"].request("DELETE", f"/tenants/{seed['tenant']}", json=retirement(seed))
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "tenant_retirement_owned_agents_active"
    assert str(seed["other"]) in result.json()["detail"]["agent_ids"]
    async with owner_sessionmaker() as db:
        assert (await db.get(Tenant, seed["tenant"])).is_active is True
        assert (await db.get(User, seed["member"])).is_active is True
        assert (await db.get(Agent, seed["hr"])).deactivated_at is None
        assert (await db.get(RuntimeTask, seed["task"])).status == "running"


async def test_hr_cannot_be_deactivated_through_employee_endpoint(hr_company, owner_sessionmaker):
    seed = hr_company
    result = await seed["api"].post(f"/agents/{seed['hr']}/deactivate")
    assert result.status_code == 403, result.text
    assert result.json()["detail"] == "The system HR assistant cannot be deactivated."
    async with owner_sessionmaker() as db:
        assert (await db.get(Agent, seed["hr"])).deactivated_at is None


async def test_hr_outward_work_must_be_settled_before_atomic_retirement(hr_company, owner_sessionmaker):
    from app.services.remote_a2a_tasks import SCHEMA

    seed = hr_company
    outward_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=outward_id, tenant_id=seed["tenant"], parent_agent_id=seed["hr"],
                           parent_session_id=str(seed["session"]), root_user_id=seed["owner"],
                           task_type="remote_a2a", status="running", metadata_json={"schema": SCHEMA}))
        await db.commit()
    result = await seed["api"].request("DELETE", f"/tenants/{seed['tenant']}", json=retirement(seed))
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "agent_retirement_required"
    assert result.json()["detail"]["remote_work_count"] == 1
    assert result.json()["detail"]["new_execution"] == "not_withdrawn"
    async with owner_sessionmaker() as db:
        assert (await db.get(Tenant, seed["tenant"])).is_active is True
        assert (await db.get(User, seed["member"])).is_active is True
        assert (await db.get(Agent, seed["hr"])).deactivated_at is None
        assert (await db.get(RuntimeTask, outward_id)).status == "running"


async def test_hr_deactivation_rolls_back_when_tenant_retirement_fails(
    hr_company, owner_sessionmaker, monkeypatch,
):
    from app.services import tool_config_service

    seed = hr_company

    async def fail_scrub(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(503, detail="Fixture scrub failure")

    monkeypatch.setattr(tool_config_service, "scrub_tenant_tool_secrets", fail_scrub)
    result = await seed["api"].request("DELETE", f"/tenants/{seed['tenant']}", json=retirement(seed))
    assert result.status_code == 503, result.text
    async with owner_sessionmaker() as db:
        assert (await db.get(Tenant, seed["tenant"])).is_active is True
        assert (await db.get(User, seed["member"])).is_active is True
        assert (await db.get(Agent, seed["hr"])).deactivated_at is None
        assert (await db.get(RuntimeTask, seed["task"])).status == "running"
        assert (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == seed["hr"]))).one().is_enabled
        assert not list(await db.scalars(select(AuditLog).where(
            AuditLog.agent_id == seed["hr"], AuditLog.action == "agent:retirement_requested")))
