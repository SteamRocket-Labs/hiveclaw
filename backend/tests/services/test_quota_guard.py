from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_quota_subject(
    owner_sessionmaker,
    *,
    tenant_quota_today: int | None = None,
    tenant_used_today: int = 0,
    agent_quota_today: int | None = None,
    agent_used_today: int = 0,
    user_role: str = "member",
):
    from app.models.agent import Agent
    from app.models.participant import Participant
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Quota Tenant",
                slug=f"quota-{tenant_id.hex[:8]}",
                quota_tokens_per_day=tenant_quota_today,
                tokens_used_today=tenant_used_today,
            )
        )
        db.add(
            User(
                id=user_id,
                username=f"quota-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@quota.test",
                password_hash="x",
                display_name="Quota User",
                role=user_role,
                tenant_id=tenant_id,
                quota_tokens_per_day=10_000,
                tokens_used_today=0,
            )
        )
        db.add(Participant(id=participant_id, type="agent", ref_id=agent_id, display_name="Quota Agent"))
        db.add(
            Agent(
                id=agent_id,
                name="Quota Agent",
                role_description="quota",
                creator_id=user_id,
                sponsor_user_id=user_id,
                participant_id=participant_id,
                tenant_id=tenant_id,
                quota_tokens_per_day=agent_quota_today,
                tokens_used_today=agent_used_today,
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id


async def test_invocation_quota_blocks_tenant_hard_cap_even_for_admin(owner_sessionmaker, monkeypatch):
    from app.services import quota_guard
    from app.services.quota_guard import QuotaExceeded, check_user_token_quota

    tenant_id, user_id, agent_id = await _seed_quota_subject(
        owner_sessionmaker,
        tenant_quota_today=100,
        tenant_used_today=100,
        user_role="platform_admin",
    )
    monkeypatch.setattr(quota_guard, "async_session", owner_sessionmaker)

    with pytest.raises(QuotaExceeded) as exc:
        await check_user_token_quota(user_id, agent_id=agent_id, tenant_id=tenant_id)

    assert exc.value.quota_type == "tenant_tokens_daily"


async def test_invocation_quota_blocks_agent_hard_cap_without_user(owner_sessionmaker, monkeypatch):
    from app.services import quota_guard
    from app.services.quota_guard import QuotaExceeded, check_user_token_quota

    tenant_id, _user_id, agent_id = await _seed_quota_subject(
        owner_sessionmaker,
        agent_quota_today=50,
        agent_used_today=50,
    )
    monkeypatch.setattr(quota_guard, "async_session", owner_sessionmaker)

    with pytest.raises(QuotaExceeded) as exc:
        await check_user_token_quota(None, agent_id=agent_id, tenant_id=tenant_id)

    assert exc.value.quota_type == "agent_tokens_daily"


async def test_record_token_usage_updates_tenant_agent_and_user_counters(owner_sessionmaker, monkeypatch):
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.token_tracker import record_token_usage

    tenant_id, user_id, agent_id = await _seed_quota_subject(
        owner_sessionmaker,
        tenant_used_today=12,
        agent_used_today=7,
    )

    @asynccontextmanager
    async def fake_tenant_scoped_session(_tenant_id):
        async with owner_sessionmaker() as db:
            yield db

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_scoped_session)
    await record_token_usage(agent_id, 5, user_id, tenant_id=tenant_id, source="quota-test")

    async with owner_sessionmaker() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()

    assert tenant.tokens_used_today == 17
    assert tenant.tokens_used_month == 5
    assert tenant.tokens_used_total == 5
    assert agent.tokens_used_today == 12
    assert agent.tokens_used_month == 5
    assert agent.tokens_used_total == 5
    assert user.tokens_used_today == 5
    assert user.tokens_used_month == 5
    assert user.tokens_used_total == 5
