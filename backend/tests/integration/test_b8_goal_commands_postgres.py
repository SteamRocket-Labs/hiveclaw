"""Local goal commands must project SQL-onupdate timestamps without implicit IO.

Exercise the HTTP command registry, real access checks, ingress boundary, event
append and commit against PostgreSQL, not the goal helper or a fake flush.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.commands import router
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.tenant import Tenant
from app.models.user import User


@pytest.mark.parametrize("command", ["goal_update", "goal_stop"])
async def test_local_goal_command_returns_committed_projection_without_lazy_io(owner_sessionmaker, command):
    suffix = uuid.uuid4().hex
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="B8 isolated company", slug=f"b8-{suffix}")
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"b8-{suffix}",
            email=f"b8-{suffix}@example.test",
            password_hash="not-a-login",
            display_name="Goal owner",
            role="member",
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=user.id,
            owner_user_id=user.id,
            name="B8 goal assistant",
            role_description="Goal command regression",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(tenant_id=tenant.id, agent_id=agent.id, user_id=user.id, title="B8 goal")
        db.add(session)
        await db.flush()
        goal = AgentSessionGoal(
            tenant_id=tenant.id,
            agent_id=agent.id,
            chat_session_id=session.id,
            created_by_user_id=user.id,
            objective="Original objective",
            token_budget=1000,
        )
        db.add(goal)
        await db.commit()
        agent_id, session_id, goal_id = agent.id, session.id, goal.id
        previous_updated_at = goal.updated_at

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: db
        arguments = (
            {"goal_id": str(goal_id), "objective": "Updated objective", "token_budget": 1500}
            if command == "goal_update"
            else {
                "goal_id": str(goal_id),
                "status": "complete",
                "completion_summary": "Finished locally",
            }
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            response = await client.post(
                f"/api/agents/{agent_id}/commands/{command}/execute",
                json={
                    "origin": "local",
                    "session_id": str(session_id),
                    "arguments": arguments,
                },
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["command"] == command
        projected = payload["result"]
        assert projected["id"] == str(goal_id)
        assert datetime.fromisoformat(projected["updated_at"]) >= previous_updated_at
        assert projected["status"] == ("active" if command == "goal_update" else "complete")

    # A new session verifies what the real endpoint committed, not identity-map state.
    async with owner_sessionmaker() as db:
        stored = await db.get(AgentSessionGoal, goal_id)
        assert projected["updated_at"] == stored.updated_at.isoformat()
        assert stored.metadata_json["last_command"] == command
        if command == "goal_update":
            assert stored.objective == projected["objective"] == "Updated objective"
            assert stored.token_budget == projected["remaining_tokens"] == 1500
        else:
            assert stored.completion_summary == projected["completion_summary"] == "Finished locally"
            assert projected["completed_at"] == stored.completed_at.isoformat()
        events = list(
            (
                await db.scalars(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type == "goal",
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].metadata_json["command"] == command
