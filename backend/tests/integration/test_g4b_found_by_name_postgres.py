"""G4b: a member names an approved company MCP service by its admin-entered
server display name and the bot finds its tools through the deferred path."""

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

SERVER_DISPLAY_NAME = "DeepWiki"
PUBLIC_URL = "https://g4b-public.example/mcp"


def _stub_remote_tools() -> list[dict]:
    return [
        {
            "name": name,
            "description": "Remote wiki evidence for " + name + ".",
            "inputSchema": {"type": "object", "properties": {"repoName": {"type": "string"}}, "required": ["repoName"]},
            "permissions": ["wiki:read"],
        }
        for name in ("ask_wiki_question", "read_wiki_contents", "read_wiki_structure")
    ]


async def _import_and_approve(app, admin, monkeypatch) -> tuple[list[str], uuid.UUID]:
    """Run the real admin path: POST /tools import → metadata review → approve
    (default purpose, no admin edit), exactly as the web UI does."""
    from app.core.security import get_current_user

    remote_tools = _stub_remote_tools()

    def respond(request):
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {"tools": remote_tools}
        if body["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "G4b", "version": "1"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_client = httpx.AsyncClient

    def transport_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", transport_client)

    app.dependency_overrides[get_current_user] = lambda: admin
    names: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://g4b.test") as client:
        for tool in remote_tools:
            name = "mcp_" + tool["name"]
            created = await client.post("/api/tools", json={"name": name, "display_name": tool["name"],
                "description": tool["description"], "type": "mcp", "mcp_server_url": PUBLIC_URL,
                "mcp_server_name": SERVER_DISPLAY_NAME, "mcp_tool_name": tool["name"],
                "parameters_schema": tool["inputSchema"], "config": {"permissions": tool["permissions"]},
                "enabled": True})
            assert created.status_code == 200, created.text
            names.append(name)
        servers = await client.get("/api/enterprise/mcp-servers")
        assert servers.status_code == 200 and len(servers.json()) == 1, servers.text
        server_id = servers.json()[0]["id"]
        metadata_path = f"/api/enterprise/mcp-servers/{server_id}/tools"
        pending = await client.get(metadata_path)
        assert pending.status_code == 200 and len(pending.json()) == 3
        for tool in pending.json():
            assert tool["trust_status"] == "pending_review" and not tool["runtime_approved"]
            # Admin approves the platform default purpose without editing it.
            reviewed = await client.put(metadata_path + f"/{tool['tool_name']}/metadata-review", json={
                "decision": "approve", "expected_fingerprint": tool["metadata_fingerprint"]})
            assert reviewed.status_code == 200, reviewed.text
    return names, server_id


async def test_approved_mcp_tool_is_found_by_server_display_name(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    from app.api.mcp_servers import router
    from app.models.agent import Agent
    from app.models.mcp_server import AgentMCPServerAssignment
    from app.models.tool import Tool
    from app.models.user import User
    from app.runtime.prompt_builder import _render_deferred_tool_index
    from app.runtime.deferred_tools import coerce_deferred_tool_candidates
    from app.services import agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search
    from app.runtime.prompts.mcp import MCP_ADMIN_ROUTE

    app, (tenant_id, owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    admin_id, member_id, second_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=tenant_id, username="g4b-admin-" + admin_id.hex,
            email=admin_id.hex + "@g4b.test", display_name="Administrator", password_hash="synthetic", role="org_admin")
        member = User(id=member_id, tenant_id=tenant_id, username="g4b-member-" + member_id.hex,
            email=member_id.hex + "@g4b.test", display_name="Member", password_hash="synthetic", role="member")
        db.add_all([admin, member])
        await db.flush()
        db.add(Agent(id=second_id, tenant_id=tenant_id, creator_id=member_id, name="Second employee"))
        await db.commit()

    scoped_runtime_factories()

    async def public_dns(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.services.governed_egress.resolve_public_addresses", public_dns)
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    names, server_id = await _import_and_approve(app, admin, monkeypatch)

    # The default reviewed purpose names the server display name and the tool,
    # with no remote text (the admin approved it without editing).
    from app.database import pin_rls_tenant_context

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        rows = (await db.scalars(select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "mcp"))).all()
        assert len(rows) == 3
        for row in rows:
            assert row.description.startswith(
                f"{row.mcp_tool_name} from the company connection '{SERVER_DISPLAY_NAME}' (g4b-public.example)."
            ), row.description
            assert "Remote wiki evidence" not in row.description
            assert row.mcp_trust_tier == "platform_generated_reviewed"

    # After approval, another bot's deferred catalog carries the server display
    # name next to the tool names, and the prompt-rendered index shows it.
    manifests = await agent_tools.available_deferred_tool_candidates_for_agent(second_id)
    labeled = {m["name"]: m.get("label", "") for m in manifests if m["name"] in names}
    assert labeled == {name: f"{name} ({SERVER_DISPLAY_NAME})" for name in names}, labeled
    rendered = _render_deferred_tool_index(coerce_deferred_tool_candidates(manifests), budget_chars=1600)
    for name in names:
        assert f"- {name} ({SERVER_DISPLAY_NAME}) — `select:{name}`" in rendered, rendered

    # A tool search for the server display name returns the three tools.
    discovered = await agent_tools.discoverable_tool_names_for_query(second_id, SERVER_DISPLAY_NAME)
    assert set(names) <= set(discovered), discovered

    # The tool_search text result shows the server display name and carries the
    # whole administrator route (G4b item 2).
    result = await _tool_search(tmp_path, SERVER_DISPLAY_NAME, agent_id=second_id)
    for name in names:
        assert f"{name} ({SERVER_DISPLAY_NAME})" in result, result
    assert MCP_ADMIN_ROUTE in result, result
    assert MCP_ADMIN_ROUTE.startswith("设置 → 公司后台 → 公司连接"), MCP_ADMIN_ROUTE

    # A bot whose assignment for this server is disabled finds none. Import
    # created an enabled assignment for every bot; disable this bot's row.
    async with app_user_sessionmaker() as db:
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, tenant_id)
        assignment = (await db.execute(select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.agent_id == second_id,
            AgentMCPServerAssignment.mcp_server_id == server_id))).scalar_one()
        assignment.enabled = False
        await db.commit()
    assert await agent_tools.discoverable_tool_names_for_query(second_id, SERVER_DISPLAY_NAME) == []


