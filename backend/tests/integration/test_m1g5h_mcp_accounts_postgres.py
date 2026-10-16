"""Owned credentials, employee capability and originating-work selection are independent."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.mcp_servers import router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.user import User
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_personal_account_creation_and_owner_grant_do_not_grant_employee_capability(
    owner_sessionmaker, app_user_sessionmaker
):
    from sqlalchemy import select
    from app.models.tool import AgentTool

    tenant, requester, agent, _, _ = await _seed_session(owner_sessionmaker)
    server, tool_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, requester)
        user.role = "member"
        await _seed_mcp(db, tenant, server, tool_id, requester)

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        account_input = {"name": "My documents", "id": str(uuid.uuid4())}
        response = await http.post(f"/api/agents/{agent}/mcp-servers/{server}/accounts", json=account_input)
        assert response.status_code == 200, response.text
        account = response.json()
        repeated = await http.post(f"/api/agents/{agent}/mcp-servers/{server}/accounts", json=account_input)
        assert repeated.json() == account
        changed = await http.post(
            f"/api/agents/{agent}/mcp-servers/{server}/accounts", json={**account_input, "name": "Different inputs"}
        )
        assert changed.status_code == 409
        assert account["owner_user_id"] == str(requester) and account["parent_server_id"] == str(server)
        assert account["id"] != str(server) and "config" not in account and "token" not in response.text
        grant = await http.put(
            f"/api/mcp-accounts/{account['id']}/grants/{agent}/{requester}",
            json={"enabled": True, "expected_version": 0},
        )
        assert grant.status_code == 200 and grant.json()["enabled"] is True, grant.text
        replay = await http.put(
            f"/api/mcp-accounts/{account['id']}/grants/{agent}/{requester}",
            json={"enabled": True, "expected_version": 0},
        )
        assert replay.json()["id"] == grant.json()["id"] and replay.json()["version"] == grant.json()["version"]
    async with owner_sessionmaker() as db:
        assert not (
            await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent, AgentTool.tool_id == tool_id))
        ).all()
    foreign_tenant, foreign_user, foreign_agent, _, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        foreign = await db.get(User, foreign_user)
    app.dependency_overrides[get_current_user] = lambda: foreign
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        denied = await http.put(
            f"/api/mcp-accounts/{account['id']}/grants/{foreign_agent}/{foreign_user}",
            json={"enabled": True, "expected_version": 0},
        )
        assert denied.status_code == 404
    from app.models.mcp_server import MCPAccountGrant

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, foreign_tenant)
        assert not (await db.scalars(select(MCPAccountGrant))).all()


async def test_selected_account_is_consumed_and_revocation_never_falls_back_to_company_credentials(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from sqlalchemy import select
    from app.api.mcp_oauth import router as oauth_router
    from app.models.audit import AuditLog
    from app.models.mcp_server import MCPServer
    from app.services import mcp_oauth, tenant_resolver
    from app.services.agent_tool_domains import web_mcp
    from app.services.mcp_accounts import account_access, bind_work_account
    from app.tools.handlers import mcp
    from app.tools.runtime import ToolExecutionContext

    tenant, requester, agent, session, _ = await _seed_session(owner_sessionmaker)
    server_id, tool_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    registration = dict(
        authorization_endpoint="https://auth.test/authorize",
        token_endpoint="https://auth.test/token",
        client_id="registered-client",
        redirect_uri="https://hive.test/api/enterprise/mcp/oauth/callback",
    )
    async with owner_sessionmaker() as db:
        owner = await db.get(User, requester)
        owner.role = "member"
        other = User(
            id=other_id,
            tenant_id=tenant,
            role="org_admin",
            username=f"account-admin-{other_id.hex}",
            email=f"{other_id.hex}@account.test",
            password_hash="x",
            display_name="Company administrator",
        )
        db.add(other)
        tool = await _seed_mcp(db, tenant, server_id, tool_id, requester)
        server = await db.get(MCPServer, server_id)
        server.config_json = {
            "oauth": {
                **registration,
                "token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company-only-bearer")),
            }
        }
        server.auth_status = "configured"
        await db.commit()

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(oauth_router, prefix="/api")
    actor = [owner]
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    from app.database import tenant_scoped_session

    monkeypatch.setattr(
        web_mcp, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    )

    exchanges = []

    async def exchange(**kwargs):
        exchanges.append(kwargs["code"])
        return mcp_oauth.OAuthTokenSet("personal-bearer-" + kwargs["code"])

    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append(api_key)

        async def call_tool(self, name, arguments):
            return "Authorized private account document"

        async def list_resources(self):
            return [{"name": "Private document", "uri": "test://private"}]

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    context = ToolExecutionContext(
        agent_id=agent, user_id=requester, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        created = await http.post(
            f"/api/agents/{agent}/mcp-servers/{server_id}/accounts",
            json={"id": str(uuid.uuid4()), "name": "Private documents"},
        )
        account_id = uuid.UUID(created.json()["id"])
        assert (
            await http.put(f"/api/agents/{agent}/mcp-servers/{server_id}", json={"enabled": True})
        ).status_code == 200
        grant_url = f"/api/mcp-accounts/{account_id}/grants/{agent}/{requester}"
        assert (await http.put(grant_url, json={"enabled": True, "expected_version": 0})).status_code == 200
        actor[0] = other
        assert (await http.put(grant_url, json={"enabled": True, "expected_version": 0})).status_code == 403
        assert (await http.post(f"/api/enterprise/mcp/{account_id}/oauth/start", json=registration)).status_code == 403
        assert (await http.delete(f"/api/enterprise/mcp-servers/{account_id}")).status_code == 404
        actor[0] = owner
        escaped = await http.post(
            f"/api/mcp-accounts/{account_id}/oauth/start",
            json={**registration, "token_endpoint": "http://169.254.169.254/latest/credentials"},
        )
        assert escaped.status_code == 409

        async def login(code):
            started = await http.post(f"/api/mcp-accounts/{account_id}/oauth/start", json=registration)
            assert started.status_code == 200, started.text
            result = await http.get(
                "/api/enterprise/mcp/oauth/callback", params={"state": started.json()["state"], "code": code}
            )
            assert result.status_code == 200, result.text
            return started.json()

        async def bind():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, tenant)
                parent = await db.get(MCPServer, server_id)
                account, grant = await account_access(
                    db, server=parent, account_id=account_id, agent_id=agent, user=owner
                )
                await bind_work_account(
                    db, server=parent, account=account, grant=grant, agent_id=agent, session_id=session, user=owner
                )
                await db.commit()

        async def call():
            return await web_mcp._execute_mcp_tool(tool.name, {}, agent, execution_context=context)

        await login("first")
        assert "configuration_required" in await call() and received == []
        await bind()
        assert await call() == "Authorized private account document"
        assert (
            await mcp.call_mcp_tool(agent, {"tool_name": tool.name}, execution_context=context)
            == "Authorized private account document"
        )
        assert "Private document" in await mcp.mcp_list_resources(agent, {}, execution_context=context)
        assert received == ["personal-bearer-first"] * 3
        assert context.exact_secret_boundary.redact_text("personal-bearer-first").redacted_count == 1
        from dataclasses import replace
        from fastapi import HTTPException
        from app.services.mcp_server_service import resolve_mcp_oauth_bearer

        other_context = replace(context, user_id=other_id)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            bearer, error = await resolve_mcp_oauth_bearer(
                db, tenant, server.server_url, tool_id=tool_id, agent_id=agent, execution_context=other_context
            )
            assert bearer == "company-only-bearer" and error is None
            legacy_bearer, error = await resolve_mcp_oauth_bearer(
                db,
                tenant,
                server.server_url,
                tool_id=tool_id,
                agent_id=agent,
                execution_context=replace(other_context, session_id="legacy-channel-work"),
            )
            assert legacy_bearer == "company-only-bearer" and error is None
        assert (
            await http.put(
                f"/api/mcp-accounts/{account_id}/grants/{agent}/{other_id}",
                json={"enabled": True, "expected_version": 0},
            )
        ).status_code == 200
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            with pytest.raises(HTTPException) as blocked:
                await resolve_mcp_oauth_bearer(
                    db, tenant, server.server_url, tool_id=tool_id, agent_id=agent, execution_context=other_context
                )
            assert blocked.value.status_code == 409
        async with owner_sessionmaker() as db:
            account = await db.get(MCPServer, account_id)
            oauth = dict(account.config_json["oauth"])
            oauth["token"] = mcp_oauth.encrypt_token_set(
                mcp_oauth.OAuthTokenSet("expired-private-bearer", refresh_token="private-refresh", expires_at=0)
            )
            account.config_json = {**account.config_json, "oauth": oauth}
            await db.commit()

        async def rotate_after_revocation(**kwargs):
            revoked = await http.put(grant_url, json={"enabled": False, "expected_version": 1})
            assert revoked.status_code == 200
            return mcp_oauth.OAuthTokenSet("rotated-private-bearer")

        monkeypatch.setattr(mcp_oauth, "refresh_access_token", rotate_after_revocation)
        assert "configuration_required" in await call() and len(received) == 3
        assert (await http.put(grant_url, json={"enabled": False, "expected_version": 1})).status_code == 200
        stale_replay = await http.put(grant_url, json={"enabled": True, "expected_version": 0})
        assert stale_replay.status_code == 409, "An old grant retry must not reverse a later revocation"
        assert "configuration_required" in await call() and len(received) == 3
        assert (await http.put(grant_url, json={"enabled": True, "expected_version": 2})).status_code == 200
        assert "configuration_required" in await call() and len(received) == 3
        await bind()
        second = await login("second")
        assert "configuration_required" in await call() and len(received) == 3
        await bind()
        assert await call() == "Authorized private account document" and received[-1] == "personal-bearer-second"
        assert (await http.post(f"/api/mcp-accounts/{account_id}/revoke")).status_code == 200
        replay = await http.get(
            "/api/enterprise/mcp/oauth/callback", params={"state": second["state"], "code": "second"}
        )
        assert replay.status_code == 200 and replay.json()["auth_status"] == "none"
        assert exchanges == ["first", "second"]
        assert "configuration_required" in await call() and len(received) == 4
    async with owner_sessionmaker() as db:
        audits = (
            await db.scalars(
                select(AuditLog).where(
                    AuditLog.agent_id == agent, AuditLog.action == "mcp.account.credentials_resolved"
                )
            )
        ).all()
        assert len(audits) == 4 and all(row.details["account_id"] == str(account_id) for row in audits)
        assert not any("bearer" in str(row.details) for row in audits)

    from app.models.mcp_server import MCPWorkAccountBinding
    async with owner_sessionmaker() as db:
        assert (await db.scalars(select(MCPWorkAccountBinding).where(MCPWorkAccountBinding.agent_id == agent))).one()
    foreign_tenant, _, _, _, _ = await _seed_session(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, foreign_tenant)
        assert not (await db.scalars(select(MCPWorkAccountBinding))).all()
