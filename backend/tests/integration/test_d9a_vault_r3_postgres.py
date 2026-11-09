"""Final-round regressions for credential storage and vault actions."""
import copy
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.custom_api_account import CustomApiAccount
from app.models.email_account import EmailAccount
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from tests.integration.test_d9a_vault_postgres import client_for, seed
from tests.integration.test_d9a_vault_r2_postgres import exa_fixture
from tests.integration.test_g2bf_tool_config_boundaries import builtin

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("company_secret", [None, "company-original"])
async def test_company_replace_keeps_connector_revision_and_personal_accounts_configured(owner_sessionmaker, app_user_sessionmaker, company_secret):
    data = await seed(owner_sessionmaker)
    from app.api.custom_api_connectors import CustomApiConnectorIn, create_custom_api_connector
    from app.services.custom_api_accounts import configured, connector_revision
    async with owner_sessionmaker() as db:
        created = await create_custom_api_connector(CustomApiConnectorIn(connector_name=f"Billing {uuid.uuid4().hex}",
            action_name="List", base_url="https://billing.fixture.test", auth_scheme="bearer",
            secret_value=company_secret, is_default=True), current_user=await db.get(User, data["admin"]), db=db)
        tool = await db.get(Tool, uuid.UUID(created["id"]))
        original = copy.deepcopy(tool.config)
        revision = connector_revision(tool)
        assert "bearer_token" not in original
        account = CustomApiAccount(tenant_id=data["tenant"], owner_user_id=data["member"], tool_id=tool.id,
            name="Personal billing", config={"bearer_token": "personal-original"}, connector_revision=revision)
        db.add(account)
        await db.commit()
        assert configured(account, tool)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        response = await http.put(f"/api/vault/company/{tool.id}?field=bearer_token", json={"values": {"bearer_token": "company-new"}})
        assert response.status_code == 200 and "company-new" not in response.text
    async with owner_sessionmaker() as db:
        current = await db.get(Tool, tool.id)
        personal = await db.get(CustomApiAccount, account.id)
        print(f"personal configured before=True after={configured(personal, current)}; connector revision unchanged={connector_revision(current) == revision}; tool config unchanged={current.config == original}")
        assert configured(personal, current)
        assert connector_revision(current) == revision
        assert current.config == original
        override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tool_id == tool.id))
        from app.services.tool_config_service import decrypt_tool_config_secrets
        assert decrypt_tool_config_secrets(override.config, tool.config_schema)["bearer_token"] == "company-new"
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        assert (await http.delete(f"/api/vault/company/{tool.id}?field=bearer_token")).status_code == 200
    async with owner_sessionmaker() as db:
        override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tool_id == tool.id))
        assert "bearer_token" not in override.config
        assert (await db.get(Tool, tool.id)).config == original


@pytest.mark.parametrize("source", ["company", "env", "empty_company"])
async def test_empty_bot_configs_do_not_list_slots_but_historical_empty_keys_remain(owner_sessionmaker, app_user_sessionmaker, monkeypatch, source):
    data = await seed(owner_sessionmaker)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EXA_API_KEY", "server-exa" if source == "env" else "")
    monkeypatch.setattr(get_settings(), "TAVILY_API_KEY", "server-tavily" if source == "env" else "")
    async with owner_sessionmaker() as db:
        empty = []
        for name in ("exa_search", "tavily_search"):
            tool = await builtin(db, name)
            tool.config = {"api_key": ""} if source == "empty_company" else {}
            assignment = AgentTool(tenant_id=data["tenant"], agent_id=data["bot"], tool_id=tool.id, config={})
            db.add(assignment)
            if source == "company":
                db.add(TenantToolConfig(tenant_id=data["tenant"], tool_id=tool.id, config={"api_key": "company-key"}))
            await db.flush()
            empty.append(str(assignment.id))
        historical_agent = Agent(tenant_id=data["tenant"], creator_id=data["member"], name="Legacy assistant")
        db.add(historical_agent)
        await db.flush()
        historical = AgentTool(tenant_id=data["tenant"], agent_id=historical_agent.id, tool_id=tool.id, config={"api_key": ""})
        db.add(historical)
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        rows = (await http.get("/api/vault")).json()
        assert not any(r["kind"] == "bot" and r["id"] in empty for r in rows)
        legacy = [r for r in rows if r["kind"] == "bot" and r["id"] == str(historical.id)]
        assert len(legacy) == 1 and legacy[0]["field"] == "api_key" and not legacy[0]["configured"]


