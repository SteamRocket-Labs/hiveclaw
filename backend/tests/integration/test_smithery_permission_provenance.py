"""Existing approved registry tools retain permission provenance on reauthorization."""

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


@pytest.mark.parametrize("source,change", [
    (None, "unchanged"), ("permissions", "unchanged"),
    ("permissions", "expanded"), ("permissions", "cleared"),
    ("annotation_permissions", "expanded"), ("annotation_permissions", "cleared"),
    ("annotation_scopes", "expanded"), ("annotation_scopes", "cleared"),
])
async def test_approved_smithery_reauthorization_rechecks_exact_permission_evidence(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, scoped_runtime_factories, source, change,  # noqa: F811
):
    from app import database
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.user import User
    from app.models.tool import Tool
    from app.services import agent_tools, resource_discovery as discovery
    from app.services.mcp_server_service import register_imported_server
    from app.tools.handlers.mcp import import_mcp_server

    app, (tenant_id, _owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    app.include_router(router, prefix="/api")
    async with owner_sessionmaker() as db:
        admin_id = uuid.uuid4()
        admin = User(id=admin_id, tenant_id=tenant_id, username="reviewer-" + admin_id.hex,
            email=admin_id.hex + "@mcp-review.test", display_name="Company reviewer",
            password_hash="synthetic", role="org_admin")
        db.add(admin)
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
    endpoint = "https://mcp.synthetic.test/ledger/" + agent_id.hex
    display_name, registry_id = "Synthetic Ledger", "synthetic/ledger"

    def descriptor(name, permissions):
        result = {"name": name, "description": "Read a reviewed ledger entry.",
            "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}}}
        if source == "permissions":
            result["permissions"] = permissions
        elif source is not None:
            result["annotations"] = {"permissions" if source == "annotation_permissions" else "scopes": permissions}
        return result

    remote = {"tools": [descriptor("lookup", ["ledger:read"]), descriptor("status", ["ledger:status"])]}

    def respond(request):
        if request.url.path == "/servers":
            return httpx.Response(200, json={"servers": [{"qualifiedName": registry_id,
                "displayName": display_name, "remote": True}]})
        assert request.url.path == "/servers/" + registry_id
        return httpx.Response(200, json={"deploymentUrl": endpoint, "tools": remote["tools"]})

    async def existing_connection(_api_key, actual_url, actual_name):
        assert actual_url == endpoint and actual_name == display_name
        return {"namespace": "synthetic", "connection_id": "ledger"}

    original_client = httpx.AsyncClient
    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(discovery, "SMITHERY_API_BASE", "http://127.0.0.1:55103")
    monkeypatch.setattr(discovery, "_ensure_smithery_connection", existing_connection)
    initial = await discovery.import_mcp_from_smithery(registry_id, agent_id,
        config={"smithery_api_key": "synthetic-registry-key"})
    assert "Imported MCP server" in initial, initial
    # Existing company-approved server and employee consent are the starting configuration.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        registration = await register_imported_server(db, tenant_id, display_name, endpoint)
        assert registration is not None
        rows = list(await db.scalars(select(Tool).where(Tool.tenant_id == tenant_id, Tool.type == "mcp")))
        names = {row.mcp_tool_name: row.name for row in rows}
    server_id = registration["id"]
    path = f"/api/enterprise/mcp-servers/{server_id}/tools"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry-review.test") as client:
        before = await client.get(path)
        assert before.status_code == 200, before.text
        fingerprints = {row["tool_name"]: row["metadata_fingerprint"] for row in before.json()}
        for row in before.json():
            approved = await client.put(path + "/" + row["tool_name"] + "/metadata-review", json={
                "decision": "approve", "expected_fingerprint": row["metadata_fingerprint"],
                "canonical_description": "Read a reviewed ledger entry."})
            assert approved.status_code == 200, approved.text
        enabled = await client.put(f"/api/agents/{agent_id}/mcp-servers/{server_id}",
            json={"enabled": True, "default_tool_mode": "auto", "always_load": True})
        assert enabled.status_code == 200, enabled.text
        for name in names:
            policy = await client.put(f"/api/agents/{agent_id}/mcp-servers/{server_id}/tools/{name}/policy",
                json={"mode": "auto"})
            assert policy.status_code == 200, policy.text
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=list(names.values()))
        assert set(names.values()).issubset({row["function"]["name"] for row in available})
        permissions = ["ledger:read", "ledger:write"] if change == "expanded" else [] if change == "cleared" else ["ledger:read"]
        remote["tools"][0] = descriptor("lookup", permissions)
        reauthorized = await import_mcp_server(agent_id, {"server_id": registry_id,
            "reauthorize": True, "config": {"smithery_api_key": "synthetic-registry-key"}})
        assert "Imported MCP server" in reauthorized, reauthorized
        after = await client.get(path)
        assert after.status_code == 200, after.text
        facts = {row["tool_name"]: row for row in after.json()}
        available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=list(names.values()))
        available_names = {row["function"]["name"] for row in available}
        old_review = await client.put(path + "/lookup/metadata-review", json={"decision": "approve",
            "expected_fingerprint": fingerprints["lookup"], "canonical_description": "Read a reviewed ledger entry."})
        observed = {"source": source, "change": change, "before": before.json(), "after": facts,
            "available": sorted(available_names), "old_review_status": old_review.status_code}
        (tmp_path / "registry-permission-provenance.json").write_text(json.dumps(observed, indent=2))
        changed = change != "unchanged"
        assert (facts["lookup"]["metadata_fingerprint"] != fingerprints["lookup"]) is changed, observed
        assert facts["lookup"]["runtime_approved"] is (not changed), observed
        assert (names["lookup"] in available_names) is (not changed), observed
        assert old_review.status_code == (409 if changed else 200), observed
        assert names["status"] in available_names and facts["status"]["runtime_approved"] is True, observed
        assert facts["status"]["metadata_fingerprint"] == fingerprints["status"]
        assert facts["lookup"]["activation"].get("permissions", []) == (permissions if source else []), observed
        if changed:
            approved = await client.put(path + "/lookup/metadata-review", json={"decision": "approve",
                "expected_fingerprint": facts["lookup"]["metadata_fingerprint"],
                "canonical_description": "Read a reviewed ledger entry."})
            assert approved.status_code == 200, approved.text
            available = await agent_tools.get_agent_tools_for_llm(agent_id, requested_names=list(names.values()))
            assert set(names.values()).issubset({row["function"]["name"] for row in available})
    async with app_user_sessionmaker() as db:
        assert (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
