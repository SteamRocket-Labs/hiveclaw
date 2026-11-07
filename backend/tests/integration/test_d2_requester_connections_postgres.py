"""A connection belongs to the requester, independently of the selected employee."""
import json
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context, tenant_scoped_session
from app.core.execution_context import ExecutionPrincipal
from app.models.agent import Agent
from app.models.mcp_server import MCPAccountGrant, MCPServer
from app.models.user import User
from app.services import mcp_oauth
from app.services.mcp_accounts import account_access, create_personal_account, resolve_work_account, revoke_account
from app.services.mcp_server_service import resolve_mcp_oauth_bearer
from app.services.session_mcp_configuration import mcp_configuration_snapshot
from app.tools.runtime import ToolExecutionContext
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _context(tenant, user, agent, session=None, *, origin="web_chat", carried=False):
    return ToolExecutionContext(agent_id=agent, user_id=user, tenant_id=str(tenant), workspace=Path("/tmp"),
        session_id=str(session) if session else None, origin_channel=origin,
        execution_principal=ExecutionPrincipal(tenant_id=tenant, source_agent_id=agent, requester_user_id=user,
            root_session_id=str(session) if session else None, origin=origin,
            delegation_chain=("parent",) if carried else ()) if carried else None)


async def _seed(owner_sessionmaker):
    tenant, user_id, bot1, session, _ = await _seed_session(owner_sessionmaker)
    server_id, tool_id, bot2, member_b = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        db.add(User(id=member_b, tenant_id=tenant, username=f"b-{member_b.hex}",
            email=f"{member_b.hex}@d2.test", display_name="Member B", password_hash="x", role="member"))
        db.add(Agent(id=bot2, tenant_id=tenant, creator_id=user_id, owner_user_id=user_id,
            name="Second colleague"))
        tool = await _seed_mcp(db, tenant, server_id, tool_id, user_id)
        server = await db.get(MCPServer, server_id)
        server.config_json = {"oauth": {"authorization_endpoint": "https://auth.test/authorize",
            "token_endpoint": "https://auth.test/token", "client_id": "documents",
            "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback", "scope": "read"}}
        await db.commit()
    return tenant, user_id, bot1, bot2, session, member_b, server_id, tool_id, tool.name


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["oauth", "token"])
async def test_member_connects_once_second_bot_uses_it_and_revoke_requires_connection(owner_sessionmaker, app_user_sessionmaker, monkeypatch, method):
    tenant, user_id, bot1, bot2, session, _, server_id, tool_id, _ = await _seed(owner_sessionmaker)
    from app.api.mcp_oauth import router as oauth_router
    from app.api.mcp_servers import router as accounts_router
    from app.core.security import get_current_user

    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
    async def database():
        async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
            yield db
    app = FastAPI()
    app.include_router(oauth_router, prefix="/api")
    app.include_router(accounts_router, prefix="/api")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    async def exchange(**kwargs):
        return mcp_oauth.OAuthTokenSet("d2-personal-bearer")
    monkeypatch.setattr(mcp_oauth, "exchange_code_for_token", exchange)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        target = await db.get(Agent, bot1)
        before = await mcp_configuration_snapshot(db, target=target, server_id=server_id, user=user)
        assert before["authorization"] == "required"
        assert before["usable_tool_count"] == 1, "Company applications are available without per-bot duplication"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hive.test") as http:
        created = await http.post(f"/api/agents/{bot1}/mcp-servers/{server_id}/accounts",
            json={"id": str(uuid.uuid4()), "name": "Documents"})
        assert created.status_code == 200, created.text
        account_id = created.json()["id"]
        status = await http.get(f"/api/mcp-accounts/{account_id}/oauth")
        assert "registration" not in status.json()
        assert "auth.test" not in status.text
        started = await http.post(f"/api/mcp-accounts/{account_id}/oauth/start",
            **({"json": {"access_token": "d2-personal-bearer"}} if method == "token" else {}))
        assert started.status_code == 200, started.text
        assert "d2-personal-bearer" not in started.text
        if method == "oauth":
            callback = await http.get("/api/enterprise/mcp/oauth/callback",
                params={"state": started.json()["state"], "code": "local-code"}, headers={"Accept": "text/html"})
            assert callback.status_code == 200, callback.text
            assert "hive.mcp.connected" in callback.text, "The OAuth return must notify the originating conversation"
            assert "d2-personal-bearer" not in callback.text
        mine = await http.get("/api/mcp-accounts")
        assert mine.status_code == 200 and [row["id"] for row in mine.json()] == [account_id]
        assert "d2-personal-bearer" not in mine.text
    for bot in (bot1, bot2):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            token, error = await resolve_mcp_oauth_bearer(db, tenant, "https://mcp.example.test/sse",
                tool_id=tool_id, agent_id=bot, execution_context=_context(tenant, user_id, bot, session))
            assert (token, error) == ("d2-personal-bearer", None), "The requester must not authorize each employee separately"
            assert not (await db.scalars(select(MCPAccountGrant))).all()
    from app.tools.handlers import mcp
    monkeypatch.setattr("app.database.tenant_scoped_session",
        lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker))
    status = await mcp.mcp_auth_status(bot2, {"server": str(server_id)}, execution_context=_context(tenant, user_id, bot2, session))
    assert "authorization: configured" in status
    assert "d2-personal-bearer" not in status and "https://" not in status
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        await revoke_account(db, account=await db.get(MCPServer, uuid.UUID(account_id)))
        token, error = await resolve_mcp_oauth_bearer(db, tenant, "https://mcp.example.test/sse",
            tool_id=tool_id, agent_id=bot2, execution_context=_context(tenant, user_id, bot2, session))
        assert token is None and error
        after = await mcp_configuration_snapshot(db, target=await db.get(Agent, bot2), server_id=server_id, user=user)
        assert after["authorization"] == "required"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["same_bot", "delegated_child", "group", "scheduled"])
