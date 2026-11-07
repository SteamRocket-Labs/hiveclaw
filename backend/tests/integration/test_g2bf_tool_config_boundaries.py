"""G2bf exercises company writes, handover and real local provider requests."""

import json
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

from app.core.execution_context import tool_configuration_context
from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import AuditLog
from app.models.notification import Notification
from app.models.tool import AgentTool, Tool
from app.services.tool_config_service import MASKED_SECRET_SENTINEL, resolve_tool_config, update_tenant_tool_config
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


@pytest.fixture(autouse=True)
async def restore_platform_defaults(owner_sessionmaker):
    from app.services.tool_seeder import BUILTIN_TOOLS

    defaults = {row["name"]: row.get("config", {}) for row in BUILTIN_TOOLS}
    async with owner_sessionmaker() as db:
        stored = {row.id: deepcopy(row.config) for row in await db.scalars(select(Tool).where(Tool.tenant_id.is_(None)))}
    yield
    async with owner_sessionmaker() as db:
        for row in await db.scalars(select(Tool).where(Tool.tenant_id.is_(None))):
            row.config = stored.get(row.id, defaults.get(row.name, {}))
        await db.commit()


@asynccontextmanager
async def tool_api(db, actor):
    from app.api.tools import router

    app = FastAPI()
    app.include_router(router)

    async def session():
        yield db

    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local.test") as client:
        yield client


async def builtin(db, name):
    from app.services.tool_seeder import BUILTIN_TOOLS

    seed = next(row for row in BUILTIN_TOOLS if row["name"] == name)
    tool = await db.scalar(select(Tool).where(Tool.name == name, Tool.tenant_id.is_(None)))
    if tool is None:
        tool = Tool(name=name, display_name=seed["display_name"], type="builtin", enabled=True,
            is_default=True, category=seed["category"], config_schema=seed.get("config_schema", {}))
        db.add(tool)
        await db.flush()
    return tool


async def test_b1_company_writes_include_a_deactivated_bot(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        tool = await builtin(db, "firecrawl_search")
        state.bot.deactivated_at = datetime.now(timezone.utc)
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=tool.id, enabled=False)
        db.add(assignment)
        await db.commit()
        async with tool_api(db, state.admin) as client:
            enabled = await client.put(f"/tools/{tool.id}", json={"enabled": True})
            assert enabled.status_code == 200, enabled.text
            await db.refresh(assignment)
            assert assignment.enabled is True
            rotated = await client.put(f"/tools/{tool.id}", json={"config": {"api_key": "synthetic-company-rotated"}})
            assert rotated.status_code == 200, rotated.text
        await db.refresh(assignment)
        assert (await resolve_tool_config(tool.name, agent_id=state.bot.id, db=db))["api_key"] == "synthetic-company-rotated"
        assert assignment.config["api_key"]


@pytest.mark.parametrize("path", ["tool", "category"])
async def test_b1_unmanaged_member_cannot_lock_a_bot(owner_sessionmaker, path):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        statements = []

        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", observe)
        try:
            async with tool_api(db, state.peer) as client:
                endpoint = f"tool-config/{state.tool.id}" if path == "tool" else "category-config/general"
                response = await client.put(f"/tools/agents/{state.bot.id}/{endpoint}", json={"config": {}})
                assert response.status_code == 403, response.text
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert not any("FROM agents" in sql and "FOR NO KEY UPDATE" in sql for sql in statements)


