"""Original owners can withdraw source use without deleting retained evidence."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("origin", ["web", "feishu", "dingtalk", "wecom"])
async def test_bound_original_withdrawal_blocks_future_consumers_but_retains_history(
    original_api, provider, app_user_sessionmaker, origin,  # noqa: F811
):
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.original_tool_sources import validate_original_dependency
    from app.services.session_v2_persistence import accept_human_input
    from app.tools.runtime import ToolExecutionRequest
    from tests.integration.test_m4ab_channel_originals_postgres import save
    from tests.integration.test_m4ac_original_input_binding_postgres import authority, intent
    from tests.integration.test_m4ad_original_tool_reads_postgres import context, read

    d, client = original_api
    if origin == "web":
        response = await client.post("/chat/upload", files={"file": ("source.txt", b"WITHDRAWABLE_ORIGINAL")},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert response.status_code == 200, response.text
        row = response.json()
        attachment = {"name": row["filename"], "path": row["workspace_path"], "original_attachment": row["original_attachment"]}
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            path = await save(db, d, origin)
            await db.commit()
        found = await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})
        assert found.status_code == 200, found.text
        claim = {key: found.json()[key] for key in ("id", "content_sha256", "size_bytes")}
        attachment = {"name": "source.bin", "path": path, "original_attachment": claim}
        row = {"workspace_path": path, "original_attachment": claim}
    ref_id = uuid.UUID(row["original_attachment"]["id"])
    digest = row["original_attachment"]["content_sha256"]
    frame = await context(d, app_user_sessionmaker)
    import json

    assert json.loads(str(await read(frame, row)))["ok"] is True
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await accept_human_input(db, authority=await authority(db, d), intent=intent(d, attachment))
        ref = await db.get(StorageBlobRef, ref_id)
        ref.legal_hold = True
        await db.commit()
        before = set((await db.scalars(select(StorageBlobRef.id).where(StorageBlobRef.blob_id == ref.blob_id))).all())
        blob_id = ref.blob_id
    copy = d.root / row["workspace_path"]
    copy.write_bytes(b"EDITED_WORKING_COPY")
    status = await client.get(f"/chat/attachments/{ref_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "active" and status.json()["can_withdraw"] is True
    result = await client.post(f"/chat/attachments/{ref_id}/withdraw", json={"expected_content_sha256": digest})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "withdrawn" and result.json()["physical_bytes_reclaimed"] is None
    assert (await client.post(f"/chat/attachments/{ref_id}/withdraw", json={"expected_content_sha256": digest})).json() == result.json()
    status = await client.get(f"/chat/attachments/{ref_id}/status")
    assert status.json()["state"] == "withdrawn" and status.json()["can_withdraw"] is False
    if origin != "web":
        restored = await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": row["workspace_path"]})
        assert restored.status_code == 200 and restored.json()["id"] == str(ref_id), restored.text
    downloaded = await client.get(f"/chat/attachments/{ref_id}/download")
    assert downloaded.status_code == 410 and downloaded.json()["detail"]["code"] == "original_source_withdrawn"
    native = await read(frame, row)
    assert "<tool_error>" in str(native) and "EDITED_WORKING_COPY" not in str(native)
    assert "WITHDRAWABLE_ORIGINAL" not in str(native) and "CHANNEL_ORIGINAL" not in str(native)
    dependency = {"source_kind": "original_attachment", "source_id": str(ref_id), "source_version": digest}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with pytest.raises(ContentInputReceiptError):
            await validate_original_dependency(db, d.tenant, dependency,
                runtime_request=ToolExecutionRequest(tool_name="fs_read", arguments={}, context=frame))
        assert set((await db.scalars(select(StorageBlobRef.id).where(StorageBlobRef.blob_id == blob_id))).all()) == before
        assert (await db.get(StorageBlobRef, ref_id)).legal_hold is True
        assert (await db.get(StorageBlob, blob_id)).state == "available"
        with pytest.raises(HTTPException) as denied:
            await accept_human_input(db, authority=await authority(db, d), intent=intent(d, attachment))
        assert denied.value.status_code == 410
    assert copy.read_bytes() == b"EDITED_WORKING_COPY"


async def test_source_inspection_does_not_grant_withdrawal_and_digest_must_match(
    original_api, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.core.security import create_access_token
    from app.models.user import User
    from app.models.tenant import Tenant

    d, client = original_api
    response = await client.post("/chat/upload", files={"file": ("source.txt", b"RETAINED_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    claim = response.json()["original_attachment"]
    other = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other, tenant_id=d.tenant, username=f"inspector-{other.hex}", email=f"{other}@fixture.test",
            display_name="Inspector", password_hash="fixture", role="org_admin", is_active=True))
        await db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(str(other), "org_admin", str(d.tenant))}
    info = await client.get(f"/chat/attachments/{claim['id']}/status", headers=headers)
    assert info.status_code == 200 and info.json()["can_withdraw"] is False, info.text
    denied = await client.post(f"/chat/attachments/{claim['id']}/withdraw", headers=headers,
        json={"expected_content_sha256": claim["content_sha256"]})
    assert denied.status_code == 403, denied.text
    changed = await client.post(f"/chat/attachments/{claim['id']}/withdraw", json={"expected_content_sha256": "0" * 64})
    assert changed.status_code == 409, changed.text
    foreign_tenant, foreign_user = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=foreign_tenant, name="Other company", slug=f"other-{foreign_tenant.hex}"))
        await db.flush()
        db.add(User(id=foreign_user, tenant_id=foreign_tenant, username=f"foreign-{foreign_user.hex}",
            email=f"{foreign_user}@fixture.test", display_name="Other company owner", password_hash="fixture", role="org_admin"))
        await db.commit()
    foreign_headers = {"Authorization": "Bearer " + create_access_token(str(foreign_user), "org_admin", str(foreign_tenant))}
    for suffix in ("status", "withdraw"):
        rejected = (await client.get(f"/chat/attachments/{claim['id']}/{suffix}", headers=foreign_headers) if suffix == "status"
            else await client.post(f"/chat/attachments/{claim['id']}/{suffix}", headers=foreign_headers,
                json={"expected_content_sha256": claim["content_sha256"]}))
        assert rejected.status_code == 404, rejected.text
    assert (await client.get(f"/chat/attachments/{claim['id']}/download")).content == b"RETAINED_ORIGINAL"


async def test_withdrawal_preserves_scoped_business_inspection_without_lending_it_to_runtime(
    original_api, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.core.resource_authority import workspace_resource_id
    from app.core.security import create_access_token
    from app.core.source_read_purpose import source_recipient_read
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.upload_originals import resolve_original_download
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from tests.integration.test_m4ac_original_input_binding_postgres import intent
    from tests.integration.test_m4ad_original_tool_reads_postgres import context, read

    d, client = original_api
    response = await client.post("/chat/upload", files={"file": ("source.txt", b"RETAINED_INSPECTION_BODY")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert response.status_code == 200, response.text
    row, inspector = response.json(), uuid.uuid4()
    source_id = uuid.UUID(row["original_attachment"]["id"])
    async with owner_sessionmaker() as db:
        db.add(User(id=inspector, tenant_id=d.tenant, username=f"inspect-{inspector.hex}", email=f"{inspector}@fixture.test",
            display_name="Business inspector", password_hash="fixture", role="org_admin", is_active=True))
        for kind, identity in (("workspace_file", workspace_resource_id(d.agent, row["workspace_path"])), ("upload_attachment", source_id)):
            db.add(ResourcePermission(tenant_id=d.tenant, principal_type="user", principal_id=inspector,
                resource_type=kind, resource_id=identity, actions=["read"], effect="allow", conditions={}))
        await db.commit()
    frame = await context(d, app_user_sessionmaker, inspector)
    assert "RETAINED_INSPECTION_BODY" in str(await read(frame, row))
    attachment = {"name": row["filename"], "path": row["workspace_path"], "original_attachment": row["original_attachment"]}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        admission = await resolve_session_mutation_authority(db, user=await db.get(User, inspector), agent_id=d.agent,
            session_id=d.session, action="mutate_session_input")
        await accept_human_input(db, authority=admission, intent=intent(d, attachment))
        await db.commit()
    withdrawn = await client.post(f"/chat/attachments/{source_id}/withdraw",
        json={"expected_content_sha256": row["original_attachment"]["content_sha256"]})
    assert withdrawn.status_code == 200, withdrawn.text
    headers = {"Authorization": "Bearer " + create_access_token(str(inspector), "org_admin", str(d.tenant))}
    inspected = await client.get(f"/chat/attachments/{source_id}/download", headers=headers)
    assert inspected.status_code == 200 and inspected.content == b"RETAINED_INSPECTION_BODY", inspected.text
    status = await client.get(f"/chat/attachments/{source_id}/status", headers=headers)
    assert status.json()["state"] == "withdrawn" and status.json()["can_inspect_retained"] is True
    native = await read(frame, row)
    assert "RETAINED_INSPECTION_BODY" not in str(native) and "auth_or_permission" in str(native)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        from app.models.security_audit import SecurityAuditEvent

        records = (await db.scalars(select(SecurityAuditEvent).where(
            SecurityAuditEvent.event_type == "storage.original_retained_inspection_authorized",
            SecurityAuditEvent.resource_id == source_id))).all()
        assert records and all(record.actor_id == inspector for record in records)
        with source_recipient_read(session_id=d.session, user_id=inspector):
            with pytest.raises(HTTPException) as denied:
                await resolve_original_download(db, await db.get(User, inspector), source_id)
        assert denied.value.status_code == 410
        admission = await resolve_session_mutation_authority(db, user=await db.get(User, inspector), agent_id=d.agent,
            session_id=d.session, action="mutate_session_input")
        with pytest.raises(HTTPException) as refused_input:
            await accept_human_input(db, authority=admission, intent=intent(d, attachment))
        assert refused_input.value.status_code == 410
