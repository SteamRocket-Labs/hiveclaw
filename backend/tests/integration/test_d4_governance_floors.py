"""Governance cases missing from the existing floor acceptance matrix."""

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api import agents, agent_knowledge, chat_sessions, enterprise, tools, users
from app.core.security import create_access_token
from app.core.tenant_middleware import TenantMiddleware
from app.database import get_current_tenant_id, get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.knowledge import KnowledgeDocument, KnowledgeSegment
from app.models.tenant import Tenant
from app.models.user import User


@pytest.fixture
async def floor_api(owner_sessionmaker, app_user_sessionmaker):
    suffix = uuid4().hex
    async with owner_sessionmaker() as db:
        companies = [Tenant(name=name, slug=f"{name}-{suffix}") for name in ("A", "B")]
        db.add_all(companies)
        await db.flush()
        members = [User(username=f"{name}-{suffix}", email=f"{name}-{suffix}@example.test",
            display_name=name, tenant_id=tenant.id, password_hash="local-only", role=role, is_active=True)
            for name, tenant, role in (("member", companies[0], "member"), ("admin", companies[0], "org_admin"),
                ("other", companies[1], "member"), ("peer", companies[0], "member"))]
        db.add_all(members)
        await db.flush()
        bots = [Agent(name=f"{name} bot", tenant_id=member.tenant_id, creator_id=member.id, owner_user_id=member.id)
            for name, member in zip(("Own", "Other"), (members[0], members[2]))]
        db.add_all(bots)
        await db.flush()
        sessions = [ChatSession(agent_id=bot.id, tenant_id=member.tenant_id, user_id=member.id, source_channel="web")
            for bot, member in zip(bots, (members[0], members[2]))]
        documents = [KnowledgeDocument(tenant_id=member.tenant_id, scope_type="person", scope_id=member.id,
            owner_user_id=member.id, title="Private document", source_kind="paste", status="ready", agent_searchable=True,
            source_sha256=uuid4().hex.ljust(64, "0"), canonical_md_path="local.md") for member in (members[0], members[2])]
        db.add_all([*sessions, *documents])
        await db.flush()
        for doc in documents:
            db.add(KnowledgeSegment(tenant_id=doc.tenant_id, document_id=doc.id, scope_type="person", scope_id=doc.scope_id,
                position=0, segment_hash="a" * 64, content="ORCHID42 is private company planning."))
        await db.commit()

    async def database():
        async with tenant_scoped_session(get_current_tenant_id(), session_factory=app_user_sessionmaker) as db:
            yield db
            await db.commit()

    app = FastAPI()
    for router in (agents.router, agent_knowledge.router, chat_sessions.router, enterprise.router, tools.router, users.router):
        app.include_router(router, prefix="/api")
    app.add_middleware(TenantMiddleware)
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d4.test") as client:
        yield client, members, bots, sessions, documents, companies


@pytest.mark.parametrize("resource", ["bot", "sessions", "knowledge", "audit"])
async def test_tenant_api_allows_own_scope_and_refuses_other_company(floor_api, resource):
    client, members, bots, sessions, docs, companies = floor_api
    member = members[1] if resource == "audit" else members[0]
    headers = {"Authorization": f"Bearer {create_access_token(str(member.id), member.role, str(member.tenant_id))}"}
    paths = {
        "bot": [f"/api/agents/{bot.id}" for bot in bots],
        "sessions": [f"/api/agents/{bot.id}/sessions" for bot in bots],
        "knowledge": [f"/api/agents/{bot.id}/knowledge/personal/documents" for bot in bots],
        "audit": [f"/api/enterprise/audit-logs?tenant_id={tenant.id}" for tenant in companies],
    }[resource]
    allowed = await client.get(paths[0], headers=headers)
    assert allowed.status_code == 200, allowed.text
    denied = await client.get(paths[1], headers=headers)
    assert denied.status_code in (403, 404), denied.text
    assert denied.json()["detail"]
    if resource == "bot":
        changed = await client.patch(paths[1], headers=headers, json={"name": "Unauthorized"})
        assert changed.status_code in (403, 404), changed.text


async def test_role_changes_require_admin_and_are_visible_in_company_audit(floor_api, owner_sessionmaker):
    client, members, bots, sessions, docs, companies = floor_api
    member, admin = members[:2]
    path = f"/api/users/{member.id}/role"
    def headers(user):
        return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role, str(user.tenant_id))}"}
    denied = await client.patch(path, headers=headers(member), json={"role": "org_admin"})
    assert denied.status_code == 403 and denied.json()["detail"]
    changed = await client.patch(path, headers=headers(admin), json={"role": "org_admin"})
    assert changed.status_code == 200, changed.text
    result = await client.get("/api/enterprise/audit-logs", headers=headers(admin))
    assert result.status_code == 200, result.text
    assert any(row["action"] == "user:role_changed" for row in result.json())
    async with owner_sessionmaker() as db:
        row = (await db.scalars(select(AuditLog).where(AuditLog.tenant_id == companies[0].id,
            AuditLog.action == "user:role_changed"))).one()
        assert row.user_id == admin.id
        assert row.details == {"target_user_id": str(member.id), "from_role": "member", "to_role": "org_admin"}


