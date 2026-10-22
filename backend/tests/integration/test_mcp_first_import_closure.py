"""Fresh MCP installation becomes usable only after exact metadata approval."""

import json
import sys
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("origin", ["qualified", "alias", "direct"])
async def test_first_import_retains_reviewed_employee_capability_and_exact_server_identity(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, origin,  # noqa: F811
):
    from app import database
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
    from app.models.tool import AgentTool, Tool
    from app.models.user import User
    from app.services import agent_tools, resource_discovery as discovery
    from app.tools.handlers.mcp import import_mcp_server

    app, (tenant_id, owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    admin_id, peer_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=tenant_id, username="first-review-" + admin_id.hex,
            email=admin_id.hex + "@first-mcp.test", display_name="Company reviewer", password_hash="synthetic", role="org_admin")
        db.add(admin)
        db.add(Agent(id=peer_id, tenant_id=tenant_id, creator_id=owner_id, name="Unassigned employee"))
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
    async with app_user_sessionmaker() as db:
        assert (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
    endpoint = "http://127.0.0.1:55104/mcp/" + agent_id.hex
    registry_id, display_name = "synthetic/ledger", "Fresh Ledger"
    remote = {"tools": [{"name": "lookup", "description": "Read a ledger entry.",
        "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}},
        "permissions": ["ledger:read"]}]}
    requests = []

    def respond(request):
        requests.append({"port": request.url.port, "path": request.url.path, "method": request.method})
        if request.url.port == 55105:
            if request.url.path == "/servers":
                return httpx.Response(200, json={"servers": [{"qualifiedName": registry_id,
                    "displayName": display_name, "remote": True}]})
            assert request.url.path == "/servers/" + registry_id
            return httpx.Response(200, json={"deploymentUrl": endpoint, "tools": remote["tools"]})
        assert str(request.url) == endpoint and request.method == "POST"
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"}}}, headers={"Mcp-Session-Id": "synthetic"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202, json={})
        assert body["method"] == "tools/list"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": remote})

    async def existing_connection(_api_key, actual_endpoint, actual_name):
        assert actual_endpoint == endpoint and actual_name == display_name
        return {"namespace": "synthetic", "connection_id": "fresh-ledger"}

    original_client = httpx.AsyncClient
    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(discovery, "SMITHERY_API_BASE", "http://127.0.0.1:55105")
    monkeypatch.setattr(discovery, "_ensure_smithery_connection", existing_connection)
    arguments = ({"server_id": display_name, "config": {"mcp_url": endpoint}} if origin == "direct" else {
        "server_id": registry_id if origin == "qualified" else "ledger",
        "config": {"smithery_api_key": "synthetic-registry-key"}})
    imported = await import_mcp_server(agent_id, {**arguments, "config": dict(arguments["config"])})
    assert "Imported MCP server" in imported, imported
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        tools = list(await db.scalars(select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "mcp")))
        assert len(tools) == 1
        tool_name = tools[0].name
        servers = list(await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant_id)))
        assignments = list(await db.scalars(select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.tenant_id == tenant_id)))
        snapshot = {"origin": origin, "imported": imported, "tool_name": tool_name,
            "servers": [{"id": str(row.id), "name": row.name, "url": row.server_url} for row in servers],
            "assignments": [{"agent_id": str(row.agent_id), "enabled": row.enabled} for row in assignments],
            "requests": requests}
        (tmp_path / "fresh-import-before-review.json").write_text(json.dumps(snapshot, indent=2))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://first-import.test") as client:
        listed = await client.get(f"/api/agents/{agent_id}/mcp-servers")
        assert listed.status_code == 200 and len(listed.json()) == 1, (snapshot, listed.text)
        assert len(servers) == 1 and servers[0].server_url == endpoint, snapshot
        server_id = listed.json()[0]["id"]
        assert {row.agent_id for row in assignments} == {agent_id}, snapshot
        path = f"/api/enterprise/mcp-servers/{server_id}/tools"
        pending = await client.get(path)
        assert pending.status_code == 200 and len(pending.json()) == 1, pending.text
        fact = pending.json()[0]
        assert fact["runtime_approved"] is False and fact["activation"].get("permissions") == ["ledger:read"], fact
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=[tool_name])
        assert tool_name not in {row["function"]["name"] for row in available}
        reviewed = await client.put(path + "/lookup/metadata-review", json={"decision": "approve",
            "expected_fingerprint": fact["metadata_fingerprint"], "canonical_description": "Read a reviewed ledger entry."})
        assert reviewed.status_code == 200, reviewed.text
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=[tool_name])
        after = await client.get(f"/api/agents/{agent_id}/mcp-servers")
        (tmp_path / "fresh-import-after-review.json").write_text(json.dumps({"origin": origin,
            "servers": after.json(), "available": [row["function"]["name"] for row in available]}, indent=2))
        assert tool_name in {row["function"]["name"] for row in available}, (snapshot, after.text)
        assert after.json()[0]["enabled"] is True
        remote["tools"][0]["permissions"] = ["ledger:read", "ledger:write"]
        await import_mcp_server(agent_id, {**arguments, "reauthorize": True, "config": dict(arguments["config"])})
        changed = (await client.get(path)).json()[0]
        assert changed["metadata_fingerprint"] != fact["metadata_fingerprint"] and changed["runtime_approved"] is False
        rejected = await client.put(path + "/lookup/metadata-review", json={"decision": "approve",
            "expected_fingerprint": fact["metadata_fingerprint"], "canonical_description": "Read a reviewed ledger entry."})
        assert rejected.status_code == 409, rejected.text
        approved = await client.put(path + "/lookup/metadata-review", json={"decision": "approve",
            "expected_fingerprint": changed["metadata_fingerprint"], "canonical_description": "Read a reviewed ledger entry."})
        assert approved.status_code == 200, approved.text
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=[tool_name])
        assert tool_name in {row["function"]["name"] for row in available}
        disabled = await client.put(f"/api/agents/{agent_id}/mcp-servers/{server_id}",
            json={"enabled": False, "default_tool_mode": "auto"})
        assert disabled.status_code == 200, disabled.text
        await import_mcp_server(agent_id, {**arguments, "reauthorize": True, "config": dict(arguments["config"])})
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=[tool_name])
        assert tool_name not in {row["function"]["name"] for row in available}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assert len(list(await db.scalars(select(MCPServer).where(MCPServer.tenant_id == tenant_id)))) == 1
        assert len(list(await db.scalars(select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "mcp")))) == 1
        assert not list(await db.scalars(select(AgentTool).where(AgentTool.agent_id == peer_id)))
        assert not list(await db.scalars(select(AgentMCPServerAssignment).where(AgentMCPServerAssignment.agent_id == peer_id)))
