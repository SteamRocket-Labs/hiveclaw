"""The settings vault projects existing credentials without acquiring authority."""
import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.custom_api_account import CustomApiAccount
from app.models.email_account import EmailAccount
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.mcp_oauth import OAuthTokenSet, encrypt_token_set
from app.services.tool_config_service import encrypt_tool_config_secrets
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def seed(owner_sessionmaker):
    tenant, member, bot, session, _ = await _seed_session(owner_sessionmaker)
    foreign, foreign_user, foreign_bot, _, _ = await _seed_session(owner_sessionmaker)
    other, admin = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, member)
        user.role, user.display_name = "member", "Mina"
        agent = await db.get(Agent, bot)
        agent.name = "Finance assistant"
        db.add_all([User(id=other, tenant_id=tenant, username=f"other-{other}", email=f"{other}@vault.test",
            display_name="Other member", password_hash="x", role="member"), User(id=admin, tenant_id=tenant, username=f"admin-{admin}",
            email=f"{admin}@vault.test", display_name="Company admin", password_hash="x", role="org_admin")])
        await db.flush()
        other_bot = Agent(tenant_id=tenant, creator_id=other, name="Other member bot")
        tool = Tool(tenant_id=tenant, name=f"custom_api__vault_{bot.hex}", display_name="Billing",
            type="custom_api", enabled=True, config={"base_url": "https://billing.fixture.test",
                "auth": {"scheme": "bearer"}, "action": {"method": "GET", "path": "/bills"}},
            config_schema={"fields": [{"key": "bearer_token", "type": "password", "label": "Access token"}]})
        db.add_all([other_bot, tool])
        await db.flush()
        assignment = AgentTool(tenant_id=tenant, agent_id=bot, tool_id=tool.id,
            config={"bearer_token": "vault-bot-old", "ordinary_setting": "keep-me"})
        db.add_all([assignment, AgentTool(tenant_id=tenant, agent_id=other_bot.id, tool_id=tool.id,
            config={"bearer_token": "vault-other-bot-secret"}), TenantToolConfig(tenant_id=tenant, tool_id=tool.id,
                config=encrypt_tool_config_secrets({"bearer_token": "vault-company-old"}, tool.config_schema))])
        parent = MCPServer(tenant_id=tenant, name="Documents", server_key=f"vault-{bot}",
            server_url="https://mcp.fixture.test/mcp", status="connected", auth_status="configured",
            config_json={"oauth": {"token": encrypt_token_set(OAuthTokenSet("vault-company-mcp"))}})
        db.add(parent)
        await db.flush()
        personal = MCPServer(tenant_id=tenant, owner_user_id=member, parent_server_id=parent.id,
            name="My documents", server_key=f"vault-personal-{bot}", server_url=parent.server_url,
            auth_status="configured", config_json={"oauth": {"token": encrypt_token_set(OAuthTokenSet("vault-mcp-old"))}})
        api = CustomApiAccount(tenant_id=tenant, owner_user_id=member, tool_id=tool.id, name="My billing",
            config={"bearer_token": "vault-api-old"})
        mail = EmailAccount(tenant_id=tenant, owner_user_id=member, name="My mailbox",
            config={"auth_code": "vault-mail-old", "email_provider": "gmail", "email_address": "mina@fixture.test"})
        db.add_all([personal, api, mail,
            EmailAccount(tenant_id=tenant, owner_user_id=other, name="Other private mailbox", config={"auth_code": "other-secret"}),
            EmailAccount(tenant_id=foreign, owner_user_id=foreign_user, name="Foreign mailbox", config={"auth_code": "foreign-secret"})])
        from app.services.custom_api_accounts import connector_revision
        api.connector_revision = connector_revision(tool)
        await db.commit()
        return dict(tenant=tenant, member=member, bot=bot, session=session, other=other, admin=admin,
            tool=tool.id, assignment=assignment.id, mcp=personal.id, parent=parent.id, api=api.id, email=mail.id)


async def client_for(seed, owner_sessionmaker, app_user_sessionmaker, actor="member"):
    from app.api.vault import router
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed[actor])
    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant"])
            yield db
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://vault.fixture.test")


