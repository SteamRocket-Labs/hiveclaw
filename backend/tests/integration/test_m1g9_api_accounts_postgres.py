"""API credential ownership never expands the approved connector or employee scope."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.custom_api_connectors import router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.tool import AgentTool, Tool
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_api_account_original_work_binding_and_revocation(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.api.tools import router as tools_router
    from app.api.chat_sessions import router as chat_router
    from app.services import agent_tools, custom_api_connectors, secrets_provider
    from app.services.custom_api_connectors import build_custom_api_tool_payload
    from app.tools.runtime import ToolExecutionContext
    from tests.integration.test_m1g4_question_answers_postgres import _ask

    monkeypatch.setattr(secrets_provider, "_provider", secrets_provider.FernetSecretsProvider("synthetic-api-account-fixture-only"))
    tenant, requester, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    payload = build_custom_api_tool_payload(connector_name="Fixture CRM", action_name="Read", description="Read CRM",
        base_url="https://crm.fixture.test", method="GET", path="/contacts/{id}", auth_scheme="bearer")
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        tool = Tool(id=uuid.uuid4(), tenant_id=tenant, name=payload.tool_name, display_name=payload.display_name,
            type="custom_api", config=payload.tool_config, config_schema=payload.config_schema, enabled=True)
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent, tenant_id=tenant, tool_id=tool.id, enabled=True,
            config={"bearer_token": "synthetic-company-api-key"}))
        await db.commit()
        tool_id = tool.id

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    for route in (router, tools_router, chat_router):
        app.include_router(route, prefix="/api")
    actor = [user]
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async def no_wakeup(**kwargs):
        pass
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=requester,
            arguments={"questions": [{"question": "Choose the CRM account for this work.", "options": []}],
                "configuration": {"tool_name": payload.tool_name}})
    context = ToolExecutionContext(agent_id=agent, user_id=requester, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path)
    sent = []
    async def provider(name, request):
        sent.append(request)
        return "Synthetic CRM contact"
    monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", provider)
    async def invoke():
        return await agent_tools._get_tool_runtime_service().execute_with_context(payload.tool_name, {"id": "contact-1"}, context)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://api-accounts.test") as http:
        assert "Synthetic CRM contact" in str(await invoke())
        assert sent.pop().headers["Authorization"] == "Bearer synthetic-company-api-key"
        endpoint = f"/api/agents/{agent}/custom-api-connectors/{tool_id}/accounts"
        data = {"id": str(uuid.uuid4()), "name": "My CRM account"}
        created = await http.post(endpoint, json=data)
        assert created.status_code == 200, created.text
        account = created.json()
        assert (await http.post(endpoint, json=data)).json()["id"] == account["id"]
        path = f"/api/custom-api-accounts/{account['id']}"
        saved = await http.put(path, json={"expected_version": account["version"], "connection_revision": account["connection_revision"], "config": {"bearer_token": "synthetic-private-api-key"}})
        assert saved.status_code == 200 and "synthetic-private-api-key" not in saved.text, saved.text
        assert (await http.put(path, json={"expected_version": account["version"], "connection_revision": account["connection_revision"],
            "config": saved.json()["config"]})).json()["version"] == saved.json()["version"]
        assert (await http.put(path, json={"expected_version": saved.json()["version"], "connection_revision": account["connection_revision"], "config": {
            "bearer_token": "synthetic-private-api-key", "base_url": "https://attacker.fixture.test"}})).status_code == 422
        async with owner_sessionmaker() as db:
            assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))
            assignment.enabled = False
            await db.commit()
        grant_path = f"{path}/grants/{agent}/{requester}"
        granted = await http.put(grant_path, json={"expected_version": 0, "enabled": True})
        assert granted.status_code == 200, granted.text
        assert "denied" in str(await invoke()).lower() and sent == []
        async with owner_sessionmaker() as db:
            assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))
            assert assignment.enabled is False
            assignment.enabled = True
            await db.commit()
        blocked = await invoke()
        assert "selection" in str(blocked).lower() and sent == []
        source = f"/api/tools/agents/{agent}/sessions/{session}/questions/{question}/configuration"
        selection = {"account_id": account["id"], "account_version": saved.json()["version"], "grant_version": granted.json()["version"]}
        confirmed = await http.post(f"{source}/api-account", json=selection)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "saved", confirmed.text
        assert (await http.get(source)).json()["api_account_selection"]["account_id"] == account["id"]
        result = await invoke()
        assert "Synthetic CRM contact" in str(result), result
        assert len(sent) == 1 and sent[0].headers["Authorization"] == "Bearer synthetic-private-api-key"
        assert sent[0].url == "https://crm.fixture.test/contacts/contact-1"
        async with owner_sessionmaker() as db:
            definition = await db.get(Tool, tool_id)
            original = dict(definition.config)
            definition.config = {**original, "base_url": "https://changed.fixture.test"}
            manager = User(id=uuid.uuid4(), tenant_id=tenant, username=f"manager-{uuid.uuid4().hex}",
                email=f"{uuid.uuid4().hex}@fixture.test", display_name="Fixture manager", password_hash="fixture", role="org_admin")
            db.add(manager)
            await db.commit()
        assert "authorization changed" in str(await invoke()).lower() and len(sent) == 1
        assert (await http.put(path, json={"expected_version": saved.json()["version"], "connection_revision": account["connection_revision"],
            "config": saved.json()["config"]})).status_code == 409
        actor[0] = manager
        assert (await http.get(path)).status_code == 403
        assert (await http.put(path, json={"expected_version": saved.json()["version"], "connection_revision": account["connection_revision"],
            "config": {"bearer_token": "synthetic-manager-key"}})).status_code == 403
        assert (await http.post(f"{path}/revoke", json={"expected_version": saved.json()["version"]})).status_code == 403
        assert (await http.get(source)).status_code == 403
        actor[0] = user
        async with owner_sessionmaker() as db:
            definition = await db.get(Tool, tool_id)
            definition.config = original
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            assert (await db.scalar(select(AgentTool).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))).config == {"bearer_token": "synthetic-company-api-key"}
            assert "synthetic-private-api-key" not in await db.scalar(text("SELECT config::text FROM custom_api_accounts WHERE id=:id"), {"id": uuid.UUID(account["id"])})
            await pin_rls_tenant_context(db, uuid.uuid4())
            assert await db.scalar(text("SELECT count(*) FROM custom_api_accounts")) == 0
            assert await db.scalar(text("SELECT count(*) FROM custom_api_account_grants")) == 0
            assert await db.scalar(text("SELECT count(*) FROM custom_api_work_account_bindings")) == 0
        revoked = await http.post(f"{path}/revoke", json={"expected_version": saved.json()["version"]})
        assert revoked.status_code == 200, revoked.text
        again = await invoke()
        assert "authorization changed" in str(again).lower() and len(sent) == 1
        response = await http.post(f"/api/agents/{agent}/sessions/{session}/inputs", json={"kind": "answer_request", "input_id": str(uuid.uuid4()), "idempotency_key": f"api-account:{uuid.uuid4()}",
            "request_item_id": str(question), "content_parts": [{"type": "text", "text": "Continue after saving configuration."}], "id": str(uuid.uuid4())})
        assert response.status_code == 409, response.text
        reconnected = await http.put(path, json={"expected_version": revoked.json()["version"], "connection_revision": account["connection_revision"],
            "config": {"bearer_token": "synthetic-reconnected-api-key"}})
        assert reconnected.status_code == 200, reconnected.text
        stale = await http.post(f"{path}/revoke", json={"expected_version": saved.json()["version"]})
        assert stale.status_code == 409, stale.text
        assert (await http.get(path)).json()["is_configured"] is True
        assert "authorization changed" in str(await invoke()).lower() and len(sent) == 1
        grant = await http.put(grant_path, json={"expected_version": granted.json()["version"] + 1, "enabled": True})
        assert grant.status_code == 200, grant.text
        assert (await http.post(f"{source}/api-account", json={"account_id": account["id"], "account_version": reconnected.json()["version"],
            "grant_version": grant.json()["version"]})).status_code == 200
        assert "Synthetic CRM contact" in str(await invoke()) and len(sent) == 2
        assert sent[-1].headers["Authorization"] == "Bearer synthetic-reconnected-api-key"
        async def echo_provider(name, request):
            return f"{await provider(name, request)}: {request.headers['Authorization']}"
        monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", echo_provider)
        echoed = str(await invoke())
        assert "Synthetic CRM contact" in echoed and "synthetic-reconnected-api-key" not in echoed
