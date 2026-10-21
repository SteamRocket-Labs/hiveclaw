"""Explicit original retention preserves references, generations and recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4w_original_reconciliation_postgres import pending, provider, original_api, delivered, s3_server, BODY  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
POLICY = "/enterprise/storage-original-retention"


async def configure(client, **changes):
    current = await client.get(POLICY)
    assert current.status_code == 200, current.text
    saved = await client.put(POLICY, json={"enabled": True, "retention_days": 0, "grace_days": 1,
        "expected_version": current.json()["version"], **changes})
    assert saved.status_code == 200, saved.text
    return saved.json()


async def apply(client, blob_id, action, version=1, **changes):
    return await client.post(f"/enterprise/storage-originals/{blob_id}/retention", json={
        "operation_id": str(uuid.uuid4()), "expected_content_sha256": hashlib.sha256(BODY).hexdigest(),
        "expected_policy_version": version, "action": action, **changes})


async def recover(client, blob_id):
    response = await client.post(f"/enterprise/storage-originals/{blob_id}/reconcile", json={
        "operation_id": str(uuid.uuid4()), "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"})
    assert response.status_code == 200 and response.json()["recovered"] is True, response.text


async def test_original_policy_quarantine_grace_restore_and_replay(pending, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

    d, client, blob_id, (selected, remote), member, foreign = pending
    default = await client.get(POLICY)
    assert default.status_code == 200 and default.json()["enabled"] is False, default.text
    assert (await client.get(POLICY, headers=member)).status_code == 403
    assert (await client.put(POLICY + f"?tenant_id={d.tenant}", headers=foreign,
        json={"enabled": True, "retention_days": 0, "grace_days": 0, "expected_version": 0})).status_code == 403
    assert (await apply(client, blob_id, "quarantine", version=0)).json()["hold_reason"] == "policy_disabled"
    policy = await configure(client)
    assert policy["version"] == 1 and policy["grace_days"] == 1
    assert (await client.put(POLICY, json={"enabled": False, "retention_days": 0, "grace_days": 0, "expected_version": 0})).status_code == 409
    assert (await apply(client, blob_id, "quarantine")).json()["hold_reason"] == "unresolved_state"
    await recover(client, blob_id)
    operation = str(uuid.uuid4())
    body = {"operation_id": operation, "expected_content_sha256": hashlib.sha256(BODY).hexdigest(),
        "expected_policy_version": 1, "action": "quarantine"}
    url = f"/enterprise/storage-originals/{blob_id}/retention"
    assert (await client.post(url, json=body, headers=member)).status_code == 403
    assert (await client.post(url, json=body, headers=foreign)).status_code == 404
    assert (await client.post(url, json={**body, "expected_policy_version": 0})).status_code == 409
    responses = await asyncio.gather(*(client.post(url, json=body) for _ in range(2)))
    assert all(row.status_code == 200 for row in responses), [row.text for row in responses]
    assert sorted(row.json()["replayed"] for row in responses) == [False, True]
    result = next(row.json() for row in responses if not row.json()["replayed"])
    assert result["state"] == "quarantined" and result["changed"] is True and result["sweep_eligible"] is False
    assert datetime.fromisoformat(result["delete_after"]) - datetime.fromisoformat(result["observed_at"]) == timedelta(days=1)
    assert (await client.post(url, json={**body, "action": "restore"})).status_code == 409
    requests = len(remote.requests)
    assert (await client.post(url, json=body)).json() == {**result, "replayed": True}
    assert len(remote.requests) == requests
    # Restoring remains available after an administrator disables reclamation.
    await configure(client, enabled=False)
    restored = await apply(client, blob_id, "restore", version=2)
    assert restored.status_code == 200 and restored.json()["state"] == "available", restored.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(StorageBlob, blob_id)
        assert row.state == "available" and row.quarantined_at is None and row.delete_after is None
        assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
        receipt = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == d.tenant,
            StorageGCRun.receipt_json["request"]["operation_id"].astext == operation))
        assert receipt.mode == "quarantine" and receipt.receipt_json["result"] == result
        assert remote.secret not in str(receipt.receipt_json) + restored.text
    assert (await client.get(f"/chat/attachments/{blob_id}/download", headers=member)).status_code == 404
    assert remote.writes == int(selected == "s3")


async def test_every_original_reference_and_retention_age_hold_quarantine(pending, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef

    d, client, blob_id, *_ = pending
    await recover(client, blob_id)
    await configure(client, retention_days=2)
    assert (await apply(client, blob_id, "quarantine")).json()["hold_reason"] == "retention_age"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(StorageBlob, blob_id)
        row.created_at = row.verified_at = datetime.now(timezone.utc) - timedelta(days=3)
        ref = StorageBlobRef(tenant_id=d.tenant, blob_id=blob_id, owner_type="unknown_work",
            owner_id=str(uuid.uuid4()), purpose="legal_hold", legal_hold=True,
            pinned_until=datetime.now(timezone.utc) - timedelta(days=1), metadata_json={})
        db.add(ref)
        await db.commit()
    held = await apply(client, blob_id, "quarantine")
    assert held.json()["hold_reason"] == "referenced" and held.json()["state"] == "available", held.text


async def test_new_active_generation_blocks_old_restore_without_remapping_authority(pending, app_user_sessionmaker):  # noqa: F811
    d, client, blob_id, (_, remote), member, *_ = pending
    await recover(client, blob_id)
    await configure(client, grace_days=0)
    result = await apply(client, blob_id, "quarantine")
    assert result.json()["sweep_eligible"] is True, result.text
    uploaded = await client.post("/chat/upload", headers=member, files={"file": ("current.bin", BODY)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert uploaded.status_code == 200, uploaded.text
    restored = await apply(client, blob_id, "restore")
    assert restored.status_code == 200 and restored.json()["hold_reason"] == "active_generation_exists", restored.text
    ref = uploaded.json()["original_attachment"]["id"]
    assert (await client.get(f"/chat/attachments/{ref}/download", headers=member)).content == BODY
    assert (await client.get(f"/chat/attachments/{blob_id}/download", headers=member)).status_code == 404


@pytest.mark.parametrize("condition", ["missing", "changed", "unknown"])
async def test_restore_keeps_quarantine_when_provider_bytes_cannot_be_proven(pending, app_user_sessionmaker, condition):  # noqa: F811
    from app.models.storage_blob import StorageBlob

    d, client, blob_id, (selected, remote), *_ = pending
    await recover(client, blob_id)
    await configure(client)
    assert (await apply(client, blob_id, "quarantine")).json()["state"] == "quarantined"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(StorageBlob, blob_id)
        target = d.root.parent / ".storage_objects" / row.bucket / row.object_key
        deadline = row.delete_after
    restore = None
    if selected == "s3":
        if condition == "missing":
            remote.objects.clear()
        else:
            remote.mode = "corrupt" if condition == "changed" else "denied"
    elif condition == "missing":
        target.unlink()
    elif condition == "changed":
        target.write_bytes(b"CHANGED")
    else:
        root = d.root.parent / ".storage_objects/durable"
        restore = root.with_name("retained-durable")
        root.rename(restore)
        root.symlink_to(restore, target_is_directory=True)
    try:
        result = await apply(client, blob_id, "restore")
        assert result.status_code == 200 and result.json()["observation"] == condition, result.text
        assert result.json()["state"] == "quarantined" and result.json()["changed"] is False
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            row = await db.get(StorageBlob, blob_id)
            assert row.state == "quarantined" and row.delete_after == deadline
    finally:
        if restore is not None:
            root.unlink()
            restore.rename(root)
