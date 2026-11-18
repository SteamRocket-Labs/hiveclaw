"""Recipient publication and non-owner reads retain their existing boundaries."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.database import tenant_scoped_session
from tests.integration.test_rl1_member_files_without_replay_postgres import (
    _call, _run, _wires, member_web, native_workspace,  # noqa: F401
)
from tests.integration.test_rl1_round2_boundaries_postgres import _foreign_source
from tests.integration.test_r7b_delivered_artifact_reads import report_history  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_revoked_company_source_keeps_private_report_but_blocks_email_and_grantee(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services import agent_tools, runtime_task_worker
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from tests.integration.test_artifact_publication_transactions import publish_company_source
    from tests.integration.test_company_knowledge_direct_import import _member_principal
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    n, path = member_web, "workspace/company-report.md"
    body = "COMPANY_SOURCE_MARKER\nDraft"
    edited = body.replace("Draft", "Reviewed")
    publication_id, _ = await publish_company_source(
        owner_sessionmaker, tenant=n.tenant_id, user=n.user_id, root=n.data_root,
    )
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id,
            capability="agent.knowledge.read", allowed=True, requires_approval=False))
        await db.commit()
    stream = ScriptedLLMClient.stream

    async def revoke_after_write(self, **kwargs):
        if len(self.requests) == 2:
            assert (n.agent_root / path).read_text() == body
            async with owner_sessionmaker() as db:
                grant = await db.scalar(select(ResourcePermission).where(
                    ResourcePermission.principal_id == n.user_id,
                    ResourcePermission.resource_type == "company_knowledge_scope",
                ))
                await db.delete(grant)
                await db.commit()
            async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
                result = await CompanyKnowledgeGateway().read(
                    db,
                    principal=_member_principal(tenant_id=n.tenant_id, user_id=n.user_id),
                    request=CompanyKnowledgeReadRequest(
                        document_id=None, publication_id=publication_id, segment_ids=(), max_chars=None,
                        trace_id="revoked-company-source",
                    ),
                )
                assert result.status == "not_found_or_denied", result.as_dict()
        return await stream(self, **kwargs)

    sink = []

    async def deliver(*args, **kwargs):
        sink.append((args, kwargs))
        return "Delivered to the local test sink"

    async def notify(**kwargs):
        pass

    monkeypatch.setattr(ScriptedLLMClient, "stream", revoke_after_write)
    monkeypatch.setattr(agent_tools, "_handle_email_tool", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    source_args = {"publication_id": str(publication_id)}
    model = await _run(n, [
        {"tool_calls": [_call("read_company_kb", "source-before", **source_args)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("write_file", "write-report", path=path, content=body)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("read_file", "read-report", path=path)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("edit_file", "edit-report", path=path, old_text="Draft", new_text="Reviewed")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call("read_file", "read-edited", path=path)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("send_email", "send-report", to="recipient@example.test",
            subject="Company report", body=edited)], "finish_reason": "tool_calls"},
    ], monkeypatch)
    replies = {
        message["tool_call_id"]: message["content"]
        for request in model.requests
        for message in request["messages"]
        if message["role"] == "tool"
    }
    assert "COMPANY_SOURCE_MARKER" in replies["source-before"]
    # owner 11-18 decision A: the report remains usable inside the member's private session.
    assert json.loads(replies["read-report"])["content"].startswith(body)
    assert json.loads(replies["read-edited"])["content"].startswith(edited)
    assert (n.agent_root / path).read_text() == edited

    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == n.run_id, SessionToolInvocation.tool_name == "send_email",
        ))
        assert invocation is not None and invocation.permission_item_id is not None
        assert invocation.provider_arguments_json["body"] == edited
        invocation_id, permission_id = invocation.id, invocation.permission_item_id
    approved = await n.client.post(
        f"/agents/{n.agent_id}/sessions/{n.session_id}/permissions/{permission_id}/resolve",
        json={"action": "allow_once"},
    )
    assert approved.status_code == 200, approved.text
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.get(SessionToolInvocation, invocation_id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        assert sink == []
        assert invocation.effect_state == "prepared_not_started"
        assert event.metadata_json["v2_payload"]["outcome"] == "failed"

    grantee = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=grantee, tenant_id=n.tenant_id, username=grantee.hex,
            email=f"{grantee.hex}@fixture.test", password_hash="fixture", display_name="Grantee", role="member"))
        await db.flush()
        db.add(ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=grantee,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent_id, path),
            actions=["read"], conditions={}))
        await db.commit()
        card = await db.scalar(select(ChatArtifact).where(
            ChatArtifact.runtime_task_id == n.run_id, ChatArtifact.path == path,
            ChatArtifact.snapshot_json["content_hash"].astext == hashlib.sha256(edited.encode()).hexdigest(),
        ))
        assert card is not None, "OWN_PRIVATE_EDIT_DID_NOT_DELIVER_A_CARD"
        card_id = card.id
        db.add(ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=grantee,
            resource_type="chat_artifact", resource_id=card_id, actions=["read", "download"], conditions={}))
        await db.commit()

    card_url = f"/agents/{n.agent_id}/files/artifacts/{card_id}/content"
    owner_read = await n.client.get(card_url)
    assert owner_read.status_code == 200 and owner_read.json()["content"] == edited, owner_read.text
    n.reader = grantee
    for url, params in [(card_url, {}), (f"/agents/{n.agent_id}/files/content", {"path": path})]:
        denied = await n.client.get(url, params=params)
        assert denied.status_code == 403 and "COMPANY_SOURCE_MARKER" not in denied.text, denied.text


@pytest.fixture
async def feishu_effects(member_web, owner_sessionmaker, monkeypatch):  # noqa: F811
    from app.models.capability_policy import CapabilityPolicy
    from app.models.channel_config import ChannelConfig
    from app.models.org import OrgMember
    from app.services.feishu_service import FeishuService

    n = member_web
    recipient, config_id = f"ou_{n.user_id.hex}", uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChannelConfig(id=config_id, tenant_id=n.tenant_id, agent_id=n.agent_id, channel_type="feishu",
            app_id="local-feishu-app", app_secret="local-feishu-secret", is_configured=True))
        db.add(OrgMember(tenant_id=n.tenant_id, name="Fixture recipient", feishu_open_id=recipient))
        for capability in ("channel.feishu.message", "channel.feishu.document"):
            db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()
    messages = []

    async def send_message(_self, *_args, receive_id, content, **_kwargs):
        messages.append((receive_id, json.loads(content)["text"]))
        return {"code": 0}

    monkeypatch.setattr(FeishuService, "send_message", send_message)
    return SimpleNamespace(recipient=recipient, config_id=config_id, messages=messages)


@pytest.mark.parametrize(("tool_name", "mode", "use_attachment", "source"), [
    ("send_email", "full_authority", False, "revoked"),
    ("send_email", "full_authority", False, "agent_only"),
    ("send_email", "full_authority", False, "missing_receipt"),
    ("send_email", "full_authority", True, "revoked"),
    ("send_email", "confirm_first", True, "revoked"),
    ("send_email", "full_authority", True, "missing_receipt"),
    ("send_email", "confirm_first", True, "missing_receipt"),
    ("send_email", "full_authority", True, "intact"),
    ("send_email", "confirm_first", True, "intact"),
    ("send_email", "full_authority", True, "changed_after_check"),
    ("send_feishu_message", "confirm_first", False, "revoked"),
    ("send_feishu_message", "confirm_first", False, "intact"),
])
async def test_outward_effect_requires_authorized_body_and_attachment_sources(
    member_web, feishu_effects, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
    tool_name, mode, use_attachment, source,
):
    from app.models.capability_policy import CapabilityPolicy
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.models.organization_identity import ContentInputReceipt
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionToolInvocation
    from app.services import email_accounts, email_service, runtime_task_worker
    from app.services.owner_action_policy import save_owner_action_policy
    from tests.integration.test_artifact_publication_transactions import publish_company_source, stage_report

    n, path = member_web, "workspace/company-report.md"
    dependencies = []
    if source in {"revoked", "intact", "changed_after_check"}:
        publication, version = await publish_company_source(
            owner_sessionmaker, tenant=n.tenant_id, user=n.user_id, root=n.data_root,
        )
        dependencies.append({"source_kind": "company_knowledge_publication",
            "source_id": str(publication), "source_version": version})
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability="channel.email.send",
            allowed=True, requires_approval=False))
        await save_owner_action_policy(db, tenant_id=n.tenant_id, agent_id=n.agent_id,
            actions={"tool.external_effect": mode, "tool.local_read": "full_authority",
                "tool.local_write": "confirm_first"}, changed_by_user_id=n.user_id)
        if source == "agent_only":
            owner = uuid.uuid4()
            original = n.data_root / "agent-only-source.md"
            original.write_text("Fixture source granted to the agent")
            version = hashlib.sha256(original.read_bytes()).hexdigest()
            document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant_id, scope_type="person", scope_id=owner,
                source_kind="text", source_sha256=version, canonical_md_path=original.name,
                canonical_md_sha256=version, title="Fixture source", status="ready")
            db.add(document)
            await db.flush()
            db.add(KnowledgeGrant(tenant_id=n.tenant_id, scope_id=owner, resource_type="document",
                resource_id=document.id, document_id=document.id, grantee_type="agent", grantee_id=n.agent_id,
                requester_user_id=n.user_id, session_id=str(n.session_id), purpose="interactive_session",
                permission="read", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            dependencies.append({"source_kind": "knowledge_document",
                "source_id": str(document.id), "source_version": version})
        report = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id,
            session=n.session_id, root=n.agent_root, path=path, dependencies=dependencies)
        if source == "missing_receipt":
            await db.flush()
            await db.execute(delete(ContentInputReceipt).where(ContentInputReceipt.id == report.id))
        if source == "revoked":
            grant = await db.scalar(select(ResourcePermission).where(
                ResourcePermission.principal_id == n.user_id,
                ResourcePermission.resource_type == "company_knowledge_scope",
            ))
            await db.delete(grant)
        await db.commit()

    sink = []

    async def mailbox(*args, **kwargs):
        if source == "changed_after_check":
            (n.agent_root / path).write_text("A newer file without a matching receipt")
        return {"email_address": "sender@example.test", "auth_code": "local-test-only"}

    async def deliver(*, body, _prepared_attachments, **kwargs):
        sink.append((body, _prepared_attachments))
        return "Delivered to the local test sink"

    async def notify(**kwargs):
        pass

    monkeypatch.setattr(email_accounts, "resolve_email_config", mailbox)
    monkeypatch.setattr(email_service, "send_email", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)

    body = (n.agent_root / path).read_text()
    arguments = ({"to": "recipient@example.test", "subject": "Fixture report", "body": body}
        if tool_name == "send_email" else {"open_id": feishu_effects.recipient, "message": body})
    script = []
    if use_attachment:
        arguments.update(body="See attached report.", attachments=[path])
    else:
        script.append({"tool_calls": [_call("read_file", "read-report", path=path)], "finish_reason": "tool_calls"})
    script.extend([
        {"tool_calls": [_call(tool_name, "send-report", **arguments)], "finish_reason": "tool_calls"},
        {"content": "Finished."},
    ])
    model = await _run(n, script, monkeypatch)
    if not use_attachment:
        read = next(message for message in model.requests[1]["messages"] if message.get("tool_call_id") == "read-report")
        # owner 11-18 decision A: reading the report in private succeeds before the send is refused.
        assert json.loads(read["content"])["content"].startswith(body)
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == n.run_id, SessionToolInvocation.tool_name == tool_name,
        ))
        assert invocation is not None
        invocation_id, permission_id = invocation.id, invocation.permission_item_id
    assert (permission_id is not None) == (mode == "confirm_first" and tool_name == "send_email")
    if permission_id is not None:
        response = await n.client.post(f"/agents/{n.agent_id}/sessions/{n.session_id}/permissions/{permission_id}/resolve",
            json={"action": "allow_once"})
        assert response.status_code == 200, response.text

    if source in {"intact", "changed_after_check"}:
        if tool_name == "send_email":
            assert sink == [(arguments["body"], [(n.agent_root.resolve() / path, body.encode())])]
        else:
            assert feishu_effects.messages == [(feishu_effects.recipient, body.strip())]
    else:
        assert sink == [], "UNAUTHORIZED_SOURCE_REACHED_EMAIL_SINK"
        assert feishu_effects.messages == [], "UNAUTHORIZED_SOURCE_REACHED_FEISHU_CHANNEL"
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation.effect_state == (
            "effect_committed" if source in {"intact", "changed_after_check"} else "prepared_not_started"
        )
        assert invocation.result_event_id is not None


@pytest.mark.parametrize("tool_name", ["send_feishu_message", "feishu_doc_share"])
async def test_revoked_group_memory_cannot_reach_feishu_effects(
    member_web, feishu_effects, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tool_name,  # noqa: F811
):
    from httpx import AsyncClient, Response

    from app.memory.write_gate import MemoryWriteDecision
    from app.models.capability_policy import CapabilityPolicy
    from app.models.channel_config import ChannelConfig
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services import agent_tools
    from app.services.agent_tool_domains import feishu_sharing
    from app.services.channel_session import find_or_create_shared_group_session
    from app.services.external_principal_service import bind_authenticated_self_channel_principal
    from app.services.feishu_service import FeishuService
    from tests.integration.test_m4d_group_memory_postgres import save_group
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    n = member_web
    members = {feishu_effects.recipient}
    doc_requests = []

    async def current_members(_self, *_args, **_kwargs):
        return set(members)

    async def review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={})

    async def token(_agent_id):
        return "local-feishu-app", "local-feishu-token"

    async def wiki_node(_document_token, _token):
        return None

    async def share(_self, url, *, json, **_kwargs):
        doc_requests.append((url, json))
        return Response(200, json={"code": 0})

    async def notify(**_kwargs):
        pass

    monkeypatch.setattr(FeishuService, "get_chat_member_open_ids", current_members)
    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", review)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify)
    monkeypatch.setattr(feishu_sharing, "_get_feishu_token", token)
    monkeypatch.setattr(feishu_sharing, "_feishu_wiki_get_node", wiki_node)
    monkeypatch.setattr(AsyncClient, "post", share)
    async with owner_sessionmaker() as db:
        config = await db.get(ChannelConfig, feishu_effects.config_id)
        for capability in ("agent.memory.read", "agent.memory.write"):
            db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability=capability,
                allowed=True, requires_approval=False))
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=n.tenant_id, config=config,
            provider_subject_id=feishu_effects.recipient, user_id=n.user_id, actor_user_id=n.user_id)
        assert bound.actor.id == n.user_id
        group = await find_or_create_shared_group_session(db, tenant_id=n.tenant_id, agent_id=n.agent_id,
            installation_ref=str(config.id), chat_id="rl1-fixture-group", first_message_title="Group memory work")
        await db.commit()

    group_memory = SimpleNamespace(n=SimpleNamespace(tenant=n.tenant_id, user=n.user_id, agent=n.agent_id,
        service=agent_tools._get_tool_runtime_service()), session=group.id, runs={}, rounds={})
    await save_group(group_memory, app_user_sessionmaker)

    stream = ScriptedLLMClient.stream

    async def revoke_after_admission(self, **kwargs):
        if len(self.requests) == 1:
            members.remove(feishu_effects.recipient)
        return await stream(self, **kwargs)

    monkeypatch.setattr(ScriptedLLMClient, "stream", revoke_after_admission)
    marker = "GROUP_RELEASE_MARKER"
    arguments = ({"open_id": feishu_effects.recipient, "message": marker} if tool_name == "send_feishu_message"
        else {"document_token": marker, "action": "add", "member_open_ids": [feishu_effects.recipient]})
    model = await _run(n, [
        {"tool_calls": [_call("search_memory", "recall-group", query="release", scope="facts")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call(tool_name, "outward-group", **arguments)], "finish_reason": "tool_calls"},
        {"content": "Finished."},
    ], monkeypatch)
    recalled = next(message["content"] for message in model.requests[1]["messages"]
        if message.get("tool_call_id") == "recall-group")
    assert marker in recalled, recalled
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == n.run_id, SessionToolInvocation.tool_name == tool_name,
        ))
        assert invocation is not None and invocation.permission_item_id is None
        result = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id,
        ))
        assert str(group_memory.source_id) in {
            dependency["source_id"] for dependency in result.model_request_snapshot_json["input_bindings"]["dependencies"]
        }
        assert feishu_effects.messages == [], "REVOKED_GROUP_MEMORY_REACHED_FEISHU_CHANNEL"
        assert doc_requests == [], "REVOKED_GROUP_MEMORY_REACHED_FEISHU_DOCUMENT_CLIENT"
        assert invocation.effect_state == "prepared_not_started" and invocation.result_event_id is not None


@pytest.mark.parametrize("role", ["org_admin", "member"])
@pytest.mark.parametrize(("owned", "receipted"), [(True, True), (True, False), (False, True)])
async def test_terminal_read_delivery_keeps_own_card_and_rechecks_granted_report(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch, role, owned, receipted,  # noqa: F811
):
    from fastapi import HTTPException

    from app.core.resource_authority import authorize_resource_action, workspace_resource_id
    from app.models.chat_artifact import ChatArtifact
    from app.models.organization_identity import ContentInputReceipt
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from tests.integration.test_artifact_publication_transactions import stage_report
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    n, path = member_web, "workspace/owned-report.md"
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        (await db.get(User, n.user_id)).role = role
        receipt = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id,
            user=n.user_id if owned else source.owner, session=n.session_id if owned else source.session,
            root=n.agent_root, path=path, dependencies=[source.dependency])
        if not receipted:
            await db.flush()
            await db.execute(delete(ContentInputReceipt).where(ContentInputReceipt.id == receipt.id))
        if not owned:
            db.add(ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=n.user_id,
                resource_type="workspace_file", resource_id=workspace_resource_id(n.agent_id, path),
                actions=["read"], conditions={}))
        grant_id = source.grant.id
        await db.commit()
    stream, revoked = ScriptedLLMClient.stream, []

    async def revoke_after_read(self, **kwargs):
        if len(self.requests) == 1:
            assert any("COMPANY_SOURCE_MARKER" in str(message.content) for message in kwargs["messages"])
            async with owner_sessionmaker() as db:
                await db.delete(await db.get(ResourcePermission, grant_id))
                await db.commit()
            revoked.append(True)
        return await stream(self, **kwargs)

    monkeypatch.setattr(ScriptedLLMClient, "stream", revoke_after_read)
    await _run(n, [
        {"tool_calls": [_call("read_file", "read-owned-report", path=path)], "finish_reason": "tool_calls"},
        {"content": f"DELIVERABLE: {path}"},
    ], monkeypatch)
    assert revoked == [True]
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        cards = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run_id,
            ChatArtifact.path == path)))
    if owned:
        # owner 11-18 decision A: an owned read may become a card in the same private chat.
        assert len(cards) == 1
        response = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{cards[0].id}/content")
        assert response.status_code == 200 and "COMPANY_SOURCE_MARKER" in response.json()["content"], response.text
        if not receipted:
            async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
                with pytest.raises(HTTPException) as denied:
                    await authorize_resource_action(db, await db.get(User, n.user_id), agent_id=n.agent_id,
                        resource_kind="chat_artifact", resource_id=cards[0].id, action="deliver",
                        owner_user_id=n.user_id, root_session_id=n.session_id, authority_state=cards[0].authority_state)
                assert denied.value.detail["code"] == "workspace_content_unverified"
    else:
        for card in cards:
            for endpoint in ("content", "download"):
                response = await n.client.get(f"/agents/{n.agent_id}/files/artifacts/{card.id}/{endpoint}")
                assert "COMPANY_SOURCE_MARKER" not in response.text, response.text
        assert cards == [], "REVOKED_GRANTED_READ_BECAME_A_NEW_CARD"


@pytest.mark.parametrize("surface", ["saved_selection", "read_file"])
async def test_approved_email_requires_complete_inputs(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch, surface,  # noqa: F811
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services import agent_tools, runtime_task_worker
    from app.services.session_live_input import submit_live_human_input
    from tests.integration.test_artifact_publication_transactions import stage_report

    n, path, sink = member_web, "workspace/old-unreceipted-report.md", []
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        receipt = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id,
            user=n.user_id, session=n.session_id, root=n.agent_root, path=path, dependencies=[source.dependency])
        await db.flush()
        await db.execute(delete(ContentInputReceipt).where(ContentInputReceipt.id == receipt.id))
        await db.delete(source.grant)
        await db.commit()
    script = []
    if surface == "saved_selection":
        async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
            await submit_live_human_input(db=db, agent=await db.get(Agent, n.agent_id), user=await db.get(User, n.user_id),
                session=await db.get(ChatSession, n.session_id), content="Email my selected saved report.",
                parts=[{"type": "workspace_file_ref", "agent_id": str(n.agent_id), "path": path,
                    "human_saved_hash": hashlib.sha256((n.agent_root / path).read_bytes()).hexdigest()}],
                source="web", source_channel="web", requested_kind="steer_current_turn",
                expected_turn_id=n.turn_id, expected_run_id=n.run_id)
            await db.commit()
    else:
        script.append({"tool_calls": [_call("read_file", "read-unreceipted-report", path=path)],
            "finish_reason": "tool_calls"})

    async def deliver(*args, **kwargs):
        sink.append((args, kwargs))
        return "Delivered to the local test sink"

    async def notify(**kwargs):
        pass

    monkeypatch.setattr(agent_tools, "_handle_email_tool", deliver)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    script.append({"tool_calls": [_call("send_email", "send-old-report", to="recipient@example.test",
        subject="Report", body="COMPANY_SOURCE_MARKER")], "finish_reason": "tool_calls"})
    model = await _run(n, script, monkeypatch)
    # owner 11-18 decision A: an unverifiable source leaves the member's report readable in private.
    assert "COMPANY_SOURCE_MARKER" in json.dumps(model.requests[-1])
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.scalar(select(SessionToolInvocation).where(SessionToolInvocation.run_id == n.run_id,
            SessionToolInvocation.tool_name == "send_email"))
        assert invocation is not None and invocation.permission_item_id is not None
        permission_id, invocation_id = invocation.permission_item_id, invocation.id
    response = await n.client.post(f"/agents/{n.agent_id}/sessions/{n.session_id}/permissions/{permission_id}/resolve",
        json={"action": "allow_once"})
    assert response.status_code == 200, response.text
    async with tenant_scoped_session(n.tenant_id, session_factory=app_user_sessionmaker) as db:
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert sink == [], "INCOMPLETE_INPUT_REACHED_EMAIL_SINK"
        assert invocation.effect_state == "prepared_not_started"


async def test_nonowner_execution_batch_rechecks_first_source_after_second_validation(
    member_web, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services import content_input_receipts as receipts
    from tests.integration.test_artifact_publication_transactions import stage_report

    n, paths = member_web, ["workspace/a-report.md", "workspace/b-report.md"]
    async with owner_sessionmaker() as db:
        source = await _foreign_source(n, db)
        other = await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id, session=n.session_id,
            root=n.agent_root, path="workspace/e-source.md")
        for path, dependency in zip(paths, [source.dependency, {"source_kind": "workspace_artifact",
            "source_id": str(other.id), "source_version": other.content_sha256}]):
            await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=source.owner, session=source.session,
                root=n.agent_root, path=path, dependencies=[dependency])
            db.add(ResourcePermission(tenant_id=n.tenant_id, principal_type="user", principal_id=n.user_id,
                resource_type="workspace_file", resource_id=workspace_resource_id(n.agent_id, path), actions=["read"], conditions={}))
        grant_id = source.grant.id
        await db.commit()
    validate, revoked = receipts._validate_workspace_tool_input, []

    async def revoke_during_b(db, request, receipt, **kwargs):
        result = await validate(db, request, receipt, **kwargs)
        if receipt.carrier_ref.endswith("\n" + paths[1]) and not revoked:
            async with owner_sessionmaker() as writer:
                await writer.delete(await writer.get(ResourcePermission, grant_id))
                await writer.commit()
            revoked.append(True)
        return result

    monkeypatch.setattr(receipts, "_validate_workspace_tool_input", revoke_during_b)
    model = await _run(n, [
        {"tool_calls": [_call("grep_search", "batch-read", pattern="SOURCE", root=paths[0])], "finish_reason": "tool_calls"},
        {"content": "The first report is unavailable."},
    ], monkeypatch)
    assert revoked == [True]
    read = next(m for m in model.requests[1]["messages"] if m.get("tool_call_id") == "batch-read")
    assert "COMPANY_SOURCE_MARKER" not in read["content"], read
    assert "COMPANY_SOURCE_MARKER" not in json.dumps(model.requests)
    assert "COMPANY_SOURCE_MARKER" not in json.dumps(await _wires(n, app_user_sessionmaker))


async def test_admin_inspection_scope_records_an_unreceipted_employee_file(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.user import User
    from app.services.content_input_receipts import authorize_workspace_receipt_read
    from app.services.workspace_resource_authority import WorkspaceInputRead, load_workspace_authority_scope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    n = native_workspace
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        scope = await load_workspace_authority_scope(db, await db.get(User, n.user), agent_id=n.agent, session_id=n.session)
        await db.commit()
    context = ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), workspace=n.root,
        session_id=str(n.session), runtime_task_id=str(n.run), workspace_authority_scope=scope)
    observed = await authorize_workspace_receipt_read(ToolExecutionRequest(tool_name="read_file", arguments={"path": n.path}, context=context))
    assert isinstance(observed, WorkspaceInputRead), observed
    assert observed.receipt_id is None
    observed.verify(n.target.read_bytes())
    assert context.workspace_authority_scope.input_read(n.path) is observed


@pytest.mark.parametrize("reader,snapshot", [("legacy_owner", False), ("org_admin", False),
    ("org_admin", True), ("operator", False), ("operator", True)])
async def test_card_path_facts_match_its_snapshot_or_live_file(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, reader, snapshot,  # noqa: F811
):
    from app.models.chat_artifact import ChatArtifact
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
    n, path, body = native_workspace, "workspace/card-path.md", "RL1_CARD_PATH_BYTES"
    await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body}, include_history=True)
    async with owner_sessionmaker() as db:
        card = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == n.run, ChatArtifact.path == path))
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == n.agent,
            WorkspaceResourceManifest.path == path))
        if not snapshot:
            card.snapshot_json = {key: value for key, value in card.snapshot_json.items()
                if key != "snapshot_storage_path" and (reader != "legacy_owner" or key != "content_input_receipt_id")}
        if reader == "legacy_owner":
            manifest.deleted_at = datetime.now(timezone.utc)
        else:
            await db.delete(manifest)
            card.authority_state, card.owner_user_id, card.root_session_id = "quarantined", None, None
            if reader == "org_admin":
                (await db.get(User, n.user)).role = "org_admin"
            else:
                db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
                    resource_type="agent", resource_id=n.agent, actions=["operator.inspect"],
                    conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
        card_id = card.id
        await db.commit()
    params = {"operator_view": True, "operator_reason": "Inspect the quarantined legacy card"} if reader != "legacy_owner" else {}
    response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{card_id}/content", params=params)
    if reader == "legacy_owner":
        assert response.status_code in {403, 404, 409}, response.text
        assert body not in response.text
    else:
        assert response.status_code == 200 and response.json()["content"] == body, response.text


@pytest.mark.parametrize("endpoint", ["content", "download"])
async def test_nonowner_deactivation_after_agent_access_still_refuses_sources(
    report_history, owner_sessionmaker, monkeypatch, endpoint,  # noqa: F811
):
    from app.api import files
    from app.models.user import User

    h = report_history
    await _grant_card(h, owner_sessionmaker)
    original, deactivated = files.check_agent_access, []

    async def deactivate_after_access(db, user, agent_id):
        access = await original(db, user, agent_id)
        async with owner_sessionmaker() as writer:
            (await writer.get(User, h.n.user)).is_active = False
            await writer.commit()
        deactivated.append(True)
        return access

    monkeypatch.setattr(files, "check_agent_access", deactivate_after_access)
    response = await h.n.client.get(f"/agents/{h.n.agent}/files/artifacts/{h.artifact.id}/{endpoint}")
    assert deactivated
    assert response.status_code == 403, response.text
    assert h.body not in response.text


async def test_continue_edit_hashes_the_exact_inlined_bytes(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.models.session_v2 import SessionTurnInput
    from app.services import artifact_continuation as continuation
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages

    n, path = native_workspace, "workspace/single-read.md"
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": path}, json={"content": "CHECKED_BYTES"})
    assert response.status_code == 200, response.text
    opened = continuation.open_workspace_file
    from contextlib import contextmanager

    @contextmanager
    def mutate_on_rewind(root, relative):
        with opened(root, relative) as source:
            class Handle:
                def __getattr__(self, key):
                    return getattr(source, key)

                def seek(self, offset, *args):
                    if offset == 0:
                        (n.root / path).write_text("UNCHECKED_BODY")
                    return source.seek(offset, *args)
            yield Handle()

    monkeypatch.setattr(continuation, "open_workspace_file", mutate_on_rewind)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _admitted_write_messages(db, n)
        row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.target_run_id == n.run,
            SessionTurnInput.status == "bound"))
        row.content_parts_json = [{"type": "workspace_file_ref", "agent_id": str(n.agent), "path": path,
            "human_saved_hash": response.json()["content_hash"]}]
        await db.flush()
        messages, notices = await continuation.saved_file_context(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, user_id=n.user,
            messages=[{"session_input_id": str(row.id), "workspace_file_refs": True}])
        assert not notices
        assert "CHECKED_BYTES" in messages[0].content
        assert "UNCHECKED_BODY" not in messages[0].content


async def test_saved_file_context_rejects_a_different_session_reader(
    native_workspace, app_user_sessionmaker,  # noqa: F811
):
    from app.models.session_v2 import SessionTurnInput
    from app.services.artifact_continuation import saved_file_context
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import _admitted_write_messages

    n = native_workspace
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await _admitted_write_messages(db, n)
        row = await db.scalar(select(SessionTurnInput).where(SessionTurnInput.target_run_id == n.run,
            SessionTurnInput.status == "bound"))
        row.content_parts_json = [{"type": "workspace_file_ref", "agent_id": str(n.agent), "path": n.path,
            "human_saved_hash": hashlib.sha256(n.target.read_bytes()).hexdigest()}]
        await db.flush()
        messages, notices = await saved_file_context(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session, run_id=n.run, user_id=n.foreign,
            messages=[{"session_input_id": str(row.id), "workspace_file_refs": True}])
        assert notices
        assert "PRIVATE_FOREIGN_FILE_BODY" not in messages[0].content


async def _grant_card(h, factory):
    from app.core.resource_authority import workspace_resource_id
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.security_audit import ResourcePermission
    from app.models.workspace_resource import WorkspaceResourceManifest

    n, session_id = h.n, uuid.uuid4()
    async with factory() as db:
        db.add(ChatSession(id=session_id, tenant_id=n.tenant, agent_id=n.agent, user_id=n.foreign))
        await db.flush()
        card = await db.get(ChatArtifact, h.artifact.id)
        card.owner_user_id, card.root_session_id, card.session_id = n.foreign, session_id, session_id
        for path in (h.path, h.csv_path):
            manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == n.agent,
                WorkspaceResourceManifest.path == path))
            manifest.owner_user_id, manifest.root_session_id = n.foreign, session_id
            grant = ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
                resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path), actions=["read"], conditions={})
            db.add(grant)
            if path == h.csv_path:
                source_grant = grant
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="chat_artifact", resource_id=card.id, actions=["read", "download"], conditions={}))
        await db.commit()
        return source_grant.id


@pytest.mark.parametrize("endpoint", ["content", "download"])
async def test_grant_card_rechecks_upstream_grant_revoked_mid_read(
    report_history, owner_sessionmaker, monkeypatch, endpoint,  # noqa: F811
):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.security_audit import ResourcePermission
    from app.services import content_input_receipts as receipts

    h = report_history
    grant_id = await _grant_card(h, owner_sessionmaker)
    url = f"/agents/{h.n.agent}/files/artifacts/{h.artifact.id}/{endpoint}"
    before = await h.n.client.get(url)
    assert before.status_code == 200, before.text
    original, revoked = receipts._input_row, []

    async def revoke(db, model, source_id, **kwargs):
        row = await original(db, model, source_id, **kwargs)
        if model is ContentInputReceipt and source_id == h.source_receipt_id and not revoked:
            async with owner_sessionmaker() as writer:
                await writer.delete(await writer.get(ResourcePermission, grant_id))
                await writer.commit()
            revoked.append(True)
        return row

    monkeypatch.setattr(receipts, "_input_row", revoke)
    response = await h.n.client.get(url)
    assert revoked == [True]
    assert response.status_code in {403, 409}, response.text
    assert h.body not in response.text


async def test_nonowner_office_unknown_argument_cannot_select_original_read_exemption(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.models.chat_session import ChatSession
    from app.models.security_audit import ResourcePermission
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.office_document_service import OfficeDocumentService
    from tests.integration.test_r10a_workspace_source_admission import _unverifiable_history

    n, foreign_session = native_workspace, uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=foreign_session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.foreign))
        await db.flush()
        path, _ = await _unverifiable_history(db, tenant=n.tenant, agent=n.agent, user=n.user,
            session=n.session, root=n.root)
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == n.agent,
            WorkspaceResourceManifest.path == path))
        manifest.owner_user_id, manifest.root_session_id = n.foreign, foreign_session
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path), actions=["read"], conditions={}))
        await db.commit()
    calls = []

    def adapter(*args, **kwargs):
        calls.append(True)
        return {"text": "COMPANY_SOURCE_MARKER"}

    monkeypatch.setattr(OfficeDocumentService, "run_dump", adapter)
    result = await n.service.execute("office_document_dump", {"path": path, "original_attachment_id": "ignored-by-office"},
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False)
    assert calls == []
    assert "COMPANY_SOURCE_MARKER" not in str(result), result
    assert "learned_model_source_unavailable" in str(result), result
