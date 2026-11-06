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
from app.models.audit import AuditLog
from app.models.chat_session import ChatSession
from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant
from app.models.email_account import EmailAccount, EmailAccountGrant
from app.models.external_principal import ExternalPrincipal
from app.models.knowledge import (
    KnowledgeAssertion, KnowledgeDocument, KnowledgeEntity, KnowledgeGrant, KnowledgeIndexJob, KnowledgeLink,
    KnowledgeSegment, PersonalKnowledgeProposal,
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


async def write_known_result(state, content="Report generated: ORCHID42.pdf", *, started_only=False, round_index=1, tool_name="call_api"):
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation, mark_tool_effect_started

    scope = {"tenant_id": state.tenant.id, "agent_id": state.bot.id, "session_id": state.session.id,
        "run_id": state.runtime.id, "turn_id": f"turn-{state.runtime.id.hex}", "round_index": round_index}
    wire = {"messages": [{"role": "user", "content": "Prepare the report"}], "tools": [], "max_tokens": 8192}
    request_id = await prepare_model_request(state.db, **scope, continuation_index=0, messages=wire["messages"],
        tools=[], provider="openai", model="local-fixture", wire_request=wire, attempt_owner="handover-test")
    call_id = str(uuid4())
    await seal_model_response(state.db, **scope, provider_request_id=request_id, response={"content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": "{}"}}],
        "finish_reason": "tool_calls", "usage": {}})
    await commit_sealed_model_round(state.db, **scope, provider_request_id=request_id)
    invocation = await prepare_tool_invocation(state.db, tenant_id=state.tenant.id, agent_id=state.bot.id,
        session_id=state.session.id, run_id=state.runtime.id, provider_request_id=request_id,
        provider_tool_use_id=call_id, tool_name=tool_name, arguments={})
    if started_only:
        return await mark_tool_effect_started(state.db, tenant_id=state.tenant.id, agent_id=state.bot.id,
            session_id=state.session.id, invocation_id=invocation.id)
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


async def test_old_machine_owner_epoch_denies_without_partial_revocation(handed_over):
    state = handed_over
    await state.db.refresh(state.machine)
    await state.db.refresh(state.principal)
    assert state.machine.status == state.principal.status == "active"
    assert state.machine.revoked_at is state.principal.revoked_at is None
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
    step = payload["work"][0]["known_results"][0]
    assert step["tool_name"] == "call_api" and step["state"] == "completed"
    assert datetime.fromisoformat(step["time"])
    assert "Report generated: ORCHID42.pdf" not in notices[0].body
    assert payload["work"][0]["side_effect_state"] == "unknown"
    assert payload["work"][0]["requires_confirmation"] is True
    assert len(payload["work"]) == 1
    assert state.trigger.is_enabled is False and state.trigger.config["authority_state"] == "quarantined"
    assert project_business_task(task=state.task, runtime_task=state.runtime)["actions"]["can_retry"] is False
    replay = await find_user_offboarding_replay(state.db, target_user=state.leaver, successor_user_id=state.successor.id,
        expected_agent_ids=[state.bot.id], reason="Employment ended", request_id="handover-1")
    assert replay.transferred_agent_ids == [state.bot.id]
    assert len(list(await state.db.scalars(select(Notification).where(Notification.user_id == state.successor.id)))) == 1


async def test_successor_can_decide_handed_over_work_without_receiving_peer_work(handed_over, owner_sessionmaker):
    from app.api.tasks import _load_authorized_task, reconcile_business_task_state, TaskReconcileIn
    from app.database import tenant_scoped_session

    state = handed_over
    task, _decision = await _load_authorized_task(state.db, state.successor, agent_id=state.bot.id,
        task_id=state.task.id, action="execute", operator_view=False, operator_reason=None)
    assert task.created_by == task.responsible_user_id == state.successor.id
    assert state.peer_task.responsible_user_id is None
    if state.successor.role == "member":
        with pytest.raises(HTTPException):
            await _load_authorized_task(state.db, state.successor, agent_id=state.bot.id,
                task_id=state.peer_task.id, action="read", operator_view=False, operator_reason=None)
    decision = TaskReconcileIn(decision="close_without_retry", reason="The report was already received.")
    await state.db.commit()
    async with tenant_scoped_session(str(state.tenant.id), session_factory=owner_sessionmaker) as db:
        with pytest.raises(HTTPException) as denied:
            await reconcile_business_task_state(state.bot.id, state.task.id, decision,
                current_user=await db.get(User, state.peer.id), db=db)
        assert denied.value.status_code in (403, 404)
        detail = await reconcile_business_task_state(state.bot.id, state.task.id, decision,
            current_user=await db.get(User, state.successor.id), db=db)
        response = detail.model_dump(mode="json")
        assert response["task"]["status"] == "cancelled"
        assert response["task"]["actions"]["can_reconcile"] is False


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
            await write_known_result(state, started_only=True)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"POST /deliver-report HTTP/1.1\r\n\r\n")
            await writer.drain()
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            assert len(accepted) == 1
            await offboard(state)
            notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
            work = json.loads(notice.body)["work"][0]
            assert work["known_results"][0]["tool_name"] == "call_api"
            assert work["known_results"][0]["state"] == "started"
            assert work["requires_confirmation"] is True and work["side_effect_state"] == "unknown"
            assert datetime.fromisoformat(work["known_results"][0]["time"])
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


