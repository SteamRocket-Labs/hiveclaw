"""Round-two regressions for the existing credential settings projection."""
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.custom_api_account import CustomApiAccount
from app.models.email_account import EmailAccount
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config_service import resolve_tool_config
from tests.integration.test_d9a_vault_postgres import client_for, seed
from tests.integration.test_g2bf_tool_config_boundaries import builtin

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def exa_fixture(db, data, own_key="employee-old"):
    tool = await builtin(db, "exa_search")
    tool.config = {"api_key": "", "mcp_url": "https://mcp.exa.ai/mcp"}
    assignment = AgentTool(tenant_id=data["tenant"], agent_id=data["bot"], tool_id=tool.id,
        config={"api_key": own_key, "auth_mode": "api_key", "max_results": 10,
            "mcp_url": "https://mcp.exa.ai/mcp"})
    db.add(assignment)
    await db.flush()
    return tool, assignment


@pytest.mark.parametrize("source", ["builtin", "admin_created", "admin_custom_api"])
async def test_copied_company_slots_are_read_only_and_copies_are_not_personal_slots(owner_sessionmaker, app_user_sessionmaker, source):
    data = await seed(owner_sessionmaker)
    from app.api.tools import ToolCreateIn, ToolUpdateIn, create_tool, update_global_tool
    async with owner_sessionmaker() as db:
        admin = await db.get(User, data["admin"])
        if source == "builtin":
            tool = await builtin(db, "exa_search")
            await update_global_tool(tool.id, ToolUpdateIn(config={"api_key": "company-copy", "auth_mode": "api_key"}), current_user=admin, db=db)
        else:
            created = await create_tool(ToolCreateIn(name=f"company_{uuid.uuid4().hex}", display_name="Company integration",
                type="custom_api" if source == "admin_custom_api" else "builtin",
                config={"api_key": "company-copy"}, config_schema={"fields": [{"key": "api_key", "type": "password"}]}), current_user=admin, db=db)
            tool = await db.get(Tool, uuid.UUID(created["id"]))
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == data["bot"], AgentTool.tool_id == tool.id))
        assert (await resolve_tool_config(tool.name, data["tenant"], agent_id=data["bot"], db=db))["api_key"] == "company-copy"
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        rows = (await http.get("/api/vault")).json()
        assert not any(r["kind"] == "bot" and r["id"] == str(assignment.id) and r["field"] == "api_key" for r in rows)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        rows = (await http.get("/api/vault")).json()
        row = next(r for r in rows if r["kind"] == "company" and r["id"] == str(tool.id) and r["field"] == "api_key")
        assert row["read_only"] and row["scope"] == "company"
        url = f"/api/vault/company/{tool.id}?field=api_key"
        assert (await http.put(url, json={"values": {"api_key": "must-not-replace"}})).status_code in {403, 404}
        assert (await http.delete(url)).status_code in {403, 404}
    async with owner_sessionmaker() as db:
        assert (await resolve_tool_config(tool.name, data["tenant"], agent_id=data["bot"], db=db))["api_key"] == "company-copy"


async def test_company_oauth_is_read_only_and_legacy_mcp_tool_slots_are_excluded(owner_sessionmaker, app_user_sessionmaker):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        legacy = Tool(tenant_id=data["tenant"], name=f"legacy_{uuid.uuid4().hex}", display_name="Legacy MCP", type="mcp",
            config={"api_key": "legacy-company-secret"}, config_schema={"fields": [{"key": "api_key", "type": "password"}]})
        db.add(legacy)
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        response = await http.get("/api/vault")
        assert "legacy-company-secret" not in response.text
        rows = response.json()
        assert all(r["read_only"] for r in rows if r["kind"] == "company_mcp")
        assert not any(r["id"] == str(legacy.id) for r in rows)
        for server in (data["parent"], uuid.uuid4()):
            url = f"/api/vault/company_mcp/{server}?field=access_token"
            assert (await http.put(url, json={"values": {"access_token": "must-not-save"}})).status_code in {403, 404}
            assert (await http.delete(url)).status_code in {403, 404}


