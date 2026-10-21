"""Original inputs bind trusted source refs and never erase retained history."""

import copy
import asyncio
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4v_upload_intents_postgres import provider, original_api, delivered, s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = b"BOUND_ORIGINAL\x00BYTES"


async def upload(client):
    response = await client.post("/chat/upload", files={"file": ("source.bin", BODY)},
        data={"skip_personal_kb": "true", "agent_id": str(client.agent_id)})
    assert response.status_code == 200, response.text
    result = response.json()
    return {"name": result["filename"], "path": result["workspace_path"], "original_attachment": result["original_attachment"]}


async def authority(db, d):
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    return await resolve_session_mutation_authority(db, user=await db.get(User, d.user.id), agent_id=d.agent,
        session_id=d.session, action="mutate_session_input")


def intent(d, attachment, input_id=None):
    identity = input_id or uuid.uuid4()
    return {"kind": "queue_next_turn", "input_id": str(identity), "idempotency_key": f"original:{identity}",
        "session_id": str(d.session), "content_parts": [{"type": "attachment_ref", "attachment": attachment}]}


@pytest.mark.parametrize("field", ["owner", "digest", "path"])
async def test_client_original_metadata_is_not_source_authority(original_api, provider, app_user_sessionmaker, field):  # noqa: F811
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_v2_persistence import accept_human_input

    d, client = original_api
    client.agent_id = d.agent
    attachment = copy.deepcopy(await upload(client))
    if field == "owner":
        attachment["original_attachment"]["owner_user_id"] = str(uuid.uuid4())
    elif field == "digest":
        attachment["original_attachment"]["content_sha256"] = "0" * 64
    else:
        attachment["path"] = "workspace/report.txt"
    request = intent(d, attachment)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        frame = await authority(db, d)
        with pytest.raises(HTTPException) as denied:
            await accept_human_input(db, authority=frame, intent=request)
        assert denied.value.status_code == 409
        await db.rollback()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert await db.get(SessionTurnInput, uuid.UUID(request["input_id"])) is None


async def test_actual_input_and_revision_keep_idempotent_pins_through_cancel(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlobRef
    from app.models.user import User
    from app.services.session_human_input import cancel_unbound_human_input, revise_unbound_human_input
    from app.services.session_v2_persistence import accept_human_input

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    request = intent(d, attachment)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        frame = await authority(db, d)
        first = await accept_human_input(db, authority=frame, intent=request)
        again = await accept_human_input(db, authority=frame, intent=request)
        assert first.replayed is False and again.replayed is True
        await revise_unbound_human_input(db, authority=frame, input_id=request["input_id"], expected_revision=1,
            content_parts=[{"type": "text", "text": "Use the revised instructions."}], user=await db.get(User, d.user.id))
        await cancel_unbound_human_input(db, authority=frame, input_id=request["input_id"], expected_revision=2)
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        pins = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant,
            StorageBlobRef.owner_type == "session_input"))).all()
        assert len(pins) == 1 and pins[0].metadata_json["source_ref_id"] == attachment["original_attachment"]["id"]
        assert pins[0].metadata_json["input_id"] == request["input_id"] and pins[0].metadata_json["revision"] == 1
    removed = await client.delete(f"/chat/attachments/{attachment['original_attachment']['id']}/draft",
        params={"expected_content_sha256": attachment["original_attachment"]["content_sha256"]})
    assert removed.status_code == 200 and removed.json()["status"] == "retained", removed.text
    downloaded = await client.get(f"/chat/attachments/{attachment['original_attachment']['id']}/download")
    assert downloaded.status_code == 200 and downloaded.content == BODY


async def test_explicit_unbound_draft_release_removes_only_its_private_ref(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.security_audit import SecurityAuditEvent

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    ref_id = attachment["original_attachment"]["id"]
    query = {"expected_content_sha256": attachment["original_attachment"]["content_sha256"]}
    before = len(provider[1].requests)
    response = await client.delete(f"/chat/attachments/{ref_id}/draft", params=query)
    assert response.status_code == 200 and response.json()["status"] == "released", response.text
    repeated = await client.delete(f"/chat/attachments/{ref_id}/draft", params=query)
    assert repeated.status_code == 200 and repeated.json()["status"] == "already_absent", repeated.text
    assert len(provider[1].requests) == before
    assert (d.root / attachment["path"]).read_bytes() == BODY
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert await db.get(StorageBlobRef, uuid.UUID(ref_id)) is None
        blobs = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant, StorageBlob.kind == "chat_upload"))).all()
        assert len(blobs) == 1 and blobs[0].state == "available"
        events = (await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == d.tenant,
            SecurityAuditEvent.resource_id == uuid.UUID(ref_id), SecurityAuditEvent.event_type == "storage.original_draft_released"))).all()
        assert len(events) == 1 and events[0].details["physical_bytes_reclaimed"] is None


