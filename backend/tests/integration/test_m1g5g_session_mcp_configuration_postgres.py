"""Original-work MCP authorization, explicit employee use and continuation stay separate."""

import json
import uuid

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context, tenant_scoped_session
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer, MCPServerTool
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services import mcp_oauth
from app.services.mcp_metadata_trust import current_mcp_fingerprint, review_mcp_metadata
from tests.integration.test_m1g4_question_answers_postgres import _ask
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_mcp(db, tenant, server_id, tool_id, requester):
    server = MCPServer(
        id=server_id,
        tenant_id=tenant,
        name="Company documents",
        server_key=f"docs-{server_id.hex}",
        server_url="https://mcp.example.test/sse",
        status="needs_auth",
    )
    tool = Tool(
        id=tool_id,
        tenant_id=tenant,
        name=f"mcp_docs_{tool_id.hex[:8]}",
        display_name="Find documents",
        type="mcp",
        description="Find a document",
        mcp_server_url=server.server_url,
        mcp_server_name=server.name,
        mcp_tool_name="find",
    )
    db.add_all([server, tool])
    await db.flush()
    review_mcp_metadata(
        tool, decision="approve", reviewer_id=requester, expected_fingerprint=current_mcp_fingerprint(tool)
    )
    db.add(MCPServerTool(tenant_id=tenant, mcp_server_id=server_id, tool_id=tool_id, mcp_tool_name="find"))
    await db.commit()
    return tool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flow",
    ["normal", "stale", "member", "wrong_requester", "foreign", "closed", "lost_target_access", "lost_target_revision"],
)
async def test_original_question_requires_separate_oauth_use_grant_and_readback(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, flow
):
    from app.api.chat_sessions import router as chat_router
    from app.api.mcp_oauth import router as oauth_router
    from app.api.mcp_servers import router as server_router
    from app.api.tools import router
    from app.core.security import get_current_user
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand
    from app.services import agent_tools
    from app.services.agent_tool_domains import web_mcp
    from app.tools.handlers import mcp

    tenant, requester, source, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    target = uuid.uuid4() if flow in {"lost_target_access", "lost_target_revision"} else source
    server_id, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        user.role = "member" if flow in {"member", "lost_target_access", "lost_target_revision"} else "org_admin"
        if target != source:
            from app.models.agent import Agent

            db.add(
                Agent(
                    id=target, tenant_id=tenant, creator_id=requester, owner_user_id=requester, name="Managed colleague"
                )
            )
        tool = await _seed_mcp(db, tenant, server_id, tool_id, requester)
        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant,
            username=f"mcp-admin-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@mcp.test",
            password_hash="x",
            display_name="Company admin",
            role="org_admin",
        )
        db.add(admin)
        await db.commit()
    requested_server = server_id
    if flow == "foreign":
        foreign_tenant, foreign_user, _, _, _ = await _seed_session(owner_sessionmaker)
        requested_server = uuid.uuid4()
        async with owner_sessionmaker() as db:
            await _seed_mcp(db, foreign_tenant, requested_server, uuid.uuid4(), foreign_user)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.tenant_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(
        web_mcp, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    )
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def notify(*args, **kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        question = await _ask(
            db,
            tenant_id=tenant,
            agent_id=source,
            session_id=session,
            run_id=run,
            service=agent_tools._get_tool_runtime_service(),
            user_id=requester,
            arguments={
                "questions": [{"question": "Authorize the company document connection for this work?", "options": []}],
                "configuration": {"mcp_server_id": str(requested_server), "target_agent_id": str(target)},
            },
        )
        assert question is not None, "The native question must retain the exact MCP connection request"

    async def database(request: Request):
        async with app_user_sessionmaker() as db:
            if not request.url.path.endswith("/oauth/callback"):
                await pin_rls_tenant_context(db, tenant)
            yield db
            await db.commit()

    app = FastAPI()
    for routes in (router, chat_router, oauth_router, server_router):
        app.include_router(routes, prefix="/api")
    actor = [admin if flow == "wrong_requester" else user]
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = database
    endpoint = f"/api/tools/agents/{source}/sessions/{session}/questions/{question}/mcp"
    input_id = uuid.uuid4()
    answer = {
        "kind": "answer_request",
        "input_id": str(input_id),
        "idempotency_key": f"mcp:{input_id}",
        "request_item_id": str(question),
        "content_parts": [{"type": "text", "text": "Continue with the authorized MCP connection."}],
    }
    exchanged = []

    async def exchange(**kwargs):
        exchanged.append(kwargs)
        return mcp_oauth.OAuthTokenSet(
            "private-work-bearer" if len(exchanged) == 1 else "replacement-work-bearer", refresh_token="private-refresh"
        )

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as client:
        initial = await client.get(endpoint)
        if flow in {"foreign", "wrong_requester"}:
            assert initial.status_code == (404 if flow == "foreign" else 403), initial.text
            assert (
                await client.post(endpoint + "/confirm", json={"revision": "invented"})
            ).status_code == initial.status_code
            assert exchanged == []
            return
        assert initial.status_code == 200, initial.text
        if flow in {"member", "lost_target_access", "lost_target_revision"}:
            assert initial.json()["can_authorize"] is False
            forbidden = await client.post(
                f"/api/enterprise/mcp/{server_id}/oauth/start",
                json={
                    "authorization_endpoint": "https://auth.test/authorize",
                    "token_endpoint": "https://auth.test/token",
                    "client_id": "client",
                    "redirect_uri": "https://hive.test/callback",
                },
            )
            assert forbidden.status_code == 403
            actor[0] = admin
        else:
            invalid_endpoint = await client.post(
                f"/api/enterprise/mcp/{server_id}/oauth/start",
                json={
                    "authorization_endpoint": "javascript:alert(1)",
                    "token_endpoint": "https://auth.test/token",
                    "client_id": "client",
                    "redirect_uri": "https://hive.test/callback",
                },
            )
            assert invalid_endpoint.status_code == 422
        assert not initial.json()["usage"]["enabled"] and initial.json()["authorization"] != "configured"
        actor[0] = user
        assert (await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)).status_code == 409
        actor[0] = admin if flow in {"member", "lost_target_access", "lost_target_revision"} else user
        started = await client.post(
            f"/api/enterprise/mcp/{server_id}/oauth/start",
            json={
                "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token",
                "client_id": "client",
                "client_secret": "private-client",
                "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback",
            },
        )
        assert started.status_code == 200, started.text
        callback = await client.get(
            "/api/enterprise/mcp/oauth/callback", params={"state": started.json()["state"], "code": "private-code"}
        )
        assert callback.status_code == 200, callback.text
        actor[0] = user
        configured = (await client.get(endpoint)).json()
        assert configured["authorization"] == "configured" and not configured["usage"]["enabled"]
        assert (await client.post(endpoint + "/confirm", json={"revision": configured["revision"]})).status_code == 409
        grant = await client.put(
            f"/api/agents/{target}/mcp-servers/{server_id}", json={"enabled": True, "default_tool_mode": "auto"}
        )
        assert grant.status_code == 200, grant.text
        current = (await client.get(endpoint)).json()
        assert current["usage"]["enabled"] and current["usable_tool_count"] == 1
        if flow == "normal":
            disclosure = await client.get(f"/api/enterprise/mcp/{server_id}/oauth")
            assert {"id": str(target), "name": current["target_agent_name"]} in disclosure.json()["authorized_agents"]
            assert "private-work-bearer" not in disclosure.text and "private-client" not in disclosure.text
            async with owner_sessionmaker() as db:
                runtime_tool = await db.get(Tool, tool_id)
                runtime_tool.enabled = False
                await db.commit()
            disabled = (await client.get(endpoint)).json()
            assert disabled["usable_tool_count"] == 0 and disabled["revision"] != current["revision"]
            assert (
                await client.post(endpoint + "/confirm", json={"revision": disabled["revision"]})
            ).status_code == 409
            async with owner_sessionmaker() as db:
                runtime_tool = await db.get(Tool, tool_id)
                runtime_tool.enabled = True
                await db.commit()
            current = (await client.get(endpoint)).json()
        if flow == "closed":
            from app.services.session_v2_persistence import SessionEventDraft, append_session_events
            from app.services.session_user_question import question_payload

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant)
                original = (
                    await db.scalars(
                        select(ChatTranscriptEvent)
                        .where(
                            ChatTranscriptEvent.item_id == question, ChatTranscriptEvent.item_kind == "user_question"
                        )
                        .order_by(ChatTranscriptEvent.sequence.desc())
                    )
                ).first()
                await append_session_events(
                    db,
                    tenant_id=tenant,
                    agent_id=source,
                    session_id=session,
                    drafts=[
                        SessionEventDraft(
                            item_id=question,
                            item_kind="user_question",
                            lifecycle="cancelled",
                            scope=original.scope_json,
                            actor={"type": "user", "id": str(requester)},
                            payload=question_payload(original),
                            invocation_id=original.invocation_id,
                        )
                    ],
                )
                await db.commit()
            assert (await client.post(endpoint + "/confirm", json={"revision": current["revision"]})).status_code == 409
            return
        confirmed = await client.post(endpoint + "/confirm", json={"revision": current["revision"]})
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "ready", confirmed.text
        assert (await client.post(endpoint + "/confirm", json={"revision": current["revision"]})).json()[
            "receipt_ref"
        ] == confirmed.json()["receipt_ref"]
        if flow in {"lost_target_access", "lost_target_revision"}:
            if flow == "lost_target_revision":
                admitted = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
                assert admitted.status_code == 200
            from app.models.agent import Agent

            async with owner_sessionmaker() as db:
                colleague = await db.get(Agent, target)
                colleague.owner_user_id = admin.id
                await db.commit()
            denied = (
                await client.patch(
                    f"/api/agents/{source}/sessions/{session}/inputs/{input_id}",
                    json={"content_parts": answer["content_parts"]},
                    headers={"If-Match": "1"},
                )
                if flow == "lost_target_revision"
                else await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
            )
            assert denied.status_code == 403, denied.text
            if flow == "lost_target_revision":
                from app.models.session_v2 import SessionTurnInput

                async with owner_sessionmaker() as db:
                    unchanged = await db.get(SessionTurnInput, input_id)
                    assert unchanged.revision == 1 and unchanged.content_parts_json == answer["content_parts"]
            return
        if flow == "stale":
            public = await client.get(f"/api/enterprise/mcp/{server_id}/oauth")
            assert public.json()["registration"]["client_id"] == "client" and public.json()["client_secret_configured"]
            replacement = await client.post(
                f"/api/enterprise/mcp/{server_id}/oauth/start",
                json={
                    "authorization_endpoint": "https://auth.test/authorize",
                    "token_endpoint": "https://auth.test/token",
                    "client_id": "client",
                    "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback",
                },
            )
            assert replacement.status_code == 200
            assert (
                await client.get(
                    "/api/enterprise/mcp/oauth/callback",
                    params={"state": replacement.json()["state"], "code": "replacement-code"},
                )
            ).status_code == 200
            assert exchanged[-1]["client_secret"] == "private-client"
            stale = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "mcp_configuration_changed"
            assert (await client.post(endpoint + "/confirm", json={"revision": current["revision"]})).status_code == 409
            reviewed = (await client.get(endpoint)).json()
            assert reviewed["state"] == "waiting" and reviewed["revision"] != current["revision"]
            assert (
                await client.post(endpoint + "/confirm", json={"revision": reviewed["revision"]})
            ).status_code == 200
        resumed = await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)
        assert resumed.status_code == 200 and resumed.json()["status"] == "queued", resumed.text
        replayed = (await client.post(f"/api/agents/{source}/sessions/{session}/inputs", json=answer)).json()
        assert replayed["replayed"] and replayed["input_id"] == resumed.json()["input_id"] == str(input_id)
    assert len(exchanged) == (2 if flow == "stale" else 1)
    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append(api_key)

        async def call_tool(self, name, arguments):
            return "MCP document result"

        async def list_resources(self):
            return [{"uri": "test://document", "name": "Document"}]

        async def read_resource(self, uri):
            return "Authorized document content"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    assert await web_mcp._execute_mcp_tool(tool.name, {}, source) == "MCP document result"
    assert await mcp.call_mcp_tool(source, {"tool_name": tool.name}) == "MCP document result"
    assert "Document" in await mcp.mcp_list_resources(source, {})
    assert "Authorized document content" in await mcp.mcp_read_resource(source, {"uri": "test://document"})
    assert received == ["replacement-work-bearer" if flow == "stale" else "private-work-bearer"] * 4
    async with owner_sessionmaker() as db:
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session))).all()
        commands = (await db.scalars(select(SessionCommand).where(SessionCommand.session_id == session))).all()
        assert len(commands) == 1
        for secret in (
            "private-work-bearer",
            "replacement-work-bearer",
            "private-client",
            "private-code",
            "private-refresh",
        ):
            assert secret not in json.dumps([event.metadata_json for event in events])
            assert secret not in json.dumps([command.request_json for command in commands])
        assignment = (
            await db.scalars(select(AgentMCPServerAssignment).where(AgentMCPServerAssignment.agent_id == source))
        ).one()
        assignment.enabled = False
        await db.commit()
    assert "denied" in await web_mcp._execute_mcp_tool(tool.name, {}, source)
    assert len(received) == 4


