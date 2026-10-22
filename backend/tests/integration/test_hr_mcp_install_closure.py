"""Confirmed employee capabilities stay reviewable on fresh and reused installs."""

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import get_db, pin_rls_tenant_context
from tests.integration.test_m1g_originated_creation_postgres import (  # noqa: F401
    _arguments, _client, direct_origin, origin,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def registry_configuration(owner_sessionmaker):
    from app.models.tool import Tool
    from app.services.resource_discovery import clear_provider_config_cache

    async with owner_sessionmaker() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "discover_resources", Tool.tenant_id.is_(None)))
        created = tool is None
        if created:
            tool = Tool(name="discover_resources", display_name="Synthetic registry configuration", type="builtin")
            db.add(tool)
        previous = tool.config
        tool.config = {**(previous or {}), "smithery_api_key": "synthetic-registry-key"}
        await db.commit()
        tool_id = tool.id
    clear_provider_config_cache()
    yield
    async with owner_sessionmaker() as db:
        tool = await db.get(Tool, tool_id)
        if created:
            await db.delete(tool)
        else:
            tool.config = previous
        await db.commit()
    clear_provider_config_cache()


@pytest.mark.parametrize("registry_ref", ["synthetic/ledger", "ledger"])
async def test_hr_installs_and_reuses_reviewable_mcp_for_only_requested_employees(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, registry_ref, registry_configuration,  # noqa: F811
):
    from app.api.mcp_servers import router
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.hr_creation import HrCreationDraft
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
    from app.models.runtime_task import RuntimeTask
    from app.models.tool import AgentTool, Tool
    from app.models.user import User
    from app.services import agent_tools, resource_discovery as discovery
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning
    from app.tools.handlers.hr import preview_agent_blueprint
    from app.tools.runtime import ToolExecutionRequest

    user, context, _model_id, _input_id = direct_origin
    admin_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        admin = User(id=admin_id, tenant_id=user.tenant_id, username="hr-review-" + admin_id.hex,
            email=admin_id.hex + "@hr-mcp.test", display_name="Company reviewer", password_hash="synthetic", role="org_admin")
        db.add(admin)
        await db.commit()
    async with app_user_sessionmaker() as db:
        assert (await db.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)

    async def no_model(*_args, **_kwargs):
        return None, "synthetic_transport"

    async def refinement(**kwargs):
        return {"role_description": kwargs["role_description"], "boundaries": kwargs["boundaries"]}

    async def no_external_office_probe():
        return False

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_model)
    monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", refinement)
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", no_external_office_probe)
    endpoint = "http://127.0.0.1:55114/mcp/" + context.agent_id.hex
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert request.url.port == 55115
        if request.url.path == "/servers":
            return httpx.Response(200, json={"servers": [{"qualifiedName": "synthetic/ledger",
                "displayName": "Ledger", "remote": True}]})
        assert request.url.path == "/servers/synthetic/ledger"
        return httpx.Response(200, json={"deploymentUrl": endpoint, "tools": [{"name": "lookup",
            "description": "Read a ledger entry.", "inputSchema": {"type": "object", "properties": {}},
            "permissions": ["ledger:read"]}]})

    original_client = httpx.AsyncClient

    async def existing_connection(_api_key, actual_url, actual_name):
        assert actual_url == endpoint and actual_name == "Ledger"
        return {"namespace": "synthetic", "connection_id": "hr-ledger"}

    monkeypatch.setattr(discovery.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(discovery, "SMITHERY_API_BASE", "http://127.0.0.1:55115")
    monkeypatch.setattr(discovery, "_ensure_smithery_connection", existing_connection)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, user.tenant_id)
            yield db

    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_db] = scoped_db
    employees = []
    server_id = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hr-mcp.test") as client:
        for ordinal in range(2):
            preview = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
                tool_name="preview_agent_blueprint", context=context,
                arguments={**_arguments(), "name": f"Ledger employee {ordinal}", "mcp_server_ids": [registry_ref]},
            )))
            assert preview["status"] == "preview", preview
            path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{preview['blueprint_id']}"
            async with _client(user, app_user_sessionmaker) as source:
                confirmed = await source.post(path + "/confirm", json={"blueprint_version": preview["blueprint_version"]})
                assert confirmed.status_code == 200, confirmed.text
                task_id = uuid.UUID(confirmed.json()["provisioning_task_id"])
                async with owner_sessionmaker() as db:
                    task = await db.get(RuntimeTask, task_id)
                    task.status, task.claimed_by, task.claim_version = "running", "hr-mcp-test-worker", 1
                    await db.commit()
                assert await execute_claimed_hr_provisioning(task_id) == "completed"
                assert await execute_claimed_hr_provisioning(task_id) == "completed"
                completed = await source.get(path)
                assert completed.status_code == 200, completed.text
                receipt = completed.json()
                assert receipt["draft_status"] == "completed" and receipt["session_id"] == context.session_id
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, user.tenant_id)
                draft = await db.get(HrCreationDraft, uuid.UUID(preview["blueprint_id"]))
                employee_id = draft.created_agent_id
                employees.append(employee_id)
                tools = list(await db.scalars(select(Tool).where(Tool.tenant_id == user.tenant_id, Tool.type == "mcp")))
                assert len(tools) == 1, receipt
                tool_name = tools[0].name
                servers = list(await db.scalars(select(MCPServer).where(MCPServer.tenant_id == user.tenant_id)))
                assignments = list(await db.scalars(select(AgentMCPServerAssignment).where(
                    AgentMCPServerAssignment.tenant_id == user.tenant_id)))
                snapshot = {"registry_ref": registry_ref, "ordinal": ordinal, "receipt": receipt,
                    "server_ids": [str(row.id) for row in servers],
                    "assigned_agents": [str(row.agent_id) for row in assignments], "requests": list(requests)}
                (tmp_path / f"hr-mcp-{ordinal}.json").write_text(json.dumps(snapshot, indent=2))
                assert len(servers) == 1 and servers[0].server_url == endpoint, snapshot
                assert {row.agent_id for row in assignments} == set(employees), snapshot
                if server_id is None:
                    server_id = str(servers[0].id)
                else:
                    assert str(servers[0].id) == server_id
                assert (await db.get(Agent, employee_id)).owner_user_id == user.id
                assert not list(await db.scalars(select(AgentTool).where(AgentTool.agent_id == context.agent_id,
                    AgentTool.tool_id == tools[0].id)))
            listed = await client.get(f"/api/agents/{employee_id}/mcp-servers")
            assert listed.status_code == 200 and len(listed.json()) == 1, (snapshot, listed.text)
            if ordinal == 0:
                review_path = f"/api/enterprise/mcp-servers/{server_id}/tools"
                pending = await client.get(review_path)
                assert pending.status_code == 200 and len(pending.json()) == 1, pending.text
                fact = pending.json()[0]
                assert fact["runtime_approved"] is False and fact["activation"].get("permissions") == ["ledger:read"]
                available = await agent_tools.get_agent_tools_for_llm(employee_id, requested_names=[tool_name])
                assert tool_name not in {row["function"]["name"] for row in available}
                reviewed = await client.put(review_path + "/lookup/metadata-review", json={"decision": "approve",
                    "expected_fingerprint": fact["metadata_fingerprint"], "canonical_description": "Read a reviewed ledger entry."})
                assert reviewed.status_code == 200, reviewed.text
                requests_before_reuse = list(requests)
            else:
                assert requests == requests_before_reuse
            available = await agent_tools.get_agent_tools_for_llm(employee_id, requested_names=[tool_name])
            assert tool_name in {row["function"]["name"] for row in available}, snapshot