@pytest.mark.parametrize("source", ["web", "feishu"])
async def test_omitting_client_original_fields_still_pins_the_canonical_source(original_api, provider, app_user_sessionmaker, source):  # noqa: F811
    from app.models.storage_blob import StorageBlobRef
    from app.services.session_v2_persistence import accept_human_input
    from tests.integration.test_m4ab_channel_originals_postgres import save as save_channel

    d, client = original_api
    client.agent_id = d.agent
    if source == "web":
        attachment = await upload(client)
        source_ref = attachment.pop("original_attachment")["id"]
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            path = await save_channel(db, d, "feishu")
            await db.commit()
        attachment = {"path": path}
        source_ref = (await client.get("/chat/attachments/workspace-original", params={"agent_id": str(d.agent), "path": path})).json()["id"]
    request = intent(d, attachment)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await accept_human_input(db, authority=await authority(db, d), intent=request)
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        pin = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant, StorageBlobRef.owner_type == "session_input"))
        assert pin.metadata_json["source_ref_id"] == source_ref and pin.metadata_json["input_id"] == request["input_id"]


async def test_actual_http_admission_rejects_forged_source_and_retry_commits_once(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.api import chat_sessions
    from app.models.storage_blob import StorageBlobRef

    d, client = original_api
    client.agent_id = d.agent
    client._transport.app.include_router(chat_sessions.router)
    attachment = await upload(client)
    request = intent(d, attachment)
    request.pop("session_id")
    forged = copy.deepcopy(request)
    forged["content_parts"][0]["attachment"]["original_attachment"]["owner_user_id"] = str(uuid.uuid4())
    url = f"/agents/{d.agent}/sessions/{d.session}/inputs"
    denied = await client.post(url, json=forged)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "original_source_mismatch", denied.text
    accepted = await client.post(url, json=request)
    assert accepted.status_code == 200 and accepted.json()["admission_state"] == "admitted", accepted.text
    repeated = await client.post(url, json=request)
    assert repeated.status_code == 200 and repeated.json()["replayed"] is True, repeated.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        pins = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant, StorageBlobRef.owner_type == "session_input"))).all()
        assert len(pins) == 1


@pytest.mark.parametrize("hold", ["legal", "future", "legacy"])
async def test_draft_release_preserves_holds_and_unverifiable_historical_sources(original_api, provider, app_user_sessionmaker, hold):  # noqa: F811
    from datetime import datetime, timedelta, timezone
    from app.models.storage_blob import StorageBlobRef

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    ref_id = attachment["original_attachment"]["id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        ref = await db.get(StorageBlobRef, uuid.UUID(ref_id))
        if hold == "legal":
            ref.legal_hold = True
        elif hold == "future":
            ref.pinned_until = datetime.now(timezone.utc) + timedelta(days=1)
        else:
            ref.metadata_json = {key: value for key, value in ref.metadata_json.items() if key not in {"draft_state", "workspace_path", "workspace_manifest_id"}}
        await db.commit()
    query = {"expected_content_sha256": attachment["original_attachment"]["content_sha256"]}
    requests = len(provider[1].requests)
    response = await client.delete(f"/chat/attachments/{ref_id}/draft", params=query)
    assert response.status_code == 200 and response.json()["status"] == "retained", response.text
    assert len(provider[1].requests) == requests
    downloaded = await client.get(f"/chat/attachments/{ref_id}/download")
    assert downloaded.status_code == 200 and downloaded.content == BODY


async def test_draft_release_cannot_bypass_real_command_history_when_legacy_pins_are_missing(original_api, provider, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlobRef
    from app.models.user import User
    from app.services.session_human_input import revise_unbound_human_input
    from app.services.session_v2_persistence import accept_human_input

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    request = intent(d, attachment)
    ref_id = uuid.UUID(attachment["original_attachment"]["id"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        frame = await authority(db, d)
        await accept_human_input(db, authority=frame, intent=request)
        await revise_unbound_human_input(db, authority=frame, input_id=request["input_id"], expected_revision=1,
            content_parts=[{"type": "text", "text": "Revised input without this attachment."}], user=await db.get(User, d.user.id))
        # Model a historical import without the new pin/state marker while
        # retaining its real immutable accepted command and revision events.
        for pin in (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant,
            StorageBlobRef.owner_type == "session_input"))).all():
            await db.delete(pin)
        ref = await db.get(StorageBlobRef, ref_id)
        ref.metadata_json = {**ref.metadata_json, "draft_state": "unbound"}
        await db.commit()
    response = await client.delete(f"/chat/attachments/{ref_id}/draft",
        params={"expected_content_sha256": attachment["original_attachment"]["content_sha256"]})
    assert response.status_code == 200 and response.json() == {"status": "retained", "reference_id": str(ref_id), "reason": "work_reference"}, response.text


async def test_draft_release_requires_its_current_owner_and_exact_digest(original_api, provider, app_user_sessionmaker, owner_sessionmaker):  # noqa: F811
    from app.core.security import create_access_token
    from app.models.user import User

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    ref = attachment["original_attachment"]
    other_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other_id, tenant_id=d.tenant, username=other_id.hex, email=other_id.hex + "@fixture.test",
            password_hash="fixture", display_name="Other member", role="member"))
        await db.commit()
    requests = len(provider[1].requests)
    url = f"/chat/attachments/{ref['id']}/draft"
    denied = await client.delete(url, params={"expected_content_sha256": ref["content_sha256"]},
        headers={"Authorization": "Bearer " + create_access_token(str(other_id), "member", str(d.tenant))})
    assert denied.status_code == 403, denied.text
    stale = await client.delete(url, params={"expected_content_sha256": "0" * 64})
    assert stale.status_code == 409, stale.text
    assert len(provider[1].requests) == requests
    downloaded = await client.get(f"/chat/attachments/{ref['id']}/download")
    assert downloaded.status_code == 200 and downloaded.content == BODY


