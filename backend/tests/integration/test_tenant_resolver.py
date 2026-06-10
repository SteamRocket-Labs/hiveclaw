"""resolve_tenant_for_agent against real PostgreSQL — RLS 阶段1 DD-A red tests.

The chicken-and-egg breaker must read an agent's tenant even under enforced
RLS (non-owner role), where a bare read of ``agents`` fail-closes. These tests
seed an agent as the owner (RLS-bypassing) then resolve as the non-owner
``app_user`` role — proving the bypass lookup is what lets background tenant
resolution survive the role flip.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.plan_request import AgentPlanRequest
from app.models.tenant import Tenant
from app.models.user import User
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_plan


async def _seed_agent(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant→user→agent as the owner role (bypasses RLS for setup)."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as s:
        s.add(Tenant(id=tenant_id, name="T", slug=f"t-{suffix}"))
        s.add(
            User(
                id=user_id,
                username=f"u-{suffix}",
                email=f"u-{suffix}@example.test",
                password_hash="x",
                display_name="U",
                tenant_id=tenant_id,
            )
        )
        s.add(Agent(id=agent_id, name="A", creator_id=user_id, tenant_id=tenant_id))
        await s.commit()
    return agent_id, tenant_id


async def test_resolves_tenant_under_nonowner_role(owner_sessionmaker, app_user_sessionmaker):
    """The audited bypass lookup reads the tenant even as the non-owner role —
    the whole point of the helper."""
    agent_id, tenant_id = await _seed_agent(owner_sessionmaker)
    resolved = await resolve_tenant_for_agent(agent_id, session_factory=app_user_sessionmaker)
    assert str(resolved) == str(tenant_id)


async def test_bare_nonowner_read_fails_closed_without_bypass(owner_sessionmaker, app_user_sessionmaker):
    """Contrast case: a plain (non-bypass) read of agents as the non-owner role
    with empty GUC sees nothing — exactly the gap resolve_tenant_for_agent closes."""
    agent_id, _ = await _seed_agent(owner_sessionmaker)
    async with app_user_sessionmaker() as s:
        await s.execute(text("SET LOCAL app.current_tenant_id = ''"))
        row = (await s.execute(select(Agent.tenant_id).where(Agent.id == agent_id))).scalar_one_or_none()
    assert row is None


async def test_returns_none_for_missing_agent_id():
    assert await resolve_tenant_for_agent(None) is None


async def _seed_plan(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert tenant→user→agent→plan as the owner role (bypasses RLS for setup)."""
    agent_id, tenant_id = await _seed_agent(owner_sessionmaker)
    plan_id = uuid.uuid4()
    async with owner_sessionmaker() as s:
        s.add(
            AgentPlanRequest(
                id=plan_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                intent_type="autonomous_wake",
                status="confirmed",
            )
        )
        await s.commit()
    return plan_id, tenant_id


async def test_resolves_plan_tenant_under_nonowner_role(owner_sessionmaker, app_user_sessionmaker):
    """The audited bypass lookup reads agent_plan_requests.tenant_id as the
    non-owner role — what lets a plan_id-only PlanModeService method scope itself
    after the stage-2a policy lands on the table."""
    plan_id, tenant_id = await _seed_plan(owner_sessionmaker)
    resolved = await resolve_tenant_for_plan(plan_id, session_factory=app_user_sessionmaker)
    assert str(resolved) == str(tenant_id)


async def test_bare_nonowner_plan_read_fails_closed_without_bypass(owner_sessionmaker, app_user_sessionmaker):
    """Contrast: a plain read of the policied agent_plan_requests as the non-owner
    role with empty GUC sees nothing — the gap resolve_tenant_for_plan closes."""
    plan_id, _ = await _seed_plan(owner_sessionmaker)
    async with app_user_sessionmaker() as s:
        await s.execute(text("SET LOCAL app.current_tenant_id = ''"))
        row = (
            await s.execute(select(AgentPlanRequest.tenant_id).where(AgentPlanRequest.id == plan_id))
        ).scalar_one_or_none()
    assert row is None


async def test_returns_none_for_missing_plan_id():
    assert await resolve_tenant_for_plan(None) is None
