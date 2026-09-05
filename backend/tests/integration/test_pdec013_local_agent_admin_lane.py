"""PDEC-013 administrator access to the Local Agent browser/business lane.

Real-PostgreSQL regressions for the exact surfaces the owner contract names:
a scoped platform administrator with no explicit ``AgentPermission`` reaching
an employee Agent's business sessions through both agent-scoped and
session-id-only entries, browser WS-ticket actor validation at subscription,
and the demotion/offboarding negatives — while the host owner routing and the
real initiating actor stay unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.local_agent_channel import LocalAgentChannelSession
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel_service


class _Lane:
    def __init__(self) -> None:
        self.tenant_a: uuid.UUID
        self.tenant_b: uuid.UUID
        self.employee_host: uuid.UUID
        self.employee_other: uuid.UUID
        self.org_admin: uuid.UUID
        self.platform_admin: uuid.UUID
        self.employee_b: uuid.UUID
        self.agent: uuid.UUID
        self.channel_session_id: uuid.UUID
        self.chat_session_id: uuid.UUID


async def _seed_lane(owner_sessionmaker) -> _Lane:
    lane = _Lane()
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenant_a = Tenant(name=f"Lane A {token}", slug=f"lane-a-{token}")
        tenant_b = Tenant(name=f"Lane B {token}", slug=f"lane-b-{token}")
        session.add_all([tenant_a, tenant_b])
        await session.flush()

        def _user(name: str, role: str, tenant_id: uuid.UUID) -> User:
            return User(
                username=f"{name}-{token}",
                email=f"{name}-{token}@test.invalid",
                password_hash="x",
                display_name=name,
                tenant_id=tenant_id,
                role=role,
                is_active=True,
            )

        host = _user("lane-host", "member", tenant_a.id)
        other = _user("lane-other", "member", tenant_a.id)
        org_admin = _user("lane-admin", "org_admin", tenant_a.id)
        platform_admin = _user("lane-platform", "platform_admin", tenant_b.id)
        employee_b = _user("lane-employee-b", "member", tenant_b.id)
        session.add_all([host, other, org_admin, platform_admin, employee_b])
        await session.flush()

        agent = Agent(
            name=f"Lane local agent {token}",
            tenant_id=tenant_a.id,
            creator_id=host.id,
            owner_user_id=host.id,
            sponsor_user_id=host.id,
            status="running",
            agent_type="local_agent",
        )
        session.add(agent)
        await session.flush()

        chat_session = ChatSession(
            agent_id=agent.id,
            tenant_id=tenant_a.id,
            user_id=host.id,
            title=f"host session {token}",
            source_channel="web",
        )
        session.add(chat_session)
        await session.flush()

        channel_session = LocalAgentChannelSession(
            tenant_id=tenant_a.id,
            owner_user_id=host.id,
            source_agent_id=agent.id,
            chat_session_id=chat_session.id,
            source="web",
            status="active",
        )
        session.add(channel_session)
        await session.commit()

        lane.tenant_a = tenant_a.id
        lane.tenant_b = tenant_b.id
        lane.employee_host = host.id
        lane.employee_other = other.id
        lane.org_admin = org_admin.id
        lane.platform_admin = platform_admin.id
        lane.employee_b = employee_b.id
        lane.agent = agent.id
        lane.channel_session_id = channel_session.id
        lane.chat_session_id = chat_session.id
    return lane


async def _load_actor(db, user_id: uuid.UUID) -> User:
    actor = await db.get(User, user_id)
    if actor is None:
        async with enter_rls_bypass(db, reason="test lane actor lookup", actor_id=str(user_id)) as bypass_db:
            actor = await bypass_db.get(User, user_id)
    assert actor is not None
    return actor


async def test_scoped_admin_sees_employee_business_sessions(owner_sessionmaker, app_user_sessionmaker) -> None:
    lane = await _seed_lane(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, lane.tenant_a)

        # Agent-scoped listing: the per-actor filter hides the employee's
        # mirrored session; the scoped administrator business view sees it.
        employee_rows = await channel_service.list_agent_channel_sessions(
            db,
            tenant_id=lane.tenant_a,
            owner_user_id=lane.employee_host,
            actor_user_id=lane.employee_other,
            source_agent_id=lane.agent,
        )
        assert employee_rows == []

        org_admin = await _load_actor(db, lane.org_admin)
        admin_rows = await channel_service.list_agent_channel_sessions(
            db,
            tenant_id=lane.tenant_a,
            owner_user_id=lane.employee_host,
            actor_user_id=None,  # route passes None for scoped administrators
            source_agent_id=lane.agent,
        )
        assert [str(row["id"]) for row in admin_rows] == [str(lane.channel_session_id)]

        # Session-id-only entry: the scope is recovered from the row's own
        # tenant, the returned host owner stays the employee, and the access
        # decision is role-based rather than grant-based.
        payload, host_owner = await channel_service.get_channel_session_for_actor(
            db,
            session_id=lane.channel_session_id,
            actor_user_id=lane.org_admin,
            access_user=org_admin,
        )
        assert str(host_owner) == str(lane.employee_host)
        assert str(payload["id"]) == str(lane.channel_session_id)

        # An ordinary same-tenant member still cannot resolve it.
        with pytest.raises(HTTPException) as exc:
            await channel_service.get_channel_session_for_actor(
                db,
                session_id=lane.channel_session_id,
                actor_user_id=lane.employee_other,
                access_user=await _load_actor(db, lane.employee_other),
            )
        assert exc.value.status_code == 404


async def test_browser_ws_ticket_validates_real_actor_at_subscription(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    lane = await _seed_lane(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, lane.tenant_a)
        org_admin = await _load_actor(db, lane.org_admin)

        ticket = await channel_service.create_browser_session_ws_ticket(
            db,
            tenant_id=lane.tenant_a,
            actor_user_id=lane.org_admin,
            session_id=lane.channel_session_id,
            access_user=org_admin,
        )
        assert ticket["single_use"] is False

        resolved = await channel_service.resolve_browser_session_ws_ticket(
            db, ticket=ticket["ticket"], session_id=lane.channel_session_id
        )
        assert str(resolved["owner_user_id"]) == str(lane.employee_host)
        assert str(resolved["tenant_id"]) == str(lane.tenant_a)

        # A host-owner ticket keeps working for the host actor.
        host_ticket = await channel_service.create_browser_session_ws_ticket(
            db,
            tenant_id=lane.tenant_a,
            actor_user_id=lane.employee_host,
            session_id=lane.channel_session_id,
        )
        resolved_host = await channel_service.resolve_browser_session_ws_ticket(
            db, ticket=host_ticket["ticket"], session_id=lane.channel_session_id
        )
        assert str(resolved_host["owner_user_id"]) == str(lane.employee_host)

    # Role demotion inside the ticket lifetime: the actor is a plain member
    # without a mirrored session, so the reusable ticket stops working.
    async with owner_sessionmaker() as owner_db:
        demoted = await owner_db.get(User, lane.org_admin)
        assert demoted is not None
        demoted.role = "member"
        await owner_db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, lane.tenant_a)
        with pytest.raises(HTTPException) as exc:
            await channel_service.resolve_browser_session_ws_ticket(
                db, ticket=ticket["ticket"], session_id=lane.channel_session_id
            )
        assert exc.value.status_code == 403

    # Offboarding the actor invalidates the ticket the same way.
    async with owner_sessionmaker() as owner_db:
        offboarded = await owner_db.get(User, lane.employee_host)
        assert offboarded is not None
        offboarded.is_active = False
        await owner_db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, lane.tenant_a)
        with pytest.raises(HTTPException) as exc:
            await channel_service.resolve_browser_session_ws_ticket(
                db, ticket=host_ticket["ticket"], session_id=lane.channel_session_id
            )
        assert exc.value.status_code == 403


async def test_platform_admin_without_agent_permission_uses_role_lane(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    lane = await _seed_lane(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        # Selected company pins the tenant scope for the platform administrator.
        await pin_rls_tenant_context(db, lane.tenant_a)
        platform_actor = await _load_actor(db, lane.platform_admin)
        # Mirror get_current_user: detach before overriding tenant scope in memory.
        db.expunge(platform_actor)
        platform_actor.tenant_id = lane.tenant_a

        payload, host_owner = await channel_service.get_channel_session_for_actor(
            db,
            session_id=lane.channel_session_id,
            actor_user_id=lane.platform_admin,
            access_user=platform_actor,
        )
        assert str(host_owner) == str(lane.employee_host)

        ticket = await channel_service.create_browser_session_ws_ticket(
            db,
            tenant_id=lane.tenant_a,
            actor_user_id=lane.platform_admin,
            session_id=lane.channel_session_id,
            access_user=platform_actor,
        )
        resolved = await channel_service.resolve_browser_session_ws_ticket(
            db, ticket=ticket["ticket"], session_id=lane.channel_session_id
        )
        assert str(resolved["owner_user_id"]) == str(lane.employee_host)

    # A foreign-company member without any scope gets nothing, not even
    # existence: the session resolves like a missing row.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, lane.tenant_b)
        employee_b = await _load_actor(db, lane.employee_b)
        with pytest.raises(HTTPException) as exc:
            await channel_service.get_channel_session_for_actor(
                db,
                session_id=lane.channel_session_id,
                actor_user_id=lane.employee_b,
                access_user=employee_b,
            )
        assert exc.value.status_code == 404
