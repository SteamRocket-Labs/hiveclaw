"""Agent Team member spawn FK-ordering regressions (real PostgreSQL).

Production defect (B4): ``spawn_subagent(team_name + name)`` failed with
asyncpg ``ForeignKeyViolationError`` on
``agent_team_events_receiver_member_id_fkey`` because the
``member_spawned`` event row was flushed in the same unit of work as — but
with no guaranteed ordering before — the new ``agent_team_members`` row it
references. SQLAlchemy's unit of work only orders inserts across mappers via
``relationship()`` dependencies; these models use bare ``ForeignKey`` columns,
so the event INSERT can be emitted before the member INSERT and Postgres
rejects it.

These tests drive the REAL ``create_agent_team_runtime`` /
``spawn_agent_team_member_runtime`` persistence against Testcontainers
PostgreSQL. Only the post-admission fanout messenger is stubbed: the FK
failure occurs at the ``member_spawned`` flush, before any messaging, and the
messenger's runtime-task continuation is out of scope here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import User
from app.services import agent_team_runtime_service


async def _seed_principal(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="Team Spawn Tenant", slug=f"team-spawn-{uuid.uuid4().hex[:10]}")
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"team-spawn-{uuid.uuid4().hex[:10]}",
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password_hash="x",
            display_name="Team Spawn Owner",
            tenant_id=tenant.id,
            role="org_admin",
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=user.id,
            owner_user_id=user.id,
            name="Team Lead",
            role_description="Agent Team member spawn regression lead.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        parent_session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            tenant_id=tenant.id,
            title=f"team-parent-{uuid.uuid4().hex[:8]}",
        )
        db.add(parent_session)
        await db.commit()
        return tenant.id, user.id, agent.id, parent_session.id


def _stub_fanout(monkeypatch) -> None:
    async def _stub_message(**kwargs):
        return {
            "ok": True,
            "results": [{"status": "queued", "member_id": "", "member_name": "", "child_session_id": ""}],
        }

    monkeypatch.setattr(agent_team_runtime_service, "message_agent_team_members_runtime", _stub_message)


async def test_team_create_and_member_spawn_persist_in_order(owner_sessionmaker, monkeypatch) -> None:
    tenant_id, user_id, agent_id, session_id = await _seed_principal(owner_sessionmaker)
    _stub_fanout(monkeypatch)

    async with owner_sessionmaker() as db:
        team_result = await agent_team_runtime_service.create_agent_team_runtime_result(
            db=db,
            agent=type("A", (), {"id": agent_id, "tenant_id": tenant_id})(),
            user=type("U", (), {"id": user_id})(),
            parent_session=type("S", (), {"id": session_id, "root_session_id": session_id})(),
            name="Regression Team",
            members=[],
            source="team_create_tool",
            command="team_create",
            append_parent_events=False,
            emit_created_hook=False,
        )
        team = team_result.team
        await db.commit()
        team_id = team.id
        db.expunge_all()

    async with owner_sessionmaker() as db:
        team = (await db.execute(select(AgentTeam).where(AgentTeam.id == team_id))).scalar_one()
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        parent_session = (await db.execute(select(ChatSession).where(ChatSession.id == session_id))).scalar_one()

        spawn_result = await agent_team_runtime_service.spawn_agent_team_member_runtime(
            db=db,
            agent=agent,
            user=user,
            parent_session=parent_session,
            team=team,
            spec=agent_team_runtime_service.TeamMemberCreateSpec(name="worker-1", role="doer"),
            prompt="Do the bounded regression work.",
            source="agent_tool_teammate_spawn",
        )
        assert spawn_result["status"] == "teammate_spawned"
        member_id = spawn_result["member"]["id"]
        # Production commits inside _register_team_fanout_requested_set; the
        # stub here removes that commit, so persist explicitly.
        await db.commit()

    async with owner_sessionmaker() as db:
        member = (await db.execute(select(AgentTeamMember).where(AgentTeamMember.id == member_id))).scalar_one()
        assert member.member_name == "worker-1"
        events = (
            (
                await db.execute(
                    select(AgentTeamEvent)
                    .where(AgentTeamEvent.team_id == team.id)
                    .order_by(AgentTeamEvent.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        event_types = [event.event_type for event in events]
        assert "team_created" in event_types
        assert "member_spawned" in event_types
        spawned = next(event for event in events if event.event_type == "member_spawned")
        assert spawned.receiver_member_id == member.id