async def test_b1_member_save_takes_the_bot_lock_once(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        statements = []

        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", observe)
        try:
            async with tool_api(db, state.leaver) as client:
                response = await client.put(f"/tools/agents/{state.bot.id}/tool-config/{state.tool.id}", json={"config": {}})
                assert response.status_code == 200, response.text
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert sum("FROM agents" in sql and "FOR NO KEY UPDATE" in sql for sql in statements) == 1


@pytest.fixture
def provider_endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            if "method" in body:
                result = {"content": [{"type": "text", "text": "Local provider content"}]}
                if body["method"] == "initialize":
                    result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "local", "version": "1"}}
                payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            else:
                payload = {"success": True, "data": {"markdown": "Local provider content", "results": []}}
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("name,url_key", [("firecrawl_search", "base_url"), ("exa_fetch", "mcp_url"), ("firecrawl_fetch", "base_url")])
@pytest.mark.parametrize("override", ["company", "same_endpoint", "personal", "different_endpoint", "copied_company", "platform", "platform_override", "environment", "environment_override"])
async def test_s3_real_handlers_bind_inherited_keys_to_their_endpoint(
    owner_sessionmaker, provider_endpoint, monkeypatch, name, url_key, override,
):
    from app.services.agent_tool_domains import web_mcp

    endpoint, requests = provider_endpoint
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        tool = await builtin(db, name)
        if name == "firecrawl_search":
            (await builtin(db, "firecrawl_fetch")).config = {}
        tool.config = {url_key: endpoint + "/platform", "auth_mode": "auto", "api_key": "synthetic-platform"}
        company = {url_key: endpoint + "/company", "api_key": "synthetic-company", "auth_mode": "auto"}
        if override.startswith("environment"):
            from app.config import get_settings

            monkeypatch.setattr(get_settings(), "EXA_API_KEY" if name == "exa_fetch" else "FIRECRAWL_API_KEY", "synthetic-environment")
            tool.config["api_key"] = ""
        elif not override.startswith("platform"):
            await update_tenant_tool_config(db, state.tenant.id, tool.id, config=company, config_schema=tool.config_schema)
        await db.commit()
        if override == "copied_company":
            async with tool_api(db, state.admin) as client:
                copied = await client.put(f"/tools/{tool.id}", json={"config": company})
            assert copied.status_code == 200, copied.text
        bot_config = {} if override in {"company", "platform", "environment"} else {url_key: endpoint + ("/company" if override == "same_endpoint" else "/bot")}
        if override == "personal":
            bot_config["api_key"] = "synthetic-personal"
        async with tool_api(db, state.leaver) as client:
            saved = await client.put(f"/tools/agents/{state.bot.id}/tool-config/{tool.id}", json={"config": bot_config})
        assert saved.status_code == 200, saved.text
        with tool_configuration_context(tenant_id=state.tenant.id, agent_id=state.bot.id):
            handler = getattr(web_mcp, "_" + name)
            result = await handler({"query": "local test"} if name == "firecrawl_search" else {"url": "https://8.8.8.8/article"})
        assert requests, result
        expected = {"company": "synthetic-company", "same_endpoint": "synthetic-company", "personal": "synthetic-personal",
            "different_endpoint": None, "copied_company": None, "platform_override": None,
            "platform": "synthetic-platform", "environment": "synthetic-environment", "environment_override": None}[override]
        assert all(row["authorization"] == (f"Bearer {expected}" if expected else None) for row in requests), requests
        prefix = "/company" if override in {"company", "same_endpoint"} else "/platform" if override in {"platform", "environment"} else "/bot"
        assert all(row["path"].startswith(prefix) for row in requests)


@pytest.mark.parametrize("personal", [False, True])
async def test_s1_handover_preserves_company_copies_and_audits_only_cleared_tools(owner_sessionmaker, personal):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        tool = await builtin(db, "firecrawl_search")
        async with tool_api(db, state.admin) as client:
            saved = await client.put(f"/tools/{tool.id}", json={"config": {"api_key": "synthetic-company"}})
        assert saved.status_code == 200, saved.text
        if personal:
            async with tool_api(db, state.leaver) as client:
                saved = await client.put(f"/tools/agents/{state.bot.id}/tool-config/{tool.id}", json={"config": {"api_key": "synthetic-personal"}})
            assert saved.status_code == 200, saved.text
        await offboard(state)
        resolved = await resolve_tool_config(tool.name, agent_id=state.bot.id, db=db)
        assert resolved["api_key"] == ("" if personal else "synthetic-company")
        card = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id))
        names = [row["name"] for row in json.loads(card.body)["tools_needing_credentials"]]
        assert names == ([tool.display_name] if personal else [])
        audit = await db.scalar(select(AuditLog).where(AuditLog.tenant_id == state.tenant.id, AuditLog.action == "user:offboarded"))
        assert audit.details["cleared_tool_names"] == names
        assert "synthetic-" not in json.dumps(audit.details)