async def test_list_is_an_authorized_projection_and_never_returns_secret_json(owner_sessionmaker, app_user_sessionmaker):
    data = await seed(owner_sessionmaker)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        response = await http.get("/api/vault")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {(r["kind"], r["id"]) for r in rows} == {
        (kind, str(data[key])) for kind, key in (("mcp", "mcp"), ("api", "api"), ("email", "email"), ("bot", "assignment"))}
    assert all(row["scope"] != "company" for row in rows)
    assert next(row for row in rows if row["kind"] == "bot")["bots"] == [{"id": str(data["bot"]), "name": "Finance assistant"}]
    assert not any(key in row for row in rows for key in ("last_used", "stored_by", "config", "value"))
    for value in ("vault-bot-old", "vault-company-old", "vault-mcp-old", "vault-api-old", "vault-mail-old", "other-secret", "foreign-secret"):
        assert value not in json.dumps(rows)
    async with owner_sessionmaker() as db:
        db.add(AgentMCPServerAssignment(tenant_id=data["tenant"], agent_id=data["bot"], mcp_server_id=data["parent"], enabled=False))
        await db.commit()
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        response = await http.get("/api/vault")
        assert response.status_code == 200, response.text
        assert any(r["scope"] == "company" and r["id"] == str(data["tool"]) for r in response.json())
        assert not any(r["scope"] == "personal" for r in response.json())
        company_mcp = next(r for r in response.json() if r["kind"] == "company_mcp")
        assert str(data["bot"]) not in {b["id"] for b in company_mcp["bots"]}
        assert "vault-" not in response.text


async def test_replace_and_remove_change_real_tool_requests_without_clearing_other_settings(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    data = await seed(owner_sessionmaker)
    from app.services import agent_tools, custom_api_connectors
    from app.tools.runtime import ToolExecutionContext
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    sent = []
    async def provider(name, request):
        sent.append(request.headers.get("Authorization"))
        return "Bills ready"
    monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", provider)
    context = ToolExecutionContext(agent_id=data["bot"], user_id=data["member"], tenant_id=str(data["tenant"]),
        session_id=str(data["session"]), workspace=tmp_path)
    async with owner_sessionmaker() as db:
        tool = await db.get(Tool, data["tool"])
    async def invoke():
        return await agent_tools._get_tool_runtime_service().execute_with_context(tool.name, {}, context)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        endpoint = f'/api/vault/bot/{data["assignment"]}?field=bearer_token'
        saved = await http.put(endpoint, json={"values": {"bearer_token": "vault-bot-new"}})
        assert saved.status_code == 200, saved.text
        assert "vault-bot-new" not in saved.text
        assert "Bills ready" in str(await invoke())
        assert sent == ["Bearer vault-bot-new"]
        removed = await http.delete(endpoint)
        assert removed.status_code == 200, removed.text
        assert removed.json()["removal"]["fallback"] == "company"
        result = str(await invoke())
        assert "Bills ready" in result
        assert sent == ["Bearer vault-bot-new", "Bearer vault-company-old"]
    async with owner_sessionmaker() as db:
        row = await db.get(AgentTool, data["assignment"])
        assert row.config == {"ordinary_setting": "keep-me"}


@pytest.mark.parametrize("kind,key", [("mcp", "mcp"), ("api", "api"), ("email", "email"), ("bot", "assignment"), ("company", "tool")])
async def test_existing_authority_rejects_other_member_and_company_writes(owner_sessionmaker, app_user_sessionmaker, kind, key):
    data = await seed(owner_sessionmaker)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "other") as http:
        url = f'/api/vault/{kind}/{data[key]}' + ('?field=bearer_token' if kind in {"bot", "company"} else '')
        saved = await http.put(url, json={"values": {"bearer_token": "unauthorized-value"}})
        removed = await http.delete(url)
        assert saved.status_code in {403, 404}, saved.text
        assert removed.status_code in {403, 404}, removed.text