async def test_delegated_requester_cannot_use_target_owners_private_knowledge(floor_api, owner_sessionmaker):
    from app.services.personal_knowledge_access import AgentRuntimePrincipal, HumanBrowserPrincipal
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    _, members, bots, _, docs, companies = floor_api
    owner, requester = members[0], members[3]
    service = PersonalKnowledgeService()
    async with owner_sessionmaker() as db:
        allowed = await service.search_personal_with_authority(db, tenant_id=companies[0].id,
            owner_user_id=owner.id, principal=HumanBrowserPrincipal(user_id=owner.id), query="ORCHID42")
        assert allowed.status == "ok" and any("ORCHID42" in hit.snippet for hit in allowed.hits)
        denied = await service.search_personal_with_authority(db, tenant_id=companies[0].id,
            owner_user_id=owner.id, principal=AgentRuntimePrincipal(agent_id=bots[0].id,
                requester_user_id=requester.id, session_id=str(uuid4()), delegation_id=str(uuid4()), purpose="a2a_delegation"), query="ORCHID42")
        assert denied.status == "denied" and denied.hits == []
        assert denied.authority.deny_reason_code == "explicit_grant_required"


async def test_stored_credential_is_encrypted_and_hidden_from_api_and_model_input(floor_api, owner_sessionmaker):
    from sqlalchemy import text
    from app.config import get_settings
    from app.models.session_v2 import SessionTurnInput
    from app.models.tool import AgentTool, Tool
    from app.services.secrets_provider import init_secrets_provider
    from app.services.session_human_input import input_parts_to_runtime_messages
    from app.services.session_live_input import submit_live_human_input

    client, members, bots, sessions, _, companies = floor_api
    user, bot, session = members[0], bots[0], sessions[0]
    secret = "d4-stored-credential-7f2398c61a"
    init_secrets_provider(get_settings().SECRETS_MASTER_KEY)
    async with owner_sessionmaker() as db:
        tool = Tool(tenant_id=companies[0].id, name=f"d4-{uuid4().hex}", display_name="D4 tool", type="builtin",
            config_schema={"fields": [{"key": "api_key", "type": "password"}]})
        db.add(tool)
        await db.flush()
        binding = AgentTool(tenant_id=companies[0].id, agent_id=bot.id, tool_id=tool.id, config={"api_key": secret})
        db.add(binding)
        await db.commit()
        raw = await db.scalar(text("SELECT config::text FROM agent_tools WHERE id=:id"), {"id": binding.id})
        assert secret not in raw and "__hive_agent_tool_config_v1__" in raw
    headers = {"Authorization": f"Bearer {create_access_token(str(user.id), user.role, str(user.tenant_id))}"}
    response = await client.get(f"/api/tools/agents/{bot.id}/with-config", headers=headers)
    assert response.status_code == 200, response.text
    assert tool.name in response.text and secret not in response.text
    async with owner_sessionmaker() as db:
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, bot.id), user=await db.get(User, user.id),
            session=await db.get(ChatSession, session.id), content=f"Keep the report; {secret}", source="web", source_channel="web")
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        messages = input_parts_to_runtime_messages([row])
        assert secret not in str(messages)
        assert "Keep the report" in str(messages)


async def test_personal_credential_is_usable_by_owner_and_private_from_peer(floor_api, owner_sessionmaker):
    from fastapi import HTTPException
    from app.config import get_settings
    from app.models.email_account import EmailAccount
    from app.services.email_accounts import account_access
    from app.services.secrets_provider import init_secrets_provider

    _, members, bots, _, _, companies = floor_api
    init_secrets_provider(get_settings().SECRETS_MASTER_KEY)
    async with owner_sessionmaker() as db:
        account = EmailAccount(tenant_id=companies[0].id, owner_user_id=members[0].id, name="Private mailbox",
            config={"email_address": "d4-owner@example.com", "auth_code": "d4-private-mail-key"})
        db.add(account)
        await db.commit()
        allowed, _ = await account_access(db, account_id=account.id, tenant_id=companies[0].id,
            agent_id=bots[0].id, requester_id=members[0].id)
        assert allowed.id == account.id
        with pytest.raises(HTTPException) as denied:
            await account_access(db, account_id=account.id, tenant_id=companies[0].id,
                agent_id=bots[0].id, requester_id=members[3].id)
        assert denied.value.status_code == 403 and denied.value.detail


async def test_handover_receiver_keeps_peer_sessions_private_through_api(floor_api, owner_sessionmaker):
    client, members, bots, sessions, _, companies = floor_api
    leaver, admin, _, receiver = members
    bot = bots[0]
    async with owner_sessionmaker() as db:
        private = ChatSession(agent_id=bot.id, tenant_id=companies[0].id, user_id=admin.id, source_channel="web")
        db.add(private)
        await db.commit()
    def headers(user):
        return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role, str(user.tenant_id))}"}
    allowed = await client.get(f"/api/agents/{bot.id}/sessions/{sessions[0].id}/messages", headers=headers(leaver))
    assert allowed.status_code == 200, allowed.text
    moved = await client.post(f"/api/users/{leaver.id}/offboard", headers=headers(admin), json={
        "successor_user_id": str(receiver.id), "expected_agent_ids": [str(bot.id)],
        "reason": "Employment ended", "request_id": f"d4-{uuid4()}",
    })
    assert moved.status_code == 200, moved.text
    bot_read = await client.get(f"/api/agents/{bot.id}", headers=headers(receiver))
    assert bot_read.status_code == 200, bot_read.text
    denied = await client.get(f"/api/agents/{bot.id}/sessions/{private.id}/messages", headers=headers(receiver))
    assert denied.status_code == 403 and denied.json()["detail"], denied.text
    listed = await client.get(f"/api/agents/{bot.id}/sessions", headers=headers(receiver))
    assert listed.status_code == 200 and str(private.id) not in listed.text, listed.text
