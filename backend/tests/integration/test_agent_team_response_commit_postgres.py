"""A successful team-create response must already be visible to its consumer."""

import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.integration.test_agent_team_member_spawn_postgres import team_spawn_principal  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_team_is_committed_before_its_success_response(
    owner_sessionmaker,
    app_user_sessionmaker,
    team_spawn_principal,  # noqa: F811
    monkeypatch,
):
    from app import database
    from app.api import agent_teams
    from app.core.security import get_current_user
    from app.models.agent_team import AgentTeam
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = team_spawn_principal
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)

    async def current_user():
        async with app_user_sessionmaker() as db:
            await database.pin_rls_tenant_context(db, tenant_id)
            return await db.get(User, user_id)

    app = FastAPI()
    app.include_router(agent_teams.router, prefix="/api")
    app.dependency_overrides[get_current_user] = current_user
    response_visibility = []

    async def observe_response(scope, receive, send):
        async def observed_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                payload = json.loads(message["body"])
                async with app_user_sessionmaker() as reader:
                    await database.pin_rls_tenant_context(reader, tenant_id)
                    stored = await reader.scalar(select(AgentTeam.id).where(AgentTeam.id == uuid.UUID(payload["id"])))
                    response_visibility.append(stored == uuid.UUID(payload["id"]))
            await send(message)

        await app(scope, receive, observed_send)

    async with AsyncClient(transport=ASGITransport(app=observe_response), base_url="http://fixture.local") as client:
        response = await client.post(
            f"/api/agents/{agent_id}/agent-teams",
            json={"parent_session_id": str(session_id), "name": "Committed team"},
        )
    assert response.status_code == 200, response.text
    assert response_visibility == [True]
    async with app_user_sessionmaker() as db:
        await database.pin_rls_tenant_context(db, tenant_id)
        assert await db.scalar(select(AgentTeam.id).where(AgentTeam.id == uuid.UUID(response.json()["id"])))
