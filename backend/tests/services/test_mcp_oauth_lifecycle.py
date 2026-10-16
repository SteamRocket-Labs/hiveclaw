from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import delete, select

from app.api.mcp_oauth import router
from app.core.security import get_current_admin
from app.database import get_db, pin_rls_tenant_context
from app.models.mcp_server import MCPServer
from app.services import mcp_oauth
from app.services.mcp_server_service import resolve_mcp_oauth_bearer


@pytest.fixture
async def oauth_server(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.tool import Tool
    from app.models.user import User

    tenant_id, user_id, server_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="OAuth company", slug=f"oauth-{tenant_id.hex}"))
        await db.flush()
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"oauth-{user_id.hex}",
                email=f"{user_id.hex}@oauth.test",
                display_name="OAuth admin",
                password_hash="x",
                role="org_admin",
            )
        )
        db.add(
            MCPServer(
                id=server_id,
                tenant_id=tenant_id,
                name="OAuth MCP",
                server_key="oauth-test",
                server_url="https://mcp.example.test/sse",
                status="needs_auth",
            )
        )
        await db.commit()
    yield SimpleNamespace(
        tenant_id=tenant_id,
        id=user_id,
        role="org_admin",
        server_id=server_id,
        server_url="https://mcp.example.test/sse",
    )
    async with owner_sessionmaker() as db:
        await db.execute(delete(Agent).where(Agent.tenant_id == tenant_id))
        await db.execute(delete(Tool).where(Tool.tenant_id == tenant_id))
        await db.execute(delete(MCPServer).where(MCPServer.tenant_id == tenant_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await db.commit()


@asynccontextmanager
async def _client(factory, actor):
    app = FastAPI()
    app.include_router(router)

    async def database(request: Request):
        async with factory() as db:
            # Authentication normally pins the admin request; the callback has no login.
            if request.url.path.endswith("/oauth/start"):
                await pin_rls_tenant_context(db, actor.tenant_id)
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_admin] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hive.test") as client:
        yield client


async def _start(client, actor):
    response = await client.post(
        f"/enterprise/mcp/{actor.server_id}/oauth/start",
        json={
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "client_id": "test-client",
            "client_secret": "client-secret",
            "redirect_uri": "https://hive.test/enterprise/mcp/oauth/callback",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _callback(client, start, **params):
    return await client.get(
        "/enterprise/mcp/oauth/callback", params={"state": start["state"], "code": "private-code", **params}
    )


@pytest.mark.asyncio
async def test_unscoped_callback_replay_and_actual_bearer_readback(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch
):
    calls = []

    async def exchange(**kwargs):
        calls.append(kwargs)
        return mcp_oauth.OAuthTokenSet("private-access", refresh_token="private-refresh")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        started = await _start(client, oauth_server)
        result = await _callback(client, started)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store" and result.headers["referrer-policy"] == "no-referrer"
        assert result.json()["auth_status"] == "configured"
        assert (await _callback(client, started)).json() == result.json()
        status = await client.get(f"/enterprise/mcp/{oauth_server.server_id}/oauth")
        assert status.status_code == 200, status.text
        assert status.json()["attempt"]["outcome"] == "configured"
    assert len(calls) == 1
    assert calls[0]["client_secret"] == "client-secret" and len(calls[0]["code_verifier"]) >= 43
    async with app_user_sessionmaker() as db:
        assert (await db.execute(select(MCPServer))).scalars().all() == []
        await pin_rls_tenant_context(db, oauth_server.tenant_id)
        assert await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url) == (
            "private-access",
            None,
        )
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        stored = json.dumps(server.config_json)
        assert server.config_json["oauth"]["last_attempt"]["requester_id"] == str(oauth_server.id)
        for secret in (
            "private-access",
            "private-refresh",
            "client-secret",
            "private-code",
            calls[0]["code_verifier"],
            started["state"],
        ):
            assert secret not in stored
        assert server.config_json["oauth"]["token"].startswith("enc:v1:")
        assert server.status == "connected"
    public = result.text + status.text
    assert "client-secret" not in public and "private-access" not in public and "private-code" not in public


@pytest.mark.asyncio
async def test_pending_or_unreadable_oauth_does_not_fall_through(
    app_user_sessionmaker, owner_sessionmaker, oauth_server
):
    async with _client(app_user_sessionmaker, oauth_server) as client:
        await _start(client, oauth_server)
    for broken in (False, True):
        if broken:
            async with owner_sessionmaker() as db:
                server = await db.get(MCPServer, oauth_server.server_id)
                server.config_json = {"oauth": {"token": "enc:v1:damaged"}}
                await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, oauth_server.tenant_id)
            bearer, error = await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url)
            assert bearer is None and error, "OAuth failure must not request the server without its bearer"