async def test_concurrent_source_admission_and_draft_release_do_not_deadlock_audit(original_api, provider, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.core import policy
    from app.models.storage_blob import StorageBlobRef
    from app.services.original_input_refs import release_unused_original_draft
    from app.services.session_v2_persistence import accept_human_input

    d, client = original_api
    client.agent_id = d.agent
    attachment = await upload(client)
    request = intent(d, attachment)
    ref = attachment["original_attachment"]
    audited, release_requested = asyncio.Event(), asyncio.Event()
    original_lock = policy._lock_audit_chain
    async def ordered_audit(db, tenant):
        name = asyncio.current_task().get_name()
        if name == "original-test-bind" and not audited.is_set():
            await original_lock(db, tenant)
            audited.set()
            await asyncio.wait_for(release_requested.wait(), 10)
        else:
            if name == "original-test-release":
                release_requested.set()
            await original_lock(db, tenant)
    monkeypatch.setattr(policy, "_lock_audit_chain", ordered_audit)
    async def bind():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            frame = await authority(db, d)
            await policy.write_audit_event(db, tenant_id=d.tenant, actor_type="user", actor_id=d.user.id,
                event_type="test.original_source_read", action="inspect_source")
            receipt = await accept_human_input(db, authority=frame, intent=request)
            await db.commit()
            return receipt
    async def release():
        await asyncio.wait_for(audited.wait(), 10)
        async with app_user_sessionmaker() as db:
            from app.models.user import User

            await pin_rls_tenant_context(db, d.tenant)
            result = await release_unused_original_draft(db, user=await db.get(User, d.user.id),
                ref_id=uuid.UUID(ref["id"]), digest=ref["content_sha256"])
            await db.commit()
            return result
    results = await asyncio.wait_for(asyncio.gather(asyncio.create_task(bind(), name="original-test-bind"),
        asyncio.create_task(release(), name="original-test-release"), return_exceptions=True), 20)
    assert not any(isinstance(result, BaseException) for result in results), results
    assert results[1]["status"] == "retained" and results[1]["reason"] == "work_reference"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert await db.get(StorageBlobRef, uuid.UUID(ref["id"])) is not None


@pytest.mark.parametrize("demoted", [False, True])
async def test_selected_company_platform_actor_uses_canonical_identity_not_company_user_filter(
    original_api, provider, app_user_sessionmaker, owner_sessionmaker, demoted,  # noqa: F811
):
    from io import BytesIO
    from starlette.datastructures import UploadFile
    from app.api.upload import save_upload_for_agent
    from app.models.storage_blob import StorageBlobRef
    from app.models.user import User
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from tests.services.test_session_input_control_v2 import _seed_session

    d, _client = original_api
    _home, actor_id, *_ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        actor = await db.get(User, actor_id)
        actor.role = "platform_admin"
        await db.commit()
    # Match the authenticated selected-company actor: detached scope change,
    # while its live canonical User remains in its separate home company.
    actor.tenant_id = d.tenant
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        published = await save_upload_for_agent(file=UploadFile(BytesIO(BODY), filename="admin.bin"),
            agent_id=d.agent, tenant_id=d.tenant, user_id=actor_id, db=db, skip_personal_kb=True)
        await db.commit()
    request = intent(d, {"path": published["workspace_path"], "original_attachment": published["original_attachment"]})
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        from app.database import _current_tenant_id

        selected = _current_tenant_id.set(d.tenant)
        try:
            frame = await resolve_session_mutation_authority(db, user=actor, agent_id=d.agent, session_id=d.session, action="mutate_session_input")
            if demoted:
                async with owner_sessionmaker() as owner:
                    (await owner.get(User, actor_id)).role = "member"
                    await owner.commit()
                with pytest.raises(HTTPException) as denied:
                    await accept_human_input(db, authority=frame, intent=request)
                assert denied.value.status_code == 403
                await db.rollback()
            else:
                await accept_human_input(db, authority=frame, intent=request)
                await db.commit()
        finally:
            _current_tenant_id.reset(selected)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        pins = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant,
            StorageBlobRef.owner_type == "session_input"))).all()
        assert len(pins) == (0 if demoted else 1)
    async with owner_sessionmaker() as db:
        canonical = await db.get(User, actor_id)
        assert canonical.tenant_id == _home and canonical.role == ("member" if demoted else "platform_admin")
