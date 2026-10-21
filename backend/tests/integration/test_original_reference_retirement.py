"""Explicit reference retirement connects closed work to existing retention."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

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
    if origin == "web" and provider[0] == "filesystem":
        from app.models.chat_session import ChatSession

        child_id = uuid.uuid4()
        async with owner_sessionmaker() as db:
            db.add(ChatSession(id=child_id, tenant_id=d.tenant, agent_id=d.agent, user_id=d.user.id, root_session_id=d.session))
            await db.commit()
        restricted = await client.delete(f"/agents/{d.agent}/sessions/{d.session}", headers=owner_headers)
        assert restricted.status_code == 409, restricted.text
        async with owner_sessionmaker() as db:
            assert await db.get(ChatSession, d.session) is not None
            held_refs = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all()
            assert all("session_retirement" not in ref.metadata_json for ref in held_refs)
            await db.delete(await db.get(ChatSession, child_id))
            await db.commit()
    deleted = await client.delete(f"/agents/{d.agent}/sessions/{d.session}", headers=owner_headers)
    assert deleted.status_code == 204, deleted.text
    inspected = await client.get(f"/chat/attachments/{reference}/download")
    assert inspected.status_code == 200 and inspected.content == body, inspected.text
    after = (await client.get(base + "/references")).json()
    pin = next(item for item in after["items"] if item["owner_type"] == "session_input")
    assert pin["retirable"] is True, pin
    if origin == "web" and provider[0] == "filesystem":
        async with owner_sessionmaker() as db:
            target = await db.get(StorageBlobRef, uuid.UUID(pin["reference_id"]))
            saved_proof = target.metadata_json
            target.legal_hold = True
            await db.commit()
        stale = {"operation_id": str(uuid.uuid4()), "action": "retire_reference", "expected_content_sha256": claim["content_sha256"],
            "reference_id": pin["reference_id"], "expected_reference_sha256": pin["snapshot_sha256"]}
        changed = await client.post(base + "/references", json=stale)
        assert changed.status_code == 200 and changed.json()["changed"] is False and changed.json()["hold_reason"] == "reference_changed"
        assert (await client.post(base + "/references", json=stale)).json()["replayed"] is True
        assert (await client.get(base + "/references", headers=owner_headers)).status_code == 403
        assert (await client.post(base + "/references", json=stale, headers=owner_headers)).status_code == 403
        assert (await client.get(base + "/references", params={"tenant_id": str(uuid.uuid4())})).status_code == 403
        async with owner_sessionmaker() as db:
            (await db.get(User, admin)).role = "member"
            await db.commit()
        assert (await client.post(base + "/references", json=stale)).status_code == 403
        async with owner_sessionmaker() as db:
            (await db.get(User, admin)).role = "org_admin"
            await db.commit()
        for kind in ("legal", "future", "legacy"):
            async with owner_sessionmaker() as db:
                target = await db.get(StorageBlobRef, uuid.UUID(pin["reference_id"]))
                target.legal_hold = kind == "legal"
                target.pinned_until = datetime.now(timezone.utc) + timedelta(days=1) if kind == "future" else None
                target.metadata_json = {key: value for key, value in saved_proof.items() if key != "session_retirement"} if kind == "legacy" else saved_proof
                await db.commit()
            row = next(item for item in (await client.get(base + "/references")).json()["items"] if item["reference_id"] == pin["reference_id"])
            assert row["retirable"] is False, row
            held = await client.post(base + "/references", json={**stale, "operation_id": str(uuid.uuid4()),
                "expected_reference_sha256": row["snapshot_sha256"]})
            assert held.status_code == 200 and held.json()["outcome"] == "reference_held" and held.json()["changed"] is False
        async with owner_sessionmaker() as db:
            target = await db.get(StorageBlobRef, uuid.UUID(pin["reference_id"]))
            target.metadata_json, target.legal_hold, target.pinned_until = saved_proof, False, None
            await db.commit()
        pin = next(item for item in (await client.get(base + "/references")).json()["items"] if item["reference_id"] == pin["reference_id"])

    async def retire(item):
        request = {"operation_id": str(uuid.uuid4()), "action": "retire_reference",
            "expected_content_sha256": claim["content_sha256"], "reference_id": item["reference_id"],
            "expected_reference_sha256": item["snapshot_sha256"]}
        result = await client.post(base + "/references", json=request)
        assert result.status_code == 200 and result.json()["outcome"] == "reference_retired", result.text
        replay = await client.post(base + "/references", json=request)
        assert replay.status_code == 200 and replay.json()["replayed"] is True, replay.text
        receipt = await client.get(base + f"/operations/references/{request['operation_id']}")
        assert receipt.status_code == 200 and receipt.json()["result"] == result.json(), receipt.text
        assert result.json()["physical_bytes_reclaimed"] is None

    if origin == "web" and provider[0] == "filesystem":
        from tests.integration.original_reference_ui import retire_references_through_ui

        ui = await retire_references_through_ui(d, client, app_user_sessionmaker, admin)
        assert set(ui["retired_references"]) == {pin["reference_id"], str(reference)}
        assert ui["post_count"] == 2 and ui["lost_acknowledgement_recovered"]
    else:
        await retire(pin)
        root = next(item for item in (await client.get(base + "/references")).json()["items"]
            if item["reference_id"] == str(reference))
        assert root["retirable"] is True, root
        await retire(root)
    assert (await client.get(base + "/references")).json()["items"] == []
    backup_id = None
    if origin == "web" and provider[0] == "filesystem":
        async with owner_sessionmaker() as db:
            backup = StorageBlobRef(tenant_id=d.tenant, blob_id=blob_id, owner_type="recovery_bundle", owner_id="test-owned-bundle",
                purpose="disaster_recovery", metadata_json={"schema": "test.recovery_holder.v1"})
            db.add(backup)
            await db.commit()
            backup_id = backup.id
    policy = (await client.get("/enterprise/storage-original-retention")).json()
    configured = await client.put("/enterprise/storage-original-retention", json={"enabled": True,
        "retention_days": 0, "grace_days": 0, "expected_version": policy["version"]})
    assert configured.status_code == 200, configured.text
    request = {"operation_id": str(uuid.uuid4()), "action": "quarantine", "expected_content_sha256": claim["content_sha256"],
        "expected_policy_version": configured.json()["version"]}
    quarantine = await client.post(base + "/retention", json=request)
    if backup_id is not None:
        assert quarantine.status_code == 200 and quarantine.json()["hold_reason"] == "referenced", quarantine.text
        backup_row = (await client.get(base + "/references")).json()["items"][0]
        assert backup_row["retirable"] is False
        denied = await client.post(base + "/references", json={"operation_id": str(uuid.uuid4()), "action": "retire_reference",
            "expected_content_sha256": claim["content_sha256"], "reference_id": str(backup_id),
            "expected_reference_sha256": backup_row["snapshot_sha256"]})
        assert denied.status_code == 200 and denied.json()["changed"] is False
        async with owner_sessionmaker() as db:
            # Its separate fixture owner releases this test-owned backup edge;
            # the original-reference endpoint is deliberately unable to do so.
            await db.delete(await db.get(StorageBlobRef, backup_id))
            await db.commit()
        request["operation_id"] = str(uuid.uuid4())
        quarantine = await client.post(base + "/retention", json=request)
    assert quarantine.status_code == 200 and quarantine.json()["sweep_eligible"] is True, quarantine.text
    snapshot = quarantine.json()
    swept = await client.post(base + "/sweep", json={**request, "operation_id": str(uuid.uuid4()), "action": "sweep",
        "expected_manifest_sha256": snapshot["manifest_sha256"]})
    assert swept.status_code == 200 and swept.json()["state"] == "deleted", swept.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert (await db.get(StorageBlob, blob_id)).state == "deleted"
    assert current.read_bytes() == b"KEEP_CURRENT_EDIT" and recovery.read_bytes() == body
