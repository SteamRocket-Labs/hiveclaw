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
