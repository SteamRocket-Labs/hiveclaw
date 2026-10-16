"""Secure configuration saves and exact original-work continuation share one question."""

import json
import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.models.tool import AgentTool, Tool
from app.models.user import User
from tests.integration.test_m1g4_question_answers_postgres import _ask
from tests.services.test_session_input_control_v2 import _mark_web_terminal_boundary_delivered, _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("target_kind", ["self", "created_employee", "ended_work", "unmanaged", "foreign_target", "wrong_requester", "skip", "connection_tool"])
async def test_configuration_card_saves_without_transcript_secrets_and_resumes_exact_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, target_kind,
):
    from app.api.chat_sessions import router as chat_router
    from app.api.tools import router
    from app.core.security import get_current_user
    from app.services import agent_tools
    from app.services.tool_config_service import MASKED_SECRET_SENTINEL, resolve_tool_config

    tenant, requester, source, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    _, foreign_user, foreign_agent, _, _ = await _seed_session(owner_sessionmaker)
    target = foreign_agent if target_kind == "foreign_target" else source if target_kind in {"self", "skip"} else uuid.uuid4()
    tool_id, name = uuid.uuid4(), f"configuration_{uuid.uuid4().hex[:12]}"
    async with owner_sessionmaker() as db:
        target_creator = requester
        if target_kind == "unmanaged":
            target_creator = uuid.uuid4()
            db.add(User(id=target_creator, tenant_id=tenant, username=f"other-{target_creator.hex}",
                email=f"{target_creator.hex}@config.test", password_hash="synthetic", display_name="Other owner"))
            await db.flush()
        if target not in {source, foreign_agent}:
            db.add(Agent(id=target, tenant_id=tenant, creator_id=target_creator,
                name="Created employee"))
        db.add(Tool(id=tool_id, name=name, display_name="Search account", enabled=True, is_default=True,
            type="mcp" if target_kind == "connection_tool" else "builtin",
            tenant_id=tenant if target_kind == "connection_tool" else None,
            category="general", config={"endpoint": "inherited"},
            config_schema={"fields": [{"key": "api_key", "label": "Key", "type": "password"}]}))
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(db, tenant_id=tenant, agent_id=source, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=requester, arguments={
                "questions": [{"question": "Connect the search account?", "options": []}],
                "configuration": {"target_agent_id": str(target), "tool_name": name}})
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_id == question))
        assert event.metadata_json["v2_payload"]["configuration_request"] == {
            "target_agent_id": str(target), "tool_name": name}
        if target_kind == "ended_work":
            task = await db.get(RuntimeTask, run)
            task.status = "completed"
            await _mark_web_terminal_boundary_delivered(db, task=task, agent_id=source, session_id=session)
        if target_kind == "wrong_requester":
            (await db.get(RuntimeTask, run)).root_user_id = None
        await db.commit()
        user = await db.get(User, requester)
    actor = [user]

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, actor[0].tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = scoped_db
    endpoint = f"/api/tools/agents/{source}/sessions/{session}/questions/{question}/configuration"
    input_id = uuid.uuid4()
    answer = {"kind": "answer_request", "input_id": str(input_id), "idempotency_key": f"config:{input_id}",
        "request_item_id": str(question), "content_parts": [{"type": "text", "text": "Continue after saving configuration."}],
        "terminal_fallback": "queue_next_turn"}
    secret = f"synthetic-config-secret-{uuid.uuid4().hex}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://config-work-test") as client:
        loaded = await client.get(endpoint)
        if target_kind == "connection_tool":
            assert loaded.status_code == 409 and loaded.json()["detail"]["code"] == "configuration_uses_connection_settings"
            denied = await client.put(endpoint, json={"config": {"api_key": secret}})
            assert denied.status_code == 409
            return
        if target_kind in {"unmanaged", "foreign_target", "wrong_requester"}:
            assert loaded.status_code in {403, 404}, loaded.text
            denied = await client.put(endpoint, json={"config": {"api_key": secret}})
            assert denied.status_code in {403, 404}
            async with owner_sessionmaker() as db:
                assert await db.scalar(select(AgentTool).where(AgentTool.agent_id == target, AgentTool.tool_id == tool_id)) is None
            return
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["target_agent_id"] == str(target) and loaded.json()["state"] == "waiting"
        if target_kind == "skip":
            answer["content_parts"][0]["text"] = "Continue without changing configuration."
            skipped = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
            assert skipped.status_code == 200 and skipped.json()["status"] == "queued", skipped.text
            late = await client.put(endpoint, json={"config": {"api_key": secret}})
            assert late.status_code == 409
            async with owner_sessionmaker() as db:
                assert await db.scalar(select(AgentTool).where(AgentTool.agent_id == target, AgentTool.tool_id == tool_id)) is None
            return
        early = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert early.status_code == 409 and early.json()["detail"]["code"] == "configuration_not_saved"
        saved = await client.put(endpoint, json={"config": {"api_key": secret}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["state"] == "saved" and saved.json()["receipt_ref"]
        assert saved.json()["tool"]["config"] == {"endpoint": "inherited", "api_key": MASKED_SECRET_SENTINEL}
        assert secret not in saved.text
        reloaded = await client.get(endpoint)
        assert reloaded.status_code == 200 and reloaded.json()["receipt_ref"] == saved.json()["receipt_ref"]
        replay = await client.put(endpoint, json={"config": {"api_key": "must-not-replace-saved-secret"}})
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        resumed = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == ("rolled_over" if target_kind == "ended_work" else "queued")
        duplicate = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert duplicate.status_code == 200 and duplicate.json()["replayed"] is True
        if target_kind == "self":
            invalid = await client.patch(f"/api/agents/{source}/sessions/{session}/inputs/{input_id}",
                json={"content_parts": [{"type": "text", "text": secret}]}, headers={"If-Match": "1"})
            assert invalid.status_code == 422 and secret not in invalid.text
        async with owner_sessionmaker() as db:
            actor[0] = await db.get(User, foreign_user)
        denied = await client.get(endpoint)
        assert denied.status_code in {403, 404}
        denied = await client.put(endpoint, json={"config": {"api_key": "forbidden"}})
        assert denied.status_code in {403, 404}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        config = await resolve_tool_config(name, tenant, agent_id=target, db=db)
        assert config["api_key"] == secret
        if target != source:
            assert not (await resolve_tool_config(name, tenant, agent_id=source, db=db)).get("api_key")
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == target, AgentTool.tool_id == tool_id))
        raw = await db.scalar(text("select config::text from agent_tools where id=:id"), {"id": assignment.id})
        assert secret not in raw
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session))).all()
        commands = (await db.scalars(select(SessionCommand).where(SessionCommand.session_id == session))).all()
        assert secret not in json.dumps([e.metadata_json for e in events])
        assert secret not in "\n".join(e.content or "" for e in events)
        assert secret not in json.dumps([c.request_json for c in commands])
        row = await db.get(SessionTurnInput, input_id)
        assert row.request_item_id == question
        completed = next(e for e in events if e.item_kind == "user_question" and e.lifecycle == "completed")
        assert completed.scope_json["run_id"] == str(run) and completed.input_id == input_id
        if target_kind == "ended_work":
            successor = await db.get(RuntimeTask, uuid.uuid5(input_id, "session-v2-successor-run"))
            assert successor.root_user_id == requester and successor.parent_agent_id == source
            assert successor.parent_session_id == str(session) and successor.prompt == answer["content_parts"][0]["text"]
        else:
            assert row.target_run_id == run
        assert len((await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == session))).all()) == 1