async def test_reuse_requires_unambiguous_current_company_registry_identity(
    origin, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.tenant import Tenant
    from app.models.tool import AgentTool, Tool
    from app.services.capability_reuse_service import reuse_existing_mcp_server_for_agent

    user, context = origin
    other_tenant = uuid.uuid4()
    rows = []
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=other_tenant, name="Other registry company", slug="other-registry-" + other_tenant.hex))
        await db.flush()
        for tenant_id, package, name in [
            (user.tenant_id, "alpha/ledger", "mcp__alpha_ledger__lookup"),
            (user.tenant_id, "beta/ledger", "mcp__beta_ledger__lookup"),
            (user.tenant_id, "alpha/ledger_extra", "mcp_ledger_unrelated_lookup"),
            (other_tenant, "alpha/ledger", "mcp__alpha_ledger__lookup"),
        ]:
            tool = Tool(id=uuid.uuid4(), tenant_id=tenant_id, name=name, display_name=package,
                type="mcp", mcp_server_name=package, mcp_server_url="http://127.0.0.1:55116/" + uuid.uuid4().hex,
                mcp_tool_name="lookup", config={"package": package})
            rows.append(tool)
            db.add(tool)
        legacy = Tool(id=uuid.uuid4(), tenant_id=user.tenant_id, name="mcp_legacy_lookup", display_name="Legacy",
            type="mcp", mcp_server_name="Legacy", mcp_server_url="http://127.0.0.1:55116/legacy", config={})
        db.add(legacy)
        await db.commit()
    assert await reuse_existing_mcp_server_for_agent(
        agent_id=context.agent_id, tenant_id=user.tenant_id, server_id="ledger") is None
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, user.tenant_id)
        assert not list(await db.scalars(select(AgentTool).where(AgentTool.agent_id == context.agent_id)))
    selected = await reuse_existing_mcp_server_for_agent(
        agent_id=context.agent_id, tenant_id=user.tenant_id, server_id="alpha/ledger")
    assert selected is not None and selected["tool_count"] == 1
    unique_alias = await reuse_existing_mcp_server_for_agent(
        agent_id=context.agent_id, tenant_id=user.tenant_id, server_id="ledger_extra")
    assert unique_alias is not None and unique_alias["tool_count"] == 1
    assert await reuse_existing_mcp_server_for_agent(
        agent_id=context.agent_id, tenant_id=user.tenant_id, server_id="other/legacy") is None
    compatible = await reuse_existing_mcp_server_for_agent(
        agent_id=context.agent_id, tenant_id=user.tenant_id, server_id="legacy")
    assert compatible is not None and compatible["tool_count"] == 1
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, user.tenant_id)
        assigned = list(await db.scalars(select(AgentTool.tool_id).where(AgentTool.agent_id == context.agent_id)))
        assert set(assigned) == {rows[0].id, rows[2].id, legacy.id}