async def test_before_approval_the_service_is_not_discoverable(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories,  # noqa: F811
):
    from app.api.mcp_servers import router
    from app.models.agent import Agent
    from app.models.user import User
    from app.services import agent_tools

    app, (tenant_id, owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    admin_id, member_id, second_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=tenant_id, username="g4b2-admin-" + admin_id.hex,
            email=admin_id.hex + "@g4b.test", display_name="Administrator", password_hash="synthetic", role="org_admin")
        member = User(id=member_id, tenant_id=tenant_id, username="g4b2-member-" + member_id.hex,
            email=member_id.hex + "@g4b.test", display_name="Member", password_hash="synthetic", role="member")
        db.add_all([admin, member])
        await db.flush()
        db.add(Agent(id=second_id, tenant_id=tenant_id, creator_id=member_id, name="Second employee"))
        await db.commit()

    scoped_runtime_factories()

    async def public_dns(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.services.governed_egress.resolve_public_addresses", public_dns)
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    remote_tools = _stub_remote_tools()

    def respond(request):
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {"tools": remote_tools}
        if body["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "G4b", "version": "1"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_client = httpx.AsyncClient

    def transport_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", transport_client)

    from app.core.security import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    names: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://g4b.test") as client:
        for tool in remote_tools:
            name = "mcp_" + tool["name"]
            created = await client.post("/api/tools", json={"name": name, "display_name": tool["name"],
                "description": tool["description"], "type": "mcp", "mcp_server_url": PUBLIC_URL,
                "mcp_server_name": SERVER_DISPLAY_NAME, "mcp_tool_name": tool["name"],
                "parameters_schema": tool["inputSchema"], "config": {"permissions": tool["permissions"]},
                "enabled": True})
            assert created.status_code == 200, created.text
            names.append(name)

    # Pending review: no catalog entry, no discovery by the server display name.
    manifests = await agent_tools.available_deferred_tool_candidates_for_agent(second_id)
    assert not any(m["name"] in names for m in manifests)
    assert await agent_tools.discoverable_tool_names_for_query(second_id, SERVER_DISPLAY_NAME) == []
