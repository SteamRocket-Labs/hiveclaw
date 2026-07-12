from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


R022_TABLES = {
    "agent_teams",
    "agent_team_members",
    "agent_team_events",
    "agent_collaboration_groups",
    "agent_collaboration_group_members",
    "agent_session_goals",
    "ai_asset_usage_events",
    "local_agent_channels",
    "local_agent_channel_events",
    "local_agent_channel_messages",
    "local_agent_channel_sessions",
    "local_agent_channel_ws_tickets",
    "workspace_resource_manifests",
}


async def test_fresh_bootstrap_forces_and_policies_every_r022_table(owner_engine) -> None:
    async with owner_engine.connect() as connection:
        flags = (
            await connection.execute(
                text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables)"),
                {"tables": sorted(R022_TABLES)},
            )
        ).all()
        policies = (
            await connection.execute(
                text("SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE tablename = ANY(:tables)"),
                {"tables": sorted(R022_TABLES)},
            )
        ).all()

    assert {row.relname for row in flags} == R022_TABLES
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)
    assert {row.tablename for row in policies} == R022_TABLES
    assert all(row.policyname == f"tenant_isolation_{row.tablename}" for row in policies)
    assert all(row.qual and row.with_check for row in policies)
    derived = {row.tablename: row for row in policies if row.tablename in {"agent_team_members", "agent_team_events"}}
    assert all("agent_teams" in row.qual and "team_id" in row.qual for row in derived.values())


async def test_team_parent_and_children_are_cross_tenant_read_write_isolated(
    owner_sessionmaker,
    app_user_engine,
) -> None:
    from app.models.agent import Agent
    from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenants = [
            Tenant(name=f"RLS A {token}", slug=f"rls-a-{token}"),
            Tenant(name=f"RLS B {token}", slug=f"rls-b-{token}"),
        ]
        session.add_all(tenants)
        await session.flush()
        users = [
            User(
                username=f"rls-a-{token}",
                email=f"rls-a-{token}@test.invalid",
                password_hash="x",
                display_name="RLS A",
                tenant_id=tenants[0].id,
            ),
            User(
                username=f"rls-b-{token}",
                email=f"rls-b-{token}@test.invalid",
                password_hash="x",
                display_name="RLS B",
                tenant_id=tenants[1].id,
            ),
        ]
        session.add_all(users)
        await session.flush()
        agents = [
            Agent(
                name="RLS Agent A",
                tenant_id=tenants[0].id,
                creator_id=users[0].id,
                owner_user_id=users[0].id,
                sponsor_user_id=users[0].id,
                status="running",
            ),
            Agent(
                name="RLS Agent B",
                tenant_id=tenants[1].id,
                creator_id=users[1].id,
                owner_user_id=users[1].id,
                sponsor_user_id=users[1].id,
                status="running",
            ),
        ]
        session.add_all(agents)
        await session.flush()
        chat_sessions = [
            ChatSession(agent_id=agents[0].id, tenant_id=tenants[0].id, user_id=users[0].id, title="RLS A"),
            ChatSession(agent_id=agents[1].id, tenant_id=tenants[1].id, user_id=users[1].id, title="RLS B"),
        ]
        session.add_all(chat_sessions)
        await session.flush()
        teams = [
            AgentTeam(
                tenant_id=tenants[0].id,
                lead_agent_id=agents[0].id,
                parent_session_id=chat_sessions[0].id,
                created_by_user_id=users[0].id,
                name="Team A",
            ),
            AgentTeam(
                tenant_id=tenants[1].id,
                lead_agent_id=agents[1].id,
                parent_session_id=chat_sessions[1].id,
                created_by_user_id=users[1].id,
                name="Team B",
            ),
        ]
        session.add_all(teams)
        await session.flush()
        members = [
            AgentTeamMember(team_id=teams[0].id, member_name="Worker A", chat_session_id=chat_sessions[0].id),
            AgentTeamMember(team_id=teams[1].id, member_name="Worker B", chat_session_id=chat_sessions[1].id),
        ]
        session.add_all(members)
        await session.flush()
        events = [
            AgentTeamEvent(team_id=teams[0].id, sender_member_id=members[0].id, event_type="message"),
            AgentTeamEvent(team_id=teams[1].id, sender_member_id=members[1].id, event_type="message"),
        ]
        session.add_all(events)
        await session.commit()

    tenant_a, tenant_b = tenants[0].id, tenants[1].id
    team_a, team_b = teams[0].id, teams[1].id
    session_b = chat_sessions[1].id
    try:
        async with app_user_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a}'"))
            assert await connection.scalar(text("SELECT count(*) FROM agent_teams")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM agent_team_members")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM agent_team_events")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM agent_teams WHERE id = :id"), {"id": team_b}) == 0
            await transaction.rollback()

        async with app_user_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a}'"))
            with pytest.raises(DBAPIError, match="row-level security"):
                await connection.execute(
                    text(
                        "INSERT INTO agent_team_members "
                        "(id, team_id, member_name, chat_session_id, runtime_task_type, status) "
                        "VALUES (:id, :team_id, 'Cross tenant', :session_id, 'team_member', 'idle')"
                    ),
                    {"id": uuid.uuid4(), "team_id": team_b, "session_id": session_b},
                )
            await transaction.rollback()

        async with app_user_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET LOCAL app.current_tenant_id = 'BYPASS'"))
            assert await connection.scalar(text("SELECT count(*) FROM agent_teams")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM agent_team_members")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM agent_team_events")) == 2
            await transaction.rollback()
    finally:
        async with owner_sessionmaker() as session:
            await session.execute(
                text("DELETE FROM agent_team_events WHERE team_id IN (:a, :b)"), {"a": team_a, "b": team_b}
            )
            await session.execute(
                text("DELETE FROM agent_team_members WHERE team_id IN (:a, :b)"), {"a": team_a, "b": team_b}
            )
            await session.execute(text("DELETE FROM agent_teams WHERE id IN (:a, :b)"), {"a": team_a, "b": team_b})
            await session.execute(
                text("DELETE FROM chat_sessions WHERE id IN (:a, :b)"),
                {"a": chat_sessions[0].id, "b": chat_sessions[1].id},
            )
            await session.execute(
                text("DELETE FROM agents WHERE id IN (:a, :b)"),
                {"a": agents[0].id, "b": agents[1].id},
            )
            await session.execute(
                text("DELETE FROM participants WHERE ref_id IN (:a, :b)"),
                {"a": agents[0].id, "b": agents[1].id},
            )
            await session.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": users[0].id, "b": users[1].id},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": tenant_a, "b": tenant_b},
            )
            await session.commit()
