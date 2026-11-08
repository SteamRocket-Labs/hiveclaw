"""Post-landing G3 review regressions through the real API and PostgreSQL."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import agents, enterprise, users
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User


@pytest.fixture
async def review_stack(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        tenant = Tenant(id=uuid4(), name="Review company", slug=f"review-{uuid4().hex}")
        other = Tenant(id=uuid4(), name="Other company", slug=f"other-{uuid4().hex}")
        db.add_all([tenant, other])
        await db.flush()
        def member(company, role):
            key = uuid4().hex
            return User(id=uuid4(), tenant_id=company.id, username=key, email=f"{key}@review.test",
                password_hash="unused", display_name=role, role=role, is_active=True)
        admin, leaver, foreign = member(tenant, "org_admin"), member(tenant, "member"), member(other, "org_admin")
        db.add_all([admin, leaver, foreign])
        await db.flush()
        bot = Agent(id=uuid4(), tenant_id=tenant.id, name="Research assistant", creator_id=admin.id,
            owner_user_id=leaver.id, sponsor_user_id=admin.id, status="stopped",
            deactivated_at=datetime.now(timezone.utc), deactivation_reason="retirement_pending")
        hr = Agent(id=uuid4(), tenant_id=tenant.id, name="__system_hr__", agent_class="internal_system",
            creator_id=admin.id, owner_user_id=admin.id, sponsor_user_id=admin.id, status="idle")
        db.add_all([bot, hr])
        await db.commit()
        state = SimpleNamespace(db=db, tenant=tenant, admin=admin, leaver=leaver, foreign=foreign, bot=bot, hr=hr, actor=admin)
        app = FastAPI()
        for router in (agents.router, enterprise.router, users.router):
            app.include_router(router)
        async def database():
            yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: state.actor
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            state.client = client
            yield state
        await db.rollback()


@pytest.mark.asyncio
async def test_g3f_review_2_restore_is_one_named_tenant_scoped_audit_row(review_stack):
    s = review_stack
    response = await s.client.post(f"/agents/{s.bot.id}/restore")
    assert response.status_code == 204, response.text
    rows = list(await s.db.scalars(select(AuditLog).where(AuditLog.action == "agent:restored", AuditLog.agent_id == s.bot.id)))
    assert len(rows) == 1
    assert rows[0].tenant_id == s.tenant.id and rows[0].user_id == s.admin.id
    result = await s.client.get("/enterprise/audit-logs")
    assert result.status_code == 200, result.text
    restored = [row for row in result.json() if row["action"] == "agent:restored"]
    assert len(restored) == 1
    assert restored[0]["agent_id"] == str(s.bot.id)
    assert restored[0]["agent_name"] == s.bot.name
    # A repeated restore cannot fabricate another completed operation.
    assert (await s.client.post(f"/agents/{s.bot.id}/restore")).status_code == 409
    s.actor = s.foreign
    result = await s.client.get("/enterprise/audit-logs")
    assert result.status_code == 200, result.text
    assert not any(row["id"] == str(rows[0].id) for row in result.json())
    assert s.bot.name not in result.text


@pytest.mark.asyncio
async def test_g3f_review_3_system_hr_deactivation_is_forbidden_and_stays_active(review_stack):
    s = review_stack
    response = await s.client.post(f"/agents/{s.hr.id}/deactivate")
    await s.db.refresh(s.hr)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "The system HR assistant cannot be deactivated."
    assert s.hr.deactivated_at is None and s.hr.status == "idle"


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.asyncio
async def test_g3f_review_1_preview_echoes_the_computed_target_and_successor(review_stack, selected):
    s = review_stack
    query = f"?successor_user_id={s.admin.id}" if selected else ""
    response = await s.client.get(f"/users/{s.leaver.id}/offboarding-preview{query}")
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == str(s.leaver.id)
    assert response.json()["successor_user_id"] == str(s.admin.id)


@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.asyncio
async def test_g3f_round2_item1_member_cannot_hide_owned_bot_as_system(review_stack, rename):
    s = review_stack
    s.bot.creator_id = s.leaver.id
    s.bot.deactivated_at = None
    s.bot.deactivation_reason = None
    s.bot.status = "idle"
    await s.db.commit()
    original_name = s.bot.name
    s.actor = s.leaver
    payload = {"agent_class": "internal_system"}
    if rename:
        payload["name"] = "__system_hr__"
    response = await s.client.patch(f"/agents/{s.bot.id}", json=payload)
    assert response.status_code == 422, response.text
    assert "platform" in str(response.json()["detail"]).lower()
    await s.db.refresh(s.bot)
    assert s.bot.agent_class == "internal_tenant" and s.bot.name == original_name
    for actor in (s.leaver, s.admin):
        s.actor = actor
        response = await s.client.get("/agents/")
        assert response.status_code == 200, response.text
        assert str(s.bot.id) in {bot["id"] for bot in response.json()}


@pytest.mark.asyncio
async def test_g3f_round2_item1_admin_cannot_create_system_bot(review_stack):
    s = review_stack
    response = await s.client.post("/agents/", json={"name": "__system_hr__", "agent_class": "internal_system"})
    assert response.status_code == 422, response.text
    assert "platform" in str(response.json()["detail"]).lower()
    assert len(list(await s.db.scalars(select(Agent).where(Agent.tenant_id == s.tenant.id)))) == 2


@pytest.mark.parametrize("payload", [{"name": "Renamed HR"}, {"agent_class": "internal_tenant"}, {"agent_class": None}])
@pytest.mark.asyncio
async def test_g3f_round2_item1_hr_identity_cannot_be_changed(review_stack, payload):
    s = review_stack
    response = await s.client.patch(f"/agents/{s.hr.id}", json=payload)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    await s.db.refresh(s.hr)
    assert s.hr.name == "__system_hr__" and s.hr.agent_class == "internal_system"


@pytest.mark.asyncio
async def test_g3f_round2_item1_member_keeps_ordinary_class_changes(review_stack):
    s = review_stack
    s.bot.creator_id = s.leaver.id
    s.bot.deactivated_at = None
    s.bot.deactivation_reason = None
    s.bot.status = "idle"
    await s.db.commit()
    s.actor = s.leaver
    for agent_class in ("external_gateway", "external_api", "internal_tenant"):
        response = await s.client.patch(f"/agents/{s.bot.id}", json={"agent_class": agent_class})
        assert response.status_code == 200, response.text
        assert response.json()["agent_class"] == agent_class
        await s.db.refresh(s.bot)
        assert s.bot.agent_class == agent_class


@pytest.mark.asyncio
async def test_g3f_round2_item2_asset_lifecycle_duplicates_omitted_but_other_operations_named(review_stack):
    from app.services.ai_assets import register_agent_asset
    s = review_stack
    s.bot.deactivated_at = None
    s.bot.deactivation_reason = None
    s.bot.status = "idle"
    await register_agent_asset(s.db, s.bot, change_source="create", actor_user_id=s.admin.id,
        change_message="Agent created")
    s.bot.role_description = "Updated responsibilities"
    await register_agent_asset(s.db, s.bot, change_source="update", actor_user_id=s.admin.id,
        change_message="Agent fields updated")
    await s.db.commit()
    assert (await s.client.post(f"/agents/{s.bot.id}/deactivate")).status_code == 204
    assert (await s.client.post(f"/agents/{s.bot.id}/restore")).status_code == 204
    raw = list(await s.db.scalars(select(AuditLog).where(AuditLog.tenant_id == s.tenant.id)))
    assert len([row for row in raw if row.action.startswith("ai_asset.")]) == 4
    response = await s.client.get("/enterprise/audit-logs")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert sorted(row["action"] for row in rows) == ["agent:restored", "agent:retirement_requested", "ai_asset.create", "ai_asset.update"]
    assert all(row["agent_id"] == str(s.bot.id) and row["agent_name"] == s.bot.name for row in rows)
    assert not any("native_key" in row["details"] or "revision_id" in row["details"] for row in rows)
    s.actor = s.foreign
    foreign = await s.client.get("/enterprise/audit-logs")
    assert foreign.status_code == 200 and s.bot.name not in foreign.text
