"""Final D2 credential findings through PostgreSQL and the real spawn tool."""

from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.mcp_server import MCPServer
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services import mcp_oauth
from app.services.mcp_server_service import resolve_mcp_oauth_bearer
from tests.integration.test_d2_requester_connections_postgres import _context, _seed
from tests.integration.test_d2_round2_postgres import _app, _personal

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("origin", ["headless", "headless_no_company", "member", "nested_headless"])
async def test_spawned_mcp_work_keeps_the_root_requester_scope(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, background, origin
):
    from app.runtime.workflow_engine import LeafOutcome
    from app.services.workflow_runtime_service import WorkflowRuntimeService
    from app.services import agent_tools, subagent_run_service
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.tools.handlers import subagent
    from app.tools.runtime import ToolExecutionRequest
    from tests.services.test_workflow_runtime_service import _definition
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _patch_invoker_seams
    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import _patch_environment_seams
    import app.runtime.invoker as invoker

    tenant, user_id, bot, _, session, _, server_id, _, name = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await _personal(db, tenant, user_id, server_id, "owner-personal")
        parent = await db.get(MCPServer, server_id)
        if origin != "headless_no_company":
            parent.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company"))}}
            parent.auth_status = "configured"
        agent = await db.get(Agent, bot)
        agent.owner_user_id = user_id
        await db.commit()

    @asynccontextmanager
    async def scoped():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db

    _patch_environment_seams(monkeypatch, app_user_sessionmaker)
    for module in ("app.services.tenant_resolver", "app.tools.resolver", "app.tools.governance_resolver"):
        monkeypatch.setattr(module + ".async_session", app_user_sessionmaker)
    monkeypatch.setattr(subagent_run_service, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    model = SimpleNamespace(provider="openai", model="local-spawn", api_key="local", base_url=None, max_output_tokens=2048)

    async def parent_runtime(_agent_id):
        return model, None, agent

    async def worker_runtime(_agent_id):
        return {"ctx_kwargs": {"parent_agent_id": bot, "tenant_id": tenant, "model": model}}

    async def noop(**kwargs):
        pass

    monkeypatch.setattr(subagent, "_resolve_parent_runtime", parent_runtime)
    monkeypatch.setattr(subagent, "memory_store_for_tenant", lambda _: None)
    monkeypatch.setattr(subagent_run_service, "_resolve_parent_runtime", worker_runtime)
    monkeypatch.setattr(subagent_run_service, "_memory_store_for_worker_spec", lambda **kwargs: None)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", noop)
    client = ScriptedLLMClient([])
    _patch_invoker_seams(monkeypatch, client, {"tenant_id": tenant, "data_root": tmp_path,
        "agent_root": tmp_path / str(bot)}, scoped)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *args, **kwargs: [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}])
    seen = []

    class Transport:
        def __init__(self, url, *, api_key):
            seen.append(api_key)

        async def call_tool(self, name, arguments):
            return "SPAWN_DOCUMENT_RESULT"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)

    async def spawn(context):
        # Only model responses and remote transport are fake; admission, spawn,
        # child invocation, tool dispatch and credential resolution are real.
        client.script.extend([{"tool_calls": [{"id": uuid.uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": "{}"}}], "finish_reason": "tool_calls"},
            {"content": "Finished the document request.", "finish_reason": "stop"}])
        result = json.loads(await subagent.spawn_subagent_tool(ToolExecutionRequest(
            tool_name="spawn_subagent", context=context,
            arguments={"task": "Read the document", "subagent_type": "explorer", "run_in_background": background})))
        assert result["ok"], result
        if background:
            async with scoped() as db:
                claimed = await RuntimeTaskClaimService(db=db, worker_id="d2-r3", task_types=("subagent",)).claim_available(batch_size=10)
                await db.commit()
                task = next(row for row in claimed if row.id == uuid.UUID(result["run_id"]))
                version = task.claim_version
            fence = set_runtime_task_fence(task_id=result["run_id"], worker_id="d2-r3", claim_version=version)
            try:
                assert await subagent_run_service.dispatch_persisted_subagent_run(result["run_id"])
            finally:
                reset_runtime_task_fence(fence)
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, uuid.UUID(result["run_id"]))
                assert task.status == "completed", (task.status, task.result_summary)
        else:
            assert result["status"] == "completed", result

    if origin == "member":
        await spawn(_context(tenant, user_id, bot, session))
    else:
        run_id = uuid.uuid4()

        async def leaf(request):
            async with owner_sessionmaker() as db:
                root = await db.get(RuntimeTask, run_id)
                assert root.metadata_json["headless_session_created"] is True and root.root_user_id == user_id
                task_id = root.id
                if origin == "nested_headless":
                    # A durable ancestor may itself point at the workflow root.
                    task_id = uuid.uuid4()
                    db.add(RuntimeTask(id=task_id, tenant_id=tenant, parent_agent_id=bot, task_type="subagent",
                        status="completed", root_user_id=user_id, root_runtime_task_id=run_id))
                    await db.commit()
                context = replace(_context(tenant, root.root_user_id, bot, root.parent_session_id), runtime_task_id=str(task_id))
            await spawn(context)
            return LeafOutcome(ok=True, output="Spawn completed")

        definition = _definition()
        definition["steps"] = definition["steps"][:1]
        outcome = await WorkflowRuntimeService(session_factory=owner_sessionmaker).start_run(tenant_id=tenant,
            definition_data=definition, args={"target": "documents"}, leaf_executor=leaf, agent_id=bot, run_id=run_id)
        assert outcome.outcome.status == "completed", outcome.outcome

    if origin == "headless_no_company":
        assert seen == []
        assert "auth_required" in json.dumps(client.requests)
    else:
        assert seen and set(seen) == ({"owner-personal"} if origin == "member" else {"company"}), seen
        assert "SPAWN_DOCUMENT_RESULT" in json.dumps(client.requests)
    async with owner_sessionmaker() as db:
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant,
            AuditLog.action == "mcp.account.credentials_resolved")))
        assert len(audits) == (1 if origin == "member" else 0)
        if audits:
            assert audits[0].user_id == user_id and audits[0].agent_id == bot