@pytest.mark.parametrize("successor_role", ["org_admin", "member"])
@pytest.mark.parametrize("session_path", ["direct_parent", "direct_child", "team_root", "delegation_hex"])
async def test_handover_notice_does_not_copy_private_conversation_results(owner_sessionmaker, successor_role, session_path):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    async with owner_sessionmaker() as db:
        state = await seed_handover(db, successor_role=successor_role)
        await write_known_result(state)
        runtime = RuntimeTask(id=uuid4(), tenant_id=state.tenant.id, parent_agent_id=state.bot.id,
            root_user_id=state.leaver.id, task_type="web_chat_turn", status="running", metadata_json={"phase": "invoking"})
        await assign_runtime_task_writer_generation(db, runtime)
        db.add(runtime)
        await db.flush()
        session = ChatSession(id=uuid4(), tenant_id=state.tenant.id, agent_id=state.bot.id, user_id=state.leaver.id,
            runtime_task_id=runtime.id, session_kind="human_chat", visibility_scope="direct_user")
        db.add(session)
        await db.flush()
        runtime.parent_session_id = runtime.root_session_id = str(session.id)
        runtime.child_session_id = str(session.id) if session_path == "direct_child" else None
        if session_path in {"team_root", "delegation_hex"}:
            child = ChatSession(id=uuid4(), tenant_id=state.tenant.id, agent_id=state.bot.id,
                user_id=state.leaver.id, session_kind="team_member" if session_path == "team_root" else "delegation_run")
            db.add(child)
            await db.flush()
            runtime.child_session_id = child.id.hex if session_path == "delegation_hex" else str(child.id)
            if session_path == "team_root":
                runtime.parent_session_id = str(child.id)
            else:
                runtime.task_type = "delegation"
                runtime.child_agent_id = state.bot.id
                child.runtime_task_id = runtime.id
                child.peer_agent_id = state.bot.id
                child.parent_session_id = session.id
        execution_session = child if session_path in {"team_root", "delegation_hex"} else session
        private = SimpleNamespace(**{**vars(state), "runtime": runtime, "session": execution_session})
        marker = "PRIVATE_CONVERSATION_SHOULD_STAY_WITH_LEAVER"
        await write_known_result(private, marker)
        await offboard(state)
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        assert marker not in notice.body
        assert "Report generated: ORCHID42.pdf" not in notice.body
        assert [work["id"] for work in json.loads(notice.body)["work"]] == [str(state.runtime.id)]
        assert session.user_id == state.leaver.id
        assert await db.scalar(select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.run_id == runtime.id,
            ChatTranscriptEvent.event_type == "tool_result.completed", ChatTranscriptEvent.content == marker))
        await db.rollback()


def pending_proposal(state):
    return PersonalKnowledgeProposal(tenant_id=state.tenant.id, owner_user_id=state.leaver.id,
        proposed_by_agent_id=state.bot.id, title="Review the guide", proposed_content="New company procedure",
        content_hash="a" * 64, baseline_document_id=state.doc.id, diff_unified="+New company procedure",
        sensitivity="PL1_public", purpose="Keep the guide current", dedupe_key=uuid4().hex,
        idempotency_key=uuid4().hex, policy_outcome="ask", status="pending")


