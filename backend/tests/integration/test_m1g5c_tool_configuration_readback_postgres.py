"""Employee settings read and save the same effective configuration as the runtime."""

import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config_service import MASKED_SECRET_SENTINEL, resolve_tool_config, update_tenant_tool_config
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("employee_override", [False, True])
async def test_profile_configuration_inherits_company_defaults_and_reads_back_a_masked_save(
    owner_sessionmaker, app_user_sessionmaker, employee_override,
):
    from app.api.tools import router
    from app.core.security import get_current_user

    tenant_id, user_id, agent_id, _session_id, _run_id = await _seed_session(owner_sessionmaker)
    other_tenant, other_user_id, other_agent, _, _ = await _seed_session(owner_sessionmaker)
    tool_id, name = uuid.uuid4(), f"config_readback_{uuid.uuid4().hex[:12]}"
    async with owner_sessionmaker() as db:
        db.add(Tool(id=tool_id, name=name, display_name="Readback probe", type="builtin", category="general",
            is_default=True, enabled=True, config={"endpoint": "platform-value", "api_key": "synthetic-platform-key"},
            config_schema={"fields": [{"key": "endpoint", "type": "text"}, {"key": "api_key", "type": "password"}]}))
        await db.flush()
        await update_tenant_tool_config(db, tenant_id, tool_id,
            config={"endpoint": "company-value", "api_key": "synthetic-company-key"},
            config_schema={"fields": [{"key": "api_key", "type": "password"}]})
        await update_tenant_tool_config(db, other_tenant, tool_id,
            config={"endpoint": "other-company-value", "api_key": "synthetic-other-company-key"},
            config_schema={"fields": [{"key": "api_key", "type": "password"}]})
        if employee_override:
            db.add(AgentTool(tenant_id=tenant_id, agent_id=agent_id, tool_id=tool_id, config={"endpoint": "employee-value"}))
        await db.commit()
        user = await db.get(User, user_id)
        other_user = await db.get(User, other_user_id)
    actor = [user]

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, actor[0].tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://config-test") as client:
        response = await client.get(f"/api/tools/agents/{agent_id}/with-config")
        assert response.status_code == 200, response.text
        row = next(t for t in response.json() if t["id"] == str(tool_id))
        assert row["global_config"]["endpoint"] == "company-value"
        assert row["config"]["endpoint"] == ("employee-value" if employee_override else "company-value")
        assert row["config"]["api_key"] == MASKED_SECRET_SENTINEL
        assert "synthetic-" not in response.text and "enc:v1:" not in response.text
        save = await client.put(f"/api/tools/agents/{agent_id}/tool-config/{tool_id}", json={"config": {
            "api_key": "synthetic-employee-key", "timeout": 9,
        }})
        assert save.status_code == 200, save.text
        assert save.json()["ok"] is True
        saved = save.json()["tool"]
        assert saved["config"]["endpoint"] == "company-value" and saved["config"]["timeout"] == 9
        assert saved["agent_config"] == {"api_key": MASKED_SECRET_SENTINEL, "timeout": 9}
        assert "synthetic-" not in save.text and "enc:v1:" not in save.text
        replay = await client.put(f"/api/tools/agents/{agent_id}/tool-config/{tool_id}",
            json={"config": saved["agent_config"]})
        assert replay.status_code == 200 and replay.json()["tool"] == saved
        reread = await client.get(f"/api/tools/agents/{agent_id}/with-config")
        assert next(t for t in reread.json() if t["id"] == str(tool_id)) == saved
        actor[0] = other_user
        foreign = await client.get(f"/api/tools/agents/{agent_id}/with-config")
        assert foreign.status_code in {403, 404}
        denied_write = await client.put(f"/api/tools/agents/{agent_id}/tool-config/{tool_id}", json={"config": {"timeout": 100}})
        assert denied_write.status_code in {403, 404}
        own = await client.get(f"/api/tools/agents/{other_agent}/with-config")
        assert own.status_code == 200, own.text
        own_row = next(t for t in own.json() if t["id"] == str(tool_id))
        assert own_row["config"]["endpoint"] == "other-company-value"
        assert own_row["config"]["api_key"] == MASKED_SECRET_SENTINEL and "synthetic-" not in own.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        effective = await resolve_tool_config(name, tenant_id, agent_id=agent_id, db=db)
        assert effective == {"endpoint": "company-value", "api_key": "synthetic-employee-key", "timeout": 9}
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id))
        raw = await db.scalar(text("select config::text from agent_tools where id=:id"), {"id": assignment.id})
        assert "synthetic-employee-key" not in raw
        assert "synthetic-platform-key" not in json.dumps(assignment.config)