async def test_real_search_handler_uses_employee_configuration_in_parallel_executions(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.agent_tool_domains import web_mcp
    from app.core.execution_context import get_tool_agent_id, get_tool_tenant_id, tool_configuration_context
    from app.services.tool_config_service import update_tenant_tool_config
    from app.tools.collector import collect_tools
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, user, first, _, _ = await _seed_session(owner_sessionmaker)
    second, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=second, tenant_id=tenant, creator_id=user, name="Second employee"))
        db.add(Tool(id=tool_id, name="exa_search", display_name="Exa", enabled=True, type="builtin",
            config={"auth_mode": "api_key"}, config_schema={"fields": [{"key": "api_key", "type": "password"}]}))
        await db.flush()
        await update_tenant_tool_config(db, tenant, tool_id, config={"api_key": "synthetic-company-key"}, config_schema={})
        db.add_all([AgentTool(tenant_id=tenant, agent_id=employee, tool_id=tool_id,
            config={"api_key": f"synthetic-{employee}"}) for employee in (first, second)])
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    seen = {}

    async def provider(query, api_key, *_args, **_kwargs):
        await asyncio.sleep(0)
        seen[query] = api_key
        return "Synthetic provider response"

    monkeypatch.setattr(web_mcp, "_search_exa", provider)
    registry = collect_tools().exec_registry
    outer_tenant, outer_agent = uuid.uuid4(), uuid.uuid4()
    with tool_configuration_context(tenant_id=outer_tenant, agent_id=outer_agent):
        results = await asyncio.gather(*[registry.try_execute(ToolExecutionRequest(tool_name="exa_search",
            arguments={"query": f"probe {employee}"}, context=ToolExecutionContext(
                tenant_id=str(tenant), agent_id=employee, user_id=user, workspace=tmp_path))) for employee in (first, second)])
        assert get_tool_agent_id() == outer_agent and get_tool_tenant_id() == outer_tenant
    assert all(str(result) == "Synthetic provider response" for result in results)
    assert seen == {f"probe {employee}": f"synthetic-{employee}" for employee in (first, second)}
