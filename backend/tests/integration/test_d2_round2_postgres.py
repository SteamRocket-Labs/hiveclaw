"""Round-two findings on the real credential, workflow and answer paths."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.mcp_server import MCPServer
from app.models.runtime_task import RuntimeTask
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services import mcp_oauth
from app.services.mcp_accounts import create_personal_account
from app.services.mcp_server_service import resolve_mcp_oauth_bearer
from tests.integration.test_d2_requester_connections_postgres import _context, _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _personal(db, tenant, user_id, server_id, token="personal-bearer"):
    public = await create_personal_account(db, user=await db.get(User, user_id), tenant_id=tenant,
        server_id=server_id, account_id=uuid.uuid4(), name="Documents")
    account = await db.get(MCPServer, uuid.UUID(public["id"]))
    account.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet(token)),
        "authorization_id": str(uuid.uuid4())}}
    account.auth_status = "configured"
    await db.commit()
    return account


def _app(user, tenant, factory, *routers):
    app = FastAPI()
    for router in routers:
        app.include_router(router, prefix="/api")
    async def database():
        async with tenant_scoped_session(tenant, session_factory=factory) as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.parametrize("scope", ["personal", "company"])
async def test_b2_automatic_personal_use_is_audited_once_and_company_use_is_not(
    owner_sessionmaker, app_user_sessionmaker, scope
):
    tenant, user, bot, _, session, _, server, tool, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        account = await _personal(db, tenant, user, server) if scope == "personal" else await db.get(MCPServer, server)
        account.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("d2-audit-private-token"))}}
        account.auth_status = "configured"
        account_id = account.id
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert await resolve_mcp_oauth_bearer(db, tenant, "https://mcp.example.test/sse", tool_id=tool,
            agent_id=bot, execution_context=_context(tenant, user, bot, session)) == ("d2-audit-private-token", None)
    async with owner_sessionmaker() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant,
            AuditLog.action == "mcp.account.credentials_resolved")))
        assert len(rows) == (1 if scope == "personal" else 0), "B2: automatic personal resolution must be audited"
        if rows:
            assert (rows[0].user_id, rows[0].agent_id, rows[0].details["account_id"]) == (user, bot, str(account_id))
            assert "d2-audit-private-token" not in str(rows[0].details)


@pytest.mark.parametrize("case", ["creation", "legacy_duplicates"])
async def test_s2_creation_reuses_one_account_and_legacy_duplicates_pick_latest_connection(
    owner_sessionmaker, app_user_sessionmaker, case
):
    tenant, user_id, bot, _, session, _, server_id, tool, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        first = await _personal(db, tenant, user_id, server_id, "first")
        if case == "creation":
            second = await create_personal_account(db, user=await db.get(User, user_id), tenant_id=tenant,
                server_id=server_id, account_id=uuid.uuid4(), name="Other tab")
            assert second["id"] == str(first.id), "S2: another conversation must reuse the member's existing account"
            return
        # Legacy rows remain removable in settings; connection time, not creation time, decides reuse.
        latest = MCPServer(id=uuid.uuid4(), tenant_id=tenant, parent_server_id=server_id, owner_user_id=user_id,
            name="Legacy duplicate", server_key=uuid.uuid4().hex, server_url=first.server_url, auth_status="configured",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
            config_json={"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("latest"))}})
        db.add(latest)
        first.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db.commit()
        latest_id = latest.id
    from app.services.session_mcp_configuration import mcp_configuration_snapshot
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert await resolve_mcp_oauth_bearer(db, tenant, first.server_url, tool_id=tool, agent_id=bot,
            execution_context=_context(tenant, user_id, bot, session)) == ("latest", None)
        snapshot = await mcp_configuration_snapshot(db, target=await db.get(Agent, bot), server_id=server_id,
            user=await db.get(User, user_id))
        assert snapshot["account"]["id"] == str(latest_id)
        # R3 item 5: card snapshots no longer list accounts; settings/DB still expose legacy rows.
        accounts = list(await db.scalars(select(MCPServer).where(MCPServer.parent_server_id == server_id)))
        assert {row.id for row in accounts} == {first.id, latest_id}
        assert not {"account_use_enabled", "can_grant_account", "grant_id", "grant_version"} & snapshot.keys()
        assert "accounts" not in snapshot


async def test_s3_reconnect_uses_new_authorization_without_rebinding_existing_conversation(
    owner_sessionmaker, app_user_sessionmaker
):
    from app.services.mcp_accounts import revoke_account
    tenant, user_id, bot, _, session, _, server_id, tool, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        first = await _personal(db, tenant, user_id, server_id, "before")
        # R3 item 5: execution uses the requester's connection without a work binding.
    async def resolve():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            return await resolve_mcp_oauth_bearer(db, tenant, first.server_url, tool_id=tool, agent_id=bot,
                execution_context=_context(tenant, user_id, bot, session))
    assert await resolve() == ("before", None)
    async with owner_sessionmaker() as db:
        await revoke_account(db, account=await db.get(MCPServer, first.id))
    token, error = await resolve()
    assert token is None and error
    async with owner_sessionmaker() as db:
        await _personal(db, tenant, user_id, server_id, "after")
    assert await resolve() == ("after", None), "S3: a stale work selection must not block a reconnected account"


async def test_s4_headless_workflow_tool_uses_company_connection(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.runtime.workflow_engine import LeafOutcome
    from app.services.workflow_runtime_service import WorkflowRuntimeService
    from tests.services.test_workflow_runtime_service import _definition
    from app.tools.handlers import mcp
    tenant, user_id, bot, _, _, _, server_id, _, name = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await _personal(db, tenant, user_id, server_id, "owner-only")
        parent = await db.get(MCPServer, server_id)
        parent.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company"))}}
        parent.auth_status = "configured"
        await db.commit()
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda tid, **kwargs: tenant_scoped_session(tid, session_factory=app_user_sessionmaker))
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    seen = []
    class Transport:
        def __init__(self, url, *, api_key):
            seen.append(api_key)
        async def call_tool(self, name, arguments):
            return "Company document"
    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    run_id = uuid.uuid4()
    async def leaf(request):
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.metadata_json["headless_session_created"] is True and task.root_user_id == user_id
            context = replace(_context(tenant, task.root_user_id, bot, task.parent_session_id), runtime_task_id=str(run_id))
        result = await mcp.call_mcp_tool(bot, {"tool_name": name}, execution_context=context)
        assert result == "Company document", result
        return LeafOutcome(ok=True, output=result)
    handle = await WorkflowRuntimeService(session_factory=owner_sessionmaker).start_run(tenant_id=tenant,
        definition_data=_definition(), args={"target": "documents"}, leaf_executor=leaf, agent_id=bot, run_id=run_id)
    assert handle.outcome.status == "completed"
    assert seen == ["company", "company"], "S4: a session-owner fallback does not authorize personal credentials"


async def test_s5_stale_bot_key_does_not_hide_company_key_and_new_bot_keys_are_rejected(
    owner_sessionmaker, app_user_sessionmaker
):
    from app.api.tools import router
    from app.services.mcp_authz import record_mcp_company_credential, resolve_mcp_api_key
    tenant, user_id, bot, _, _, _, _, tool_id, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "org_admin"
        tool = await db.get(Tool, tool_id)
        tool.config = {**(tool.config or {}), "api_key": "company-key"}
        await pin_rls_tenant_context(db, tenant)
        await record_mcp_company_credential(db, tool=tool, actor_id=user_id, source="confirmation")
        db.add(AgentTool(tenant_id=tenant, agent_id=bot, tool_id=tool_id, config={"api_key": "stale-key"}))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        assert await resolve_mcp_api_key(db, tool=await db.get(Tool, tool_id), tenant_id=tenant,
            agent_config={"api_key": "stale-key"}) == "company-key"
    async with AsyncClient(transport=ASGITransport(app=_app(user, tenant, app_user_sessionmaker, router)), base_url="https://hive.test") as http:
        result = await http.put(f"/api/tools/agents/{bot}/tool-config/{tool_id}", json={"config": {"api_key": "new-key"}})
        assert result.status_code == 400 and "new-key" not in result.text, "S5: personal keys belong to member connections"


async def test_s6_token_save_updates_endpoint_caps_input_and_audits_without_secret(owner_sessionmaker, app_user_sessionmaker):
    from app.api.mcp_servers import router
    tenant, user_id, bot, _, session, _, server_id, tool_id, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        account = await _personal(db, tenant, user_id, server_id)
        user = await db.get(User, user_id)
        parent = await db.get(MCPServer, server_id)
        parent.server_url = "https://mcp.example.test/new-endpoint"
        parent.transport = "streamable_http"
        tool = await db.get(Tool, tool_id)
        tool.mcp_server_url = parent.server_url
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=_app(user, tenant, app_user_sessionmaker, router)), base_url="https://hive.test") as http:
        path = f"/api/mcp-accounts/{account.id}/token"
        oversized = await http.put(path, json={"access_token": "x" * 16385})
        assert oversized.status_code == 400 and "x" * 32 not in oversized.text
        saved = await http.put(path, json={"access_token": "new-personal-token"})
        assert saved.status_code == 200 and "new-personal-token" not in saved.text, saved.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        current = await db.get(MCPServer, account.id)
        assert (current.server_url, current.transport) == (parent.server_url, parent.transport)
        assert await resolve_mcp_oauth_bearer(db, tenant, parent.server_url, tool_id=tool_id, agent_id=bot,
            execution_context=_context(tenant, user_id, bot, session)) == ("new-personal-token", None)
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "mcp.account.token_saved", AuditLog.tenant_id == tenant)))
        assert len(rows) == 1 and rows[0].user_id == user_id
        assert rows[0].details == {"account_id": str(account.id), "server_id": str(server_id)}


async def test_disabled_tool_assignment_is_denied_by_governance_and_hidden(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.services import agent_tools
    from app.tools.governance_resolver import ToolGovernanceResolver
    tenant, _, bot, _, _, _, _, tool_id, name = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(AgentTool(tenant_id=tenant, agent_id=bot, tool_id=tool_id, enabled=False))
        await db.commit()
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker))
    resolver = ToolGovernanceResolver().build_dependencies().resolve_mcp_tool_mode
    assert await resolver(bot, name, {}) == "deny", "Grok nit: an explicit disabled assignment is authoritative"
    assert name not in await agent_tools.list_agent_mcp_deferred_tools(bot, "")


@pytest.mark.parametrize("actor,with_ids", [("external_principal", True), ("business_team", True), ("user", False)])
async def test_b3_question_answers_and_configuration_updates_do_not_change_terminal_claim(
    owner_sessionmaker, actor, with_ids
):
    from sqlalchemy import func
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionTurnInput
    from app.services.runtime_terminal_boundary_outbox import ClaimedTerminalBoundary, terminal_boundary_binding_sha256
    from app.services.web_terminal_boundary_processor import build_web_terminal_boundary_binding, _load_terminal_material
    from tests.services.test_session_input_control_v2 import _authority, _accepted_input, _seed_session
    tenant, user_id, bot, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    event_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run)
        task.status = "completed"
        db.add(ChatTranscriptEvent(id=event_id, tenant_id=tenant, agent_id=bot, session_id=session,
            run_id=run, sequence=1, event_type="tool_result", actor_type="runtime", content="Finished tool work",
            turn_id=f"turn-{run.hex}", projection_status="projected"))
        await db.commit()
        binding = await build_web_terminal_boundary_binding(db, tenant_id=tenant, runtime_task_id=run,
            agent_id=bot, session_id=str(session), event_kind="turn_stop", terminal_status="completed",
            authority_ref="runtime_task", authority_id=run)
        claim = ClaimedTerminalBoundary(id=uuid.uuid4(), tenant_id=tenant, runtime_task_id=run, agent_id=bot,
            session_id=str(session), event_kind="turn_stop", terminal_status="completed", authority_ref="runtime_task",
            authority_id=str(run), binding=binding, binding_sha256=terminal_boundary_binding_sha256(binding),
            idempotency_key="c" * 64, claim_token=uuid.uuid4(), attempt=1)
        authority = await _authority(db, user_id=user_id, agent_id=bot, session_id=session)
        admitted = await _accepted_input(db, authority=authority, session_id=session, kind="queue_next_turn")
        row = await db.get(SessionTurnInput, admitted.input_id)
        sequence = await db.scalar(select(func.max(ChatTranscriptEvent.sequence)).where(ChatTranscriptEvent.session_id == session))
        db.add(ChatTranscriptEvent(tenant_id=tenant, agent_id=bot, session_id=session, run_id=run,
            sequence=sequence + 1, event_type="user_question", item_kind="user_question", lifecycle="completed",
            actor_type=actor, command_id=row.command_id if with_ids else None, input_id=row.id if with_ids else None,
            content="Continue", turn_id=f"turn-{run.hex}", projection_status="projected"))
        await db.commit()
    async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
        material = await _load_terminal_material(db, claim)
        assert material.terminal_event_id == event_id, "B3: an answer cannot replace the committed execution frontier"


@pytest.mark.parametrize("mark_refused", [False, True])
async def test_grok1_two_answers_produce_one_continuation_and_failed_mark_produces_none(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, mark_refused
):
    from app.api.chat_sessions import router as chat_router
    from app.api.tools import router as tools_router
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionInputAdmission
    from app.services import agent_tools
    from tests.integration.test_m1g4_question_answers_postgres import _ask
    from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
    from tests.services.test_session_input_control_v2 import _seed_session, _mark_web_terminal_boundary_delivered
    tenant, user_id, bot, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async def noop(**kwargs):
        pass
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", noop)
    server = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "org_admin"
        await _seed_mcp(db, tenant, server, uuid.uuid4(), user_id)
        connection = await db.get(MCPServer, server)
        connection.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company"))}}
        connection.auth_status = "configured"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(db, tenant_id=tenant, agent_id=bot, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=user_id,
            arguments={"questions": [{"question": "Connect documents?", "options": []}],
                "configuration": {"mcp_server_id": str(server)}})
        task = await db.get(RuntimeTask, run)
        task.status = "completed"
        await _mark_web_terminal_boundary_delivered(db, task=task, agent_id=bot, session_id=session)
        await db.commit()
    app = _app(user, tenant, app_user_sessionmaker, chat_router, tools_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        endpoint = f"/api/tools/agents/{bot}/sessions/{session}/questions/{question}/mcp"
        snapshot = (await http.get(endpoint)).json()
        confirmed = await http.post(endpoint + "/confirm", json={"revision": snapshot["revision"]})
        assert confirmed.status_code == 200, confirmed.text
        if mark_refused:
            async def refused(**kwargs):
                return False
            monkeypatch.setattr("app.services.conversation_interaction_service.mark_latest_pending_clarification_answered", refused)
        async def answer():
            input_id = uuid.uuid4()
            return await http.post(f"/api/agents/{bot}/sessions/{session}/inputs", json={
                "kind": "answer_request", "input_id": str(input_id), "idempotency_key": f"answer:{input_id}",
                "request_item_id": str(question), "terminal_fallback": "queue_next_turn",
                "content_parts": [{"type": "text", "text": "Continue with the authorized MCP connection."}]})
        answers = await asyncio.gather(answer(), answer())
        assert all(item.status_code == 200 for item in answers), [item.text for item in answers]
        states = [item.json()["status"] for item in answers]
        assert states.count("rolled_over") == (0 if mark_refused else 1), states
        current = await http.get(endpoint)
        assert current.json()["answered"] is (not mark_refused)
    async with owner_sessionmaker() as db:
        tasks = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant, RuntimeTask.id != run)))
        assert len(tasks) == (0 if mark_refused else 1), "Grok 1: a refused answer cannot enqueue work"
        events = list(await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session,
            ChatTranscriptEvent.item_id == question, ChatTranscriptEvent.lifecycle == "completed")))
        assert len(events) == (0 if mark_refused else 1)


async def test_s7_real_delegate_async_worker_uses_requester_b_connection_and_audits_b(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    import json
    from types import SimpleNamespace
    from contextlib import asynccontextmanager
    from app.agents import orchestrator
    from app.core.execution_context import ExecutionPrincipal
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.agent import AgentPermission
    from app.models.chat_session import ChatSession
    from app.services import agent_tools
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _patch_invoker_seams
    from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import _patch_environment_seams
    import app.runtime.invoker as invoker

    tenant, owner_a, parent, target_id, parent_session, member_b, server, _, name = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        await _personal(db, tenant, owner_a, server, "owner-a-private")
        await _personal(db, tenant, member_b, server, "requester-b-private")
        target = await db.get(Agent, target_id)
        source = await db.get(Agent, parent)
        source.owner_user_id = member_b
        chat = await db.get(ChatSession, parent_session)
        chat.user_id = member_b
        db.add(AgentPermission(tenant_id=tenant, agent_id=target_id, scope_type="company", access_level="use"))
        await db.commit()
    @asynccontextmanager
    async def scoped():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db
    _patch_environment_seams(monkeypatch, app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async def noop(**kwargs):
        pass
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", noop)
    model = SimpleNamespace(provider="openai", model="synthetic-delegated", api_key="local", base_url=None, max_output_tokens=2048)
    async def target_runtime(child, **kwargs):
        assert child == target_id
        return target, model
    monkeypatch.setattr(orchestrator, "_resolve_resumable_target_runtime", target_runtime)
    client = ScriptedLLMClient([{"tool_calls": [{"id": "read-b-documents", "type": "function",
        "function": {"name": name, "arguments": "{}"}}], "finish_reason": "tool_calls"},
        {"content": "Read the requester's document.", "finish_reason": "stop"}])
    seed = {"tenant_id": tenant, "data_root": tmp_path, "agent_root": tmp_path / str(target_id)}
    _patch_invoker_seams(monkeypatch, client, seed, scoped)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *args, **kwargs: [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}])
    seen = []
    class Transport:
        def __init__(self, url, *, api_key):
            seen.append(api_key)
        async def call_tool(self, name, arguments):
            return "B_DOCUMENT_RESULT"
    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    principal = ExecutionPrincipal(tenant_id=tenant, source_agent_id=parent, requester_user_id=member_b,
        root_session_id=str(parent_session), origin="web_chat")
    handle = await orchestrator.delegate_async(target=target, target_model=model, owner_id=member_b,
        session_id=str(uuid.uuid4()), parent_agent_id=parent, parent_session_id=str(parent_session), tenant_id=tenant,
        execution_identity=ExecutionIdentityRef("delegated_user", str(member_b), "Member B"),
        execution_principal=principal.to_evidence(), conversation_messages=[{"role": "user", "content": "Read my documents."}])
    assert handle.status == "queued", handle
    async with scoped() as db:
        claimed = await RuntimeTaskClaimService(db=db, worker_id="d2-delegation", task_types=("delegation",)).claim_available(batch_size=10)
        await db.commit()
        task = next(row for row in claimed if row.id == uuid.UUID(handle.task_id))
        assert task.root_user_id == member_b
        claim_version = task.claim_version
    fence = set_runtime_task_fence(task_id=handle.task_id, worker_id="d2-delegation", claim_version=claim_version)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(handle.task_id, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)
    (tmp_path / "delegation-model-requests.json").write_text(json.dumps(client.requests, default=str))
    assert seen == ["requester-b-private"], [{"role": m["role"], "content": m.get("content")} for r in client.requests for m in r["messages"] if m["role"] == "tool"]
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, uuid.UUID(handle.task_id))
        assert task.status == "completed", (task.status, task.result_summary, task.metadata_json)
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.agent_id == target_id,
            AuditLog.action == "mcp.account.credentials_resolved")))
        assert len(audits) == 1 and audits[0].user_id == member_b
    assert seen == ["requester-b-private"], "S7: a real delegated tool call must never use the target owner's connection"
    assert "B_DOCUMENT_RESULT" in json.dumps(client.requests) and "owner-a-private" not in json.dumps(client.requests)
