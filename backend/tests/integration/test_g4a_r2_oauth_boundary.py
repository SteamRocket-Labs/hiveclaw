"""OAuth start, legacy callback and call-time refresh share public egress."""

import copy
import json
import sys
import time
import uuid

import httpx
import pytest

from app.database import pin_rls_tenant_context
from app.models.mcp_server import MCPServer, MCPServerTool
from app.services import mcp_oauth
from tests.services.test_mcp_oauth_lifecycle import oauth_server, _client, _start, _callback  # noqa: F401
from tests.services.test_g4a_r2_public_clients import wire  # noqa: F401
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "public_mcp_dns")

PRIVATE_ENDPOINTS = ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:6379/",
    "http://[::ffff:10.0.0.1]/token", "http://100.64.0.1/"]


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint"])
@pytest.mark.parametrize("endpoint", PRIVATE_ENDPOINTS + ["ftp://public.example/token", "https:///token",
    "https://u@public.example/token", "https://u:p@public.example/token", "javascript:alert(1)"])
async def test_oauth_start_rejects_invalid_endpoints_without_storing(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, field, endpoint,  # noqa: F811
):
    async with owner_sessionmaker() as db:
        before = copy.deepcopy((await db.get(MCPServer, oauth_server.server_id)).config_json)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        response = await client.post(f"/enterprise/mcp/{oauth_server.server_id}/oauth/start", json={
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token", field: endpoint,
            "client_id": "synthetic", "client_secret": "synthetic-secret", "redirect_uri": "https://hive.test/callback"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "oauth_endpoint_invalid"
    assert "公开" in response.json()["detail"]["message"]
    async with owner_sessionmaker() as db:
        assert (await db.get(MCPServer, oauth_server.server_id)).config_json == before


@pytest.mark.parametrize("endpoint", PRIVATE_ENDPOINTS)
async def test_callback_refuses_legacy_stored_private_token_endpoint(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, wire, endpoint,  # noqa: F811
):
    requests = wire(lambda _request: httpx.Response(200, json={"access_token": "synthetic-token"}))
    async with _client(app_user_sessionmaker, oauth_server) as client:
        start = await _start(client, oauth_server)
        async with owner_sessionmaker() as db:
            server = await db.get(MCPServer, oauth_server.server_id)
            config = copy.deepcopy(server.config_json)
            config["oauth"]["pending"]["token_endpoint"] = endpoint
            server.config_json = config
            await db.commit()
        response = await _callback(client, start)
        assert response.status_code == 400, response.text
    assert requests == []
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        assert not (server.config_json or {}).get("oauth", {}).get("token")


async def _approved_tool(owner_sessionmaker, actor):
    from app.models.agent import Agent
    from app.models.tool import Tool, AgentTool
    from app.services.mcp_metadata_trust import current_mcp_fingerprint, review_mcp_metadata

    agent_id, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = Agent(id=agent_id, tenant_id=actor.tenant_id, creator_id=actor.id, name="Public document reader")
        tool = Tool(id=tool_id, tenant_id=actor.tenant_id, name="mcp__r2__read", display_name="Read documents",
            description="Read reviewed public documents.", type="mcp", mcp_server_url=actor.server_url,
            mcp_server_name="OAuth MCP", mcp_tool_name="read", parameters_schema={"type": "object"})
        db.add_all([agent, tool])
        await db.flush()
        review_mcp_metadata(tool, decision="approve", reviewer_id=actor.id, expected_fingerprint=current_mcp_fingerprint(tool))
        db.add_all([AgentTool(agent_id=agent_id, tool_id=tool_id, tenant_id=actor.tenant_id, enabled=True),
            MCPServerTool(tenant_id=actor.tenant_id, mcp_server_id=actor.server_id, tool_id=tool_id, mcp_tool_name="read")])
        (await db.get(MCPServer, actor.server_id)).status = "connected"
        await db.commit()
    return agent_id


def _runtime_factories(monkeypatch, app_user_sessionmaker, scoped_runtime_factories):
    from app import database
    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    scoped_runtime_factories()


def _error(result):
    return json.loads(result.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])


@pytest.mark.parametrize("entry", ["generic", "native", "mcp_list_resources", "mcp_read_resource",
    "mcp_list_prompts", "mcp_get_prompt"])
async def test_approved_company_tool_rebinding_names_administrator_not_member_url(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch, wire, scoped_runtime_factories, entry,  # noqa: F811
):
    from app.tools.handlers import mcp
    from app.services.agent_tool_domains.web_mcp import _execute_mcp_tool

    agent_id = await _approved_tool(owner_sessionmaker, oauth_server)
    _runtime_factories(monkeypatch, app_user_sessionmaker, scoped_runtime_factories)
    async def private_dns(_host, _port): return ("10.0.0.1",)
    monkeypatch.setattr("app.services.governed_egress.resolve_public_addresses", private_dns)
    requests = wire(lambda _request: httpx.Response(200, json={"result": {"content": []}}))
    if entry == "generic":
        result = await mcp.call_mcp_tool(agent_id, {"tool_name": "mcp__r2__read", "arguments": {}})
    elif entry == "native":
        result = await _execute_mcp_tool("mcp__r2__read", {}, agent_id=agent_id)
    else:
        result = await getattr(mcp, entry)(agent_id, {"server": str(oauth_server.server_id),
            "uri": "mcp://documents/readme", "prompt_name": "readme", "arguments": {}})
    error = _error(result)
    assert error["error_class"] == "configuration_required", result
    assert error["diagnostic_hint"] == "请管理员检查应用连接地址。"
    assert error["diagnostic_message"] == "已批准的应用连接地址当前不是可公开访问的地址，请管理员检查应用连接地址。"
    assert "请提供" not in result
    assert "请检查网址" not in result
    assert requests == []


async def test_call_refused_refresh_keeps_existing_oauth_failure_path_and_names_administrator(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch, wire, scoped_runtime_factories,  # noqa: F811
):
    from app.tools.handlers.mcp import call_mcp_tool

    agent_id = await _approved_tool(owner_sessionmaker, oauth_server)
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        server.auth_status = mcp_oauth.AUTH_CONFIGURED
        server.config_json = {"oauth": {"token_endpoint": PRIVATE_ENDPOINTS[0], "client_id": "synthetic",
            "client_secret": mcp_oauth.encrypt_value("synthetic-secret"),
            "token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("synthetic-old",
                refresh_token="synthetic-refresh", expires_at=time.time()-100))}}
        await db.commit()
    _runtime_factories(monkeypatch, app_user_sessionmaker, scoped_runtime_factories)
    requests = wire(lambda _request: httpx.Response(200, json={"access_token": "synthetic-new"}))
    result = await call_mcp_tool(agent_id, {"tool_name": "mcp__r2__read", "arguments": {}})
    error = _error(result)
    assert error["error_class"] == "auth_required", result
    assert error["diagnostic_hint"] == "请管理员检查应用连接地址。"
    assert requests == []
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, oauth_server.tenant_id)
        server = await db.get(MCPServer, oauth_server.server_id)
        assert server.auth_status == mcp_oauth.AUTH_EXPIRED
        assert server.config_json["oauth"]["refresh"]["status"] == "failed"


