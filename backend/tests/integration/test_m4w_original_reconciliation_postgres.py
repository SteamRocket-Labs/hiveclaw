"""Company administrators reconcile exact originals without creating read grants."""

import hashlib
import asyncio
import uuid

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import provider, original_api, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = b"PENDING_PRIVATE_ORIGINAL"


@pytest.fixture
async def pending(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import enterprise, upload
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db
    from app.models.storage_blob import StorageBlob
    from app.models.user import User
    from tests.services.test_session_input_control_v2 import _seed_session

    d, uploader = original_api
    with monkeypatch.context() as failure:
        def failed_copy(*args):
            raise OSError("Synthetic post-provider workspace failure")

        failure.setattr(upload, "_write_upload_copy", failed_copy)
        with pytest.raises(OSError):
            await uploader.post("/chat/upload", files={"file": ("pending.bin", BODY)},
                data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    foreign, foreign_user, *_ = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db:
        admin = User(id=uuid.uuid4(), tenant_id=d.tenant, username=uuid.uuid4().hex, email=uuid.uuid4().hex + "@fixture.test",
            display_name="Storage administrator", role="org_admin", password_hash="fixture")
        db.add(admin)
        (await db.get(User, foreign_user)).role = "org_admin"
        await db.commit()
        admin_id = admin.id
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))
        assert row.state == "uploading"
        blob_id = row.id
        assert tuple((await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()) == (False, False)

    async def scoped_db(request: Request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, uuid.UUID(decode_access_token(token)["tid"]))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(enterprise.router)
    app.include_router(upload.router)
    app.dependency_overrides[get_db] = scoped_db
    def headers(user, role, tenant):
        return {"Authorization": "Bearer " + create_access_token(str(user), role, str(tenant))}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://original-reconciliation.test",
        headers=headers(admin_id, "org_admin", d.tenant)) as client:
        yield d, client, blob_id, provider, headers(d.user.id, "member", d.tenant), headers(foreign_user, "org_admin", foreign)


async def test_admin_verifies_recovers_and_replays_without_minting_private_refs(pending, app_user_sessionmaker):
    from app.models.security_audit import SecurityAuditEvent
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

    d, client, blob_id, (selected, remote), member, foreign = pending
    base = "/enterprise/storage-originals"
    url = f"{base}/{blob_id}/reconcile"
    body = {"operation_id": str(uuid.uuid4()), "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"}
    before = len(remote.requests)
    assert (await client.get(base, headers=member)).status_code == 403
    assert (await client.post(url, headers=member, json=body)).status_code == 403
    assert (await client.post(url + f"?tenant_id={d.tenant}", headers=foreign, json=body)).status_code == 403
    assert (await client.post(url, headers=foreign, json=body)).status_code == 404
    assert len(remote.requests) == before
    listed = await client.get(base, params={"limit": 1})
    assert listed.status_code == 200, listed.text
    assert [row["blob_id"] for row in listed.json()["items"]] == [str(blob_id)]
    wrong = await client.post(url, json={**body, "expected_content_sha256": "0" * 64})
    assert wrong.status_code == 409 and len(remote.requests) == before, wrong.text
    concurrent = await asyncio.gather(*(client.post(url, json=body) for _ in range(2)))
    assert all(response.status_code == 200 for response in concurrent), [row.text for row in concurrent]
    assert sorted(response.json()["replayed"] for response in concurrent) == [False, True]
    recovered = next(response for response in concurrent if response.json()["replayed"] is False)
    result = recovered.json()
    assert result["outcome"] == "present" and result["state"] == "available" and result["recovered"] is True
    assert result["replayed"] is False
    requests = len(remote.requests)
    if selected == "s3":
        assert requests == before + 1, "Concurrent operation replay must not repeat the provider read"
    repeated = await client.post(url, json=body)
    assert repeated.status_code == 200 and repeated.json() == {**result, "replayed": True}, repeated.text
    assert len(remote.requests) == requests
    conflict = await client.post(url, json={**body, "action": "inspect"})
    assert conflict.status_code == 409 and len(remote.requests) == requests
    assert (await client.get(base)).json()["items"] == []
    assert (await client.get(f"/chat/attachments/{blob_id}/download", headers=member)).status_code == 404
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(StorageBlob, blob_id)
        assert row.state == "available" and row.verified_at is not None
        assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
        receipt = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == d.tenant,
            StorageGCRun.receipt_json["request"]["operation_id"].astext == body["operation_id"]))
        assert receipt.mode == "restore" and receipt.receipt_json["result"] == result
        assert receipt.receipt_json["evidence"]["observed"] == {
            "available": True, "sha256": hashlib.sha256(BODY).hexdigest(), "size_bytes": len(BODY)}
        events = (await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == d.tenant,
            SecurityAuditEvent.event_type == "storage.original_reconciled"))).all()
        assert len(events) == 1 and events[0].actor_type == "user"
        assert remote.secret not in str(receipt.receipt_json) + str(events[0].details) + recovered.text
    if selected == "s3":
        assert remote.writes == 1


@pytest.mark.parametrize("condition,outcome", [("missing", "missing"), ("corrupt", "changed"), ("oversized", "changed"), ("unavailable", "unknown")])
async def test_missing_changed_or_unavailable_original_preserves_unknown_intent(pending, app_user_sessionmaker, condition, outcome):
    from app.config import get_settings
    from app.models.storage_blob import StorageBlob, StorageBlobRef

    d, client, blob_id, (selected, remote), *_ = pending
    restore = None
    if selected == "s3":
        if condition == "missing":
            remote.objects.clear()
        elif condition == "oversized":
            for key in remote.objects:
                remote.objects[key] = BODY * 2
        else:
            remote.mode = "corrupt" if condition == "corrupt" else "denied"
    else:
        root = d.root.parent / ".storage_objects/durable"
        target = root / str(d.tenant) / hashlib.sha256(BODY).hexdigest()
        if condition == "missing":
            target.unlink()
        elif condition == "corrupt":
            target.write_bytes(b"CHANGED")
        elif condition == "oversized":
            target.write_bytes(BODY * 2)
        else:
            restore = root.with_name("retained-durable")
            root.rename(restore)
            root.symlink_to(restore, target_is_directory=True)
    try:
        response = await client.post(f"/enterprise/storage-originals/{blob_id}/reconcile", json={
            "operation_id": str(uuid.uuid4()), "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"})
        assert response.status_code == 200 and response.json()["outcome"] == outcome, response.text
        assert response.json()["recovered"] is False and response.json()["state"] == "uploading"
        assert remote.secret not in response.text and str(get_settings().AGENT_DATA_DIR) not in response.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            row = await db.get(StorageBlob, blob_id)
            assert row.state == "uploading" and row.verified_at is None
            assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
    finally:
        if restore is not None:
            root.unlink()
            restore.rename(root)
