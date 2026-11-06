"""Explicit reference retirement connects closed work to existing retention."""

import hashlib
import uuid
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("origin", ["web", "feishu", "dingtalk", "wecom"])
async def test_deleted_work_retires_exact_source_edges_before_governed_physical_cleanup(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, origin,  # noqa: F811
):
    from app.api import enterprise, chat_sessions
    from app.core.security import create_access_token
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.user import User
    from app.services import runtime_task_service, runtime_task_worker
    from app.services.session_v2_persistence import accept_human_input
    from tests.integration.test_m4ab_channel_originals_postgres import save
    from tests.integration.test_m4ac_original_input_binding_postgres import authority, intent

    d, client = original_api
    client._transport.app.include_router(enterprise.router)
    client._transport.app.include_router(chat_sessions.router)
    owner_headers = dict(client.headers)
    admin = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=admin, tenant_id=d.tenant, username=f"retention-{admin.hex}", email=f"{admin}@fixture.test",
            display_name="Storage administrator", password_hash="fixture", role="org_admin", is_active=True))
        await db.commit()
    if origin == "web":
        uploaded = await client.post("/chat/upload", files={"file": ("source.txt", b"RETIRED_ORIGINAL_BODY")},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert uploaded.status_code == 200, uploaded.text
        row = uploaded.json()
        path, claim = row["workspace_path"], row["original_attachment"]
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            path = await save(db, d, origin)
            await db.commit()
        found = await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})
        assert found.status_code == 200, found.text
        claim = found.json()
    reference = uuid.UUID(claim["id"])
    attachment = {"name": "source.txt", "path": path, "original_attachment": claim}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await accept_human_input(db, authority=await authority(db, d), intent=intent(d, attachment))
        source = await db.get(StorageBlobRef, reference)
        blob_id = source.blob_id
        await db.commit()
    body = (await client.get(f"/chat/attachments/{reference}/download")).content
    assert hashlib.sha256(body).hexdigest() == claim["content_sha256"]
    recovery = d.root.parent / "independent-recovery.bin"
    recovery.write_bytes(body)
    current = d.root / path
    current.write_bytes(b"KEEP_CURRENT_EDIT")
    withdrawn = await client.post(f"/chat/attachments/{reference}/withdraw",
        json={"expected_content_sha256": claim["content_sha256"]})
    assert withdrawn.status_code == 200, withdrawn.text
    client.headers["Authorization"] = "Bearer " + create_access_token(str(admin), "org_admin", str(d.tenant))
    base = f"/enterprise/storage-originals/{blob_id}"
    before = await client.get(base + "/references")
    assert before.status_code == 200, before.text
    assert len(before.json()["items"]) == 2 and not any(item["retirable"] for item in before.json()["items"])
    deleted = await client.delete(f"/agents/{d.agent}/sessions/{d.session}", headers=owner_headers)
    assert deleted.status_code == 409, deleted.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert all("session_retirement" not in ref.metadata_json for ref in
            (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all())

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(runtime_task_service, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    assert await runtime_task_service.update_runtime_task_record(str(d.run), status="killed")
    # G1: with the work settled, the owner's removal takes the conversation out
    # of their own list. The row, its transcript and every pin stay exactly as
    # they were — nothing stamps a closure proof, because no conversation ever
    # closes.
    from app.models.chat_session import ChatSession

    deleted = await client.delete(f"/agents/{d.agent}/sessions/{d.session}", headers=owner_headers)
    assert deleted.status_code == 204, deleted.text
    async with owner_sessionmaker() as db:
        row = await db.get(ChatSession, d.session)
        assert row is not None, "the conversation row must stay in the database"
        assert str(d.user.id) in ((row.transcript_metadata_json or {}).get("removed_from_list_by") or {})
        assert all("session_retirement" not in ref.metadata_json for ref in
            (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all())
    inspected = await client.get(f"/chat/attachments/{reference}/download")
    assert inspected.status_code == 200 and inspected.content == body, inspected.text

    after = (await client.get(base + "/references")).json()
    pin = next(item for item in after["items"] if item["owner_type"] == "session_input")
    # G1: a conversation that never closes keeps holding its session pin for
    # the work that referenced the original (work_reference). Explicit
    # retirement of the held pin stays a recorded no-op — the retired-proof
    # path belonged to the removed physical deletion.
    assert pin["retirable"] is False and pin["hold_reason"] == "work_reference", pin
    held = await client.post(base + "/references", json={"operation_id": str(uuid.uuid4()),
        "action": "retire_reference", "expected_content_sha256": claim["content_sha256"],
        "reference_id": pin["reference_id"], "expected_reference_sha256": pin["snapshot_sha256"]})
    assert held.status_code == 200 and held.json()["outcome"] == "reference_held", held.text
    assert held.json()["changed"] is False, held.text
    # The withdrawn source stays held too: the immortal session pin (and the
    # conversation's turn inputs) still reference it.
    root = next(item for item in (await client.get(base + "/references")).json()["items"]
        if item["reference_id"] == str(reference))
    assert root["retirable"] is False and root["hold_reason"] == "work_reference", root
    assert (await client.get(base + "/references")).json()["items"], "references stay listed"

    # Governed physical cleanup is held by the same immortal references: the
    # blob is quarantined only when nothing references it, so it stays
    # available with the current edit and the independent recovery copy.
    policy = (await client.get("/enterprise/storage-original-retention")).json()
    configured = await client.put("/enterprise/storage-original-retention", json={"enabled": True,
        "retention_days": 0, "grace_days": 0, "expected_version": policy["version"]})
    assert configured.status_code == 200, configured.text
    request = {"operation_id": str(uuid.uuid4()), "action": "quarantine",
        "expected_content_sha256": claim["content_sha256"], "expected_policy_version": configured.json()["version"]}
    quarantined = await client.post(base + "/retention", json=request)
    assert quarantined.status_code == 200 and quarantined.json()["hold_reason"] == "referenced", quarantined.text
    assert quarantined.json()["sweep_eligible"] is False, quarantined.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert (await db.get(StorageBlob, blob_id)).state == "available"
    assert current.read_bytes() == b"KEEP_CURRENT_EDIT" and recovery.read_bytes() == body
