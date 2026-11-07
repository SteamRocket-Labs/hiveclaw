"""G2b uses the real rebuild, promotion, and tool credential boundaries."""

import json
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument, KnowledgeIndexJob
from app.models.notification import Notification
from app.models.security_audit import ResourcePermission
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
from app.services.company_knowledge_promotion import CompanyKnowledgePromotionService, PersonalPromotionIntakeRequest
from app.services.custom_api_connectors import build_custom_api_tool_payload, execute_custom_api_tool
from app.services.personal_knowledge_access import HumanBrowserPrincipal
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.tool_config_service import resolve_tool_config
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


async def test_successor_promotes_moved_document_without_manual_rebuild(owner_sessionmaker):
    root = get_settings().AGENT_DATA_DIR
    service = PersonalKnowledgeService(data_root=root, extractor=None)
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        imported = await service.ingest_markdown(db, tenant_id=state.tenant.id, owner_user_id=state.leaver.id,
            title="Promotion handover", markdown="# Handover\n\nThe project code is ORCHID42.",
            source_kind="paste", sensitivity="PL1_public", created_by_user_id=state.leaver.id)
        db.add(ResourcePermission(tenant_id=state.tenant.id, principal_type="user", principal_id=state.successor.id,
            resource_type="company_knowledge_scope", resource_id=state.tenant.id, actions=["propose"],
            conditions={}, effect="allow", sensitivity_ceiling="PL3_sensitive", purposes=["interactive_session"],
            created_by_user_id=state.admin.id))
        await db.commit()
        tenant_id, successor_id, leaver_id, peer_id = state.tenant.id, state.successor.id, state.leaver.id, state.peer.id
        from app.api.users import router
        from app.core.security import get_current_user
        app = FastAPI()
        app.include_router(router)

        async def current_admin():
            return state.admin

        app.dependency_overrides[get_current_user] = current_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local.test") as client:
            response = await client.post(f"/users/{state.leaver.id}/offboard", json={
                "successor_user_id": str(successor_id), "expected_agent_ids": [str(state.bot.id)],
                "reason": "Employment ended", "request_id": "handover-promotion"})
        assert response.status_code == 200, response.text

    # The offboarding response itself schedules the existing worker; no manual Rebuild or drain.
    async with owner_sessionmaker() as db:
        promotion = await CompanyKnowledgePromotionService(data_root=root).queue_personal_promotion(db,
            principal=CompanyKnowledgePrincipal(tenant_id=tenant_id, accountable_user_id=successor_id,
                accountable_role="member", actor_type="user", actor_id=successor_id,
                purpose="interactive_session", session_id="handover-promotion"),
            request=PersonalPromotionIntakeRequest(document_id=imported.document_id, proposed_namespace="handover",
                purpose="interactive_session", risk_level="normal", title=None, attest_scope_change=True,
                idempotency_key=str(uuid4()), trace_id=str(uuid4())))
        assert promotion.status == "queued"
        document = await db.get(KnowledgeDocument, imported.document_id)
        assert document.scope_id == successor_id
        assert document.canonical_md_path.startswith(f"persons/{successor_id}/kb/")
        job = await db.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id))
        assert job.job_metadata_json["queued_import_kind"] == "rebuild"
        for user_id in (leaver_id, peer_id):
            denied = await service.get_personal_document_with_authority(db, tenant_id=tenant_id,
                owner_user_id=successor_id, document_id=document.id, principal=HumanBrowserPrincipal(user_id=user_id))
            assert denied.status == "denied"
        await db.rollback()