async def test_personal_mcp_removal_reports_company_fallback_and_never_reuses_old_token(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    data = await seed(owner_sessionmaker)
    from app.models.mcp_server import MCPServerTool
    from app.database import tenant_scoped_session
    from app.services.mcp_metadata_trust import current_mcp_fingerprint, review_mcp_metadata
    from app.services.mcp_client import MCPClient
    from app.tools.handlers.mcp import call_mcp_tool
    from app.tools.runtime import ToolExecutionContext
    async with owner_sessionmaker() as db:
        parent = await db.get(MCPServer, data["parent"])
        tool = Tool(tenant_id=data["tenant"], name=f'mcp_vault_{data["bot"].hex}', display_name="Find documents",
            description="Find documents.", type="mcp", mcp_server_url=parent.server_url, mcp_tool_name="find", enabled=True)
        db.add(tool)
        await db.flush()
        review_mcp_metadata(tool, decision="approve", reviewer_id=data["member"], expected_fingerprint=current_mcp_fingerprint(tool))
        db.add(MCPServerTool(tenant_id=data["tenant"], mcp_server_id=parent.id, tool_id=tool.id, mcp_tool_name="find"))
        await db.commit()
    sent = []
    async def provider(client, name, arguments):
        sent.append(client._headers()["Authorization"])
        return "Documents ready"
    monkeypatch.setattr(MCPClient, "call_tool", provider)
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker))
    context = ToolExecutionContext(agent_id=data["bot"], user_id=data["member"], tenant_id=str(data["tenant"]), workspace=tmp_path)
    async def invoke():
        return await call_mcp_tool(data["bot"], {"tool_name": tool.name}, execution_context=context)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        url = f'/api/vault/mcp/{data["mcp"]}'
        saved = await http.put(url, json={"values": {"access_token": "vault-mcp-new"}})
        assert saved.status_code == 200, saved.text
        assert "vault-mcp-new" not in saved.text
        assert "Documents ready" in await invoke()
        assert sent == ["Bearer vault-mcp-new"]
        removed = await http.delete(url)
        assert removed.status_code == 200, removed.text
        assert removed.json()["removal"]["fallback"] == "company"
        assert "Documents ready" in await invoke()
        assert sent == ["Bearer vault-mcp-new", "Bearer vault-company-mcp"]
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        removed = await http.delete(f'/api/vault/company_mcp/{data["parent"]}?field=access_token')
        assert removed.status_code in {403, 404}
    async with owner_sessionmaker() as db:
        parent = await db.get(MCPServer, data["parent"])
        parent.config_json = {"oauth": {"authorization_id": "synthetic-company-authorization-removed"}}
        parent.auth_status = "none"
        parent.status = "needs_auth"
        await db.commit()
    assert "ask_user_question" in await invoke()
    assert sent == ["Bearer vault-mcp-new", "Bearer vault-company-mcp"]


async def test_private_api_replacement_uses_new_value_after_existing_account_review(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    data = await seed(owner_sessionmaker)
    from app.services import agent_tools, custom_api_connectors
    from app.services.custom_api_accounts import set_grant, bind_account, connector_revision
    from app.tools.runtime import ToolExecutionContext
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async with owner_sessionmaker() as db:
        user, agent, tool = await db.get(User, data["member"]), await db.get(Agent, data["bot"]), await db.get(Tool, data["tool"])
        account = await db.get(CustomApiAccount, data["api"])
        grant = await set_grant(db, account=account, agent_id=agent.id, requester_id=user.id, enabled=True, expected_version=0)
        await bind_account(db, agent=agent, tool=tool, session_id=data["session"], user=user,
            account_id=account.id, account_version=account.version, grant_version=grant["version"])
        await db.commit()
    sent = []
    async def provider(name, request):
        sent.append(request.headers["Authorization"])
        return "Private bills ready"
    monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", provider)
    context = ToolExecutionContext(agent_id=agent.id, user_id=user.id, tenant_id=str(data["tenant"]), session_id=str(data["session"]), workspace=tmp_path)
    async def invoke():
        return await agent_tools._get_tool_runtime_service().execute_with_context(tool.name, {}, context)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        path = f'/api/vault/api/{data["api"]}'
        saved = await http.put(path, json={"values": {"bearer_token": "vault-private-api-new"}, "version": account.version,
            "connection_revision": connector_revision(tool)})
        assert saved.status_code == 200, saved.text
        assert "vault-private-api-new" not in saved.text
        stale = await http.put(path, json={"values": {"bearer_token": "DO_NOT_SAVE_STALE_SECRET"}, "version": account.version,
            "connection_revision": connector_revision(tool)})
        assert stale.status_code == 409 and "DO_NOT_SAVE_STALE_SECRET" not in stale.text
        stale_delete = await http.delete(f"{path}?version={account.version}")
        assert stale_delete.status_code == 409
        assert "review" in str(await invoke()).lower() and not sent, "Replacement must not enlarge an old work authorization"
        async with owner_sessionmaker() as db:
            current = await db.get(CustomApiAccount, account.id)
            await bind_account(db, agent=agent, tool=tool, session_id=data["session"], user=user,
                account_id=current.id, account_version=current.version, grant_version=grant["version"])
            await db.commit()
        assert "Private bills ready" in str(await invoke())
        assert sent == ["Bearer vault-private-api-new"]
        removed = await http.delete(f"{path}?version={current.version}")
        assert removed.status_code == 200 and removed.json()["removal"] == {"fallback": "company", "review_required": True}
        assert "review" in str(await invoke()).lower() and sent == ["Bearer vault-private-api-new"]


async def test_mailbox_replacement_reaches_imap_and_revocation_keeps_the_existing_review_contract(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    data = await seed(owner_sessionmaker)
    from app.services.email_accounts import set_grant, bind_account
    from app.services.agent_tool_domains.email import _handle_email_tool
    from app.tools.runtime import ToolExecutionContext
    async with owner_sessionmaker() as db:
        user, agent, account = await db.get(User, data["member"]), await db.get(Agent, data["bot"]), await db.get(EmailAccount, data["email"])
        grant = await set_grant(db, account=account, agent_id=agent.id, requester_id=user.id, enabled=True, expected_version=0)
        await bind_account(db, agent=agent, session_id=data["session"], user=user, account_id=account.id,
            account_version=account.version, grant_version=grant["version"])
        await db.commit()
    sent = []
    class Imap:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, address, password): sent.append(password)
        def select(self, *args, **kwargs): return "OK", []
        def search(self, *args): return "OK", [b""]
    monkeypatch.setattr("app.services.email_service.imaplib.IMAP4_SSL", Imap)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    context = ToolExecutionContext(agent_id=agent.id, user_id=user.id, tenant_id=str(data["tenant"]), session_id=str(data["session"]), workspace=tmp_path)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker) as http:
        path = f'/api/vault/email/{data["email"]}'
        saved = await http.put(path, json={"values": {"auth_code": "vault-private-mail-new"}, "version": account.version})
        assert saved.status_code == 200 and "vault-private-mail-new" not in saved.text
        async with owner_sessionmaker() as db:
            current = await db.get(EmailAccount, account.id)
            await bind_account(db, agent=agent, session_id=data["session"], user=user, account_id=current.id,
                account_version=current.version, grant_version=grant["version"])
            await db.commit()
        assert "No emails" in await _handle_email_tool("read_emails", agent.id, tmp_path, {}, context=context)
        assert sent == ["vault-private-mail-new"]
        removed = await http.delete(f"{path}?version={current.version}")
        assert removed.status_code == 200 and removed.json()["removal"]["review_required"]
        assert "review" in (await _handle_email_tool("read_emails", agent.id, tmp_path, {}, context=context)).lower()
        assert sent == ["vault-private-mail-new"]