async def test_b_never_uses_a_even_with_legacy_cross_member_grant(owner_sessionmaker, app_user_sessionmaker, entry):
    tenant, user_id, bot1, bot2, session, member_b, server_id, tool_id, _ = await _seed(owner_sessionmaker)
    bot = bot2 if entry == "delegated_child" else bot1
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        personal = await create_personal_account(db, user=user, tenant_id=tenant, server_id=server_id,
            account_id=uuid.uuid4(), name="A's documents")
        account = await db.get(MCPServer, uuid.UUID(personal["id"]))
        account.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("member-a-bearer"))}}
        account.auth_status = "configured"
        db.add(MCPAccountGrant(tenant_id=tenant, account_id=account.id, agent_id=bot,
            requester_user_id=member_b, granted_by_user_id=user_id, enabled=True))
        await db.commit()
        account_id = account.id
    context = _context(tenant, member_b, bot, None if entry == "scheduled" else session,
        origin=entry, carried=entry in {"delegated_child", "scheduled", "group"})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        server = await db.get(MCPServer, server_id)
        account, _ = await resolve_work_account(db, server=server, agent_id=bot, context=context)
        assert account.id == server_id
        token, error = await resolve_mcp_oauth_bearer(db, tenant, server.server_url, tool_id=tool_id,
            agent_id=bot, execution_context=context)
        assert token is None and error
        with pytest.raises(HTTPException) as failure:
            await account_access(db, server=server, account_id=account_id, agent_id=bot, user=await db.get(User, member_b))
        assert failure.value.status_code == 403


@pytest.mark.asyncio
async def test_company_connection_has_default_tool_visibility_and_executes_for_second_bot(owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    tenant, _, _, bot2, session, member_b, server_id, tool_id, tool_name = await _seed(owner_sessionmaker)
    from app.services import agent_tools
    from app.services.agent_tool_domains import web_mcp
    from app.tools.handlers import mcp
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, server_id)
        server.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet("company-bearer"))}}
        server.auth_status = "configured"
        await db.commit()
    scoped = lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "tenant_scoped_session", scoped)
    monkeypatch.setattr(web_mcp, "tenant_scoped_session", scoped)
    monkeypatch.setattr("app.database.tenant_scoped_session", scoped)
    seen = []
    class Transport:
        def __init__(self, url, *, api_key):
            seen.append(api_key)
        async def call_tool(self, name, arguments):
            assert name == "find"
            return "Requested document"
    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    visible = await agent_tools.list_agent_mcp_deferred_tools(bot2, "")
    assert tool_name in visible
    result = await mcp.call_mcp_tool(bot2, {"tool_name": tool_name}, execution_context=_context(tenant, member_b, bot2, session))
    assert result == "Requested document", result
    assert seen == ["company-bearer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["same_bot", "delegated_child", "scheduled"])
async def test_legacy_employee_key_cannot_act_as_a_personal_connection(owner_sessionmaker, app_user_sessionmaker, monkeypatch, entry):
    from app.models.tool import AgentTool, Tool
    from app.services.agent_tool_domains import web_mcp
    tenant, _, bot1, bot2, session, member_b, server_id, _, tool_name = await _seed(owner_sessionmaker)
    bot = bot2 if entry == "delegated_child" else bot1
    async with owner_sessionmaker() as db:
        server = await db.get(MCPServer, server_id)
        server.config_json = {}
        tool = await db.scalar(select(Tool).where(Tool.name == tool_name))
        db.add(AgentTool(tenant_id=tenant, agent_id=bot, tool_id=tool.id, enabled=True,
            config={"api_key": "member-a-legacy-key"}))
        await db.commit()
    monkeypatch.setattr(web_mcp, "tenant_scoped_session",
        lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker))
    used = []
    class Transport:
        def __init__(self, url, *, api_key):
            used.append(api_key)
        async def call_tool(self, name, arguments):
            return "An unauthorized call happened"
    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    result = await web_mcp._execute_mcp_tool(tool_name, {}, bot,
        execution_context=_context(tenant, member_b, bot, None if entry == "scheduled" else session,
            origin=entry, carried=entry != "same_bot"))
    assert used == []
    assert "configuration_required" in result and "member-a-legacy-key" not in result