@pytest.mark.parametrize("root_id", ["", "not-a-uuid", str(uuid.uuid4())])
async def test_unresolvable_root_does_not_infer_a_personal_requester(owner_sessionmaker, app_user_sessionmaker, root_id):
    tenant, user_id, bot, _, session, _, server_id, tool, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await _personal(db, tenant, user_id, server_id, "owner-personal")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        bearer, error = await resolve_mcp_oauth_bearer(db, tenant, "https://mcp.example.test/sse", tool_id=tool,
            agent_id=bot, execution_context=replace(_context(tenant, user_id, bot, session), runtime_task_id=root_id))
        assert bearer is None and error, "R3 S4: unknown roots cannot authorize a personal connection"


@pytest.mark.parametrize("company", [True, False])
async def test_rotate_after_revocation_never_restores_the_removed_personal_token(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, company
):
    from app.api.mcp_servers import router
    tenant, user_id, bot, _, session, _, server_id, tool, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        account = await _personal(db, tenant, user_id, server_id)
        account.config_json = {"oauth": {"token_endpoint": "https://auth.test/token", "client_id": "documents",
            "token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("expired-personal", refresh_token="refresh", expires_at=0))}}
        if company:
            parent = await db.get(MCPServer, server_id)
            parent.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company"))}}
            parent.auth_status = "configured"
        user = await db.get(User, user_id)
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=_app(user, tenant, app_user_sessionmaker, router)), base_url="https://hive.test") as http:
        async def rotate_after_revocation(**kwargs):
            assert (await http.post(f"/api/mcp-accounts/{account.id}/revoke")).status_code == 200
            return mcp_oauth.OAuthTokenSet("rotated-personal")

        monkeypatch.setattr(mcp_oauth, "refresh_access_token", rotate_after_revocation)

        async def resolve():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant)
                return await resolve_mcp_oauth_bearer(db, tenant, "https://mcp.example.test/sse", tool_id=tool,
                    agent_id=bot, execution_context=_context(tenant, user_id, bot, session))

        bearer, error = await resolve()
        assert bearer is None and "changed during refresh" in error
        # R3 ruling 2 / plan section 11 item 12: company connections are on by default.
        bearer, error = await resolve()
        assert (bearer, error) == ("company", None) if company else bearer is None and error
    async with owner_sessionmaker() as db:
        assert not (await db.get(MCPServer, account.id)).config_json["oauth"].get("token")
        assert not list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant,
            AuditLog.action == "mcp.account.credentials_resolved")))


async def test_unconfirmed_company_key_hint_uses_the_conversation_card():
    from app.services.mcp_authz import MCPStoredCredentialRequired
    from app.tools.handlers.mcp import _render_mcp_authz_error
    hint = _render_mcp_authz_error("call_mcp_tool", MCPStoredCredentialRequired("Company key requires confirmation"))
    assert "The member can connect their account through the card in this conversation." in hint


async def test_personal_token_put_rejects_whitespace(owner_sessionmaker, app_user_sessionmaker):
    from app.api.mcp_servers import router
    tenant, user_id, _, _, _, _, server_id, _, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        account = await _personal(db, tenant, user_id, server_id, "existing-personal")
        user = await db.get(User, user_id)
    async with AsyncClient(transport=ASGITransport(app=_app(user, tenant, app_user_sessionmaker, router)), base_url="https://hive.test") as http:
        response = await http.put(f"/api/mcp-accounts/{account.id}/token", json={"access_token": " \t\n "})
        assert response.status_code == 400, response.text
    async with owner_sessionmaker() as db:
        token = mcp_oauth.decrypt_token_set((await db.get(MCPServer, account.id)).config_json["oauth"]["token"])
        assert token.access_token == "existing-personal"