async def invoke_exa(data, monkeypatch, app_user_sessionmaker, sent):
    from app.core.execution_context import tool_configuration_context
    from app.services.agent_tool_domains import web_mcp
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    async def provider(query, api_key, max_results, **kwargs):
        sent.append((api_key, max_results))
        return "Local search complete"
    monkeypatch.setattr(web_mcp, "_search_exa", provider)
    with tool_configuration_context(tenant_id=data["tenant"], agent_id=data["bot"]):
        return await web_mcp._exa_search({"query": "synthetic quarterly report"})


async def test_builtin_replace_preserves_real_exa_settings_and_provider_receives_new_key(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        tool, assignment = await exa_fixture(db, data)
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.put(f"/api/vault/bot/{assignment.id}?field=api_key", json={"values": {"api_key": "employee-new"}})
        assert response.status_code == 200 and "employee-new" not in response.text
    sent = []
    assert "Local search complete" in await invoke_exa(data, monkeypatch, app_user_sessionmaker, sent)
    assert sent == [("employee-new", 10)]
    async with owner_sessionmaker() as db:
        stored = (await db.get(AgentTool, assignment.id)).config
        assert stored["auth_mode"] == "api_key" and stored["max_results"] == 10
        assert stored["mcp_url"] == "https://mcp.exa.ai/mcp"


@pytest.mark.parametrize("fallback", ["company", "env", "none"])
async def test_builtin_remove_drops_only_own_key_and_resolves_lower_layer(owner_sessionmaker, app_user_sessionmaker, monkeypatch, fallback):
    data = await seed(owner_sessionmaker)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EXA_API_KEY", "environment-key" if fallback == "env" else "")
    async with owner_sessionmaker() as db:
        tool, assignment = await exa_fixture(db, data)
        if fallback == "company":
            db.add(TenantToolConfig(tenant_id=data["tenant"], tool_id=tool.id, config={"api_key": "company-key"}))
        other = await db.scalar(select(Agent).where(Agent.creator_id == data["other"]))
        historical = AgentTool(tenant_id=data["tenant"], agent_id=other.id, tool_id=tool.id, config={"api_key": ""})
        db.add(historical)
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.delete(f"/api/vault/bot/{assignment.id}?field=api_key")
        assert response.status_code == 200, response.text
        assert response.json()["removal"]["fallback"] == ("configuration" if fallback == "none" else "company")
    async with owner_sessionmaker() as db:
        stored = (await db.get(AgentTool, assignment.id)).config
        assert "api_key" not in stored
        assert stored["auth_mode"] == "api_key" and stored["max_results"] == 10
        assert (await db.get(AgentTool, historical.id)).config == {"api_key": ""}
        assert (await resolve_tool_config(tool.name, data["tenant"], agent_id=other.id, db=db))["api_key"] == ""
    sent = []
    result = await invoke_exa(data, monkeypatch, app_user_sessionmaker, sent)
    assert sent == ([] if fallback == "none" else [("company-key" if fallback == "company" else "environment-key", 10)])
    if fallback == "none":
        assert "not configured" in result


@pytest.mark.parametrize("kind", ["api", "email"])
@pytest.mark.parametrize("operation", ["replace", "remove"])
@pytest.mark.parametrize("client_version", [None, 0, 1])
async def test_account_versions_are_required_and_never_replaced_by_current_version(owner_sessionmaker, app_user_sessionmaker, kind, operation, client_version):
    data = await seed(owner_sessionmaker)
    from app.services.custom_api_accounts import connector_revision
    async with owner_sessionmaker() as db:
        account = await db.get(CustomApiAccount if kind == "api" else EmailAccount, data[kind])
        account.version = 3
        revision = connector_revision(await db.get(Tool, data["tool"]))
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        path = f"/api/vault/{kind}/{data[kind]}"
        if operation == "remove":
            response = await http.delete(path + (f"?version={client_version}" if client_version is not None else ""))
        else:
            response = await http.put(path, json={"values": {"bearer_token" if kind == "api" else "auth_code": "must-not-save"},
                "version": client_version, "connection_revision": revision})
        assert response.status_code == 409, response.text
        assert "must-not-save" not in response.text
    async with owner_sessionmaker() as db:
        account = await db.get(CustomApiAccount if kind == "api" else EmailAccount, data[kind])
        assert account.version == 3


async def test_malformed_revision_is_fixed_422_without_secret_echo(owner_sessionmaker, app_user_sessionmaker):
    data = await seed(owner_sessionmaker)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.put(f"/api/vault/api/{data['api']}", json={"values": {"bearer_token": "DO_NOT_ECHO"}, "version": 1, "connection_revision": "malformed"})
        assert response.status_code == 422 and response.json() == {"detail": "Enter the credential"}


@pytest.mark.parametrize("kind", ["api", "email"])
@pytest.mark.parametrize("configured", [True, False])
async def test_unused_account_removal_uses_company_fallback_without_claiming_work_needs_review(owner_sessionmaker, app_user_sessionmaker, kind, configured):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        if kind == "email":
            from app.services.email_accounts import EMAIL_SCHEMA
            mail = Tool(tenant_id=data["tenant"], name="send_email", display_name="Company mail", type="builtin", config_schema=EMAIL_SCHEMA)
            db.add(mail)
            await db.flush()
            db.add(TenantToolConfig(tenant_id=data["tenant"], tool_id=mail.id,
                config={"auth_code": "company-mail" if configured else "", "email_address": "company@fixture.test"}))
        elif not configured:
            override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tool_id == data["tool"]))
            override.config = {"bearer_token": ""}
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        before = next(r for r in (await http.get("/api/vault")).json() if r["kind"] == kind)
        assert before["removal"].get("review_required", False) is False
        response = await http.delete(f"/api/vault/{kind}/{data[kind]}?version=1")
        assert response.status_code == 200
        assert response.json()["removal"]["fallback"] == ("company" if configured else "configuration")
        assert not response.json()["removal"].get("review_required", False)