async def test_member_can_revoke_own_account_for_disabled_connector(owner_sessionmaker, app_user_sessionmaker):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        (await db.get(Tool, data["tool"])).enabled = False
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.delete(f"/api/vault/api/{data['api']}?version=1")
        assert response.status_code == 200, response.text
    async with owner_sessionmaker() as db:
        account = await db.get(CustomApiAccount, data["api"])
        assert not account.config and account.version == 2


async def test_bot_key_equal_to_environment_key_is_still_an_own_slot(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    data = await seed(owner_sessionmaker)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EXA_API_KEY", "same-key")
    async with owner_sessionmaker() as db:
        tool, assignment = await exa_fixture(db, data, own_key="same-key")
        tool.config = {}
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        rows = (await http.get("/api/vault")).json()
        assert any(r["kind"] == "bot" and r["id"] == str(assignment.id) and r["field"] == "api_key" for r in rows)


@pytest.mark.parametrize("kind", ["api", "email"])
async def test_saving_same_personal_secret_again_does_not_require_review(owner_sessionmaker, app_user_sessionmaker, kind):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        from app.services import custom_api_accounts, email_accounts
        service = custom_api_accounts if kind == "api" else email_accounts
        account = await db.get(CustomApiAccount if kind == "api" else EmailAccount, data[kind])
        agent, user = await db.get(Agent, data["bot"]), await db.get(User, data["member"])
        grant = await service.set_grant(db, account=account, agent_id=agent.id, requester_id=user.id, enabled=True, expected_version=0)
        extra = {"tool": await db.get(Tool, data["tool"])} if kind == "api" else {}
        await service.bind_account(db, agent=agent, session_id=data["session"], user=user, account_id=account.id,
            account_version=account.version, grant_version=grant["version"], **extra)
        revision = custom_api_accounts.connector_revision(extra["tool"]) if extra else None
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        payload = {"values": {"bearer_token" if kind == "api" else "auth_code": "personal-new"}, "version": 1}
        if revision:
            payload["connection_revision"] = revision
        first = await http.put(f"/api/vault/{kind}/{data[kind]}", json=payload)
        assert first.status_code == 200 and first.json()["removal"]["review_required"]
        payload["version"] = 2
        second = await http.put(f"/api/vault/{kind}/{data[kind]}", json=payload)
        assert second.status_code == 200 and second.json()["removal"]["review_required"] is False
    async with owner_sessionmaker() as db:
        assert (await db.get(CustomApiAccount if kind == "api" else EmailAccount, data[kind])).version == 2


async def test_bot_replace_reads_other_settings_after_lock(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    data = await seed(owner_sessionmaker)
    from app.services import agent_tool_assignment_service as assignments
    async with owner_sessionmaker() as db:
        tool, assignment = await exa_fixture(db, data)
        await db.commit()
    original = assignments.lock_agent_tool_config_write
    changed = False
    async def lock(db, agent_id, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            async with owner_sessionmaker() as other:
                row = await other.get(AgentTool, assignment.id)
                row.config = {**row.config, "max_results": 7}
                await other.commit()
        return await original(db, agent_id, **kwargs)
    monkeypatch.setattr(assignments, "lock_agent_tool_config_write", lock)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.put(f"/api/vault/bot/{assignment.id}?field=api_key", json={"values": {"api_key": "employee-new"}})
        assert response.status_code == 200
    async with owner_sessionmaker() as db:
        assert (await db.get(AgentTool, assignment.id)).config["max_results"] == 7
