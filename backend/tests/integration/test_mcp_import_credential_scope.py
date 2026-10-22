"""Employee import credentials do not become company defaults or cross employees."""

import json
import sys
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("entry", ["native", "generic", "resources"])
@pytest.mark.parametrize("company_default", [False, True])
@pytest.mark.parametrize("credential_location", ["config", "apiKey", "api_key"])
@pytest.mark.parametrize("stored_legacy", [False, True])
async def test_direct_import_uses_only_the_current_employee_or_explicit_company_credential(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, entry, company_default,  # noqa: F811
    credential_location, stored_legacy,
):
    from app import database
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.mcp_server import MCPServer, MCPServerTool
    from app.models.tool import AgentTool, Tool
    from app.models.user import User
    from app.services import agent_tools, resource_discovery as discovery
    from app.services.agent_tool_domains import web_mcp
    from app.tools.handlers import mcp
    from app.tools.runtime import ToolExecutionContext

    app, (tenant, owner, agent, session, run) = await _app(owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    peer, peer_session, admin_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=tenant, username="credential-review-" + admin_id.hex,
            email=admin_id.hex + "@mcp-import.test", password_hash="synthetic", role="org_admin", display_name="Reviewer")
        db.add(admin)
        db.add(Agent(id=peer, tenant_id=tenant, creator_id=owner, owner_user_id=owner, name="Separate employee"))
        await db.flush()
        db.add(ChatSession(id=peer_session, agent_id=peer, tenant_id=tenant, user_id=owner,
            source_channel="web", runtime_source="web_chat", actor_type="user", session_kind="human_chat",
            visibility_scope="direct_user", listed_surface="chat"))
        await db.commit()
    app.dependency_overrides[get_current_user] = lambda: admin
    original_factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original_factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    scoped_runtime_factories()

    async def no_external_office_probe():
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", no_external_office_probe)
    endpoint = "http://127.0.0.1:55124/mcp/" + agent.hex
    remote = {"tools": [{"name": "lookup", "description": "Read a public ledger entry.",
        "inputSchema": {"type": "object", "properties": {}}, "permissions": ["ledger:read"]}]}
    observed = []

    def respond(request):
        assert str(request.url) == endpoint and request.method == "POST"
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "synthetic-ledger", "version": "1"}}
        elif method == "notifications/initialized":
            return httpx.Response(202, json={})
        elif method == "tools/list":
            result = remote
        else:
            assert method in {"tools/call", "resources/list"}
            observed.append({"method": method, "authorization": request.headers.get("Authorization")})
            result = ({"content": [{"type": "text", "text": "Public ledger response"}]} if method == "tools/call" else {
                "resources": [{"name": "Public ledger", "uri": "ledger://public"}]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))

    async def install(employee, api_key=None):
        url = endpoint
        config = {}
        if api_key and credential_location != "config":
            url += f"?{credential_location}={api_key}"
        elif api_key:
            config["api_key"] = api_key
        return await mcp.import_mcp_server(employee, {"server_id": "Public Ledger",
            "config": {"mcp_url": url, **config}})

    assert "Imported MCP server" in await install(peer)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
        tool = await db.scalar(select(Tool).where(Tool.tenant_id == tenant, Tool.type == "mcp"))
        tool_id, tool_name = tool.id, tool.name
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mcp-credential.test") as client:
        if company_default:
            configured = await client.put(f"/api/tools/{tool_id}", json={"config": {"api_key": "synthetic-company-key"}})
            assert configured.status_code == 200, configured.text
        assert "Imported MCP server" in await install(agent, "synthetic-employee-key")
        assert "Imported MCP server" in await install(peer)
        legacy_url = endpoint + "?api_key=synthetic-unattributed-legacy-key"
        if stored_legacy:
            async with owner_sessionmaker() as db:
                historical = await db.get(Tool, tool_id)
                historical.mcp_server_url = legacy_url
                historical.mcp_server_name = None
                server = await db.scalar(select(MCPServer).where(MCPServer.tenant_id == tenant))
                server.server_url = legacy_url
                await db.commit()
        listed = await client.get(f"/api/agents/{agent}/mcp-servers")
        assert listed.status_code == 200 and len(listed.json()) == 1, listed.text
        review_path = f"/api/enterprise/mcp-servers/{listed.json()[0]['id']}/tools"
        pending = await client.get(review_path)
        assert pending.status_code == 200 and len(pending.json()) == 1, pending.text
        fact = pending.json()[0]
        reviewed = await client.put(review_path + "/lookup/metadata-review", json={"decision": "approve",
            "expected_fingerprint": fact["metadata_fingerprint"], "canonical_description": "Read a reviewed public ledger entry."})
        assert reviewed.status_code == 200, reviewed.text
        if stored_legacy:
            async with owner_sessionmaker() as db:
                await db.execute(delete(MCPServerTool).where(MCPServerTool.tool_id == tool_id))
                await db.commit()
        contexts = {employee: ToolExecutionContext(agent_id=employee, user_id=owner, tenant_id=str(tenant),
            session_id=str(work), runtime_task_id=str(run) if employee == agent else None, workspace=tmp_path)
            for employee, work in [(agent, session), (peer, peer_session)]}

        async def invoke(employee):
            context = contexts[employee]
            if entry == "native":
                return await web_mcp._execute_mcp_tool(tool_name, {}, employee, execution_context=context)
            if entry == "generic":
                return await mcp.call_mcp_tool(employee, {"tool_name": tool_name}, execution_context=context)
            return await mcp.mcp_list_resources(employee, {}, execution_context=context)

        discovery_views = [await mcp.list_mcp_tools(peer, {}),
            await mcp.inspect_mcp_tool(peer, {"tool_name": tool_name}), await mcp.mcp_auth_status(peer, {})]
        profile = await client.get(f"/api/tools/agents/{peer}")
        assert profile.status_code == 200, profile.text
        (tmp_path / "credential-readback.json").write_text(json.dumps({"views": discovery_views,
            "profile": profile.json()}, indent=2))
        assert all("synthetic-unattributed-legacy-key" not in view for view in discovery_views), discovery_views
        assert "synthetic-unattributed-legacy-key" not in profile.text
        for employee in [agent, peer]:
            available = await agent_tools.get_agent_tools_for_llm(employee, requested_names=[tool_name])
            assert tool_name in {row["function"]["name"] for row in available}
            if stored_legacy and not company_default and employee == peer:
                result = await invoke(employee)
                assert "configuration_required" in result, result
                assert len(observed) == 1, observed
            else:
                assert "Public ledger" in await invoke(employee)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            shared = await db.get(Tool, tool_id)
            first_config = await db.scalar(select(AgentTool.config).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))
            second_config = await db.scalar(select(AgentTool.config).where(AgentTool.agent_id == peer, AgentTool.tool_id == tool_id))
            raw = await db.scalar(text("SELECT config::text FROM agent_tools WHERE agent_id=:agent AND tool_id=:tool"),
                {"agent": agent, "tool": tool_id})
            snapshot = {"entry": entry, "company_default": company_default,
                "credential_location": credential_location, "stored_legacy": stored_legacy, "observed": list(observed),
                "shared_config": shared.config, "first_config": first_config, "second_config": second_config,
                "raw_employee_key_present": "synthetic-employee-key" in raw}
            (tmp_path / "credential-consumption.json").write_text(json.dumps(snapshot, indent=2))
        assert observed[0]["authorization"] == "Bearer synthetic-employee-key", snapshot
        expected_peer = "Bearer synthetic-company-key" if company_default else None
        if not (stored_legacy and not company_default):
            assert observed[1]["authorization"] == expected_peer, snapshot
        assert first_config.get("api_key") == "synthetic-employee-key" and not second_config.get("api_key")
        assert "synthetic-employee-key" not in json.dumps(shared.config) and "synthetic-employee-key" not in raw
        assert shared.config.get("api_key") == ("synthetic-company-key" if company_default else None)
        assert shared.mcp_server_url == (legacy_url if stored_legacy else endpoint)
        changed = await client.put(f"/api/tools/agents/{agent}/tool-config/{tool_id}",
            json={"config": {"api_key": "synthetic-rotated-employee-key"}})
        assert changed.status_code == 200, changed.text
        if stored_legacy and not company_default:
            configured_peer = await client.put(f"/api/tools/agents/{peer}/tool-config/{tool_id}",
                json={"config": {"api_key": "synthetic-peer-key"}})
            assert configured_peer.status_code == 200, configured_peer.text
            expected_peer = "Bearer synthetic-peer-key"
        assert "Public ledger" in await invoke(agent)
        assert "Public ledger" in await invoke(peer)
        assert observed[-2]["authorization"] == "Bearer synthetic-rotated-employee-key"
        assert observed[-1]["authorization"] == expected_peer
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            assert (await db.get(Tool, tool_id)).mcp_server_url == (legacy_url if stored_legacy else endpoint)