@pytest.mark.parametrize("scope", ["bot", "company"])
async def test_custom_api_company_choice_is_marked_for_review_after_secret_change(owner_sessionmaker, app_user_sessionmaker, scope):
    data = await seed(owner_sessionmaker)
    from app.services.custom_api_accounts import bind_account, company_config
    async with owner_sessionmaker() as db:
        tool, agent, user = await db.get(Tool, data["tool"]), await db.get(Agent, data["bot"]), await db.get(User, data["member"])
        if scope == "company":
            (await db.get(AgentTool, data["assignment"])).config = {}
        _, revision = await company_config(db, tool=tool, agent_id=agent.id)
        await bind_account(db, agent=agent, tool=tool, session_id=data["session"], user=user, account_id=None, company_revision=revision)
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin" if scope == "company" else "member") as http:
        record_id = data["tool"] if scope == "company" else data["assignment"]
        response = await http.put(f"/api/vault/{scope}/{record_id}?field=bearer_token", json={"values": {"bearer_token": "company-choice-new"}})
        assert response.status_code == 200 and response.json()["removal"].get("review_required") is True


async def test_missing_api_connector_never_becomes_an_email_credential(owner_sessionmaker):
    data = await seed(owner_sessionmaker)
    from app.api.vault import _personal_rows
    async with owner_sessionmaker() as db:
        rows = await _personal_rows(db, await db.get(User, data["member"]), data["tenant"], {}, {}, {})
        account = next(r for r in rows if r["kind"] == "api")
        assert account["fields"] == [] and not account["configured"]
        assert "auth_code" not in json.dumps(account)