@pytest.mark.asyncio
async def test_concurrent_explicit_grants_preserve_denies_and_quarantine_unreviewed_tools(
    owner_sessionmaker, app_user_sessionmaker
):
    import asyncio

    from app.services.mcp_server_service import set_agent_mcp_assignment

    tenant, requester, agent, _, _ = await _seed_session(owner_sessionmaker)
    server_id, approved_id, denied_id, pending_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        await _seed_mcp(db, tenant, server_id, approved_id, requester)
        denied = Tool(
            id=denied_id,
            tenant_id=tenant,
            name=f"denied-{denied_id.hex}",
            display_name="Explicitly disabled",
            type="mcp",
            description="Read a record",
            mcp_server_url="https://mcp.example.test/sse",
            mcp_server_name="Company documents",
            mcp_tool_name="denied",
        )
        pending = Tool(
            id=pending_id,
            tenant_id=tenant,
            name=f"pending-{pending_id.hex}",
            display_name="Awaiting review",
            type="mcp",
            enabled=False,
            mcp_trust_status="pending_review",
        )
        db.add_all([denied, pending])
        await db.flush()
        review_mcp_metadata(
            denied, decision="approve", reviewer_id=requester, expected_fingerprint=current_mcp_fingerprint(denied)
        )
        for tool in (denied, pending):
            db.add(MCPServerTool(tenant_id=tenant, mcp_server_id=server_id, tool_id=tool.id, mcp_tool_name=tool.name))
        db.add(AgentTool(tenant_id=tenant, agent_id=agent, tool_id=denied_id, enabled=False))
        await db.commit()

    async def grant():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            return await set_agent_mcp_assignment(db, tenant, agent, server_id, enabled=True)

    first, second = await asyncio.gather(grant(), grant())
    assert first["id"] == second["id"]
    async with owner_sessionmaker() as db:
        rows = (await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent))).all()
        assert len(rows) == 3
        by_id = {row.tool_id: row for row in rows}
        assert by_id[approved_id].enabled is True
        assert by_id[denied_id].enabled is False
        assert by_id[pending_id].enabled is False and by_id[pending_id].mcp_trust_requested_enabled is True
