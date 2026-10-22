"""Registry providers use platform config and exact per-agent overrides."""

import json
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text

from app.database import pin_rls_tenant_context, tenant_scoped_session
from tests.integration.test_m1g6_role_configuration_postgres import _app

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("mode", ["absent", "private_only", "platform_only", "platform_and_private"])
async def test_registry_transport_and_key_writeback_use_platform_rows(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, mode,
):
    from app.models.tool import AgentTool, Tool
    from app.services import resource_discovery as discovery

    _app_instance, (tenant_id, _owner_id, agent_id, _session_id, _run_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker)
    platform, private = {}, {}
    try:
        async with owner_sessionmaker() as db:
            for name in ["discover_resources", "import_mcp_server"]:
                assert not list(await db.scalars(select(Tool).where(Tool.name == name)))
                for scope, present in [("platform", "platform" in mode), ("private", "private" in mode)]:
                    if not present:
                        continue
                    row = Tool(id=uuid.uuid4(), name=name, display_name=name, type="builtin", enabled=True,
                        tenant_id=None if scope == "platform" else tenant_id,
                        config={"smithery_api_key": "synthetic-" + scope + "-smithery",
                            "modelscope_api_token": "synthetic-" + scope + "-modelscope"})
                    db.add(row)
                    (platform if scope == "platform" else private)[name] = row.id
            await db.commit()

        def scoped(actual_tenant, **kwargs):
            assert actual_tenant == tenant_id
            return tenant_scoped_session(actual_tenant, session_factory=app_user_sessionmaker, **kwargs)

        async def resolve(actual_agent):
            assert actual_agent == agent_id
            return tenant_id

        monkeypatch.setattr(discovery, "async_session", app_user_sessionmaker)
        monkeypatch.setattr(discovery, "tenant_scoped_session", scoped)
        monkeypatch.setattr(discovery, "resolve_tenant_for_agent", resolve)
        monkeypatch.setattr(discovery, "SMITHERY_API_BASE", "http://127.0.0.1:55101")
        monkeypatch.setattr(discovery, "MODELSCOPE_API_BASE", "http://127.0.0.1:55102")
        discovery.clear_provider_config_cache()
        requests = []

        def respond(request):
            requests.append({"port": request.url.port, "path": request.url.path,
                "authorization": request.headers.get("authorization"), "cookie": request.headers.get("cookie")})
            if request.url.params.get("q") == "synthetic-import":
                return httpx.Response(404, json={})
            if request.url.port == 55101:
                return httpx.Response(200, json={"servers": [{"qualifiedName": "synthetic/server",
                    "displayName": "Synthetic registry resource", "remote": True, "verified": True}]})
            return httpx.Response(200, json={"success": True, "data": {"mcp_server_list": [{
                "id": "synthetic-modelscope", "name": "Synthetic model resource", "is_hosted": True}]}})

        original_client = httpx.AsyncClient
        monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs))
        result = await discovery.search_registries("synthetic lookup")
        selected = await discovery._get_smithery_api_key(agent_id)
        async with app_user_sessionmaker() as db:
            assert (await db.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
        observed = {"mode": mode, "requests": requests, "result": result, "agent_selected": selected}
        (tmp_path / "registry-platform-source.json").write_text(json.dumps(observed, indent=2))
        expected = "synthetic-platform-smithery" if platform else ""
        assert requests[0]["authorization"] == ("Bearer " + expected if expected else None), observed
        assert selected == expected, observed
        model_requests = [row for row in requests if row["port"] == 55102]
        assert len(model_requests) == int(bool(platform)), observed
        if platform:
            assert model_requests[0]["authorization"] == "Bearer synthetic-platform-modelscope"
            assert model_requests[0]["cookie"] == "m_session_id=synthetic-platform-modelscope"
        assert "synthetic-private" not in json.dumps(requests), observed
        assert "synthetic-platform-smithery" not in result and "synthetic-private" not in result

        imported = await discovery.import_mcp_from_smithery("synthetic-import", agent_id,
            config={"smithery_api_key": "synthetic-agent-override"})
        assert "404" in imported, imported
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            assignments = list(await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent_id)))
            assert {row.tool_id for row in assignments} == set(platform.values())
            assert all(row.config["smithery_api_key"] == "synthetic-agent-override" for row in assignments)
            shadow = list(await db.scalars(select(Tool).where(Tool.id.in_(private.values()))))
            assert all(row.config["smithery_api_key"] == "synthetic-private-smithery" for row in shadow)
        discovery.clear_provider_config_cache()
        assert await discovery._get_smithery_api_key(agent_id) == ("synthetic-agent-override" if platform else "")
        discovery.clear_provider_config_cache()
    finally:
        discovery.clear_provider_config_cache()
        ids = [*platform.values(), *private.values()]
        if ids:
            async with owner_sessionmaker() as db:
                await db.execute(delete(AgentTool).where(AgentTool.tool_id.in_(ids)))
                await db.execute(delete(Tool).where(Tool.id.in_(ids)))
                await db.commit()