async def test_overlapping_knowledge_merges_all_references_and_searches_once(owner_sessionmaker, tmp_path):
    from copy import deepcopy
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        old_rows = (state.doc, state.segment, state.entity, state.assertion, state.link, state.job)
        replacements = {row.id: uuid4() for row in old_rows}
        source_ref = {"document_id": str(state.doc.id), "segment_id": str(state.segment.id),
            "seg_hash": state.segment.segment_hash, "position": 0}
        for row in (state.entity, state.assertion, state.link):
            row.source_refs_json = [source_ref]
        copies = []
        for row in old_rows:
            values = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns
                if column.name not in {"created_at", "updated_at"}}
            for key, value in values.items():
                if isinstance(value, type(state.leaver.id)):
                    values[key] = state.successor.id if value == state.leaver.id else replacements.get(value, value)
            if "source_refs_json" in values:
                values["source_refs_json"] = [{**source_ref, "document_id": str(replacements[state.doc.id]),
                    "segment_id": str(replacements[state.segment.id])}]
            copy = type(row)(**values)
            db.add(copy)
            copies.append(copy)
            await db.flush()
        for user, doc in ((state.leaver, state.doc), (state.successor, copies[0])):
            path = tmp_path / f"{user.id}.md"
            path.write_text("Handover code ORCHID42 belongs to the company.")
            doc.canonical_md_path = str(path)
        state.doc.created_by_user_id = state.leaver.id
        state.doc.doc_metadata_json = {"queued_markdown_path": "leaver/queued.md",
            "queued_source_path": "leaver/source.txt", "processed_canonical_md_path": "leaver/processed.md",
            "content": "DOCUMENT_CONTENT_MUST_NOT_ENTER_AUDIT"}
        state.job.status, state.job.stage, state.job.attempt_count = "degraded", "indexed", 2
        state.job.job_metadata_json = {"queued_at": "2026-11-05T01:00:00+00:00",
            "claimed_at": "2026-11-05T01:01:00+00:00", "claimed_token": uuid4().hex,
            "finished_at": "2026-11-05T01:02:00+00:00", "created_by_user_id": str(state.leaver.id),
            "processed_document_id": str(state.doc.id), "processed_result_job_id": str(state.job.id),
            "queued_markdown_path": "leaver/queued.md", "queued_source_path": "leaver/source.txt",
            "processed_canonical_md_path": "leaver/processed.md", "content": "JOB_CONTENT_MUST_NOT_ENTER_AUDIT"}
        document_provenance = {"id": str(state.doc.id), "created_by_user_id": str(state.leaver.id),
            "created_at": state.doc.created_at.isoformat(), "canonical_md_path": state.doc.canonical_md_path,
            "tenant_id": str(state.tenant.id), "scope_id": str(state.leaver.id), "owner_user_id": str(state.leaver.id),
            "source_sha256": state.doc.source_sha256, "status": "ready", "sensitivity": "PL1_public", "agent_searchable": True,
            "canonical_md_sha256": None, "artifact_hash": None,
            "doc_metadata_json": {key: value for key, value in state.doc.doc_metadata_json.items() if key != "content"}}
        job_provenance = {"id": str(state.job.id), "status": "degraded", "stage": "indexed", "attempt_count": 2,
            "tenant_id": str(state.tenant.id), "scope_id": str(state.leaver.id), "document_id": str(state.doc.id),
            "artifact_hash": state.job.artifact_hash, "created_at": state.job.created_at.isoformat(),
            "job_metadata_json": {key: value for key, value in state.job.job_metadata_json.items() if key != "content"}}
        alias = KnowledgeEntity(tenant_id=state.tenant.id, scope_type="person", scope_id=state.leaver.id,
            canonical_name="Orchid alias", entity_type="project", merged_into_entity_id=state.entity.id)
        extra = KnowledgeLink(tenant_id=state.tenant.id, scope_type="person", scope_id=state.leaver.id,
            from_kind="assertion", from_id=state.assertion.id, to_kind="segment", to_id=state.segment.id,
            relation="supported_by", source_refs_json=[source_ref])
        proposal = pending_proposal(state)
        proposal.source_refs_json = [str(state.doc.id)]
        peer_edge = KnowledgeLink(tenant_id=state.tenant.id, scope_type="person", scope_id=state.peer.id,
            from_kind="document", from_id=state.doc.id, to_kind="entity", to_id=state.entity.id,
            relation="mentions", source_refs_json=[source_ref])
        db.add_all([alias, extra, proposal, peer_edge])
        extra_segment = KnowledgeSegment(tenant_id=state.tenant.id, scope_type="person", scope_id=state.leaver.id,
            document_id=state.doc.id, position=1, segment_hash="1" * 64, content="Second dropped segment")
        db.add(extra_segment)
        await db.flush()
        await offboard(state)
        audit = (await db.scalars(select(AuditLog).where(AuditLog.tenant_id == state.tenant.id,
            AuditLog.action == "user:offboarded"))).one()
        provenance = audit.details["merged_knowledge_provenance"]
        assert provenance["documents"] == [document_provenance]
        assert provenance["index_jobs"] == [job_provenance]
        for kind, original in zip(("entities", "assertions", "links"), old_rows[2:5]):
            assert [entry["id"] for entry in provenance[kind]] == [str(original.id)]
        assert provenance["segments"] == [{"document_id": str(state.doc.id), "dropped_segment_count": 2}]
        assert await db.scalar(select(KnowledgeSegment.id).where(KnowledgeSegment.id == extra_segment.id)) is None
        assert "CONTENT_MUST_NOT_ENTER_AUDIT" not in json.dumps(audit.details)
        for row in old_rows:
            assert await db.scalar(select(type(row).id).where(type(row).id == row.id)) is None
        await db.refresh(alias)
        await db.refresh(extra)
        await db.refresh(proposal)
        await db.refresh(peer_edge)
        assert alias.merged_into_entity_id == replacements[state.entity.id]
        assert extra.from_id == replacements[state.assertion.id] and extra.to_id == copies[0].id
        assert extra.to_kind == "document"
        assert extra.source_refs_json[0]["document_id"] == str(copies[0].id)
        assert extra.source_refs_json == [{"document_id": str(copies[0].id)}]
        assert proposal.baseline_document_id == copies[0].id
        assert proposal.source_refs_json == [str(copies[0].id)]
        assert peer_edge.from_id == copies[0].id and peer_edge.to_id == copies[2].id
        assert peer_edge.source_refs_json == [{"document_id": str(copies[0].id)}]
        assert all((tmp_path / f"{user.id}.md").exists() for user in (state.leaver, state.successor))
        service = PersonalKnowledgeService()
        docs = await service.list_personal_documents(db, tenant_id=state.tenant.id,
            owner_user_id=state.successor.id, principal=HumanBrowserPrincipal(user_id=state.successor.id))
        assert [doc.document_id for doc in docs] == [copies[0].id]
        for principal in (HumanBrowserPrincipal(user_id=state.successor.id),
            AgentRuntimePrincipal(agent_id=state.bot.id, requester_user_id=state.successor.id, session_id=str(uuid4()))):
            results = await service.search_personal_with_authority(db, tenant_id=state.tenant.id,
                owner_user_id=state.successor.id, principal=principal, query="ORCHID42")
            assert results.status == "ok" and len(results.hits) == 1
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        assert json.loads(notice.body)["knowledge"] == [{"id": str(copies[0].id), "title": state.doc.title, "merged": True}]
        await db.rollback()


