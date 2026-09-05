"""PDEC-013 scoped business administrator authority against real PostgreSQL RLS.

These regressions pin the owner's three-role contract at the exact boundaries
where identity and tenant scoping decide: Agent capability resolution, cross
user session authority (read, mutation, read-only kinds), the durable command
recovery lane for cross-company platform administrators, and the rule that a
legacy ``manage`` grant never becomes administrator identity.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.permissions import (
    SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
    authorize_session_action,
    check_agent_access,
)
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission, SecurityAuditEvent
from app.models.session_v2 import SessionCommand
from app.models.tenant import Tenant
from app.models.user import User
from app.services.session_v2_persistence import resolve_session_command_authority


class _World:
    """Tenant/user/agent/session fixture ids shared across the checks below."""

    def __init__(self) -> None:
        self.tenant_a: uuid.UUID
        self.tenant_b: uuid.UUID
        self.tenant_home: uuid.UUID
        self.employee_a: uuid.UUID
        self.employee_a2: uuid.UUID
        self.employee_a3: uuid.UUID
        self.admin_a: uuid.UUID
        self.employee_b: uuid.UUID
        self.platform_admin: uuid.UUID
        self.agent_a: uuid.UUID
        self.session_a: uuid.UUID
        self.session_read_only: uuid.UUID


async def _seed_world(owner_sessionmaker) -> _World:
    world = _World()
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenant_a = Tenant(name=f"P13 A {token}", slug=f"p13-a-{token}")
        tenant_b = Tenant(name=f"P13 B {token}", slug=f"p13-b-{token}")
        tenant_home = Tenant(name=f"P13 Home {token}", slug=f"p13-home-{token}")
        session.add_all([tenant_a, tenant_b, tenant_home])
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

        employee_a = _user("employee-a", "member", tenant_a.id)
        employee_a2 = _user("employee-a2", "member", tenant_a.id)
        employee_a3 = _user("employee-a3", "member", tenant_a.id)
        admin_a = _user("admin-a", "org_admin", tenant_a.id)
        employee_b = _user("employee-b", "member", tenant_b.id)
        platform_admin = _user("platform-p13", "platform_admin", tenant_home.id)
        session.add_all([employee_a, employee_a2, employee_a3, admin_a, employee_b, platform_admin])
        await session.flush()

        agent_a = Agent(
            name=f"Employee private Agent {token}",
            tenant_id=tenant_a.id,
            creator_id=employee_a.id,
            owner_user_id=employee_a.id,
            sponsor_user_id=employee_a.id,
            status="running",
        )
        session.add(agent_a)
        await session.flush()

        session_a = ChatSession(
            agent_id=agent_a.id,
            tenant_id=tenant_a.id,
            user_id=employee_a.id,
            title=f"employee private session {token}",
            source_channel="web",
            session_kind="human_chat",
        )
        session_ro = ChatSession(
            agent_id=agent_a.id,
            tenant_id=tenant_a.id,
            user_id=employee_a.id,
            title=f"delegation child {token}",
            source_channel="agent",
            session_kind="delegation_run",
            runtime_source="delegation",
        )
        session.add_all([session_a, session_ro])
        await session.flush()

        # A legacy generic-resource ``manage`` grant for a same-tenant member:
        # delegated capability, deliberately not administrator identity.
        session.add(
            ResourcePermission(
                tenant_id=tenant_a.id,
                principal_type="user",
                principal_id=employee_a2.id,
                resource_type="agent",
                resource_id=agent_a.id,
                actions=["manage", "read", "execute"],
                effect="allow",
                sensitivity_ceiling="PL4_credential",
            )
        )
        await session.commit()

        world.tenant_a = tenant_a.id
        world.tenant_b = tenant_b.id
        world.tenant_home = tenant_home.id
        world.employee_a = employee_a.id
        world.employee_a2 = employee_a2.id
        world.employee_a3 = employee_a3.id
        world.admin_a = admin_a.id
        world.employee_b = employee_b.id
        world.platform_admin = platform_admin.id
        world.agent_a = agent_a.id
        world.session_a = session_a.id
        world.session_read_only = session_ro.id
    return world


async def _load_actor(db, user_id: uuid.UUID) -> User:
    """Load the canonical User row exactly like the request-time identity path."""
    actor = await db.get(User, user_id)
    if actor is None:
        async with enter_rls_bypass(db, reason="test canonical actor lookup", actor_id=str(user_id)) as bypass_db:
            actor = await bypass_db.get(User, user_id)
    assert actor is not None
    return actor


async def test_scoped_admin_agent_authority_matrix(owner_sessionmaker, app_user_sessionmaker) -> None:
    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)

        # Platform administrator with a valid selected company and NO
        # AgentPermission/ResourcePermission row at all.
        platform_actor = await _load_actor(db, world.platform_admin)
        # Mirror get_current_user: detach before overriding tenant scope in memory.
        db.expunge(platform_actor)
        platform_actor.tenant_id = world.tenant_a
        agent, access_level = await check_agent_access(db, platform_actor, world.agent_a)
        assert str(agent.id) == str(world.agent_a)
        assert access_level == "manage"

        # Company administrator inside their own company.
        admin_actor = await _load_actor(db, world.admin_a)
        _agent, admin_level = await check_agent_access(db, admin_actor, world.agent_a)
        assert admin_level == "manage"

        # Same-tenant member without any grant is refused.
        member_actor = await _load_actor(db, world.employee_a3)
        with pytest.raises(HTTPException) as exc:
            await check_agent_access(db, member_actor, world.agent_a)
        assert exc.value.status_code == 403

        # A foreign-company member resolves the Agent exactly like a missing row.
        foreign_actor = await _load_actor(db, world.employee_b)
        with pytest.raises(HTTPException) as exc:
            await check_agent_access(db, foreign_actor, world.agent_a)
        assert exc.value.status_code == 404


async def test_scoped_admin_cross_user_session_authority_and_audit(owner_sessionmaker, app_user_sessionmaker) -> None:
    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        admin_actor = await _load_actor(db, world.admin_a)

        read_decision = await authorize_session_action(
            db,
            admin_actor,
            agent_id=world.agent_a,
            session_id=world.session_a,
            action="read_messages",
        )
        assert read_decision.authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE

        write_decision = await authorize_session_action(
            db,
            admin_actor,
            agent_id=world.agent_a,
            session_id=world.session_a,
            action="submit_session_human_input",
            require_writable=True,
        )
        assert write_decision.authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE

        # The real actor and scope are audited on the tenant audit plane.
        audit_rows = (
            (
                await db.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.event_type == "session.scoped_business_admin_access",
                        SecurityAuditEvent.resource_id == world.session_a,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {str(row.actor_id) for row in audit_rows} == {str(world.admin_a)}
        for audit_row in audit_rows:
            assert str(audit_row.tenant_id) == str(world.tenant_a)
            assert audit_row.details["session_user_id"] == str(world.employee_a)
            assert audit_row.details["authority_source"] == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE

        # Intrinsically read-only session kinds stay read-only for admins.
        with pytest.raises(HTTPException) as exc:
            await authorize_session_action(
                db,
                admin_actor,
                agent_id=world.agent_a,
                session_id=world.session_read_only,
                action="submit_session_human_input",
                require_writable=True,
            )
        assert exc.value.status_code == 409

        # A legacy ``manage`` resource grant is not administrator identity:
        # the same member still cannot touch another user's session.
        member_actor = await _load_actor(db, world.employee_a2)
        _agent, member_level = await check_agent_access(db, member_actor, world.agent_a)
        assert member_level == "manage"
        with pytest.raises(HTTPException) as exc:
            await authorize_session_action(
                db,
                member_actor,
                agent_id=world.agent_a,
                session_id=world.session_a,
                action="read_messages",
            )
        assert exc.value.status_code == 403


async def test_platform_admin_durable_command_recovery_across_companies(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    world = await _seed_world(owner_sessionmaker)

    command = SessionCommand(
        id=uuid.uuid4(),
        tenant_id=world.tenant_a,
        principal_type="user",
        principal_id=world.platform_admin,
        session_id=world.session_a,
        namespace="human_input",
        idempotency_key=f"p13-{uuid.uuid4().hex[:12]}",
        command_kind="human_input",
        status="accepted",
    )

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        session = (await db.execute(select(ChatSession).where(ChatSession.id == world.session_a))).scalar_one()
        context = await resolve_session_command_authority(
            db, command=command, session=session, action="mutate_session_input"
        )
        # Administrator-authored input stays attributed to the administrator;
        # the employee session keeps its original owner.
        assert str(context.actor.id) == str(world.platform_admin)
        assert str(context.authority.principal_id) == str(world.platform_admin)
        assert context.authority.authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
        assert str(context.authority.tenant_id) == str(world.tenant_a)

    # A demoted administrator (canonical DB role now member) is a typed
    # recovery failure, not a silently accepted cross-company command.
    async with owner_sessionmaker() as owner_db:
        demoted = await owner_db.get(User, world.platform_admin)
        assert demoted is not None
        demoted.role = "member"
        await owner_db.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        session = (await db.execute(select(ChatSession).where(ChatSession.id == world.session_a))).scalar_one()
        with pytest.raises(RuntimeError, match="session_command_user_authority_mismatch"):
            await resolve_session_command_authority(db, command=command, session=session, action="mutate_session_input")
