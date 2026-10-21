"""Inactive source custody has an explicit, retained, scoped disposition path."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("custodian", ["user", "external"])
async def test_inactive_original_disposition_preserves_owner_holds_and_current_copy(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, custodian,  # noqa: F811
):
    from app.api import enterprise, chat_sessions
    from app.core.security import create_access_token
    from app.models.chat_session import ChatSession
    from app.models.channel_config import ChannelConfig
    from app.models.external_principal import ExternalPrincipal
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.user import User
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.external_principal_service import resolve_or_create_external_principal, load_external_runtime_actor
    from app.services.feishu_uploads import save_channel_upload

    d, client = original_api
    client._transport.app.include_router(enterprise.router)
    client._transport.app.include_router(chat_sessions.router)
    admin = uuid.uuid4()
    body = b"RETAIN_INACTIVE_ORIGINAL"
    async with owner_sessionmaker() as db:
        db.add(User(id=admin, tenant_id=d.tenant, username=admin.hex, email=f"{admin}@fixture.test",
            password_hash="fixture", display_name="Storage administrator", role="org_admin", is_active=True))
        if custodian == "external":
            config = ChannelConfig(id=uuid.uuid4(), tenant_id=d.tenant, agent_id=d.agent, channel_type="feishu",
                app_id="synthetic-disposition", app_secret="synthetic-disposition-secret", is_configured=True)
            db.add(config)
            await db.flush()
            principal = (await resolve_or_create_external_principal(db, tenant_id=d.tenant, provider="feishu",
                installation_ref=channel_installation_ref(config, fallback=f"feishu:{d.agent}"), channel_config_id=config.id,
                subject_id="unmapped-source-uploader", display_name="External uploader")).principal
            owner_id = principal.id
            source_session = uuid.uuid4()
            db.add(ChatSession(id=source_session, tenant_id=d.tenant, agent_id=d.agent, user_id=None,
                external_principal_id=owner_id, source_channel="feishu"))
        else:
            owner_id, source_session = d.user.id, d.session
        await db.commit()
    if custodian == "user":
        response = await client.post("/chat/upload", files={"file": ("source.txt", body)},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert response.status_code == 200, response.text
        claim, path = response.json()["original_attachment"], response.json()["workspace_path"]
        reference_id = uuid.UUID(claim["id"])
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            actor = await load_external_runtime_actor(db, tenant_id=d.tenant, principal_id=owner_id, expected_user_id=None)
            path = await save_channel_upload(db, actor=actor, session=await db.get(ChatSession, source_session), provider="feishu",
                message_id="owned-inactive-source", resource_key="synthetic-resource-key", filename="source.txt", content=body)
            ref = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant,
                StorageBlobRef.owner_type == "channel_upload", StorageBlobRef.metadata_json["path"].astext == path))
            reference_id = ref.id
            claim = {"id": str(ref.id), "content_sha256": ref.metadata_json["content_sha256"]}
            await db.commit()
    copy = d.root / path
    copy.write_bytes(b"KEEP_CURRENT_WORK_COPY")
    if custodian == "user":
        from app.services.agent_ownership_service import transfer_agent_owner

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            handover = await transfer_agent_owner(db, agent_id=d.agent, new_owner_id=admin,
                actor=await db.get(User, owner_id), expected_owner_id=owner_id, reason="Continue employee work after uploader offboarding")
            assert handover.status == "transferred"
            await db.commit()
    async with owner_sessionmaker() as db:
        ref = await db.get(StorageBlobRef, reference_id)
        original_metadata = dict(ref.metadata_json)
        blob_id = ref.blob_id
        ref.legal_hold = True
        ref.pinned_until = datetime.now(timezone.utc) + timedelta(days=1)
        if custodian == "user":
            (await db.get(User, owner_id)).is_active = False
        else:
            principal = await db.get(ExternalPrincipal, owner_id)
            principal.status, principal.revoked_at = "revoked", datetime.now(timezone.utc)
        await db.commit()
    client.headers["Authorization"] = "Bearer " + create_access_token(str(admin), "org_admin", str(d.tenant))
    base = f"/enterprise/storage-originals/{blob_id}"
    row = (await client.get(base + "/references")).json()["items"][0]
    assert row["withdrawable"] is True, row
    request = {"operation_id": str(uuid.uuid4()), "action": "withdraw_source", "reference_id": str(reference_id),
        "expected_content_sha256": claim["content_sha256"], "expected_reference_sha256": row["withdrawal_snapshot_sha256"]}
    async with owner_sessionmaker() as db:
        owner = await db.get(User if custodian == "user" else ExternalPrincipal, owner_id)
        if custodian == "user":
            owner.is_active = True
        else:
            owner.status = "active"
        await db.commit()
    stale = await client.post(base + "/references", json=request)
    assert stale.status_code == 200 and stale.json()["outcome"] == "source_withdrawal_held" and stale.json()["changed"] is False
    assert stale.json()["hold_reason"] == "reference_changed"
    active = (await client.get(base + "/references")).json()["items"][0]
    assert active["withdrawable"] is False and active["withdrawal_hold_reason"] == "source_owner_active"
    held = await client.post(base + "/references", json={**request, "operation_id": str(uuid.uuid4()),
        "expected_reference_sha256": active["withdrawal_snapshot_sha256"]})
    assert held.status_code == 200 and held.json()["changed"] is False and held.json()["hold_reason"] == "source_owner_active"
    async with owner_sessionmaker() as db:
        owner = await db.get(User if custodian == "user" else ExternalPrincipal, owner_id)
        if custodian == "user":
            owner.is_active = False
        else:
            owner.status = "revoked"
        ref = await db.get(StorageBlobRef, reference_id)
        ref.metadata_json = {**original_metadata,
            "owner_user_id" if custodian == "user" else "external_principal_id": str(uuid.uuid4())}
        await db.commit()
    unverified = (await client.get(base + "/references")).json()["items"][0]
    assert unverified["withdrawable"] is False and unverified["withdrawal_hold_reason"] == "source_custody_reconciliation_required"
    refused = await client.post(base + "/references", json={**request, "operation_id": str(uuid.uuid4()),
        "expected_reference_sha256": unverified["withdrawal_snapshot_sha256"]})
    assert refused.status_code == 200 and refused.json()["changed"] is False
    async with owner_sessionmaker() as db:
        (await db.get(StorageBlobRef, reference_id)).metadata_json = original_metadata
        await db.commit()
    row = (await client.get(base + "/references")).json()["items"][0]
    request.update(operation_id=str(uuid.uuid4()), expected_reference_sha256=row["withdrawal_snapshot_sha256"])
    if custodian == "user" and provider[0] == "filesystem":
        from tests.integration.original_reference_ui import retire_references_through_ui

        ui = await retire_references_through_ui(d, client, app_user_sessionmaker, admin, withdraw_source=True)
        request, result = ui["request"], ui["result"]
        assert ui["post_count"] == 1 and ui["lost_acknowledgement_recovered"]
    else:
        response = await client.post(base + "/references", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
    assert result["outcome"] == "source_withdrawn" and result["physical_bytes_reclaimed"] is None
    assert (await client.post(base + "/references", json=request)).json()["replayed"] is True
    receipt = await client.get(base + f"/operations/references/{request['operation_id']}")
    assert receipt.status_code == 200 and receipt.json()["result"] == result, receipt.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        retained = await db.get(StorageBlobRef, reference_id)
        assert retained.metadata_json == {**original_metadata, "withdrawal": retained.metadata_json["withdrawal"]}
        assert retained.legal_hold and retained.pinned_until is not None
        assert (await db.get(StorageBlob, blob_id)).state == "available"
    download = await client.get(f"/chat/attachments/{reference_id}/download")
    assert download.status_code == 200 and download.content == body, download.text
    from fastapi import HTTPException
    from app.core.source_read_purpose import source_recipient_read
    from app.services.upload_originals import resolve_original_download

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with source_recipient_read(session_id=source_session, user_id=admin):
            with pytest.raises(HTTPException) as denied:
                await resolve_original_download(db, await db.get(User, admin), reference_id)
        assert denied.value.status_code == 410
    assert copy.read_bytes() == b"KEEP_CURRENT_WORK_COPY"
    row = (await client.get(base + "/references")).json()["items"][0]
    assert row["withdrawable"] is False and row["retirable"] is False and row["hold_reason"] == "legal_hold"
    from app.services import runtime_task_service, runtime_task_worker

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(runtime_task_service, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    assert await runtime_task_service.update_runtime_task_record(str(d.run), status="killed")
    assert (await client.delete(f"/agents/{d.agent}/sessions/{source_session}")).status_code == 204
    async with owner_sessionmaker() as db:
        retained = await db.get(StorageBlobRef, reference_id)
        retained.legal_hold, retained.pinned_until = False, None
        await db.commit()
    row = (await client.get(base + "/references")).json()["items"][0]
    assert row["retirable"] is True, row
    retired = await client.post(base + "/references", json={**request, "operation_id": str(uuid.uuid4()),
        "action": "retire_reference", "expected_reference_sha256": row["snapshot_sha256"]})
    assert retired.status_code == 200 and retired.json()["outcome"] == "reference_retired", retired.text
    assert (await client.get(base + "/references")).json()["items"] == []
    policy = (await client.get("/enterprise/storage-original-retention")).json()
    configured = await client.put("/enterprise/storage-original-retention", json={"enabled": True,
        "retention_days": 0, "grace_days": 0, "expected_version": policy["version"]})
    assert configured.status_code == 200, configured.text
    cleanup = {"operation_id": str(uuid.uuid4()), "action": "quarantine", "expected_content_sha256": claim["content_sha256"],
        "expected_policy_version": configured.json()["version"]}
    quarantined = await client.post(base + "/retention", json=cleanup)
    assert quarantined.status_code == 200 and quarantined.json()["sweep_eligible"], quarantined.text
    recovery = d.root.parent / "independent-recovery.bin"
    recovery.write_bytes(download.content)
    swept = await client.post(base + "/sweep", json={**cleanup, "operation_id": str(uuid.uuid4()), "action": "sweep",
        "expected_manifest_sha256": quarantined.json()["manifest_sha256"]})
    assert swept.status_code == 200 and swept.json()["state"] == "deleted", swept.text
    assert copy.read_bytes() == b"KEEP_CURRENT_WORK_COPY" and recovery.read_bytes() == body