@pytest.mark.asyncio
async def test_exchange_claim_survives_concurrency_and_new_authorization(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def exchange(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return mcp_oauth.OAuthTokenSet("superseded-token")
        return mcp_oauth.OAuthTokenSet("current-token")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        old = await _start(client, oauth_server)
        running = asyncio.create_task(_callback(client, old))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            duplicate = await _callback(client, old)
            assert duplicate.status_code == 409 and duplicate.json()["detail"]["outcome"] == "unknown"
            async with owner_sessionmaker() as db:
                server = await db.get(MCPServer, oauth_server.server_id)
                assert server.config_json["oauth"]["pending"]["status"] == "exchanging"
            new = await _start(client, oauth_server)
        finally:
            release.set()
        result = await running
        assert result.status_code == 409 and result.json()["detail"]["code"] == "oauth_attempt_superseded"
        assert (await _callback(client, new)).status_code == 200
    assert len(calls) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, oauth_server.tenant_id)
        assert await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url) == (
            "current-token",
            None,
        )


@pytest.mark.asyncio
async def test_unknown_exchange_is_not_redeemed_again_and_errors_are_safe(
    app_user_sessionmaker, oauth_server, monkeypatch
):
    calls = []

    async def exchange(**kwargs):
        calls.append(kwargs)
        raise mcp_oauth.OAuthError("private-code private-access client-secret timeout")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        started = await _start(client, oauth_server)
        first = await _callback(client, started)
        second = await _callback(client, started)
        assert first.status_code == second.status_code == 409
        assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
        assert first.json()["detail"]["outcome"] == second.json()["detail"]["outcome"] == "unknown"
        assert all(
            secret not in first.text + second.text for secret in ("private-code", "private-access", "client-secret")
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expiry_denial_and_state_tampering_never_exchange(app_user_sessionmaker, oauth_server, monkeypatch):
    async def forbidden(**kwargs):
        pytest.fail("No token exchange is authorized")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", forbidden)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        started = await _start(client, oauth_server)
        denied = await _callback(client, started, code="", error="access_denied", error_description="private-code")
        assert denied.status_code == 400 and denied.json()["detail"]["outcome"] == "cancelled"
        assert (await _callback(client, started)).json() == denied.json()
        started = await _start(client, oauth_server)
        tampered = {**started, "state": started["state"].replace(oauth_server.tenant_id.hex, uuid.uuid4().hex)}
        assert (await _callback(client, tampered)).status_code == 400
        monkeypatch.setattr("app.services.mcp_server_service.time.time", lambda: started["expires_at"] + 1)
        expired = await _callback(client, started)
        assert expired.status_code == 400 and expired.json()["detail"]["outcome"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["configured", "unknown", "reauthorized"])
async def test_rotating_refresh_is_claimed_once(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch, outcome
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    expired = mcp_oauth.OAuthTokenSet("expired-access", refresh_token="rotating-refresh", expires_at=1)
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        server.config_json = {
            "oauth": {
                "token": mcp_oauth.encrypt_token_set(expired),
                "client_id": "client",
                "token_endpoint": "https://auth.example.test/token",
            }
        }
        await db.commit()

    async def refresh(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        if outcome == "unknown":
            raise mcp_oauth.OAuthError("provider transport interrupted")
        return mcp_oauth.OAuthTokenSet("refreshed-access", refresh_token="next-refresh")

    async def consume():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, oauth_server.tenant_id)
            return await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url)

    monkeypatch.setattr(mcp_oauth, "refresh_access_token", refresh)
    running = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        blocked = await asyncio.wait_for(consume(), 3)
        assert blocked[0] is None and blocked[1]
        if outcome == "reauthorized":

            async def exchange(**kwargs):
                return mcp_oauth.OAuthTokenSet("new-authorization")

            monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
            async with _client(app_user_sessionmaker, oauth_server) as client:
                started = await _start(client, oauth_server)
                assert (await _callback(client, started)).status_code == 200
    finally:
        release.set()
        result = await asyncio.wait_for(running, 3)
    repeated = await consume()
    if outcome == "unknown":
        assert result[0] is repeated[0] is None and result[1] and repeated[1]
    elif outcome == "reauthorized":
        assert result[0] is None and result[1]
        assert repeated == ("new-authorization", None)
    else:
        assert result == repeated == ("refreshed-access", None)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_legacy_migration_encrypts_without_losing_configuration(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / "alembic/versions/mcp_oauth_encryption_1016.py"
    spec = importlib.util.spec_from_file_location("mcp_oauth_encryption", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    legacy = {
        "keep": {"metadata": ["unchanged"]},
        "oauth": {
            "token": json.dumps(mcp_oauth.OAuthTokenSet("legacy-access", refresh_token="legacy-refresh").to_dict()),
            "client_secret": "legacy-client",
            "pending": {"state": "legacy-state", "code_verifier": "legacy-verifier", "client_secret": "pending-client"},
        },
    }
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        server.config_json = legacy
        await db.commit()

    def apply(connection, method):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, method)()

    monkeypatch.delenv("SECRETS_MASTER_KEY")
    async with owner_sessionmaker() as db:
        with pytest.raises(RuntimeError, match="SECRETS_MASTER_KEY"):
            await (await db.connection()).run_sync(apply, "upgrade")
        await db.rollback()
        assert (await db.get(MCPServer, oauth_server.server_id)).config_json == legacy
    monkeypatch.setenv("SECRETS_MASTER_KEY", "hive-pytest-secrets-master-key-0001")
    async with owner_sessionmaker() as db:
        await (await db.connection()).run_sync(apply, "upgrade")
        await db.commit()
        server = await db.get(MCPServer, oauth_server.server_id)
        encrypted = server.config_json
        assert encrypted["keep"] == legacy["keep"]
        assert encrypted["oauth"]["pending"]["state"] == "legacy-state"
        for secret in ("legacy-access", "legacy-refresh", "legacy-client", "legacy-verifier", "pending-client"):
            assert secret not in json.dumps(encrypted)
        assert mcp_oauth.decrypt_token_set(encrypted["oauth"]["token"]).access_token == "legacy-access"
        assert mcp_oauth.decrypt_value(encrypted["oauth"]["pending"]["code_verifier"]) == "legacy-verifier"
        await (await db.connection()).run_sync(apply, "upgrade")
        await (await db.connection()).run_sync(apply, "downgrade")
        await db.commit()
        await db.refresh(server)
        assert server.config_json == encrypted
    async with _client(app_user_sessionmaker, oauth_server) as client:
        assert (await _callback(client, {"state": "legacy-state"})).status_code == 400
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, oauth_server.tenant_id)
        assert await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url) == (
            "legacy-access",
            None,
        )


