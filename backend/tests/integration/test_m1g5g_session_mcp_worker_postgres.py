"""Local outer-worker proof with scripted model/summary and MCP transport seams.

The actual tool service, model-round persistence, question, HTTP APIs, terminal
outbox consumer, admitted-input dispatcher, claim service and successor run are
used under scoped app-role sessions. Context decorators and optional telemetry
reuse the existing worker fixture; this is not live-model/provider acceptance.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context, tenant_scoped_session as real_tenant_session
from app.models.mcp_server import AgentMCPServerAssignment
from app.models.tool import AgentTool
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    _seed_run,
    _patch_runtime_seams,
    _patch_invoker_seams,
)
from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
    _patch_runtime_settings,
    _patch_environment_seams,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("account_scope", ["company", "personal"])
async def test_original_work_authorization_reaches_the_actual_successor_worker(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    account_scope,
    drain_terminal_boundary_for_task,
):
    from app.services import agent_tools, web_chat_runtime as runtime
    from app.services import tenant_resolver

    real_resolver = tenant_resolver.resolve_tenant_for_agent
    import app.runtime.invoker as invoker
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    seed = await _seed_run(
        owner_sessionmaker, tmp_path, prompt="Find the company document using its authorized MCP connection."
    )
    server_id, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        tool = await _seed_mcp(db, seed["tenant_id"], server_id, tool_id, seed["user_id"])
        db.add(AgentTool(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], tool_id=tool_id, enabled=True))
        db.add(
            AgentMCPServerAssignment(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                mcp_server_id=server_id,
                enabled=True,
                default_tool_mode="auto",
            )
        )
        await db.commit()

    @asynccontextmanager
    async def scoped():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    # Import native consumers before environment seams can be captured by module aliases.
    from app.tools import resolver as native_resolver, governance_resolver as native_governance
    from app.services import runtime_task_worker
    from app.services.agent_tool_domains import web_mcp as native_mcp

    _patch_runtime_seams(monkeypatch, seed, scoped)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, scoped)
    call = {
        "id": "request-oauth",
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "arguments": json.dumps(
                {
                    "questions": [{"question": "Authorize the company connection for this work?", "options": []}],
                    "configuration": {"mcp_server_id": str(server_id)},
                }
            ),
        },
    }
    client = ScriptedLLMClient([{"tool_calls": [call], "finish_reason": "tool_calls"}])
    _patch_invoker_seams(monkeypatch, client, seed, scoped)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", real_resolver)
    # Lazy native consumers must not retain the previous case's tenant factory.
    monkeypatch.setattr(native_resolver, "resolve_tenant_for_agent", real_resolver)
    monkeypatch.setattr(native_resolver, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(native_governance, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(native_mcp, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr(native_mcp, "resolve_tenant_for_agent", real_resolver)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr("app.tools.resolver.async_session", scoped)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", scoped)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(
        invoker,
        "get_agent_tools_for_llm",
        lambda *a, **k: [
            {"type": "function", "function": {"name": "ask_user_question", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": tool.name, "parameters": {"type": "object"}}},
        ],
    )
    await asyncio.wait_for(runtime.execute_web_chat_run(seed["run_id"]), 15)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed" and task.metadata_json["interactive_pause"] == "awaiting_user_clarification"
        question = (
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == seed["session_id"],
                    ChatTranscriptEvent.item_kind == "user_question",
                )
            )
        ).one()
        question_id = question.item_id
        from app.models.user import User

        user = await db.get(User, seed["user_id"])
        user.role = "org_admin"
        await db.commit()
    import app.services.conversation_summarizer as summaries

    async def summary(*a, **k):
        return "Local synthetic turn summary"

    monkeypatch.setattr(summaries, "_llm_summarize", summary)
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox

    monkeypatch.setattr(runtime_task_worker, "tenant_scoped_session", real_tenant_session)
    monkeypatch.setattr("app.database.tenant_scoped_session", real_tenant_session)

    counts = await drain_terminal_boundary_for_task(
        runtime_task_worker.drain_web_terminal_boundary_outbox_once,
        task_id=seed["run_id"],
        worker_id="oauth-boundary-worker",
        session_factory=app_user_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        statuses = (
            await db.scalars(
                select(RuntimeTerminalBoundaryOutbox.status).where(
                    RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]
                )
            )
        ).all()
        assert statuses and set(statuses) == {"delivered"}, (counts, statuses)
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.api.tools import router as tools_router
    from app.api.chat_sessions import router as chat_router
    from app.api.mcp_oauth import router as oauth_router
    from app.api.mcp_servers import router as server_router
    from app.core.security import get_current_user
    from app.database import get_db
    from app.services import mcp_oauth

    app = FastAPI()
    for router in (tools_router, chat_router, oauth_router, server_router):
        app.include_router(router, prefix="/api")

    async def database():
        async with scoped() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user

    async def exchange(**kw):
        return mcp_oauth.OAuthTokenSet(
            "worker-personal-bearer" if kw["code"] == "personal" else "worker-authorized-bearer"
        )

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)

    async def no_wakeup(**kw):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    endpoint = f"/api/tools/agents/{seed['agent_id']}/sessions/{seed['session_id']}/questions/{question_id}/mcp"
    input_id = uuid.uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        started = await http.post(
            f"/api/enterprise/mcp/{server_id}/oauth/start",
            json={
                "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token",
                "client_id": "client",
                "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback",
            },
        )
        assert started.status_code == 200, started.text
        callback = await http.get(
            "/api/enterprise/mcp/oauth/callback", params={"state": started.json()["state"], "code": "synthetic"}
        )
        assert callback.status_code == 200, callback.text
        selection = {}
        if account_scope == "personal":
            async with owner_sessionmaker() as db:
                current_user = await db.get(User, seed["user_id"])
                current_user.role = "member"
                await db.commit()
            user.role = "member"
            created = await http.post(
                f"/api/agents/{seed['agent_id']}/mcp-servers/{server_id}/accounts",
                json={"id": str(uuid.uuid4()), "name": "My work documents"},
            )
            assert created.status_code == 200, created.text
            account_id = created.json()["id"]
            granted = await http.put(
                f"/api/mcp-accounts/{account_id}/grants/{seed['agent_id']}/{seed['user_id']}",
                json={"enabled": True, "expected_version": 0},
            )
            assert granted.status_code == 200, granted.text
            personal = await http.post(
                f"/api/mcp-accounts/{account_id}/oauth/start",
                json={
                    "authorization_endpoint": "https://auth.test/authorize",
                    "token_endpoint": "https://auth.test/token",
                    "client_id": "client",
                    "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback",
                },
            )
            assert personal.status_code == 200, personal.text
            callback = await http.get(
                "/api/enterprise/mcp/oauth/callback", params={"state": personal.json()["state"], "code": "personal"}
            )
            assert callback.status_code == 200, callback.text
            selection = {"account_id": account_id}
            selected = await http.post(endpoint + "/select", json=selection)
            assert selected.status_code == 200 and selected.json()["state"] == "waiting", selected.text
            assert (await http.get(endpoint)).json()["account"]["id"] == account_id
        assert len(client.requests) == 1
        async with owner_sessionmaker() as db:
            tasks = (
                await db.scalars(select(RuntimeTask).where(RuntimeTask.root_session_id == str(seed["session_id"])))
            ).all()
            assert len(tasks) == 1 and tasks[0].status == "completed"
        current = (await http.get(endpoint, params=selection)).json()
        assert (
            await http.post(endpoint + "/confirm", json={"revision": current["revision"], **selection})
        ).status_code == 200
        resumed = await http.post(
            f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/inputs",
            json={
                "kind": "answer_request",
                "input_id": str(input_id),
                "idempotency_key": f"worker:{input_id}",
                "request_item_id": str(question_id),
                "content_parts": [{"type": "text", "text": "Continue with the authorized MCP connection."}],
            },
        )
        assert resumed.status_code == 200 and resumed.json()["admission_state"] == "admitted", resumed.text
    # Preserve the production dispatch recovery age; do not shorten its fence.
    await asyncio.sleep(5.1)
    counts = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="oauth-input-dispatch", session_factory=scoped
    )
    assert counts["dispatched"] == 1, counts
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with scoped() as db:
        claim = RuntimeTaskClaimService(
            db=db, worker_id="oauth-worker-2", task_types=["web_chat_turn"], lease_seconds=60
        )
        claimed = await claim.claim_available(batch_size=1)
        assert len(claimed) == 1
        successor_id = claimed[0].id
    client.script.extend(
        [
            {
                "tool_calls": [
                    {"id": "read-doc", "type": "function", "function": {"name": tool.name, "arguments": "{}"}}
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "Found the requested company document.", "finish_reason": "stop"},
        ]
    )
    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append(api_key)

        async def call_tool(self, name, arguments):
            return "Company document content from synthetic MCP" + (
                " worker-personal-bearer" if account_scope == "personal" else ""
            )

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    await asyncio.wait_for(runtime.execute_web_chat_run(successor_id), 15)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, successor_id)
        events = (
            await db.scalars(
                select(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.run_id == successor_id)
                .order_by(ChatTranscriptEvent.sequence)
            )
        ).all()
        assert task.parent_session_id == str(seed["session_id"]) and task.root_user_id == seed["user_id"]
        assert len(client.requests) == 3
        recovered = json.dumps(client.requests[1]["messages"])
        assert "Find the company document using its authorized MCP connection." in recovered
        assert "Continue with the authorized MCP connection." in recovered
        assert received == ["worker-personal-bearer" if account_scope == "personal" else "worker-authorized-bearer"]
        assert task.status == "completed"
        assert any(
            e.item_kind == "tool_result" and "Company document content from synthetic MCP" in (e.content or "")
            for e in events
        )
        assert any(e.item_kind == "assistant_final" and e.lifecycle == "completed" for e in events)
        assert "worker-personal-bearer" not in json.dumps(client.requests)
        assert "worker-personal-bearer" not in json.dumps([event.content for event in events])
        assert not any(
            secret in json.dumps([event.metadata_json for event in events])
            for secret in ["worker-authorized-bearer", "worker-personal-bearer"]
        )