async def test_moved_tool_credentials_are_cleared_but_company_and_peer_configs_survive(owner_sessionmaker, monkeypatch):
    async def no_request(*args, **kwargs):
        pytest.fail("A cleared credential must refuse before an HTTP request")

    monkeypatch.setattr("app.services.custom_api_connectors.execute_prepared_custom_api_request", no_request)
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        payload = build_custom_api_tool_payload(connector_name=f"handover-{uuid4().hex}", action_name="read",
            description="Fixture", base_url="https://api.example.test", method="GET", path="/status", auth_scheme="api_key")
        state.tool.name, state.tool.config = payload.tool_name, payload.tool_config
        state.tool.config_schema = {"fields": [*payload.config_schema["fields"],
            {"key": "vendor_pin", "type": "password"}, {"key": "region", "type": "text"}]}
        config = {"api_key": "sk-e2e-fake-personal", "vendor_pin": "sk-e2e-fake-vendor", "region": "local",
            "nested": [{"access_token": "sk-e2e-fake-nested", "timeout": 7}],
            "empty": {"password": ""}, "label": "__HIVE_SECRET_SET__"}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id, config=config)
        other_bots = [Agent(id=uuid4(), tenant_id=state.tenant.id, creator_id=owner.id, owner_user_id=owner.id,
            sponsor_user_id=owner.id, name=name, role_description="Fixture")
            for owner, name in ((state.peer, "Peer bot"), (state.admin, "Company bot"))]
        db.add_all(other_bots)
        await db.flush()
        untouched = [AgentTool(tenant_id=state.tenant.id, agent_id=bot.id, tool_id=state.tool.id, config=config)
            for bot in other_bots]
        company = TenantToolConfig(tenant_id=state.tenant.id, tool_id=state.tool.id,
            config={"api_key": "sk-e2e-fake-company", "region": "company"})
        managed_tool = Tool(id=uuid4(), tenant_id=state.tenant.id, name=f"company-{uuid4().hex}",
            display_name="Company-managed API", type="custom_api", config_schema={"fields": [
                *payload.config_schema["fields"], {"key": "region", "type": "text"}]},
            config=payload.tool_config)
        db.add(managed_tool)
        await db.flush()
        managed_assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id,
            tool_id=managed_tool.id, config={"region": "managed"})
        managed_config = TenantToolConfig(tenant_id=state.tenant.id, tool_id=managed_tool.id,
            config={"api_key": "sk-e2e-fake-company-managed"})
        db.add_all([managed_assignment, managed_config])
        db.add_all([assignment, *untouched, company])
        await db.flush()
        await offboard(state)
        await db.refresh(assignment)
        # S1: safe region scalars survive; personal credentials are still emptied.
        assert assignment.config == {**dict.fromkeys(config, ""), "region": "local"}
        for row in untouched:
            await db.refresh(row)
            assert row.config == config
        await db.refresh(company)
        assert company.config == {"api_key": "sk-e2e-fake-company", "region": "company"}
        await db.refresh(managed_assignment)
        # S1: a region-only row neither loses its scalar nor asks for credentials.
        assert managed_assignment.config == {"region": "managed"}
        resolved = await resolve_tool_config(managed_tool.name, state.tenant.id, agent_id=state.bot.id, db=db)
        assert resolved["api_key"] == "sk-e2e-fake-company-managed"
        assert state.tool.config == payload.tool_config
        card = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id))
        assert json.loads(card.body)["tools_needing_credentials"] == [{"agent_id": str(state.bot.id),
            "agent_name": state.bot.name, "name": state.tool.display_name}]
        assert "sk-e2e-fake" not in card.body
        from app.api.tools import list_agent_tools_with_config
        with pytest.raises(HTTPException) as denied:
            await list_agent_tools_with_config(agent_id=state.bot.id, db=db, current_user=state.leaver)
        assert denied.value.status_code == 403
        await db.commit()
        result = await execute_custom_api_tool(state.tool.name, {}, agent_id=state.bot.id)
        assert "api_key is not configured" in result
        assert "sk-e2e-fake" not in result


async def test_tool_revocation_rolls_back_with_offboarding(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.tool.config_schema = {"fields": [{"key": "timeout", "type": "number"}]}
        assignment = AgentTool(tenant_id=state.tenant.id, agent_id=state.bot.id, tool_id=state.tool.id,
            config={"token": "sk-e2e-fake-rollback", "timeout": 9})
        db.add(assignment)
        await db.commit()
        assignment_id, leaver_id = assignment.id, state.leaver.id
        await offboard(state)
        await db.refresh(assignment)
        # S1: rollback covers personal revocation while the declared safe scalar stays.
        assert assignment.config == {"token": "", "timeout": 9}
        await db.rollback()
        restored = await db.get(AgentTool, assignment_id, populate_existing=True)
        assert restored.config == {"token": "sk-e2e-fake-rollback", "timeout": 9}
        from app.models.user import User
        assert (await db.get(User, leaver_id, populate_existing=True)).is_active is True