async def test_company_slot_clear_preserves_other_values_and_invalid_bodies_never_echo_input(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    data = await seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        override = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tool_id == data["tool"]))
        tool = await db.get(Tool, data["tool"])
        override.config = {**override.config, "ordinary_setting": "keep-company-setting"}
        original_tool_config = dict(tool.config)
        (await db.get(AgentTool, data["assignment"])).config = {}
        await db.commit()
    from app.services import agent_tools, custom_api_connectors
    from app.tools.runtime import ToolExecutionContext
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    sent = []
    async def provider(name, request):
        sent.append(request.headers["Authorization"])
        return "Company bills ready"
    monkeypatch.setattr(custom_api_connectors, "execute_prepared_custom_api_request", provider)
    context = ToolExecutionContext(agent_id=data["bot"], user_id=data["member"], tenant_id=str(data["tenant"]), workspace=tmp_path)
    async with await client_for(data, owner_sessionmaker, app_user_sessionmaker, "admin") as http:
        path = f'/api/vault/company/{data["tool"]}?field=bearer_token'
        saved = await http.put(path, json={"values": {"bearer_token": "vault-company-new"}})
        assert saved.status_code == 200 and "vault-company-new" not in saved.text
        assert "Company bills ready" in str(await agent_tools._get_tool_runtime_service().execute_with_context(tool.name, {}, context))
        assert sent == ["Bearer vault-company-new"]
        removed = await http.delete(path)
        assert removed.status_code == 200 and removed.json()["removal"]["fallback"] == "configuration"
        bad = await http.put(path, json={"values": ["NEVER_ECHO_INVALID_PRIVATE_INPUT"]})
        assert bad.status_code == 422 and "NEVER_ECHO_INVALID_PRIVATE_INPUT" not in bad.text
    async with owner_sessionmaker() as db:
        record = await db.scalar(select(TenantToolConfig).where(TenantToolConfig.tool_id == data["tool"]))
        assert record.config == {"ordinary_setting": "keep-company-setting"}
        assert (await db.get(Tool, data["tool"])).config == original_tool_config
