"""An administrator's reviewed public connection is reusable across employees."""

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_admin_import_review_and_reuse_by_two_members_without_bot_configuration(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories,  # noqa: F811
):
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.mcp_server import MCPServer
    from app.models.tool import Tool
    from app.models.user import User
    from app.services import agent_tools
    from app.tools.handlers.mcp import call_mcp_tool

    app, (tenant_id, owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    admin_id, member_id, second_id, colleague_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=tenant_id, username="g4a-admin-" + admin_id.hex,
            email=admin_id.hex + "@g4a.test", display_name="Administrator", password_hash="synthetic", role="org_admin")
        member = User(id=member_id, tenant_id=tenant_id, username="g4a-member-" + member_id.hex,
            email=member_id.hex + "@g4a.test", display_name="Member", password_hash="synthetic", role="member")
        db.add_all([admin, member])
        await db.flush()
        db.add_all([Agent(id=second_id, tenant_id=tenant_id, creator_id=owner_id, name="Second employee"),
            Agent(id=colleague_id, tenant_id=tenant_id, creator_id=member_id, name="Colleague employee")])
        await db.commit()

    scoped_runtime_factories()
    async def public_dns(_host, _port):
        return ("93.184.216.34",)
    async def no_office_probe():
        return False
    monkeypatch.setattr("app.services.governed_egress.resolve_public_addresses", public_dns)
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", no_office_probe)

    remote_tools = [{"name": name, "description": "Read a public document.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
        "permissions": ["documents:read"]} for name in ("lookup", "outline")]
    calls = []
    def respond(request):
        assert request.url.host == "g4a-public.example"
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {"tools": remote_tools}
        if body["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "G4a", "version": "1"}}
        if body["method"] == "tools/call":
            calls.append(body["params"])
            result = {"content": [{"type": "text", "text": "Public document: " + body["params"]["arguments"]["topic"]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_client = httpx.AsyncClient
    def transport_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", transport_client)

    app.dependency_overrides[get_current_user] = lambda: member
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://g4a.test") as client:
        denied = await client.post("/api/tools/test-mcp", json={"server_url": "https://g4a-public.example/mcp"})
        assert denied.status_code == 403
        app.dependency_overrides[get_current_user] = lambda: admin
        tested = await client.post("/api/tools/test-mcp", json={"server_url": "https://g4a-public.example/mcp"})
        assert tested.status_code == 200 and len(tested.json()["tools"]) == 2, tested.text
        names = []
        for tool in tested.json()["tools"]:
            name = "mcp__g4a__" + tool["name"]
            created = await client.post("/api/tools", json={"name": name, "display_name": tool["name"],
                "description": tool["description"], "type": "mcp", "mcp_server_url": "https://g4a-public.example/mcp",
                "mcp_server_name": "G4a", "mcp_tool_name": tool["name"], "parameters_schema": tool["inputSchema"],
                "config": {"permissions": tool["permissions"]}, "enabled": True})
            assert created.status_code == 200, created.text
            names.append(name)
        servers = await client.get("/api/enterprise/mcp-servers")
        assert servers.status_code == 200 and len(servers.json()) == 1, servers.text
        server_id = servers.json()[0]["id"]
        for peer in (second_id, colleague_id):
            before = await agent_tools.get_agent_tools_for_llm(peer, requested_names=names)
            assert not set(names) & {row["function"]["name"] for row in before}
        metadata_path = f"/api/enterprise/mcp-servers/{server_id}/tools"
        pending = await client.get(metadata_path)
        assert pending.status_code == 200 and len(pending.json()) == 2
        for tool in pending.json():
            assert tool["trust_status"] == "pending_review" and not tool["runtime_approved"]
            assert tool["activation"]["permissions"] == ["documents:read"]
            reviewed = await client.put(metadata_path + f"/{tool['tool_name']}/metadata-review", json={
                "decision": "approve", "expected_fingerprint": tool["metadata_fingerprint"],
                "canonical_description": "Read a reviewed public document."})
            assert reviewed.status_code == 200, reviewed.text
        for peer in (second_id, colleague_id):
            after = await agent_tools.get_agent_tools_for_llm(peer, requested_names=names)
            assert set(names) <= {row["function"]["name"] for row in after}
            result = await call_mcp_tool(peer, {"tool_name": names[0], "arguments": {"topic": str(peer)}})
            assert result == "Public document: " + str(peer), result
        assert len(calls) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assert len(list(await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant_id)))) == 1
        assert len(list(await db.scalars(select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "mcp")))) == 2
