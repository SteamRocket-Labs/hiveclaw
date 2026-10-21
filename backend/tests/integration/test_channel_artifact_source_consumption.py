"""Private channel artifacts recheck the captured version's source at send time."""

import hashlib
import json
import uuid

import pytest

from app.database import pin_rls_tenant_context
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.storage_blob import StorageBlob, StorageBlobRef
from app.models.tenant_channel_config import TenantChannelConfig  # noqa: F401 (register external principal FK target)
from app.services.channel_delivery_outbox import ChannelDeliveryIntent, ChannelDeliveryOutboxService, enqueue_channel_delivery
from app.services.chat_artifact_delivery import create_chat_artifacts_for_message
from app.services.content_input_receipts import (
    ContentInputReceiptError, record_workspace_receipt, validate_artifact_content_receipt,
)
from app.services.external_principal_service import bind_authenticated_self_channel_principal
from app.services.feishu_service import FeishuService
from app.services.workspace_resource_authority import register_workspace_path
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
REPORT = b"PRIVATE_REPORT_DERIVED_FROM_ORIGINAL"


@pytest.mark.parametrize("source_state", ["active", "withdrawn"])
async def test_private_channel_file_consumes_current_original_authority(
    original_api, provider, app_user_sessionmaker, monkeypatch, tmp_path, source_state,  # noqa: F811
):
    d, client = original_api
    uploaded = await client.post("/chat/upload", files={"file": ("original.txt", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert uploaded.status_code == 200, uploaded.text
    original = uploaded.json()["original_attachment"]
    original_id = uuid.UUID(original["id"])
    path = "workspace/private-derived-report.txt"
    (d.root / path).write_bytes(REPORT)
    calls = []

    async def text_transport(self, app_id, _secret, recipient, *_args, **_kwargs):
        calls.append({"kind": "text", "recipient": recipient})
        return {"data": {"message_id": "synthetic-text-ack"}}

    async def file_transport(self, app_id, _secret, recipient, captured, **_kwargs):
        assert captured.read_bytes() == REPORT
        calls.append({"kind": "file", "recipient": recipient})
        return {"data": {"message_id": "synthetic-file-ack"}}

    monkeypatch.setattr(FeishuService, "send_message", text_transport)
    monkeypatch.setattr(FeishuService, "upload_and_send_file", file_transport)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        original_ref = await db.get(StorageBlobRef, original_id)
        original_ref.legal_hold = True
        blob_id = original_ref.blob_id
        receipt = record_workspace_receipt(db, d.tenant, d.agent, path, hashlib.sha256(REPORT).hexdigest(), None,
            additional_dependencies=[{"source_kind": "original_attachment", "source_id": str(original_id),
                "source_version": original["content_sha256"]}])
        await register_workspace_path(db, tenant_id=d.tenant, agent_id=d.agent, path=path,
            owner_user_id=d.user.id, root_session_id=d.session, source="fixture",
            content_hash=hashlib.sha256(REPORT).hexdigest())
        message = ChatMessage(tenant_id=d.tenant, agent_id=d.agent, user_id=d.user.id, role="assistant",
            content="The report is ready.", conversation_id=str(d.session))
        db.add(message)
        await db.flush()
        parts = await create_chat_artifacts_for_message(db=db, tenant_id=d.tenant, agent_id=d.agent,
            session_id=d.session, runtime_task_id=d.run, message_id=message.id, paths=[path], workspace_root=d.root,
            source_receipt_ids={path: receipt.id})
        assert len(parts) == 1, parts
        artifact = await db.get(ChatArtifact, uuid.UUID(parts[0]["artifact_id"]))
        assert artifact.snapshot_json["content_input_receipt_id"] == str(receipt.id)
        await validate_artifact_content_receipt(db, d.tenant, d.user, artifact)
        config = ChannelConfig(id=uuid.uuid4(), tenant_id=d.tenant, agent_id=d.agent, channel_type="feishu",
            app_id="synthetic-private-channel", app_secret="synthetic-channel-secret", is_configured=True)
        db.add(config)
        await db.flush()
        bound = await bind_authenticated_self_channel_principal(db, tenant_id=d.tenant, config=config,
            provider_subject_id="ou-private-owner", user_id=d.user.id, actor_user_id=d.user.id)
        assert bound.actor.authority_bound
        session = await db.get(ChatSession, d.session)
        session.source_channel = "feishu"
        session.external_principal_id = bound.principal.id
        target = {"channel": "feishu", "receive_id": "ou-private-owner", "receive_id_type": "open_id",
            "chat_type": "p2p", "external_principal_id": str(bound.principal.id), "session_id": str(d.session)}
        session.delivery_target_json = target
        task = await db.get(RuntimeTask, d.run)
        task.status = "completed"
        task.root_user_id = d.user.id
        outbox_id = await enqueue_channel_delivery(db, ChannelDeliveryIntent(tenant_id=d.tenant,
            runtime_task_id=d.run, agent_id=d.agent, session_id=d.session, user_id=d.user.id,
            external_principal_id=bound.principal.id, channel_config_id=config.id, delivery_target=target,
            text="The report is ready.", terminal_status="completed", artifact_ids=[artifact.id]))
        await db.commit()
        artifact_id, snapshot = artifact.id, dict(artifact.snapshot_json)
    if source_state == "withdrawn":
        withdrawn = await client.post(f"/chat/attachments/{original_id}/withdraw",
            json={"expected_content_sha256": original["content_sha256"]})
        assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn", withdrawn.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            with pytest.raises(ContentInputReceiptError) as oracle:
                await validate_artifact_content_receipt(db, d.tenant, d.user, await db.get(ChatArtifact, artifact_id))
            assert oracle.value.code == "original_source_access_denied"
    counts = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        worker_id="private-artifact-source", limit=100)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(ChannelDeliveryOutbox, outbox_id)
        observed = {"provider": provider[0], "source_state": source_state, "file_calls": sum(c["kind"] == "file" for c in calls),
            "text_calls": sum(c["kind"] == "text" for c in calls), "outbox_status": row.status,
            "counts": counts, "receipts": row.delivery_receipts_json}
        (tmp_path / "private-channel-artifact-source.json").write_text(json.dumps(observed, indent=2))
        assert observed["file_calls"] == int(source_state == "active"), observed
        assert row.status == ("delivered" if source_state == "active" else "dead_letter"), observed
        if source_state == "withdrawn":
            assert "original_source_access_denied" in row.last_error
        assert (await db.get(StorageBlobRef, original_id)).legal_hold is True
        assert (await db.get(StorageBlob, blob_id)).state == "available"
        assert (await db.get(ChatArtifact, artifact_id)).snapshot_json == snapshot
    assert (d.root / snapshot["snapshot_storage_path"]).read_bytes() == REPORT
    assert (d.root / path).read_bytes() == REPORT
