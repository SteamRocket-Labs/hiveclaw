"""Offboarding revokes live credentials and drains the existing rebuild queue."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.util.concurrency import await_only

from app.config import get_settings
from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument, KnowledgeIndexJob
from app.models.notification import Notification
from app.models.security_audit import ResourcePermission
from app.models.tool import AgentTool
from app.models.user import User
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_promotion import CompanyKnowledgePromotionService, PersonalPromotionIntakeRequest
from app.services.personal_knowledge_service import PersonalKnowledgeService
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


def offboarding_app(admin):
    from app.api.users import router
    from app.core.security import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: admin
    return app


async def request_offboarding(app, state):
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://local.test") as client:
        return await client.post(f"/users/{state.leaver.id}/offboard", json={
            "successor_user_id": str(state.successor.id), "expected_agent_ids": [str(state.bot.id)],
            "reason": "Employment ended", "request_id": "round-two-handover"})


async def test_smithery_request_cannot_use_a_cached_leaver_key(owner_sessionmaker, monkeypatch):
    from app.services import resource_discovery

    resource_discovery.clear_provider_config_cache()
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"smithery_api_key": "sk-e2e-fake-leaver-smithery"})
        db.add(assignment)
        await db.commit()
        assert await resource_discovery._get_smithery_api_key(state.bot.id) == "sk-e2e-fake-leaver-smithery"
        await offboard(state)
        await db.commit()
        await db.refresh(assignment)
        assert assignment.config["smithery_api_key"] == ""
        headers = []

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                from httpx import Response
                headers.append(kwargs["headers"])
                return Response(200, json={"servers": []})

        monkeypatch.setattr(resource_discovery.httpx, "AsyncClient", Client)
        await resource_discovery._search_smithery_api("fixture", 1,
            await resource_discovery._get_smithery_api_key(state.bot.id))
        assert len(headers) == 1
        assert "sk-e2e-fake-leaver-smithery" not in json.dumps(headers)
    resource_discovery.clear_provider_config_cache()


async def test_undeclared_credentials_are_removed_and_listed_on_the_card(owner_sessionmaker):
    from app.api.tools import list_agent_tools_with_config

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.config_schema = {"fields": [{"key": "region", "type": "text"},
            {"key": "timeout", "type": "number"}, {"key": "headers", "type": "object"},
            {"key": "auth", "type": "object"}, {"key": "env", "type": "object"},
            {"key": "formatted_pin", "format": "password"}, {"key": "private_value", "writeOnly": True}]}
        original = {"region": "local", "timeout": 0, "headers": {"Authorization": "sk-e2e-fake-header"},
            "auth": {"vendor_pin": "sk-e2e-fake-auth"}, "env": {"VENDOR_PIN": "sk-e2e-fake-env"},
            "vendor_pin": "sk-e2e-fake-undeclared", "formatted_pin": "sk-e2e-fake-format",
            "private_value": "sk-e2e-fake-writeonly"}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id, config=original)
        db.add(assignment)
        await db.flush()
        # Undeclared and third-party settings are masked before a form opens.
        before = await list_agent_tools_with_config(agent_id=state.bot.id, db=db, current_user=state.leaver)
        displayed = next(tool for tool in before if tool["id"] == str(state.tool.id))
        from app.services.tool_config_service import MASKED_SECRET_SENTINEL
        assert displayed["agent_config"] == dict.fromkeys(original, MASKED_SECRET_SENTINEL)
        await offboard(state)
        await db.refresh(assignment)
        # G2bf r2 S3: one safe rule; tenant-defined text fields may hold secrets.
        assert assignment.config == dict.fromkeys(original, "")
        card = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id))
        assert json.loads(card.body)["tools_needing_credentials"] == [{"agent_id": str(state.bot.id),
            "agent_name": state.bot.name, "name": state.tool.display_name}]
        after = await list_agent_tools_with_config(agent_id=state.bot.id, db=db, current_user=state.successor)
        assert "sk-e2e-fake" not in json.dumps(after)


@pytest.mark.parametrize("endpoint", ["category", "tool"])
@pytest.mark.parametrize("caller", ["leaver", "admin"])
async def test_save_started_before_offboarding_rechecks_authority_and_stored_config(
    owner_sessionmaker, monkeypatch, endpoint, caller,
):
    from app.api import tools as tools_api
    from app.services import agent_tool_assignment_service as assignments
    from app.services.tool_config_service import MASKED_SECRET_SENTINEL

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.category = "handover-fixture"
        state.tool.config_schema = {"fields": [{"key": "api_key", "type": "password"},
            {"key": "timeout", "type": "number"}]}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"api_key": "sk-e2e-fake-before-save", "timeout": 1})
        db.add(assignment)
        await db.commit()
        assignment_id, bot_id, caller_id = assignment.id, state.bot.id, getattr(state, caller).id
        checked, resume = asyncio.Event(), asyncio.Event()
        original = assignments.lock_agent_tool_config_write
        save_db = None
        stale_configs = []

        async def pause_before_lock(session, agent_id, *, current_user=None):
            if session is save_db and not checked.is_set():
                await tools_api._require_manage_access(session, current_user, agent_id)
                # The real session retains the old encrypted config in its identity map.
                stale_configs.append(await session.get(AgentTool, assignment_id))
                checked.set()
                await resume.wait()
            return await original(session, agent_id, current_user=current_user)

        monkeypatch.setattr(tools_api, "lock_agent_tool_config_write", pause_before_lock)

        async def save():
            nonlocal save_db
            async with owner_sessionmaker() as session:
                save_db = session
                user = await session.get(User, caller_id)
                kwargs = dict(agent_id=bot_id, db=session, current_user=user,
                    data=tools_api.CategoryConfigIn(config={"api_key": MASKED_SECRET_SENTINEL, "timeout": 2}))
                try:
                    if endpoint == "category":
                        return await tools_api.update_category_config(category="handover-fixture", **kwargs)
                    return await tools_api.update_tool_config(tool_id=state.tool.id, **kwargs)
                except HTTPException as exc:
                    return exc

        task = asyncio.create_task(save())
        try:
            await asyncio.wait_for(checked.wait(), timeout=10)
            await offboard(state)
            await db.commit()
        finally:
            resume.set()
        result = await asyncio.wait_for(task, timeout=10)
        if caller == "leaver":
            assert isinstance(result, HTTPException), result
            assert result.status_code == 403
        else:
            assert not isinstance(result, HTTPException), result
        stored = await db.get(AgentTool, assignment_id, populate_existing=True)
        assert stored.config["api_key"] == ""
        # G2bf r2 S3: only platform-declared scalars survive; a later admin save is new input.
        assert stored.config["timeout"] == ("" if caller == "leaver" else 2)


@pytest.mark.parametrize("empty", ["", "   ", None])
async def test_empty_present_mcp_key_refuses_but_public_tool_still_runs(owner_sessionmaker, monkeypatch, empty):
    from app.services.agent_tool_domains import web_mcp
    from app.services.mcp_metadata_trust import apply_mcp_metadata_candidate, prepare_mcp_metadata_candidate

    calls = []

    async def call_tool(self, name, arguments):
        calls.append(name)
        return "public fixture result"

    monkeypatch.setattr("app.services.mcp_client.MCPClient.call_tool", call_tool)
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.type = "mcp"
        candidate = prepare_mcp_metadata_candidate(server_name="round-two", server_url="https://mcp.example.test/mcp",
            tool_name="read", raw_description="Read fixture", raw_schema={"type": "object", "properties": {}})
        apply_mcp_metadata_candidate(state.tool, candidate)
        state.tool.mcp_trust_status = "approved"
        state.tool.mcp_reviewed_fingerprint = candidate.fingerprint
        state.tool.enabled = True
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"api_key": empty})
        db.add(assignment)
        await db.commit()
        result = await web_mcp._execute_mcp_tool(state.tool.name, {}, state.bot.id)
        assert "configuration_required" in result
        assert calls == []
        assignment.config = {}
        await db.commit()
        assert await web_mcp._execute_mcp_tool(state.tool.name, {}, state.bot.id) == "public fixture result"
        assert len(calls) == 1


@pytest.mark.parametrize("endpoint", ["category", "tool"])
async def test_a_save_holding_the_config_lock_serializes_offboarding(owner_sessionmaker, monkeypatch, endpoint):
    from app.api import tools as tools_api

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.category = "handover-fixture"
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"api_key": "sk-e2e-fake-before-lock"})
        db.add(assignment)
        await db.commit()
        assignment_id, bot_id, tool_id, caller_id = assignment.id, state.bot.id, state.tool.id, state.leaver.id
        read_config, resume = asyncio.Event(), asyncio.Event()

        async def save():
            async with owner_sessionmaker() as session:
                user = await session.get(User, caller_id)
                execute = session.sync_session.execute

                def pause_on_config(statement, *args, **kwargs):
                    result = execute(statement, *args, **kwargs)
                    if "FROM agent_tools" in str(statement) and not read_config.is_set():
                        read_config.set()
                        await_only(resume.wait())
                    return result

                monkeypatch.setattr(session.sync_session, "execute", pause_on_config)
                kwargs = dict(agent_id=bot_id, db=session, current_user=user,
                    data=tools_api.CategoryConfigIn(config={"api_key": "sk-e2e-fake-just-saved"}))
                if endpoint == "category":
                    return await tools_api.update_category_config(category="handover-fixture", **kwargs)
                return await tools_api.update_tool_config(tool_id=tool_id, **kwargs)

        task = asyncio.create_task(save())
        try:
            await asyncio.wait_for(read_config.wait(), timeout=10)
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(DBAPIError) as blocked:
                await offboard(state)
            assert blocked.value.orig.sqlstate == "55P03"
        finally:
            await db.rollback()
            resume.set()
            await asyncio.wait_for(task, timeout=10)
        for obj in (state.leaver, state.successor, state.admin, state.bot):
            await db.refresh(obj)
        await offboard(state)
        await db.commit()
        assert (await db.get(AgentTool, assignment_id, populate_existing=True)).config["api_key"] == ""


async def test_six_moved_documents_are_all_promotable_with_daemon_off(owner_sessionmaker, monkeypatch):
    monkeypatch.setattr(get_settings(), "CORE_DAEMON_STARTUP_ENABLED", False)
    root = get_settings().AGENT_DATA_DIR
    service = PersonalKnowledgeService(data_root=root, extractor=None)
    process = service.process_import_jobs
    limits = []

    async def bounded_process(*args, **kwargs):
        limits.append(kwargs["limit"])
        return await process(*args, **kwargs)

    monkeypatch.setattr(service, "process_import_jobs", bounded_process)
    monkeypatch.setattr("app.api.agent_knowledge.PersonalKnowledgeService", lambda: service)
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        documents = [await service.ingest_markdown(db, tenant_id=state.tenant.id, owner_user_id=state.leaver.id,
            title=f"Handover {index}", markdown=f"# Handover {index}\n\nUnique project ORCHID{index}.",
            source_kind="paste", sensitivity="PL1_public", created_by_user_id=state.leaver.id) for index in range(6)]
        db.add(ResourcePermission(tenant_id=state.tenant.id, principal_type="user", principal_id=state.successor.id,
            resource_type="company_knowledge_scope", resource_id=state.tenant.id, actions=["propose"],
            conditions={}, effect="allow", sensitivity_ceiling="PL3_sensitive", purposes=["interactive_session"],
            created_by_user_id=state.admin.id))
        await db.commit()
        response = await request_offboarding(offboarding_app(state.admin), state)
        assert response.status_code == 200, response.text
        async with owner_sessionmaker() as check_db:
            for imported in documents:
                document = await check_db.get(KnowledgeDocument, imported.document_id)
                assert document.status == "ready"
                assert document.canonical_md_path.startswith(f"persons/{state.successor.id}/kb/")
                promotion = await CompanyKnowledgePromotionService(data_root=root).queue_personal_promotion(check_db,
                    principal=CompanyKnowledgePrincipal(tenant_id=state.tenant.id, accountable_user_id=state.successor.id,
                        accountable_role="member", actor_type="user", actor_id=state.successor.id,
                        purpose="interactive_session", session_id="six-document-handover"),
                    request=PersonalPromotionIntakeRequest(document_id=document.id, proposed_namespace="handover",
                        purpose="interactive_session", risk_level="normal", title=None, attest_scope_change=True,
                        idempotency_key=str(uuid4()), trace_id=str(uuid4())))
                assert promotion.status == "queued"
        assert len(limits) >= 2 and all(limit == 5 for limit in limits)


async def test_rebuild_claim_race_returns_retryable_409_and_rolls_back(owner_sessionmaker, monkeypatch):
    from app.services import offboarding_notice

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        imported = await PersonalKnowledgeService(extractor=None).ingest_markdown(db, tenant_id=state.tenant.id,
            owner_user_id=state.leaver.id, title="Race", markdown="# Race\n\nClaim after preflight.",
            source_kind="paste", sensitivity="PL1_public", created_by_user_id=state.leaver.id)
        await db.commit()
        original = offboarding_notice._move_knowledge

        async def claim_after_preflight(session, target_user, successor):
            async with owner_sessionmaker() as worker:
                await worker.execute(update(KnowledgeIndexJob).where(KnowledgeIndexJob.id == imported.job_id)
                    .values(status="running"))
                await worker.commit()
            return await original(session, target_user, successor)

        monkeypatch.setattr(offboarding_notice, "_move_knowledge", claim_after_preflight)
        response = await request_offboarding(offboarding_app(state.admin), state)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "offboarding_knowledge_processing"
        assert response.json()["detail"]["retryable"] is True
        assert (await db.get(User, state.leaver.id, populate_existing=True)).is_active is True
        assert (await db.get(Agent, state.bot.id, populate_existing=True)).owner_user_id == state.leaver.id
        assert (await db.get(KnowledgeDocument, imported.document_id, populate_existing=True)).scope_id == state.leaver.id
        assert not (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).all()
        # This test's separate worker committed a running claim; retire the fixture
        # after proving rollback so global stale-job tests cannot reclaim it.
        (await db.get(KnowledgeIndexJob, imported.job_id, populate_existing=True)).status = "ready"
        await db.commit()