@pytest.mark.asyncio
async def test_reauthorization_preserves_working_bearer_and_storage_failure_is_local(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch
):
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        server.auth_status = "configured"
        server.config_json = {
            "oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("working-bearer"))}
        }
        await db.commit()
    async with _client(app_user_sessionmaker, oauth_server) as client:
        started = await _start(client, oauth_server)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, oauth_server.tenant_id)
            assert await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url) == (
                "working-bearer",
                None,
            )
        await _callback(client, started, code="", error="access_denied")
        async with owner_sessionmaker() as db:
            before = (await db.get(MCPServer, oauth_server.server_id)).config_json
        monkeypatch.setattr(mcp_oauth, "_secrets_provider", lambda: None)
        response = await client.post(
            f"/enterprise/mcp/{oauth_server.server_id}/oauth/start",
            json={
                "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token",
                "client_id": "client",
                "redirect_uri": "https://hive.test/callback",
                "client_secret": "new-private-secret",
            },
        )
        assert response.status_code == 503 and "new-private-secret" not in response.text
        async with owner_sessionmaker() as db:
            assert (await db.get(MCPServer, oauth_server.server_id)).config_json == before


@pytest.mark.asyncio
async def test_same_url_credentials_require_concrete_server_tool_identity(
    app_user_sessionmaker, owner_sessionmaker, oauth_server, monkeypatch
):
    from app.models.agent import Agent
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServerTool
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_metadata_trust import current_mcp_fingerprint, review_mcp_metadata

    agent_id = uuid.uuid4()
    other_id, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, oauth_server.server_id)
        server.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("first-account"))}}
        db.add(
            MCPServer(
                id=other_id,
                tenant_id=oauth_server.tenant_id,
                name="Second account",
                server_key="second",
                server_url=oauth_server.server_url,
                config_json={
                    "oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("second-account"))}
                },
            )
        )
        db.add(Agent(id=agent_id, tenant_id=oauth_server.tenant_id, creator_id=oauth_server.id, name="OAuth employee"))
        tool = Tool(
            id=tool_id,
            tenant_id=oauth_server.tenant_id,
            name=f"oauth-{tool_id.hex}",
            display_name="OAuth tool",
            type="mcp",
            description="Look up a record",
            mcp_server_url=oauth_server.server_url,
            mcp_server_name="Second account",
            mcp_tool_name="lookup",
        )
        db.add(tool)
        await db.flush()
        review_mcp_metadata(
            tool, decision="approve", reviewer_id=oauth_server.id, expected_fingerprint=current_mcp_fingerprint(tool)
        )
        db.add(AgentTool(tenant_id=oauth_server.tenant_id, agent_id=agent_id, tool_id=tool_id, enabled=True))
        db.add(
            AgentMCPServerAssignment(
                tenant_id=oauth_server.tenant_id,
                agent_id=agent_id,
                mcp_server_id=other_id,
                enabled=True,
                default_tool_mode="auto",
            )
        )
        db.add(
            MCPServerTool(
                tenant_id=oauth_server.tenant_id, mcp_server_id=other_id, tool_id=tool_id, mcp_tool_name="lookup"
            )
        )
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, oauth_server.tenant_id)
        bearer, error = await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url)
        assert bearer is None and error
        assert await resolve_mcp_oauth_bearer(db, oauth_server.tenant_id, oauth_server.server_url, tool_id=tool_id) == (
            "second-account",
            None,
        )
    from app.database import tenant_scoped_session
    from app.services import tenant_resolver
    from app.services.agent_tool_domains import web_mcp

    async def exchange(**kwargs):
        return mcp_oauth.OAuthTokenSet("authorized-second-account")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    selected = SimpleNamespace(**{**vars(oauth_server), "server_id": other_id})
    async with _client(app_user_sessionmaker, selected) as client:
        started = await _start(client, selected)
        assert (await _callback(client, started)).status_code == 200
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(
        web_mcp, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    )
    received = []

    class MCPTransport:
        def __init__(self, url, *, api_key):
            received.append((url, api_key))

        async def call_tool(self, name, arguments):
            assert name == "lookup" and arguments == {"record": "test"}
            return "Synthetic MCP record"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", MCPTransport)
    assert await web_mcp._execute_mcp_tool(tool.name, {"record": "test"}, agent_id) == "Synthetic MCP record"
    assert received == [(oauth_server.server_url, "authorized-second-account")]
    # A shared runtime tool must not borrow a different connection's use grant.
    async with owner_sessionmaker() as db:
        db.add(
            MCPServerTool(
                tenant_id=oauth_server.tenant_id,
                mcp_server_id=oauth_server.server_id,
                tool_id=tool_id,
                mcp_tool_name="lookup",
            )
        )
        await db.commit()
    from app.tools.handlers import mcp

    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    assert isinstance(await mcp._resolve_agent_mcp_server(agent_id, str(oauth_server.server_id)), str)
    selected = await mcp._resolve_agent_mcp_server(agent_id, str(other_id))
    assert selected[:2] == (oauth_server.server_url, "authorized-second-account")
    assert (await mcp._resolve_agent_mcp_server(agent_id, oauth_server.server_url))[:2] == selected[:2]

    async def no_refresh(*args, **kwargs):
        pytest.fail("Observation-only auth status must not resolve or refresh credentials")

    monkeypatch.setattr("app.services.mcp_server_service.resolve_mcp_oauth_bearer", no_refresh)
    assert "server_side_only" in await mcp.mcp_auth_status(agent_id, {"server": str(other_id)})
    async with owner_sessionmaker() as db:
        assignment = (
            await db.execute(select(AgentMCPServerAssignment).where(AgentMCPServerAssignment.agent_id == agent_id))
        ).scalar_one()
        assignment.enabled = False
        await db.commit()
    monkeypatch.setattr("app.services.mcp_server_service.resolve_mcp_oauth_bearer", resolve_mcp_oauth_bearer)
    assert "denied" in await web_mcp._execute_mcp_tool(tool.name, {"record": "test"}, agent_id)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_restarting_pending_authorization_keeps_only_the_matching_stored_client_secret(
    app_user_sessionmaker, oauth_server, monkeypatch
):
    calls = []

    async def exchange(**kwargs):
        calls.append(kwargs)
        return mcp_oauth.OAuthTokenSet("pending-restart-access")

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with _client(app_user_sessionmaker, oauth_server) as client:
        initial = await _start(client, oauth_server)
        readback = await client.get(f"/enterprise/mcp/{oauth_server.server_id}/oauth")
        assert readback.json()["client_secret_configured"] and "client-secret" not in readback.text
        body = {
            "authorization_endpoint": "https://auth.example.test/authorize",
            "token_endpoint": "https://auth.example.test/token",
            "client_id": "test-client",
            "redirect_uri": "https://hive.test/enterprise/mcp/oauth/callback",
        }
        restarted = await client.post(f"/enterprise/mcp/{oauth_server.server_id}/oauth/start", json=body)
        assert restarted.status_code == 200
        assert (await _callback(client, initial)).status_code == 400
        assert (await _callback(client, restarted.json())).status_code == 200
        assert calls[-1]["client_secret"] == "client-secret"
        changed = await client.post(
            f"/enterprise/mcp/{oauth_server.server_id}/oauth/start", json={**body, "client_id": "different-client"}
        )
        assert (await _callback(client, changed.json())).status_code == 200
        assert calls[-1]["client_secret"] is None and len(calls) == 2