@pytest.mark.parametrize("endpoint", ["tool", "category", "company"])
@pytest.mark.parametrize("value", [{"Authorization": MASKED_SECRET_SENTINEL}, [{"deep": [MASKED_SECRET_SENTINEL]}]])
async def test_nested_masks_are_rejected_without_changing_storage(owner_sessionmaker, endpoint, value):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.category = "fixture"
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id, config={"headers": {"Authorization": "synthetic-old"}})
        db.add(assignment)
        await db.commit()
        path = f"/tools/{state.tool.id}" if endpoint == "company" else f"/tools/agents/{state.bot.id}/" + (
            f"tool-config/{state.tool.id}" if endpoint == "tool" else "category-config/fixture")
        async with tool_api(db, state.admin if endpoint == "company" else state.leaver) as client:
            response = await client.put(path, json={"config": {"headers": value}})
        assert response.status_code == 400, response.text
        await db.refresh(assignment)
        assert assignment.config == {"headers": {"Authorization": "synthetic-old"}}


async def test_s1_safe_only_tool_row_is_kept_without_a_handover_request(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.config_schema = {"fields": [{"key": "region", "type": "text"}]}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id, config={"region": "local"})
        db.add(assignment)
        await db.commit()
        await offboard(state)
        await db.refresh(assignment)
        assert assignment.config == {"region": "local"}
        card = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id))
        assert json.loads(card.body)["tools_needing_credentials"] == []


async def test_category_save_replaces_omitted_safe_overrides(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        tool = await builtin(db, "firecrawl_search")
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=tool.id,
            config={"max_results": 7, "api_key": "synthetic-stored"})
        db.add(assignment)
        await db.commit()
        async with tool_api(db, state.leaver) as client:
            response = await client.put(f"/tools/agents/{state.bot.id}/category-config/{tool.category}", json={"config": {}})
        assert response.status_code == 200, response.text
        await db.refresh(assignment)
        assert assignment.config == {"api_key": "synthetic-stored"}


async def test_import_worker_retains_the_five_job_cap(owner_sessionmaker, monkeypatch):
    from app.api.agent_knowledge import _process_current_user_personal_import_jobs
    from app.models.knowledge import KnowledgeIndexJob
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        db.add_all([KnowledgeIndexJob(tenant_id=state.tenant.id, document_id=state.doc.id, scope_type="person",
            scope_id=state.leaver.id, artifact_hash=str(index).zfill(64), status="queued") for index in range(6)])
        await db.commit()
        limits = []

        async def process(_self, _db, **kwargs):
            limits.append(kwargs["limit"])

        monkeypatch.setattr(PersonalKnowledgeService, "process_import_jobs", process)
        await _process_current_user_personal_import_jobs(tenant_id=state.tenant.id, owner_user_id=state.leaver.id)
        assert limits == [5]


@pytest.mark.parametrize("nested_mask", [False, True])
async def test_session_save_replaces_safe_overrides_and_rejects_nested_masks(owner_sessionmaker, nested_mask):
    from app.models.user import User
    from app.services import agent_tools
    from tests.integration.test_m1g4_question_answers_postgres import _ask
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant, requester, bot, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db:
        tool = await builtin(db, "firecrawl_search")
        db.add(AgentTool(tenant_id=tenant, agent_id=bot, tool_id=tool.id,
            config={"max_results": 7, "api_key": "synthetic-stored"}))
        question = await _ask(db, tenant_id=tenant, agent_id=bot, session_id=session, run_id=run,
            service=agent_tools._get_tool_runtime_service(), user_id=requester, arguments={
                "questions": [{"question": "Save the search configuration?", "options": []}],
                "configuration": {"target_agent_id": str(bot), "tool_name": tool.name}})
        await db.commit()
        actor = await db.get(User, requester)
        async with tool_api(db, actor) as client:
            response = await client.put(f"/tools/agents/{bot}/sessions/{session}/questions/{question}/configuration",
                json={"config": {"headers": [MASKED_SECRET_SENTINEL]} if nested_mask else {}})
        assert response.status_code == (400 if nested_mask else 200), response.text
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == bot, AgentTool.tool_id == tool.id)
            .execution_options(populate_existing=True))
        assert assignment.config == ({"max_results": 7, "api_key": "synthetic-stored"} if nested_mask
            else {"api_key": "synthetic-stored"})
