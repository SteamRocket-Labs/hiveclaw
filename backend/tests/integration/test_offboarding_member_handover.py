"""Offboarding transfers company work without transferring personal identities."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant
from app.models.email_account import EmailAccount, EmailAccountGrant
from app.models.external_principal import ExternalPrincipal
from app.models.knowledge import (
    KnowledgeAssertion, KnowledgeDocument, KnowledgeEntity, KnowledgeIndexJob, KnowledgeLink, KnowledgeSegment,
)
from app.models.machine_credential import MachineCredential
from app.models.mcp_server import MCPAccountGrant, MCPServer
from app.models.notification import Notification
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import User
from app.models.trigger import AgentTrigger
from app.services import custom_api_accounts, email_accounts, mcp_accounts
from app.services.business_task_runtime import project_business_task, reconcile_business_task
from app.services.machine_identity_service import load_machine_grant
from app.services.personal_knowledge_access import AgentRuntimePrincipal, HumanBrowserPrincipal
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.secrets_provider import init_secrets_provider
from app.services.user_offboarding_service import find_user_offboarding_replay, offboard_loaded_user


async def seed_handover(db, *, successor_role="member"):
    from app.models import import_all_models
    import_all_models()
    init_secrets_provider(get_settings().SECRETS_MASTER_KEY)
    suffix = uuid4().hex
    tenant = Tenant(id=uuid4(), name="Handover", slug=f"handover-{suffix}")
    db.add(tenant)
    await db.flush()
    users = [User(id=uuid4(), tenant_id=tenant.id, username=f"{role}-{index}-{suffix}",
        email=f"{index}-{suffix}@example.test", display_name=f"Member {index}",
        password_hash="local-fixture", role=role, is_active=True)
        for index, role in enumerate(("member", successor_role, "member", "org_admin"))]
    leaver, successor, peer, admin = users
    db.add_all(users)
    await db.flush()
    bot = Agent(id=uuid4(), tenant_id=tenant.id, creator_id=leaver.id, sponsor_user_id=leaver.id,
        owner_user_id=leaver.id, name="Handover bot", role_description="Company work")
    tool = Tool(id=uuid4(), tenant_id=tenant.id, name=f"handover_{suffix}", display_name="Local API", type="custom_api")
    db.add_all([bot, tool])
    await db.flush()
    mail = EmailAccount(id=uuid4(), tenant_id=tenant.id, owner_user_id=leaver.id, name="Mailbox",
        config={"email_address": "local@example.test", "auth_code": "local-fixture-mail"})
    api = CustomApiAccount(id=uuid4(), tenant_id=tenant.id, owner_user_id=leaver.id, tool_id=tool.id, name="API",
        config={"api_key": "local-fixture-api"})
    server = MCPServer(id=uuid4(), tenant_id=tenant.id, server_key=f"local-{suffix}", name="MCP server")
    db.add(server)
    await db.flush()
    mcp = MCPServer(id=uuid4(), tenant_id=tenant.id, server_key=f"account-{suffix}", name="MCP account",
        parent_server_id=server.id, owner_user_id=leaver.id, config_json={"token": "local-fixture-mcp"},
        auth_status="configured", status="connected")
    doc = KnowledgeDocument(id=uuid4(), tenant_id=tenant.id, scope_type="person", scope_id=leaver.id,
        owner_user_id=leaver.id, title="Handover guide", source_kind="paste", source_sha256=suffix.ljust(64, "0"),
        canonical_md_path="retained-source.md", status="ready", agent_searchable=True, sensitivity="PL1_public")
    db.add_all([mail, api, mcp, doc])
    await db.flush()
    grants = [model(tenant_id=tenant.id, account_id=account.id, agent_id=bot.id, requester_user_id=successor.id,
        enabled=True, version=1) for model, account in ((EmailAccountGrant, mail), (CustomApiAccountGrant, api), (MCPAccountGrant, mcp))]
    grants[-1].granted_by_user_id = leaver.id
    segment = KnowledgeSegment(tenant_id=tenant.id, document_id=doc.id, scope_type="person", scope_id=leaver.id,
        position=0, segment_hash=suffix.ljust(64, "1"), content="Handover code ORCHID42 belongs to the company.")
    entity = KnowledgeEntity(id=uuid4(), tenant_id=tenant.id, scope_type="person", scope_id=leaver.id,
        canonical_name="ORCHID42", entity_type="project")
    db.add_all([segment, entity, *grants])
    await db.flush()
    assertion = KnowledgeAssertion(tenant_id=tenant.id, scope_type="person", scope_id=leaver.id,
        source_document_id=doc.id, subject_entity_id=entity.id, subject_text="ORCHID42", predicate="belongs_to", object_text="company")
    link = KnowledgeLink(tenant_id=tenant.id, scope_type="person", scope_id=leaver.id,
        from_kind="document", from_id=doc.id, to_kind="entity", to_id=entity.id, relation="mentions")
    job = KnowledgeIndexJob(tenant_id=tenant.id, document_id=doc.id, scope_type="person", scope_id=leaver.id,
        artifact_hash=suffix, status="completed", stage="completed")
    principal = ExternalPrincipal(id=uuid4(), tenant_id=tenant.id, provider="a2a", installation_ref=f"machine:{suffix}",
        subject_id=suffix, display_name="Old machine", status="active")
    db.add(principal)
    await db.flush()
    machine = MachineCredential(id=uuid4(), tenant_id=tenant.id, agent_id=bot.id, principal_id=principal.id,
        issued_by_user_id=admin.id, owner_user_id=leaver.id, owner_epoch=0, issue_request_id=uuid4(),
        request_hash="0" * 64, token_hash=hashlib.sha256(suffix.encode()).hexdigest(), scopes=["agent.execute"],
        status="active", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    runtime = RuntimeTask(id=uuid4(), tenant_id=tenant.id, parent_agent_id=bot.id, root_user_id=leaver.id,
        task_type="business_task", status="running", metadata_json={"business_task_id": str(task_id := uuid4()), "phase": "invoking"})
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation
    await assign_runtime_task_writer_generation(db, runtime)
    db.add(runtime)
    await db.flush()
    task = Task(id=task_id, tenant_id=tenant.id, agent_id=bot.id, title="Send the prepared report", type="todo",
        status="doing", created_by=leaver.id, active_runtime_task_id=runtime.id,
        request_id=f"task-{suffix}", request_hash="0" * 64)
    peer_task = Task(tenant_id=tenant.id, agent_id=bot.id, title="Other member's unfinished work", type="todo",
        status="pending", created_by=peer.id, request_id=f"peer-task-{suffix}", request_hash="0" * 64)
    session = ChatSession(id=uuid4(), tenant_id=tenant.id, agent_id=bot.id, user_id=leaver.id,
        runtime_task_id=runtime.id, session_kind="user_task")
    trigger = AgentTrigger(id=uuid4(), tenant_id=tenant.id, agent_id=bot.id, name="Daily report", type="interval",
        config={"minutes": 15, "created_by": str(leaver.id)}, is_enabled=True)
    db.add_all([assertion, link, job, machine, task, peer_task, session, trigger])
    await db.flush()
    runtime.parent_session_id = runtime.root_session_id = str(session.id)
    runtime.child_session_id = str(session.id)
    return SimpleNamespace(**locals())


async def write_known_result(state, content="Report generated: ORCHID42.pdf"):
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation

    scope = {"tenant_id": state.tenant.id, "agent_id": state.bot.id, "session_id": state.session.id,
        "run_id": state.runtime.id, "turn_id": f"turn-{state.runtime.id.hex}", "round_index": 1}
    wire = {"messages": [{"role": "user", "content": "Prepare the report"}], "tools": [], "max_tokens": 8192}
    request_id = await prepare_model_request(state.db, **scope, continuation_index=0, messages=wire["messages"],
        tools=[], provider="openai", model="local-fixture", wire_request=wire, attempt_owner="handover-test")
    call_id = str(uuid4())
    await seal_model_response(state.db, **scope, provider_request_id=request_id, response={"content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "call_api", "arguments": "{}"}}],
        "finish_reason": "tool_calls", "usage": {}})
    await commit_sealed_model_round(state.db, **scope, provider_request_id=request_id)
    invocation = await prepare_tool_invocation(state.db, tenant_id=state.tenant.id, agent_id=state.bot.id,
        session_id=state.session.id, run_id=state.runtime.id, provider_request_id=request_id,
        provider_tool_use_id=call_id, tool_name="call_api", arguments={})
    return await complete_tool_invocation(state.db, tenant_id=state.tenant.id, agent_id=state.bot.id,
        session_id=state.session.id, invocation_id=invocation.id, provider_result_content=content,
        execution_evidence={"schema": "hive.tool_execution_evidence.v1", "status": "settled",
            "tool_decision": {"outcome": "allow", "input_hash": invocation.args_hash},
            "execution_frame": {"status": "completed"}})


async def offboard(state):
    return await offboard_loaded_user(state.db, target_user=state.leaver, successor=state.successor,
        actor=state.admin, expected_agent_ids=[state.bot.id], reason="Employment ended", request_id="handover-1")


@pytest_asyncio.fixture(params=["org_admin", "member"])
async def handed_over(owner_sessionmaker, request):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db, successor_role=request.param)
        await write_known_result(state)
        state.receipt = await offboard(state)
        yield state
        await db.rollback()


async def test_offboarding_clears_all_personal_credentials_and_disables_grants(handed_over):
    state = handed_over
    for account in (state.mail, state.api):
        await state.db.refresh(account)
        assert account.config == {}
        assert account.version == 2
    await state.db.refresh(state.mcp)
    assert state.mcp.config_json == {} and state.mcp.auth_status == "none"
    for grant in state.grants:
        await state.db.refresh(grant)
        assert grant.enabled is False and grant.version == 2
    assert state.leaver.is_active is False


async def test_later_personal_account_use_reports_revoked(handed_over):
    state = handed_over
    with pytest.raises(HTTPException, match="已吊销"):
        await email_accounts.account_access(state.db, account_id=state.mail.id, tenant_id=state.tenant.id,
            agent_id=state.bot.id, requester_id=state.successor.id)
    with pytest.raises(HTTPException, match="已吊销"):
        await custom_api_accounts.account_access(state.db, account_id=state.api.id, tool=state.tool,
            agent_id=state.bot.id, requester_id=state.successor.id)
    with pytest.raises(HTTPException, match="已吊销"):
        await mcp_accounts.account_access(state.db, server=state.server, account_id=state.mcp.id,
            agent_id=state.bot.id, user=state.successor)


async def test_old_machine_is_explicitly_revoked_after_handover(handed_over):
    state = handed_over
    await state.db.refresh(state.machine)
    assert state.machine.status == "revoked" and state.machine.revoked_at is not None
    with pytest.raises(HTTPException):
        await load_machine_grant(state.db, credential_id=state.machine.id, agent_id=state.bot.id,
            principal_id=state.principal.id, scope="agent.execute")


async def test_successor_lists_searches_and_uses_reassigned_knowledge(handed_over):
    state = handed_over
    for row in (state.doc, state.segment, state.entity, state.assertion, state.link, state.job):
        await state.db.refresh(row)
        assert row.scope_id == state.successor.id
    assert state.doc.owner_user_id == state.successor.id
    service = PersonalKnowledgeService()
    owner = HumanBrowserPrincipal(user_id=state.successor.id)
    docs = await service.list_personal_documents(state.db, tenant_id=state.tenant.id, owner_user_id=state.successor.id, principal=owner)
    assert state.doc.id in [doc.document_id for doc in docs]
    for principal in (owner, AgentRuntimePrincipal(agent_id=state.bot.id, requester_user_id=state.successor.id,
        session_id=str(uuid4()))):
        result = await service.search_personal_with_authority(state.db, tenant_id=state.tenant.id,
            owner_user_id=state.successor.id, principal=principal, query="ORCHID42")
        assert result.status == "ok" and any("ORCHID42" in hit.snippet for hit in result.hits)
    denied = await service.search_personal_with_authority(state.db, tenant_id=state.tenant.id,
        owner_user_id=state.successor.id, principal=HumanBrowserPrincipal(user_id=state.peer.id), query="ORCHID42")
    assert denied.status == "denied" and denied.hits == []


async def test_one_persisted_notice_has_known_results_and_per_item_work(handed_over):
    state = handed_over
    notices = list(await state.db.scalars(select(Notification).where(Notification.user_id == state.successor.id)))
    assert len(notices) == 1
    payload = json.loads(notices[0].body)
    assert payload["agents"] == [{"id": str(state.bot.id), "name": state.bot.name}]
    assert payload["knowledge"] == [{"id": str(state.doc.id), "title": state.doc.title}]
    assert payload["triggers"] == [{"id": str(state.trigger.id), "agent_id": str(state.bot.id), "name": state.trigger.name}]
    assert payload["work"][0]["known_results"] == ["Report generated: ORCHID42.pdf"]
    assert payload["work"][0]["requires_confirmation"] is True
    assert len(payload["work"]) == 1
    assert state.trigger.is_enabled is False and state.trigger.config["authority_state"] == "quarantined"
    assert project_business_task(task=state.task, runtime_task=state.runtime)["actions"]["can_retry"] is False
    replay = await find_user_offboarding_replay(state.db, target_user=state.leaver, successor_user_id=state.successor.id,
        expected_agent_ids=[state.bot.id], reason="Employment ended", request_id="handover-1")
    assert replay.transferred_agent_ids == [state.bot.id]
    assert len(list(await state.db.scalars(select(Notification).where(Notification.user_id == state.successor.id)))) == 1


async def test_successor_can_decide_handed_over_work_without_receiving_peer_work(handed_over):
    from app.api.tasks import _load_authorized_task

    state = handed_over
    task, _decision = await _load_authorized_task(state.db, state.successor, agent_id=state.bot.id,
        task_id=state.task.id, action="execute", operator_view=False, operator_reason=None)
    assert task.created_by == state.leaver.id and task.responsible_user_id == state.successor.id
    assert state.peer_task.responsible_user_id is None
    if state.successor.role == "member":
        with pytest.raises(HTTPException):
            await _load_authorized_task(state.db, state.successor, agent_id=state.bot.id,
                task_id=state.peer_task.id, action="read", operator_view=False, operator_reason=None)


async def test_completed_outward_action_lost_receipt_then_offboarding_requires_confirmation(owner_sessionmaker):
    accepted = []
    async def receiver(reader, writer):
        accepted.append(await reader.readline())
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(receiver, "127.0.0.1", 0)
    try:
        async with owner_sessionmaker() as db:
            state = await seed_handover(db, successor_role="org_admin")
            await write_known_result(state)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"POST /deliver-report HTTP/1.1\r\n\r\n")
            await writer.drain()
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            assert len(accepted) == 1
            state.task.last_result = "Report generated: ORCHID42.pdf"
            state.runtime.result_summary = "Report generated: ORCHID42.pdf"
            await offboard(state)
            assert state.task.last_result == "Report generated: ORCHID42.pdf"
            assert state.runtime.result_summary == "Report generated: ORCHID42.pdf"
            assert "interrupted" in state.task.last_error
            assert state.runtime.metadata_json["side_effect_state"] == "unknown"
            assert project_business_task(task=state.task, runtime_task=state.runtime)["actions"]["can_retry"] is False
            reconcile_business_task(db=db, task=state.task, runtime_task=state.runtime, resolved_by_user_id=state.successor.id,
                decision="close_without_retry", reason="The recipient has the report; do not send it again.")
            assert state.task.status == "cancelled" and len(accepted) == 1
            await db.rollback()
    finally:
        server.close()
        await server.wait_closed()
