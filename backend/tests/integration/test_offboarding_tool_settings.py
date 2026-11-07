"""Settings disclosure and every config writer share the handover boundary."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.notification import Notification
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config_service import MASKED_SECRET_SENTINEL
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


async def test_handover_card_uses_the_locked_value_instead_of_a_stale_identity_map(owner_sessionmaker):
    from app.api.tools import CategoryConfigIn, update_tool_config
    import json

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"api_key": ""})
        db.add(assignment)
        await db.commit()
        async with owner_sessionmaker() as editor:
            user = await editor.get(User, state.leaver.id)
            await update_tool_config(state.bot.id, state.tool.id,
                CategoryConfigIn(config={"api_key": "sk-e2e-fake-newer-save"}), user, editor)
        assert assignment.config == {"api_key": ""}
        await offboard(state)
        await db.commit()
        await db.refresh(assignment)
        assert assignment.config == {"api_key": ""}
        card = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id))
        assert json.loads(card.body)["tools_needing_credentials"] == [{"agent_id": str(state.bot.id),
            "agent_name": state.bot.name, "name": state.tool.display_name}]


async def test_unmoved_bot_masks_unknown_and_container_settings_and_allows_replacement(owner_sessionmaker):
    from app.api.tools import CategoryConfigIn, list_agent_tools_with_config, update_tool_config

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.config_schema = {"fields": [{"key": "region", "type": "text"}]}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"headers": {"Authorization": "sk-e2e-fake-header"}, "vendor_pin": "sk-e2e-fake-pin", "region": "local"})
        db.add(assignment)
        await db.commit()
        rows = await list_agent_tools_with_config(state.bot.id, current_user=state.leaver, db=db)
        config = next(row for row in rows if row["id"] == str(state.tool.id))["agent_config"]
        assert config == dict.fromkeys(assignment.config, MASKED_SECRET_SENTINEL)
        await update_tool_config(state.bot.id, state.tool.id, CategoryConfigIn(config={**config, "vendor_pin": "new-input"}),
            current_user=state.leaver, db=db)
        await db.refresh(assignment)
        assert assignment.config["vendor_pin"] == "new-input"
        assert assignment.config["headers"] == {"Authorization": "sk-e2e-fake-header"}
        assert state.bot.owner_user_id == state.leaver.id


@pytest.mark.parametrize("endpoint", ["category", "tool"])
async def test_form_opened_before_handover_cannot_restore_unknown_credentials(owner_sessionmaker, endpoint):
    from app.api import tools

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.category = "stale-form"
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"headers": {"Authorization": "sk-e2e-fake-header"}, "vendor_pin": "sk-e2e-fake-pin"})
        db.add(assignment)
        await db.commit()
        rows = await tools.list_agent_tools_with_config(state.bot.id, current_user=state.admin, db=db)
        config = next(row for row in rows if row["id"] == str(state.tool.id))["agent_config"]
        await offboard(state)
        await db.commit()
        kwargs = dict(agent_id=state.bot.id, data=tools.CategoryConfigIn(config=config), current_user=state.admin, db=db)
        if endpoint == "category":
            await tools.update_category_config(category="stale-form", **kwargs)
        else:
            await tools.update_tool_config(tool_id=state.tool.id, **kwargs)
        await db.refresh(assignment)
        assert assignment.config == {"headers": "", "vendor_pin": ""}


@pytest.mark.parametrize("url", ["https://mcp.exa.ai/mcp?api_key=sk-e2e-fake-exa", "https://user:sk-e2e-fake-exa@example.test/mcp"])
@pytest.mark.parametrize("boundary", ["display", "handover"])
async def test_platform_exa_url_with_credentials_is_masked_and_cleared(owner_sessionmaker, url, boundary):
    from app.api.tools import list_agent_tools_with_config
    from app.services.tool_seeder import BUILTIN_TOOLS

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.type, state.tool.tenant_id = "builtin", None
        state.tool.config_schema = next(tool["config_schema"] for tool in BUILTIN_TOOLS if tool["name"] == "exa_search")
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"mcp_url": url, "max_results": 5})
        db.add(assignment)
        await db.commit()
        rows = await list_agent_tools_with_config(state.bot.id, current_user=state.leaver, db=db)
        config = next(row for row in rows if row["id"] == str(state.tool.id))["agent_config"]
        if boundary == "display":
            assert config["mcp_url"] == MASKED_SECRET_SENTINEL
            return
        await offboard(state)
        await db.refresh(assignment)
        assert assignment.config == {"mcp_url": "", "max_results": 5}


@pytest.mark.parametrize("writer", ["email", "email_leaver", "self_heal"])
async def test_background_or_email_write_waits_for_handover_and_merges_current_config(owner_sessionmaker, monkeypatch, writer):
    from app.api import email_channel
    from app.services.agent_tool_domains import web_mcp

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        if writer.startswith("email"):
            tool = await db.scalar(select(Tool).where(Tool.name == "send_email"))
            if tool is None:
                tool = state.tool
                tool.name, tool.type, tool.tenant_id = "send_email", "builtin", None
        else:
            tool = state.tool
            tool.type, tool.mcp_server_url = "mcp", f"https://mcp.example.test/{uuid4()}"
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=tool.id,
            config={"vendor_pin": "sk-e2e-fake-old", "smithery_api_key": "sk-e2e-fake-old-smithery"})
        db.add(assignment)
        await db.commit()
        reached, resume = asyncio.Event(), asyncio.Event()

        async def writer_session(session):
            # Retain an object loaded before handover in this session's identity map.
            stale = await session.get(AgentTool, assignment.id)
            execute = session.execute

            async def observe(statement, *args, **kwargs):
                sql = str(statement)
                # Limit email lookup to this fixture when the suite leaves duplicate tools.
                if "FROM tools" in sql:
                    statement = statement.where(Tool.id == tool.id)
                if "FROM agents" in sql and getattr(statement, "_for_update_arg", None) is not None:
                    reached.set()
                    return await execute(statement, *args, **kwargs)
                result = await execute(statement, *args, **kwargs)
                if "FROM agent_tools" in sql and not reached.is_set():
                    reached.set()
                    await resume.wait()
                return result

            monkeypatch.setattr(session, "execute", observe)
            return stale

        @asynccontextmanager
        async def scoped(_tenant):
            async with owner_sessionmaker() as session:
                stale = await writer_session(session)
                yield session
                assert stale is not None

        async def recover(*args):
            return {"namespace": "new-namespace", "connection_id": "new-connection"}

        monkeypatch.setattr(web_mcp, "tenant_scoped_session", scoped)
        monkeypatch.setattr("app.services.resource_discovery._ensure_smithery_connection", recover)
        await offboard(state)

        async def write():
            if writer == "self_heal":
                return await web_mcp._smithery_auto_recover("unused", tool.mcp_server_url, "old", "old", state.bot.id)
            async with owner_sessionmaker() as session:
                stale = await writer_session(session)
                user = await session.get(User, state.leaver.id if writer == "email_leaver" else state.admin.id)
                try:
                    result = await email_channel.configure_email_channel(state.bot.id,
                        {"email_address": "new@example.test", "auth_code": "new-person-input"}, user, session)
                except HTTPException as exc:
                    return exc
                await session.commit()
                assert stale is not None
                return result

        task = asyncio.create_task(write())
        try:
            await asyncio.wait_for(reached.wait(), 10)
            await db.commit()
        finally:
            resume.set()
        result = await asyncio.wait_for(task, 10)
        await db.refresh(assignment)
        assert assignment.config["vendor_pin"] == ""
        assert assignment.config["smithery_api_key"] == ""
        if writer == "self_heal":
            assert assignment.config["smithery_namespace"] == "new-namespace"
            assert assignment.config["smithery_connection_id"] == "new-connection"
        elif writer == "email_leaver":
            assert isinstance(result, HTTPException) and result.status_code == 403
            assert "auth_code" not in assignment.config
        else:
            assert assignment.config["auth_code"] == "new-person-input"


@pytest.mark.parametrize("explicit", [False, True])
async def test_smithery_never_persists_a_key_read_from_storage(owner_sessionmaker, monkeypatch, explicit):
    from app.services import resource_discovery
    from httpx import Response

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        tool = await db.scalar(select(Tool).where(Tool.name == "import_mcp_server", Tool.tenant_id.is_(None)))
        if tool is None:
            tool = state.tool
            tool.name, tool.type, tool.tenant_id = "import_mcp_server", "builtin", None
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=tool.id,
            config={"smithery_api_key": "sk-e2e-fake-old"})
        db.add(assignment)
        await db.commit()
        loaded, resume = asyncio.Event(), asyncio.Event()
        original = resource_discovery._get_smithery_api_key

        async def pause(agent_id):
            value = await original(agent_id)
            assert value == "sk-e2e-fake-old"
            loaded.set()
            await resume.wait()
            return value

        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, *args, **kwargs): return Response(404, json={})

        monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", pause)
        monkeypatch.setattr(resource_discovery.httpx, "AsyncClient", Client)
        if explicit:
            await offboard(state)
            await db.commit()
            await resource_discovery.import_mcp_from_smithery("missing-fixture", state.bot.id,
                config={"smithery_api_key": "new-person-input"})
            await db.refresh(assignment)
            assert assignment.config["smithery_api_key"] == "new-person-input"
            assert not loaded.is_set()
            return
        task = asyncio.create_task(resource_discovery.import_mcp_from_smithery("missing-fixture", state.bot.id))
        try:
            await asyncio.wait_for(loaded.wait(), 10)
            await offboard(state)
            await db.commit()
        finally:
            resume.set()
        await asyncio.wait_for(task, 10)
        await db.refresh(assignment)
        assert assignment.config["smithery_api_key"] == ""


@pytest.mark.parametrize("contender", ["config", "handover"])
async def test_role_confirmation_obeys_agent_then_session_lock_order(owner_sessionmaker, monkeypatch, contender):
    from app.api import tools
    from app.services import chat_transcript
    from app.services.agent_role_settings import role_revision
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.type, state.tool.tenant_id = "builtin", None
        state.tool.config_schema = {"fields": [{"key": "timeout", "type": "number"}]}
        role_id, config_id = uuid4(), uuid4()
        await append_session_events(db, tenant_id=state.tenant.id, agent_id=state.bot.id, session_id=state.session.id,
            drafts=[SessionEventDraft(item_id=item_id, item_kind="user_question", lifecycle="waiting",
                scope={"run_id": str(state.runtime.id), "session_id": str(state.session.id), "thread_id": str(state.session.id),
                    "level": "run", "turn_id": f"turn-{state.runtime.id.hex}"}, actor={"type": "runtime"}, payload={
                    "requested_by_user_id": str(state.leaver.id), "configuration_request": {
                        "target_agent_id": str(state.bot.id), **descriptor}})
                for item_id, descriptor in [(role_id, {"employee_setting": "role"}), (config_id, {"tool_name": state.tool.name})]])
        await db.commit()
        role_advisory, resume_role, wants_agent, has_agent, wants_advisory = (asyncio.Event() for _ in range(5))
        role_db = contender_db = None
        original = chat_transcript.lock_transcript_session

        async def lock(session, **kwargs):
            if session is contender_db:
                wants_advisory.set()
            result = await original(session, **kwargs)
            if session is role_db and not role_advisory.is_set():
                role_advisory.set()
                await resume_role.wait()
            return result

        monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock)

        async def confirm():
            nonlocal role_db
            async with owner_sessionmaker() as session:
                role_db = session
                user = await session.get(User, state.leaver.id)
                return await tools.confirm_session_role_configuration(state.bot.id, state.session.id, role_id,
                    tools.RoleConfigurationConfirmIn(role_revision=role_revision(state.bot.role_description)), user, session)

        async def compete():
            nonlocal contender_db
            async with owner_sessionmaker() as session:
                contender_db = session
                execute = session.execute

                async def observe(statement, *args, **kwargs):
                    agent_lock = "FROM agents" in str(statement.compile(dialect=postgresql.dialect())) and getattr(statement, "_for_update_arg", None) is not None
                    if agent_lock:
                        wants_agent.set()
                    result = await execute(statement, *args, **kwargs)
                    if agent_lock:
                        has_agent.set()
                    return result

                monkeypatch.setattr(session, "execute", observe)
                if contender == "config":
                    user = await session.get(User, state.leaver.id)
                    return await tools.save_session_tool_configuration(state.bot.id, state.session.id, config_id,
                        tools.CategoryConfigIn(config={"timeout": 27}), user, session)
                objects = {name: await session.get(type(getattr(state, name)), getattr(state, name).id)
                    for name in ("leaver", "successor", "admin", "bot")}
                result = await offboard(SimpleNamespace(db=session, **objects))
                await session.commit()
                return result

        first = asyncio.create_task(confirm())
        second = None
        try:
            await asyncio.wait_for(role_advisory.wait(), 10)
            second = asyncio.create_task(compete())
            await asyncio.wait_for(wants_agent.wait(), 10)
            try:
                await asyncio.wait_for(has_agent.wait(), .2)
                await asyncio.wait_for(wants_advisory.wait(), 10)
            except TimeoutError:
                # The corrected path waits for the role's Agent lock before
                # taking the advisory. Confirm a real PostgreSQL lock wait.
                pid = await role_db.scalar(text("SELECT pg_backend_pid()"))
                assert await db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"), {"pid": pid})
        finally:
            resume_role.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 15)
        assert all(not isinstance(result, Exception) for result in results), results
        await db.commit()
        question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.item_id == role_id)
            .order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        assert question.metadata_json["v2_payload"]["configuration_ready"]["role_revision"] == role_revision(state.bot.role_description)
        if contender == "config":
            saved = await db.scalar(select(AgentTool).where(AgentTool.agent_id == state.bot.id, AgentTool.tool_id == state.tool.id))
            assert saved.config["timeout"] == 27
        else:
            assert (await db.get(Agent, state.bot.id, populate_existing=True)).owner_user_id == state.successor.id
