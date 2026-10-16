"""Responsibilities share canonical settings and exact original-work continuation."""

import hashlib
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.user import User
from app.models.session_v2 import SessionCommand, SessionTurnInput
from tests.integration.test_m1g4_question_answers_postgres import _ask
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _app(owner_sessionmaker, app_user_sessionmaker):
    from app.api.agents import router as agents_router
    from app.api.chat_sessions import router as chat_router
    from app.api.tools import router as tools_router

    tenant, requester, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db:
        current = await db.get(Agent, agent)
        current.sponsor_user_id = requester
        current.role_description = "Original responsibilities"
        await db.commit()
        user = await db.get(User, requester)

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    for router in (agents_router, chat_router, tools_router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    return app, (tenant, requester, agent, session, run)


async def test_role_revision_prevents_a_late_profile_write_and_replays_a_recorded_save(
    owner_sessionmaker, app_user_sessionmaker,
):
    app, (_, _, agent, _, _) = await _app(owner_sessionmaker, app_user_sessionmaker)
    revision = hashlib.sha256(b"Original responsibilities").hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://role.test") as client:
        endpoint = f"/api/agents/{agent}"
        update = {"role_description": "Review primary sources and cite the evidence.", "expected_role_revision": revision}
        saved = await client.patch(endpoint, json=update)
        assert saved.status_code == 200, saved.text
        repeated = await client.patch(endpoint, json=update)
        assert repeated.status_code == 200 and repeated.json()["role_description"] == update["role_description"]
        stale = await client.patch(endpoint, json={**update, "role_description": "A stale draft must not replace the saved role."})
        assert stale.status_code == 409, stale.text
        loaded = await client.get(endpoint)
        assert loaded.json()["role_description"] == update["role_description"]
        assert loaded.json()["role_revision"] == saved.json()["role_revision"]


@pytest.mark.parametrize("target_kind", ["self", "created_employee", "changed", "lost_target", "foreign_target"])
async def test_native_role_question_records_the_exact_setting_without_saving_or_waking_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, target_kind,
):
    from app.services import agent_tools

    app, (tenant, requester, agent, session, run) = await _app(owner_sessionmaker, app_user_sessionmaker)
    foreign_tenant, foreign_user, foreign_agent, _, _ = await _seed_session(owner_sessionmaker)
    target = agent if target_kind == "self" else foreign_agent if target_kind == "foreign_target" else uuid.uuid4()
    if target not in {agent, foreign_agent}:
        async with owner_sessionmaker() as db:
            db.add(Agent(id=target, tenant_id=tenant, creator_id=requester,
                role_description="Original responsibilities", name="New employee"))
            await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=requester, arguments={
                "questions": [{"question": "Review this employee's responsibilities?", "options": []}],
                "configuration": {"employee_setting": "role", "target_agent_id": str(target)},
            })
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://role.test") as client:
        endpoint = f"/api/tools/agents/{agent}/sessions/{session}/questions/{question}/role"
        loaded = await client.get(endpoint)
        if target_kind == "foreign_target":
            assert loaded.status_code in {403, 404}
            assert (await client.patch(f"/api/agents/{target}", json={"role_description": "Foreign write"})).status_code in {403, 404}
            return
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["role_description"] == "Original responsibilities"
        assert loaded.json()["state"] == "waiting"
        input_id = str(uuid.uuid4())
        answer = {"kind": "answer_request", "input_id": input_id, "idempotency_key": f"role:{input_id}",
            "request_item_id": str(question), "content_parts": [{"type": "text", "text": "Continue with the reviewed responsibilities."}]}
        async def no_wakeup(**kwargs):
            pass
        monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
        input_url = f"/api/agents/{agent}/sessions/{session}/inputs"
        assert (await client.post(input_url, json=answer)).status_code == 409
        stored = await client.patch(f"/api/agents/{target}", json={"role_description": "Review original evidence before reporting.",
            "expected_role_revision": loaded.json()["role_revision"]})
        assert stored.status_code == 200, stored.text
        assert (await client.post(endpoint + "/confirm", json={"role_revision": loaded.json()["role_revision"]})).status_code == 409
        confirmed = await client.post(endpoint + "/confirm", json={"role_revision": stored.json()["role_revision"]})
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "ready", confirmed.text
        assert (await client.get(endpoint)).json()["receipt_ref"] == confirmed.json()["receipt_ref"]
        async with owner_sessionmaker() as db:
            assert not (await db.scalars(select(SessionTurnInput).where(SessionTurnInput.request_item_id == question))).all()
        if target_kind == "changed":
            assert (await client.patch(f"/api/agents/{target}", json={"role_description": "A newer saved role",
                "expected_role_revision": stored.json()["role_revision"]})).status_code == 200
            assert (await client.post(input_url, json=answer)).status_code == 409
            assert (await client.get(endpoint)).json()["state"] == "waiting"
            return
        if target_kind == "lost_target":
            async with owner_sessionmaker() as db:
                current = await db.get(Agent, target)
                current.tenant_id = foreign_tenant
                current.creator_id = foreign_user
                current.owner_user_id = foreign_user
                await db.commit()
            assert (await client.post(input_url, json=answer)).status_code in {403, 404}
            return
        resumed = await client.post(input_url, json=answer)
        assert resumed.status_code == 200 and resumed.json()["admission_state"] == "admitted", resumed.text
        repeated = await client.post(input_url, json=answer)
        assert repeated.status_code == 200 and repeated.json()["replayed"] is True
        async with owner_sessionmaker() as db:
            rows = (await db.scalars(select(SessionTurnInput).where(SessionTurnInput.request_item_id == question))).all()
            assert len(rows) == 1
            commands = (await db.scalars(select(SessionCommand).where(SessionCommand.session_id == session))).all()
            assert len(commands) == 1 and commands[0].id == rows[0].command_id
