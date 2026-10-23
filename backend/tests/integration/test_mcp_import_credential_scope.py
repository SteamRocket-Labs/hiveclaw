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
@pytest.mark.parametrize("credential_location,legacy_unattributed", [
    ("config", False), ("apiKey", False), ("api_key", False), ("config", True),
])
@pytest.mark.parametrize("stored_legacy", [False, True])
async def test_direct_import_uses_only_the_current_employee_or_explicit_company_credential(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, entry, company_default,  # noqa: F811
    credential_location, stored_legacy, legacy_unattributed, created_company=False,
):
    from app import database
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.audit import AuditLog
    from app.models.chat_session import ChatSession
    from app.models.mcp_server import MCPServer, MCPServerTool
    from app.models.tool import AgentTool, Tool
    from app.models.tenant_tool_config import TenantToolConfig
    from app.models.user import User
    from app.services import agent_tools, resource_discovery as discovery
    from app.services.agent_tool_domains import web_mcp
    from app.tools.handlers import mcp
    from app.tools.runtime import ToolExecutionContext

    app, (tenant, owner, agent, session, run) = await _app(owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    peer, peer_session, admin_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        owner_user = await db.get(User, owner)
        owner_user.role = "member"
        admin = User(id=admin_id, tenant_id=tenant, username="credential-review-" + admin_id.hex,
            email=admin_id.hex + "@mcp-import.test", password_hash="synthetic", role="org_admin", display_name="Reviewer")
        db.add(admin)
        db.add(Agent(id=peer, tenant_id=tenant, creator_id=owner, owner_user_id=owner, name="Separate employee"))
        await db.flush()
        db.add(ChatSession(id=peer_session, agent_id=peer, tenant_id=tenant, user_id=owner,
            source_channel="web", runtime_source="web_chat", actor_type="user", session_kind="human_chat",
            visibility_scope="direct_user", listed_surface="chat"))
        await db.commit()
    actor = [admin]
    app.dependency_overrides[get_current_user] = lambda: actor[0]
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
    attempts = []

    def respond(request):
        attempts.append(str(request.url))
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

    if created_company:
        from app.services.mcp_naming import build_mcp_tool_name

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mcp-credential.test") as client:
            created = await client.post("/api/tools", json={"name": build_mcp_tool_name("Public Ledger", "lookup"),
                "display_name": "Public ledger", "type": "mcp", "category": "mcp", "enabled": True,
                "is_default": True, "mcp_server_name": "Public Ledger", "mcp_server_url": endpoint,
                "mcp_tool_name": "lookup", "config": {"api_key": "synthetic-company-key"}})
            assert created.status_code == 200, created.text
            assert "synthetic-company-key" not in created.text
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
            assert configured.json()["enabled"] is False
        if legacy_unattributed:
            async with owner_sessionmaker() as db:
                historical = await db.get(Tool, tool_id)
                historical.config = {**(historical.config or {}), "api_key": "synthetic-unattributed-company-key"}
                await db.commit()
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
            if (legacy_unattributed or stored_legacy and not company_default) and employee == peer:
                result = await invoke(employee)
                (tmp_path / "unattributed-credential-execution.json").write_text(json.dumps({
                    "legacy_unattributed": legacy_unattributed, "observed": list(observed), "result": result}, indent=2))
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
        if not (legacy_unattributed or stored_legacy and not company_default):
            assert observed[1]["authorization"] == expected_peer, snapshot
        if created_company:
            rotated = await client.put(f"/api/tools/{tool_id}", json={"config": {
                **shared.config, "api_key": "synthetic-rotated-company-key"}})
            assert rotated.status_code == 200, rotated.text
            before_attempts = len(attempts)
            if entry == "resources":
                assert "not_found" in await invoke(peer)
            else:
                assert "not approved" in await invoke(peer)
            assert len(attempts) == before_attempts
            rotated_fact = (await client.get(review_path)).json()[0]
            rotated_review = await client.put(review_path + "/lookup/metadata-review", json={"decision": "approve",
                "expected_fingerprint": rotated_fact["metadata_fingerprint"],
                "canonical_description": "Read a reviewed public ledger entry."})
            assert rotated_review.status_code == 200, rotated_review.text
            assert "Public ledger" in await invoke(agent)
            assert "Public ledger" in await invoke(peer)
            (tmp_path / "company-credential-rotation.json").write_text(json.dumps({
                "observed": list(observed), "employee_config": first_config,
                "company_employee_assignment": second_config}, indent=2))
            assert observed[-2]["authorization"] == "Bearer synthetic-employee-key"
            expected_peer = "Bearer synthetic-rotated-company-key"
            assert observed[-1]["authorization"] == expected_peer
        assert first_config.get("api_key") == "synthetic-employee-key" and not second_config.get("api_key")
        assert "synthetic-employee-key" not in json.dumps(shared.config) and "synthetic-employee-key" not in raw
        if legacy_unattributed:
            assert shared.config.get("api_key") == "synthetic-unattributed-company-key"
        else:
            assert shared.config.get("api_key") == ("synthetic-company-key" if company_default else None)
        assert shared.mcp_server_url == (legacy_url if stored_legacy else endpoint)
        if legacy_unattributed:
            confirmation = {"confirm_mcp_credential": True, "expected_mcp_fingerprint": fact["metadata_fingerprint"]}
            actor[0] = owner_user
            forbidden = await client.put(f"/api/tools/{tool_id}", json=confirmation)
            assert forbidden.status_code == 403, forbidden.text
            actor[0] = admin
            stale = await client.put(f"/api/tools/{tool_id}", json={**confirmation, "expected_mcp_fingerprint": "0" * 64})
            assert stale.status_code == 409, stale.text
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant)
                current_config = dict((await db.get(Tool, tool_id)).config)
            masked = await client.put(f"/api/tools/{tool_id}", json={"config": {
                **current_config, "api_key": "__HIVE_SECRET_SET__"}})
            assert masked.status_code == 200, masked.text
            assert "configuration_required" in await invoke(peer)
            assert len(observed) == 1
            if entry == "native" and not company_default and not stored_legacy:
                from app.models.tenant import Tenant

                async with owner_sessionmaker() as db:
                    (await db.get(User, admin.id)).role = "member"
                    await db.commit()
                revoked = await client.put(f"/api/tools/{tool_id}", json=confirmation)
                assert revoked.status_code == 403, revoked.text
                home = uuid.uuid4()
                async with owner_sessionmaker() as db:
                    db.add(Tenant(id=home, name="Administrator home", slug="admin-home-" + home.hex, is_active=False))
                    await db.flush()
                    principal = await db.get(User, admin.id)
                    principal.role, principal.tenant_id = "platform_admin", home
                    await db.commit()
                inactive_home = await client.put(f"/api/tools/{tool_id}", json=confirmation)
                assert inactive_home.status_code == 403, inactive_home.text
                async with owner_sessionmaker() as db:
                    (await db.get(Tenant, home)).is_active = True
                    await db.commit()
            confirmed = await client.put(f"/api/tools/{tool_id}", json=confirmation)
            assert confirmed.status_code == 200, confirmed.text
            repeated = await client.put(f"/api/tools/{tool_id}", json=confirmation)
            assert repeated.status_code == 200, repeated.text
            assert "synthetic-unattributed-company-key" not in confirmed.text
            expected_peer = "Bearer synthetic-unattributed-company-key"
            assert "Public ledger" in await invoke(peer)
            assert observed[-1]["authorization"] == expected_peer
            async with owner_sessionmaker() as db:
                audits = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant,
                    AuditLog.action == "mcp.company_credential.authorized",
                    AuditLog.details["tool_id"].as_string() == str(tool_id),
                    AuditLog.details["source"].as_string() == "confirmation")))
                assert len(audits) == 1 and audits[0].user_id == admin.id
                assert "synthetic-unattributed-company-key" not in json.dumps(audits[0].details)
                policy = await db.scalar(select(TenantToolConfig).where(
                    TenantToolConfig.tenant_id == tenant, TenantToolConfig.tool_id == tool_id))
                policy.enabled = False
                await db.commit()
            disabled_confirmation = await client.put(f"/api/tools/{tool_id}", json=confirmation)
            assert disabled_confirmation.status_code == 200, disabled_confirmation.text
            async with owner_sessionmaker() as db:
                policy = await db.scalar(select(TenantToolConfig).where(
                    TenantToolConfig.tenant_id == tenant, TenantToolConfig.tool_id == tool_id))
                assert policy.enabled is False
            before_attempts = len(attempts)
            denied = await invoke(agent)
            assert "Public ledger" not in denied, denied
            assert len(attempts) == before_attempts
            enabled = await client.put(f"/api/tools/{tool_id}", json={"enabled": True})
            assert enabled.status_code == 200, enabled.text
            async with owner_sessionmaker() as db:
                policy = await db.scalar(select(TenantToolConfig).where(
                    TenantToolConfig.tenant_id == tenant, TenantToolConfig.tool_id == tool_id))
                assert policy.enabled is True
            if not stored_legacy:
                async with owner_sessionmaker() as db:
                    (await db.get(Tool, tool_id)).mcp_server_url = endpoint + "?region=unapproved"
                    (await db.scalar(select(MCPServer).where(MCPServer.tenant_id == tenant))).server_url = endpoint + "?region=unapproved"
                    await db.commit()
                changed_fact = (await client.get(review_path)).json()[0]
                review = await client.put(review_path + "/lookup/metadata-review", json={"decision": "approve",
                    "expected_fingerprint": changed_fact["metadata_fingerprint"],
                    "canonical_description": "Read a reviewed public ledger entry."})
                assert review.status_code == 200, review.text
                before_attempts = len(attempts)
                assert "configuration_required" in await invoke(peer)
                assert len(attempts) == before_attempts
                async with owner_sessionmaker() as db:
                    (await db.get(Tool, tool_id)).mcp_server_url = endpoint
                    (await db.scalar(select(MCPServer).where(MCPServer.tenant_id == tenant))).server_url = endpoint
                    await db.commit()
                restored_fact = (await client.get(review_path)).json()[0]
                restored = await client.put(review_path + "/lookup/metadata-review", json={"decision": "approve",
                    "expected_fingerprint": restored_fact["metadata_fingerprint"],
                    "canonical_description": "Read a reviewed public ledger entry."})
                assert restored.status_code == 200, restored.text
        changed = await client.put(f"/api/tools/agents/{agent}/tool-config/{tool_id}",
            json={"config": {"api_key": "synthetic-rotated-employee-key"}})
        assert changed.status_code == 200, changed.text
        if stored_legacy and not company_default and not legacy_unattributed:
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


@pytest.mark.parametrize("entry", ["native", "generic", "resources"])
async def test_company_created_credentials_rotate_without_becoming_employee_overrides(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, entry,  # noqa: F811
):
    await test_direct_import_uses_only_the_current_employee_or_explicit_company_credential(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories,
        entry, True, "config", False, False, created_company=True,
    )