@pytest.mark.parametrize("failure", ["invalid_grant", "timeout"])
async def test_personal_refresh_failure_keeps_reauthorization_card_hint(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch, wire, scoped_runtime_factories, tmp_path, failure,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.user import User
    from app.services.mcp_accounts import create_personal_account
    from app.tools.handlers.mcp import call_mcp_tool
    from app.tools.runtime import ToolExecutionContext

    agent_id = await _approved_tool(owner_sessionmaker, oauth_server)
    account_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, oauth_server.id)
        user.role = "member"
        (await db.get(Agent, agent_id)).owner_user_id = user.id
        await create_personal_account(db, user=user, tenant_id=oauth_server.tenant_id,
            server_id=oauth_server.server_id, account_id=account_id, name="Personal documents")
        account = await db.get(MCPServer, account_id)
        account.auth_status = mcp_oauth.AUTH_CONFIGURED
        account.config_json = {"oauth": {"token_endpoint": "https://auth.example.test/token", "client_id": "synthetic",
            "token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("synthetic-old",
                refresh_token="synthetic-refresh", expires_at=time.time()-100))}}
        await db.commit()
    _runtime_factories(monkeypatch, app_user_sessionmaker, scoped_runtime_factories)
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        return httpx.Response(400, json={"error": "invalid_grant"})
    requests = wire(respond)
    context = ToolExecutionContext(agent_id=agent_id, user_id=oauth_server.id,
        tenant_id=str(oauth_server.tenant_id), workspace=tmp_path)
    result = await call_mcp_tool(agent_id, {"tool_name": "mcp__r2__read", "arguments": {}}, execution_context=context)
    error = _error(result)
    assert error["error_class"] == "auth_required", result
    assert error["diagnostic_message"] == "MCP OAuth refresh failed or its outcome is unknown; re-authorize the server."
    assert "ask_user_question" in error["diagnostic_hint"]
    assert "configuration.mcp_server_id" in error["diagnostic_hint"]
    assert "请管理员" not in result
    assert len(requests) == 1
    assert requests[0].url == "https://auth.example.test/token"
