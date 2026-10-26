"""Schedule acknowledgements must be visible before request dependency cleanup."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI

import app.database as database
from app.api import schedules
from app.core.security import get_current_user
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.plan_mode_recommendation_service import create_plan_recommendation, decline_recommendation


@pytest.mark.parametrize("operation", ["create", "update", "delete", "run"])
async def test_schedule_response_is_visible_to_an_independent_consumer(
    operation, owner_sessionmaker, app_user_sessionmaker, monkeypatch
):
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="Schedule persistence", slug=f"schedule-persistence-{suffix}")
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"schedule-{suffix}",
            email=f"schedule-{suffix}@example.test",
            password_hash="x",
            display_name="Schedule owner",
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name="Schedule employee", tenant_id=tenant.id, creator_id=user.id, owner_user_id=user.id)
        db.add(agent)
        await db.flush()
        session = ChatSession(tenant_id=tenant.id, agent_id=agent.id, user_id=user.id)
        db.add(session)
        await db.commit()

    create = {
        "name": "Original schedule",
        "instruction": "Prepare report",
        "cron_expr": "0 9 * * *",
        "is_enabled": False,
    }
    path = f"/api/agents/{agent.id}/schedules/"
    method, payload, expected_status = "POST", create, 201
    if operation != "create":
        async with tenant_scoped_session(tenant.id, session_factory=app_user_sessionmaker) as db:
            original = await schedules.create_schedule(agent.id, schedules.ScheduleCreate(**create), user, db)
        path += str(original.id)
        if operation == "update":
            method, payload, expected_status = (
                "PATCH",
                {"name": "Revised schedule", "instruction": "Revised report"},
                200,
            )
        elif operation == "delete":
            method, payload, expected_status = "DELETE", None, 204
        else:
            async with tenant_scoped_session(tenant.id, session_factory=app_user_sessionmaker) as db:
                recommendation = await create_plan_recommendation(
                    db,
                    tenant_id=tenant.id,
                    agent_id=agent.id,
                    recommended_to_user_id=user.id,
                    session_id=str(session.id),
                    source="schedules_api_manual_run",
                    original_request="Run the selected schedule once.",
                )
                decline_recommendation(recommendation, user_id=user.id)
            path += "/run"
            payload = {
                "plan_mode_decision": "declined",
                "plan_recommendation_id": str(recommendation.id),
                "confirmed_plan_session_id": str(session.id),
            }
            expected_status = 200

    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    app = FastAPI()
    app.include_router(schedules.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    observed = {}

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode() if payload else b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            observed["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response = json.loads(message["body"]) if message["body"] else None
            observed["response"] = response
            # ASGI send runs before get_db's request-scoped teardown commit.
            # Use the real list consumer and a separate non-owner connection.
            async with tenant_scoped_session(tenant.id, session_factory=app_user_sessionmaker) as reader:
                observed["schedules"] = await schedules.list_schedules(
                    agent.id, operator_view=False, operator_reason=None, current_user=user, db=reader
                )
                if operation == "run" and observed["status"] == 200:
                    identity = uuid.UUID(response["trigger_id"])
                    observed["trigger"] = await reader.get(AgentTrigger, identity)
                    observed["source"] = await reader.get(
                        InvocationSpan, uuid.uuid5(identity, "trigger-configuration-pointer")
                    )

    token = database.set_current_tenant(str(tenant.id))
    try:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "server": ("test", 80),
                "client": ("test", 1234),
            },
            receive,
            send,
        )
    finally:
        database.reset_current_tenant(token)

    assert observed["status"] == expected_status, observed["response"]
    visible = observed["schedules"]
    if operation == "delete":
        assert visible == []
    elif operation == "run":
        assert observed["response"]["status"] == "queued"
        trigger = observed["trigger"]
        assert trigger is not None
        assert trigger.type == "once" and trigger.is_enabled and trigger.max_fires == 1
        assert trigger.config["source_schedule_id"] == str(original.id)
        assert trigger.config["metadata"]["plan_recommendation_id"] == str(recommendation.id)
        assert observed["source"].metadata_json["configuration"]["fields"]["reason"] == create["instruction"]
        assert len(visible) == 1 and visible[0].id == original.id and not visible[0].is_enabled
    else:
        assert len(visible) == 1
        assert str(visible[0].id) == observed["response"]["id"]
        assert visible[0].name == payload["name"]
        assert visible[0].instruction == payload["instruction"]
        assert visible[0].is_enabled is False