async def test_pending_knowledge_proposals_move_to_successor(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        proposal = pending_proposal(state)
        db.add(proposal)
        await db.flush()
        await offboard(state)
        await db.refresh(proposal)
        assert proposal.owner_user_id == state.successor.id and proposal.status == "pending"
        await db.rollback()


@pytest.mark.parametrize("job_status", ["queued", "claimed", "running"])
@pytest.mark.parametrize("owner", ["leaver", "successor"])
async def test_processing_knowledge_blocks_offboarding_without_changes(owner_sessionmaker, job_status, owner):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.job.scope_id = getattr(state, owner).id
        state.job.status = state.job.stage = job_status
        await db.flush()

        async def snapshot():
            return {model.__tablename__: (await db.execute(select(model.__table__).where(
                Notification.user_id.in_([state.leaver.id, state.successor.id, state.peer.id, state.admin.id])
                if model is Notification else model.tenant_id == state.tenant.id).order_by(model.id))).all()
                for model in (User, Agent, KnowledgeDocument, KnowledgeSegment, KnowledgeIndexJob,
                    KnowledgeEntity, KnowledgeAssertion, KnowledgeLink, KnowledgeGrant, EmailAccount,
                    CustomApiAccount, MCPServer, MachineCredential, ExternalPrincipal, Task, RuntimeTask,
                    AgentTrigger, AuditLog, Notification)}

        before = await snapshot()
        with pytest.raises(HTTPException) as error:
            await offboard(state)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "offboarding_knowledge_processing"
        assert error.value.detail["retryable"] is True
        assert "知识仍在处理中" in error.value.detail["message"]
        assert "一两分钟后重试" in error.value.detail["message"]
        await db.flush()
        assert await snapshot() == before
        await db.rollback()


async def test_processing_knowledge_retry_keeps_stricter_merged_settings(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.doc.sensitivity, state.doc.agent_searchable = "PL3_sensitive", False
        kept = KnowledgeDocument(id=uuid4(), tenant_id=state.tenant.id, scope_type="person",
            scope_id=state.successor.id, owner_user_id=state.successor.id, source_kind=state.doc.source_kind,
            source_sha256=state.doc.source_sha256, title="Kept successor title", status="queued",
            canonical_md_path="successor.md", sensitivity="PL1_public", agent_searchable=True)
        db.add(kept)
        await db.flush()
        job = KnowledgeIndexJob(tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            document_id=kept.id, artifact_hash=state.job.artifact_hash, status="queued", stage="queued")
        db.add(job)
        await db.flush()
        with pytest.raises(HTTPException) as error:
            await offboard(state)
        assert error.value.status_code == 409 and error.value.detail["retryable"] is True
        kept.status, job.status, job.stage = "ready", "ready", "ready"
        segment = KnowledgeSegment(tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            document_id=kept.id, position=0, segment_hash="f" * 64, content="Completed successor content")
        db.add(segment)
        await db.flush()
        await offboard(state)
        await db.refresh(kept)
        assert kept.status == "ready" and kept.sensitivity == "PL3_sensitive" and kept.agent_searchable is False
        assert list(await db.scalars(select(KnowledgeSegment.id).where(KnowledgeSegment.document_id == kept.id))) == [segment.id]
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        assert json.loads(notice.body)["knowledge"] == [{"id": str(kept.id), "title": kept.title, "merged": True}]
        await db.rollback()


@pytest.mark.parametrize("missing", ["subject_entity_id", "object_entity_id", "both"])
async def test_kept_assertion_fills_missing_entity_links_and_repoints(owner_sessionmaker, missing):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.assertion.object_entity_id = state.entity.id
        entity = KnowledgeEntity(id=uuid4(), tenant_id=state.tenant.id, scope_type="person",
            scope_id=state.successor.id, canonical_name=state.entity.canonical_name, entity_type=state.entity.entity_type)
        override = KnowledgeEntity(id=uuid4(), tenant_id=state.tenant.id, scope_type="person",
            scope_id=state.successor.id, canonical_name="Successor override", entity_type="project")
        db.add_all([entity, override])
        await db.flush()
        kept = KnowledgeAssertion(tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            subject_text=state.assertion.subject_text, predicate=state.assertion.predicate, object_text=state.assertion.object_text,
            subject_entity_id=None if missing in ("subject_entity_id", "both") else override.id,
            object_entity_id=None if missing in ("object_entity_id", "both") else override.id)
        db.add(kept)
        await db.flush()
        await offboard(state)
        await db.refresh(kept)
        for field in ("subject_entity_id", "object_entity_id"):
            assert getattr(kept, field) == (entity.id if missing in (field, "both") else override.id)
        await db.rollback()


@pytest.mark.parametrize("source_kind", ["upload", "url"])
@pytest.mark.parametrize("successor_status", ["queued", "failed", "ready", "degraded", "archived"])
async def test_document_overlap_preserves_usable_content_and_restricts_settings(owner_sessionmaker, tmp_path,
        source_kind, successor_status):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.doc.source_kind = source_kind
        state.doc.canonical_md_sha256, state.doc.artifact_hash = "a" * 64, "leaver-artifact"
        state.doc.sensitivity = "PL3_sensitive"
        state.doc.canonical_md_path = str(tmp_path / "leaver.md")
        kept = KnowledgeDocument(id=uuid4(), tenant_id=state.tenant.id, scope_type="person",
            scope_id=state.successor.id, owner_user_id=state.successor.id, source_kind=source_kind,
            source_sha256=state.doc.source_sha256, title="Successor snapshot", status=successor_status,
            canonical_md_path=str(tmp_path / "successor.md"), canonical_md_sha256="b" * 64,
            artifact_hash="successor-artifact", sensitivity="PL2_pii", agent_searchable=False)
        db.add(kept)
        await db.flush()
        other_segment = KnowledgeSegment(id=uuid4(), tenant_id=state.tenant.id, scope_type="person",
            scope_id=state.successor.id, document_id=kept.id, position=0, segment_hash="c" * 64,
            content="Successor snapshot differs from leaver snapshot")
        db.add(other_segment)
        await db.flush()
        leaver_wins = successor_status not in ("ready", "degraded", "archived")
        winner, loser = (state.segment, other_segment) if leaver_wins else (other_segment, state.segment)
        state.entity.source_refs_json = [{"document_id": str(loser.document_id),
            "segment_id": str(loser.id), "seg_hash": loser.segment_hash, "position": 0}]
        state.link.to_kind, state.link.to_id = "segment", loser.id
        for path in (state.doc.canonical_md_path, kept.canonical_md_path):
            from pathlib import Path
            Path(path).write_text("Retained original snapshot")
        await db.flush()
        await offboard(state)
        await db.refresh(kept)
        assert kept.status == ("ready" if leaver_wins else successor_status)
        assert kept.canonical_md_sha256 == ("a" * 64 if leaver_wins else "b" * 64)
        assert kept.artifact_hash == ("leaver-artifact" if leaver_wins else "successor-artifact")
        assert kept.canonical_md_path == str(tmp_path / ("leaver.md" if leaver_wins else "successor.md"))
        assert kept.sensitivity == "PL3_sensitive" and kept.agent_searchable is False
        segments = list(await db.scalars(select(KnowledgeSegment).where(KnowledgeSegment.document_id == kept.id)))
        assert [segment.id for segment in segments] == [winner.id]
        await db.refresh(state.entity)
        await db.refresh(state.link)
        assert state.entity.source_refs_json == [{"document_id": str(kept.id)}]
        assert state.link.to_kind == "document" and state.link.to_id == kept.id
        audit = (await db.scalars(select(AuditLog).where(AuditLog.tenant_id == state.tenant.id,
            AuditLog.action == "user:offboarded"))).one()
        provenance = audit.details["merged_knowledge_provenance"]
        dropped = provenance["documents"][0]
        assert dropped["id"] == str(state.doc.id) and dropped["status"] == "ready"
        assert dropped["canonical_md_sha256"] == "a" * 64 and dropped["artifact_hash"] == "leaver-artifact"
        assert dropped["sensitivity"] == "PL3_sensitive" and dropped["agent_searchable"] is True
        assert provenance["segments"] == [{"document_id": str(loser.document_id), "dropped_segment_count": 1}]
        assert all((tmp_path / name).is_file() for name in ("leaver.md", "successor.md"))
        await db.rollback()


async def test_different_documents_merge_shared_graph_evidence(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        service = PersonalKnowledgeService(extractor=None)
        state.entity.canonical_name, state.entity.entity_type = "Hive Inc", "organization"
        state.entity.aliases_json, state.entity.description, state.entity.confidence = ["Hive"], "Leaver description", .95
        state.entity.source_refs_json = [{"document_id": str(state.doc.id)}]
        other_doc = KnowledgeDocument(id=uuid4(), tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            owner_user_id=state.successor.id, source_kind="paste", source_sha256="d" * 64, title="Older private document",
            canonical_md_path="successor.md", status="ready")
        db.add(other_doc)
        await db.flush()
        other = await service._upsert_entity(db, tenant_id=state.tenant.id, owner_user_id=state.successor.id,
            canonical_name="Hive Inc", entity_type="organization", aliases=["Hive Incorporated"],
            description="Successor description", confidence=.4, source_ref={"document_id": str(other_doc.id)})
        other.confidence = .4
        state.assertion.subject_text = "Hive Inc"
        state.assertion.source_refs_json, state.assertion.confidence = list(state.entity.source_refs_json), .9
        assertion = KnowledgeAssertion(id=uuid4(), tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            source_document_id=other_doc.id, subject_entity_id=other.id, subject_text="Hive Inc",
            predicate=state.assertion.predicate, object_text=state.assertion.object_text, status="rejected",
            source_refs_json=[{"document_id": str(other_doc.id)}], confidence=.3)
        state.link.from_kind, state.link.from_id = "entity", state.entity.id
        state.link.to_kind, state.link.to_id = "assertion", state.assertion.id
        state.link.source_refs_json, state.link.confidence = list(state.entity.source_refs_json), .8
        db.add(assertion)
        await db.flush()
        link = KnowledgeLink(id=uuid4(), tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            from_kind="entity", from_id=other.id, to_kind="assertion", to_id=assertion.id,
            relation=state.link.relation, source_refs_json=[{"document_id": str(other_doc.id)}], confidence=.2)
        db.add(link)
        await db.flush()
        await offboard(state)
        for row, confidence in ((other, .95), (assertion, .9), (link, .8)):
            await db.refresh(row)
            assert row.confidence == confidence
            assert {ref["document_id"] for ref in row.source_refs_json} == {str(state.doc.id), str(other_doc.id)}
        assert other.aliases_json == ["Hive Incorporated", "Hive"] and other.description == "Successor description"
        assert assertion.status == "rejected" and assertion.subject_entity_id == other.id
        assert link.from_id == other.id and link.to_id == assertion.id
        await db.rollback()


async def test_handed_over_scope_grant_never_opens_successors_older_documents(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        older = KnowledgeDocument(id=uuid4(), tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            owner_user_id=state.successor.id, source_kind="paste", source_sha256="e" * 64, title="Older private document",
            canonical_md_path="private.md", status="ready", agent_searchable=True)
        db.add(older)
        await db.flush()
        db.add(KnowledgeSegment(tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            document_id=older.id, position=0, segment_hash="e" * 64, content="ORCHID42 PRIVATE_OLDER_SUCCESSOR"))
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        grant = KnowledgeGrant(tenant_id=state.tenant.id, scope_type="person", scope_id=state.leaver.id,
            resource_type="scope", resource_id=state.leaver.id, grantee_type="agent", grantee_id=state.bot.id,
            permission="read", requester_user_id=state.leaver.id, purpose="autonomous_agent", expires_at=expiry,
            sensitivity_ceiling="PL2_pii", binding_key="scope-binding", created_by_user_id=state.leaver.id)
        db.add(grant)
        await db.flush()
        await offboard(state)
        result = await PersonalKnowledgeService().search_personal_with_authority(db, tenant_id=state.tenant.id,
            owner_user_id=state.successor.id, principal=AgentRuntimePrincipal(agent_id=state.bot.id,
                requester_user_id=state.successor.id, purpose="autonomous_agent", autonomous=True), query="ORCHID42")
        assert result.status == "ok" and {hit.document_id for hit in result.hits} == {state.doc.id}
        active = list(await db.scalars(select(KnowledgeGrant).where(KnowledgeGrant.tenant_id == state.tenant.id,
            KnowledgeGrant.revoked_at.is_(None))))
        assert len(active) == 1
        moved = active[0]
        assert moved.resource_type == "document" and moved.document_id == moved.resource_id == state.doc.id
        assert moved.scope_id == moved.requester_user_id == state.successor.id
        assert moved.purpose == "autonomous_agent" and moved.expires_at == expiry
        assert moved.sensitivity_ceiling == "PL2_pii" and moved.binding_key != grant.binding_key
        assert older.agent_searchable is True and older.sensitivity == "PL1_public"
        await db.rollback()


async def test_handover_preserves_active_task_assignee(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.task.responsible_user_id = state.peer.id
        await db.flush()
        await offboard(state)
        await db.refresh(state.task)
        assert state.task.created_by == state.successor.id
        assert state.task.responsible_user_id == state.peer.id
        await db.rollback()


@pytest.mark.parametrize("active", [True, False])
async def test_other_bot_creator_fallback_requires_active_user(owner_sessionmaker, active):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.peer.is_active = active
        other = Agent(tenant_id=state.tenant.id, creator_id=state.peer.id,
            owner_user_id=None if active else state.peer.id, name="Other bot", role_description="Company work")
        db.add(other)
        await db.flush()
        task = Task(tenant_id=state.tenant.id, agent_id=other.id, created_by=state.leaver.id,
            title="Not started work", status="pending", request_id=uuid4().hex, request_hash="0" * 64)
        db.add(task)
        await db.flush()
        await offboard(state)
        await db.refresh(task)
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        work = json.loads(notice.body)["work"]
        if active:
            assert task.created_by == task.responsible_user_id == state.peer.id
            assert str(task.id) not in {item["task_id"] for item in work}
        else:
            assert task.created_by == state.leaver.id and task.responsible_user_id is None
            assert next(item for item in work if item["task_id"] == str(task.id))["needs_admin"] is True
        await db.rollback()


async def test_autonomous_handed_over_bot_keeps_only_its_knowledge_grants(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        other_bot = Agent(tenant_id=state.tenant.id, creator_id=state.peer.id, owner_user_id=state.peer.id,
            name="Other bot", role_description="Other work")
        db.add(other_bot)
        await db.flush()
        grants = [KnowledgeGrant(tenant_id=state.tenant.id, scope_type="person", scope_id=state.leaver.id,
            resource_type="scope", resource_id=state.leaver.id, grantee_type=kind, grantee_id=grantee,
            permission="read", requester_user_id=state.leaver.id if kind == "agent" else None,
            purpose="autonomous_agent" if kind == "agent" else None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_by_user_id=state.leaver.id)
            for kind, grantee in (("agent", state.bot.id), ("agent", other_bot.id), ("user", state.peer.id))]
        db.add_all(grants)
        await db.flush()
        service = PersonalKnowledgeService()
        before = await service.search_personal_with_authority(db, tenant_id=state.tenant.id,
            owner_user_id=state.leaver.id, principal=AgentRuntimePrincipal(agent_id=state.bot.id,
                requester_user_id=state.leaver.id, purpose="autonomous_agent", autonomous=True), query="ORCHID42")
        assert before.status == "ok" and len(before.hits) == 1
        await offboard(state)
        after = await service.search_personal_with_authority(db, tenant_id=state.tenant.id,
            owner_user_id=state.successor.id, principal=AgentRuntimePrincipal(agent_id=state.bot.id,
                requester_user_id=state.successor.id, purpose="autonomous_agent", autonomous=True), query="ORCHID42")
        assert after.status == "ok" and len(after.hits) == 1
        for grant in grants:
            await db.refresh(grant)
        moved = (await db.scalars(select(KnowledgeGrant).where(KnowledgeGrant.tenant_id == state.tenant.id,
            KnowledgeGrant.revoked_at.is_(None)))).one()
        assert moved.scope_id == moved.requester_user_id == state.successor.id
        assert moved.resource_type == "document" and moved.document_id == moved.resource_id == state.doc.id
        assert grants[0].revoked_at is not None
        assert all(grant.revoked_at is not None for grant in grants[1:])
        await db.rollback()


@pytest.mark.parametrize("has_owner", [True, False])
async def test_other_bot_work_moves_only_to_its_owner_or_needs_admin(owner_sessionmaker, has_owner):
    from app.api.tasks import _load_authorized_task
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.peer.is_active = has_owner
        bot = Agent(tenant_id=state.tenant.id, creator_id=state.peer.id,
            owner_user_id=state.peer.id if has_owner else None, name="Company bot", role_description="Company work")
        db.add(bot)
        await db.flush()
        task = Task(tenant_id=state.tenant.id, agent_id=bot.id, title="Finish the company report", type="todo",
            status="pending", created_by=state.leaver.id, request_id=uuid4().hex, request_hash="0" * 64)
        db.add(task)
        await db.flush()
        await offboard(state)
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        work = json.loads(notice.body)["work"]
        await db.refresh(task)
        if has_owner:
            assert task.created_by == task.responsible_user_id == state.peer.id
            await _load_authorized_task(db, state.peer, agent_id=bot.id, task_id=task.id,
                action="execute", operator_view=False, operator_reason=None)
            assert str(task.id) not in {item["task_id"] for item in work}
        else:
            assert next(item for item in work if item["task_id"] == str(task.id))["needs_admin"] is True
        with pytest.raises(HTTPException):
            await _load_authorized_task(db, state.successor, agent_id=bot.id, task_id=task.id,
                action="read", operator_view=False, operator_reason=None)
        await db.rollback()


@pytest.mark.parametrize("reoffboarding", [False, True])
async def test_no_empty_handover_notice(owner_sessionmaker, reoffboarding):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        if reoffboarding:
            await offboard(state)
        else:
            state.bot.owner_user_id = state.bot.creator_id = state.bot.sponsor_user_id = state.peer.id
            state.task.status = "done"
            state.runtime.root_user_id = state.peer.id
            for row in (state.doc, state.segment, state.entity, state.assertion, state.link, state.job):
                row.scope_id = state.peer.id
            await db.flush()
        count = len(list(await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))))
        await offboard_loaded_user(db, target_user=state.leaver, successor=state.successor, actor=state.admin,
            expected_agent_ids=[], reason="Employment ended", request_id="new-request")
        assert len(list(await db.scalars(select(Notification).where(Notification.user_id == state.successor.id)))) == count
        await db.rollback()


async def test_ordinary_team_task_creator_keeps_control_and_assignee_gains_none(owner_sessionmaker):
    from app.api.tasks import _load_authorized_task, list_tasks
    from app.models.organization_identity import BusinessTeam
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        team = BusinessTeam(tenant_id=state.tenant.id, name="Ordinary team", created_by_user_id=state.leaver.id)
        db.add(team)
        await db.flush()
        state.task.business_team_id, state.task.audience_kind = team.id, "business_team"
        state.task.responsible_user_id = state.successor.id
        await db.flush()
        await _load_authorized_task(db, state.leaver, agent_id=state.bot.id, task_id=state.task.id,
            action="execute", operator_view=False, operator_reason=None)
        visible = await list_tasks(agent_id=state.bot.id, current_user=state.leaver, db=db,
            operator_view=False, operator_reason=None)
        assert str(state.task.id) in {str(item.id) for item in visible}
        with pytest.raises(HTTPException):
            await _load_authorized_task(db, state.successor, agent_id=state.bot.id, task_id=state.task.id,
                action="execute", operator_view=False, operator_reason=None)
        await db.rollback()


async def test_offboarding_an_assignee_preserves_active_creators_work(owner_sessionmaker):
    from app.api.tasks import _load_authorized_task
    from app.models.agent import AgentPermission
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        team = BusinessTeam(tenant_id=state.tenant.id, name="Creator's team", created_by_user_id=state.peer.id)
        db.add(team)
        await db.flush()
        db.add_all([AgentPermission(tenant_id=state.tenant.id, agent_id=state.bot.id,
            scope_type="user", scope_id=state.peer.id, access_level="use"),
            *[BusinessTeamMembership(tenant_id=state.tenant.id, business_team_id=team.id,
                user_id=user.id, role=role, status="active")
                for user, role in ((state.peer, "lead"), (state.leaver, "member"))]])
        state.peer_task.business_team_id, state.peer_task.audience_kind = team.id, "business_team"
        state.peer_task.responsible_user_id = state.leaver.id
        await db.flush()
        await _load_authorized_task(db, state.peer, agent_id=state.bot.id, task_id=state.peer_task.id,
            action="execute", operator_view=False, operator_reason=None)
        await offboard(state)
        assert state.peer_task.created_by == state.peer.id
        await _load_authorized_task(db, state.peer, agent_id=state.bot.id, task_id=state.peer_task.id,
            action="execute", operator_view=False, operator_reason=None)
        with pytest.raises(HTTPException):
            await _load_authorized_task(db, state.successor, agent_id=state.bot.id, task_id=state.peer_task.id,
                action="execute", operator_view=False, operator_reason=None)
        notice = (await db.scalars(select(Notification).where(Notification.user_id == state.successor.id))).one()
        assert str(state.peer_task.id) not in {item["task_id"] for item in json.loads(notice.body)["work"]}
        await db.rollback()


async def test_shared_then_unshared_handover_task_still_belongs_to_successor(owner_sessionmaker):
    from app.api.tasks import _load_authorized_task
    from app.models.audit import AuditLog
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
    from app.schemas.schemas import TaskTeamSharingIn
    from app.services.team_business_tasks import change_task_team_sharing
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        await offboard(state)
        audit = (await db.scalars(select(AuditLog).where(AuditLog.agent_id == state.bot.id,
            AuditLog.action == "agent:handover"))).one()
        team = BusinessTeam(tenant_id=state.tenant.id, name="Handover team", created_by_user_id=state.successor.id)
        db.add(team)
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=state.tenant.id, business_team_id=team.id,
            user_id=state.successor.id, role="lead", status="active"))
        await db.flush()
        for shared in (True, False):
            await db.refresh(state.task)
            body = TaskTeamSharingIn(request_id=uuid4(), expected_updated_at=state.task.updated_at,
                business_team_id=team.id if shared else None,
                responsible_user_id=state.successor.id if shared else None, reason="Team visibility")
            await change_task_team_sharing(db, user=state.successor, task=state.task, body=body)
        assert state.task.responsible_user_id is None
        assert audit.details["task_original_creators"][str(state.task.id)] == str(state.leaver.id)
        await _load_authorized_task(db, state.successor, agent_id=state.bot.id, task_id=state.task.id,
            action="execute", operator_view=False, operator_reason=None)
        with pytest.raises(HTTPException):
            await _load_authorized_task(db, state.peer, agent_id=state.bot.id, task_id=state.task.id,
                action="read", operator_view=False, operator_reason=None)


@pytest.mark.parametrize("kind", ["email", "api", "mcp"])
async def test_cross_tenant_identity_is_checked_before_revocation(owner_sessionmaker, kind):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        outsider = Tenant(name="Other company", slug=uuid4().hex)
        db.add(outsider)
        await db.flush()
        state.leaver.is_active = False
        await db.flush()
        with pytest.raises(HTTPException) as error:
            if kind == "email":
                await email_accounts.active_user(db, state.leaver.id, outsider.id)
            elif kind == "api":
                await custom_api_accounts.active_user(db, state.leaver.id, outsider.id)
            else:
                state.leaver.tenant_id = outsider.id
                await db.flush()
                await mcp_accounts.account_access(db, server=state.server, account_id=state.mcp.id,
                    agent_id=state.bot.id, user=state.successor)
        assert error.value.status_code == 403 and "已吊销" not in error.value.detail
        await db.rollback()
