"""Remote text delivery consumes recipient grants rather than browser inspection."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.source_read_purpose import source_recipient_read
from app.database import tenant_scoped_session
from app.models.channel_config import ChannelConfig
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_session import ChatSession
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.external_principal_service import bind_authenticated_self_channel_principal
from app.services.feishu_service import FeishuService
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from tests.integration.test_m3o_remote_a2a_tasks_postgres import configure, machine_api, remote_work  # noqa: F401
from tests.integration.test_m3q_remote_runtime_postgres import selection
from tests.integration.test_m3t_remote_chat_postgres import execute_chat, prepare_employee
from tests.services.test_remote_a2a_transport import remote_server

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = "PRIVATE_REMOTE_RECIPIENT_SOURCE"


@pytest.mark.parametrize("recipient_grant", ["granted", "agent_only", "revoked", "revoked_after_load"])
async def test_private_remote_text_uses_actual_recipient_source_authority(
    remote_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, recipient_grant,  # noqa: F811
):
    from app.api import chat_sessions
    from app.services import memory_service

    seed, api, session_id = remote_work
    api._transport.app.include_router(chat_sessions.router)
    await prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)
    source = tmp_path / "other-owner-source.md"
    source.write_text(BODY)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    document_id, source_owner, human_grant_id = uuid4(), uuid4(), uuid4()
    dependency = {"source_kind": "knowledge_document", "source_id": str(document_id), "source_version": digest}
    async with owner_sessionmaker() as db:
        db.add(User(id=source_owner, tenant_id=seed["tenant"], username=source_owner.hex,
            email=f"{source_owner}@fixture.test", display_name="Private source owner",
            password_hash="synthetic-unused", role="member", is_active=True))
        await db.flush()
        db.add(KnowledgeDocument(id=document_id, tenant_id=seed["tenant"], scope_type="person", scope_id=source_owner,
            owner_user_id=source_owner, source_kind="text", source_sha256=digest, title="Other owner source",
            status="ready", canonical_md_path=source.name, canonical_md_sha256=digest))
        await db.flush()
        db.add(KnowledgeGrant(tenant_id=seed["tenant"], scope_id=source_owner, resource_type="document",
            resource_id=document_id, document_id=document_id, grantee_type="agent", grantee_id=seed["agent"],
            requester_user_id=seed["owner"], purpose="interactive_session", session_id=str(session_id), permission="read",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        if recipient_grant != "agent_only":
            db.add(KnowledgeGrant(id=human_grant_id, tenant_id=seed["tenant"], scope_id=source_owner,
                resource_type="document", resource_id=document_id, document_id=document_id,
                grantee_type="user", grantee_id=seed["owner"], permission="read"))
        await db.commit()

    async def scoped_memory(*_args, **kwargs):
        assert kwargs["current_user_id"] == seed["owner"]
        return memory_service.MemoryContextResult(content=BODY, status="ready", code="memory_context_ready",
            user_message="Controlled source with real SQL authority", source_refs=(dependency,), source_unbound=())

    monkeypatch.setattr(memory_service, "build_memory_context", scoped_memory)
    sent = []

    async def send(_self, app_id, _secret, recipient, *args, **kwargs):
        sent.append({"app_id": app_id, "recipient": recipient, "args": args, "kwargs": kwargs})
        return {"data": {"message_id": "synthetic-private-remote-ack"}}

    monkeypatch.setattr(FeishuService, "send_message", send)
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        config = ChannelConfig(id=uuid4(), tenant_id=seed["tenant"], agent_id=seed["agent"], channel_type="feishu",
            app_id="synthetic-remote-recipient", app_secret="synthetic-secret", is_configured=True)
        db.add(config)
        await db.flush()
        linked = await bind_authenticated_self_channel_principal(db, tenant_id=seed["tenant"], config=config,
            provider_subject_id="ou-private-remote-recipient", user_id=seed["owner"], actor_user_id=seed["owner"])
        session = await db.get(ChatSession, session_id)
        session.delivery_target_json = {"channel": "feishu", "receive_id": "ou-private-remote-recipient",
            "receive_id_type": "open_id", "chat_type": "p2p", "session_id": str(session_id),
            "external_principal_id": str(linked.principal.id)}
        await db.commit()
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        selected = await api.post(path + "/runtime", json=selection(seed))
        assert selected.status_code == 200, selected.text
        remote.reply = lambda _request: {"message": {"messageId": "private-remote-reply", "role": "ROLE_AGENT",
            "parts": [{"text": BODY}]}}
        response = await api.post(f"/agents/{seed['agent']}/sessions/{session_id}/runs", json={
            "content": "Use the authorized knowledge source and return its result.", "input_id": str(uuid4()),
            "idempotency_key": "private-remote-recipient-source"})
        assert response.status_code == 201, response.text
        run_id = UUID(response.json()["run_id"])
        await execute_chat(seed, str(run_id), app_user_sessionmaker)
        posts = [row[3] for row in remote.requests if row[0] == "POST"]
        assert len(posts) == 1 and BODY in json.dumps(posts[0]["params"])
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.status == "completed", task.metadata_json
        returned = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="private-remote-original-return")
        assert returned["claimed"] == returned["delivered"] == 1, returned
        async with owner_sessionmaker() as db:
            if recipient_grant == "revoked":
                (await db.get(KnowledgeGrant, human_grant_id)).revoked_at = datetime.now(timezone.utc)
                await db.commit()
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            reader = await db.get(User, seed["owner"])
            assert reader.role == "org_admin"
            # Business inspection remains available; it is not a recipient grant.
            await _validate_input_dependencies(db, seed["tenant"], [dependency], user=reader)
            with source_recipient_read(session_id=session_id, user_id=reader.id):
                if recipient_grant in {"granted", "revoked_after_load"}:
                    await _validate_input_dependencies(db, seed["tenant"], [dependency], user=reader)
                else:
                    with pytest.raises(ContentInputReceiptError) as denied:
                        await _validate_input_dependencies(db, seed["tenant"], [dependency], user=reader)
                    assert denied.value.status_code == 403
        if recipient_grant == "revoked_after_load":
            send_part = ChannelDeliveryOutboxService._send_part

            async def revoke_before_part(self, **kwargs):
                if kwargs["item"].runtime_task_id == run_id and kwargs["part_key"] == "text":
                    async with owner_sessionmaker() as db:
                        (await db.get(KnowledgeGrant, human_grant_id)).revoked_at = datetime.now(timezone.utc)
                        await db.commit()
                return await send_part(self, **kwargs)

            monkeypatch.setattr(ChannelDeliveryOutboxService, "_send_part", revoke_before_part)
        counts = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="private-remote-source-send")
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            item = await db.scalar(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == run_id))
            observed = {"recipient_grant": recipient_grant, "calls": sent, "counts": counts,
                "status": item.status, "error": item.last_error, "metadata": item.metadata_json}
            (tmp_path / "private-remote-recipient-source.json").write_text(json.dumps(observed, indent=2, default=str))
            assert not item.artifact_ids_json and BODY in item.text_content
            assert len(sent) == int(recipient_grant == "granted"), observed
            assert item.status == ("delivered" if recipient_grant == "granted" else "dead_letter"), observed
        assert len([row for row in remote.requests if row[0] == "POST"]) == 1
