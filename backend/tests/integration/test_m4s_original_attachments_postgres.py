"""Uploaded originals retain exact bytes independently of editable work files."""

import uuid
import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def original_api(delivered, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import upload
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db

    d = delivered
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", d.root.parent)

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
    app.include_router(upload.router)
    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(d.user.id), "member", str(d.tenant))}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://original.test", headers=headers) as client:
        yield d, client


async def test_real_uploads_deduplicate_originals_and_download_after_workspace_change(original_api, app_user_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input

    d, client = original_api
    results = []
    for _ in range(2):
        result = await client.post("/chat/upload", files={"file": ("source.bin", b"ORIGINAL\x00BYTES", "application/octet-stream")},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert result.status_code == 200, result.text
        results.append(result.json())
    assert all(row.get("original_attachment", {}).get("id") for row in results)
    assert results[0]["original_attachment"]["id"] != results[1]["original_attachment"]["id"]
    assert results[0]["workspace_path"] != results[1]["workspace_path"]
    input_id = uuid.uuid4()
    attachments = [{"name": row["filename"], "path": row["workspace_path"], "original_attachment": row["original_attachment"]}
        for row in results]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, d.agent), user=await db.get(User, d.user.id),
            session=await db.get(ChatSession, d.session), content="Use these originals", source="web",
            input_id=input_id, attachments=attachments, requested_kind="queue_next_turn")
        assert receipt["admission_state"] == "admitted", receipt
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        accepted = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, accepted.command_id)
        assert command.principal_id == d.user.id and command.principal_type == "user"
        saved = [part["attachment"] for part in command.request_json["content_parts"] if part["type"] == "attachment_ref"]
        assert saved == attachments
    (d.root / results[0]["workspace_path"]).write_bytes(b"EDITED_WORK_COPY")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        blobs = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        refs = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant))).all()
        assert len(blobs) == 1 and len(refs) == 2 and all(ref.blob_id == blobs[0].id for ref in refs)
    for row in results:
        downloaded = await client.get(f"/chat/attachments/{row['original_attachment']['id']}/download")
        assert downloaded.status_code == 200 and downloaded.content == b"ORIGINAL\x00BYTES", downloaded.text
        assert "attachment" in downloaded.headers["content-disposition"]
    assert (d.root / results[0]["workspace_path"]).read_bytes() == b"EDITED_WORK_COPY"


async def test_upload_cannot_write_through_an_aliased_workspace_parent(original_api):
    d, client = original_api
    retained = d.root.parent / "retained-owner"
    retained.mkdir()
    (d.root / "workspace/uploads").symlink_to(retained, target_is_directory=True)
    result = await client.post("/chat/upload", files={"file": ("private.bin", b"NEW_UNAUTHORIZED_WRITE", "application/octet-stream")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert result.status_code == 503, result.text
    assert list(retained.iterdir()) == []


async def test_original_authority_is_per_reference_and_rechecked_after_grant_revocation(
    original_api, owner_sessionmaker, app_user_sessionmaker,
):
    from app.core.security import create_access_token
    from app.models.agent import AgentPermission
    from app.models.security_audit import ResourcePermission
    from app.models.user import User

    d, client = original_api
    other_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other_id, tenant_id=d.tenant, username=str(other_id), email=f"{other_id}@fixture.test",
            display_name="Other member", password_hash="fixture", role="member", is_active=True))
        db.add(AgentPermission(tenant_id=d.tenant, agent_id=d.agent, scope_type="user", scope_id=other_id, access_level="use"))
        await db.commit()
    result = await client.post("/chat/upload", files={"file": ("source.bin", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert result.status_code == 200, result.text
    ref_id = uuid.UUID(result.json()["original_attachment"]["id"])
    url = f"/chat/attachments/{ref_id}/download"
    other_headers = {"Authorization": "Bearer " + create_access_token(str(other_id), "member", str(d.tenant))}
    denied = await client.get(url, headers=other_headers)
    assert denied.status_code == 403 and b"PRIVATE_ORIGINAL" not in denied.content, denied.text
    # Equal bytes in the same employee's workspace do not confer another ref's ownership.
    own = await client.post("/chat/upload", headers=other_headers, files={"file": ("own.bin", b"PRIVATE_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert own.status_code == 200, own.text
    assert own.json()["original_attachment"]["id"] != str(ref_id)
    assert (await client.get(f"/chat/attachments/{own.json()['original_attachment']['id']}/download", headers=other_headers)).content == b"PRIVATE_ORIGINAL"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        grant = ResourcePermission(tenant_id=d.tenant, principal_type="user", principal_id=other_id,
            resource_type="upload_attachment", resource_id=ref_id, actions=["read"], effect="allow", conditions={})
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    allowed = await client.get(url, headers=other_headers)
    assert allowed.status_code == 200 and allowed.content == b"PRIVATE_ORIGINAL", allowed.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(ResourcePermission, grant_id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    revoked = await client.get(url, headers=other_headers)
    assert revoked.status_code == 403 and b"PRIVATE_ORIGINAL" not in revoked.content, revoked.text


async def test_concurrent_same_body_uploads_reuse_one_payload_with_independent_work_copies(original_api, app_user_sessionmaker):
    from app.models.storage_blob import StorageBlob, StorageBlobRef

    d, client = original_api
    body = b"CONCURRENT_ORIGINAL"
    results = await asyncio.gather(*(client.post("/chat/upload", files={"file": ("same.bin", body)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"}) for _ in range(2)))
    assert [result.status_code for result in results] == [200, 200], [result.text for result in results]
    rows = [result.json() for result in results]
    assert len({row["original_attachment"]["id"] for row in rows}) == 2
    assert len({row["workspace_path"] for row in rows}) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert tuple(role) == (False, False)
        blobs = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        refs = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == d.tenant))).all()
        assert len(blobs) == 1 and len(refs) == 2
    objects = list((d.root.parent / ".storage_objects/durable" / str(d.tenant)).iterdir())
    assert len(objects) == 1 and objects[0].read_bytes() == body
    files = [objects[0], *(d.root / row["workspace_path"] for row in rows)]
    assert all(path.stat().st_nlink == 1 for path in files)
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in files}) == 3


async def test_same_body_in_different_tenants_has_separate_objects_and_invisible_foreign_refs(original_api, owner_sessionmaker, app_user_sessionmaker):
    from app.core.security import create_access_token
    from app.models.storage_blob import StorageBlob
    from tests.services.test_session_input_control_v2 import _seed_session

    d, client = original_api
    tenant, user_id, agent, _, _ = await _seed_session(owner_sessionmaker, active_run=False)
    body = b"TENANT_ISOLATED_ORIGINAL"
    refs = []
    for company, user, employee in ((d.tenant, d.user.id, d.agent), (tenant, user_id, agent)):
        headers = {"Authorization": "Bearer " + create_access_token(str(user), "member", str(company))}
        result = await client.post("/chat/upload", headers=headers, files={"file": ("source.bin", body)},
            data={"agent_id": str(employee), "skip_personal_kb": "true"})
        assert result.status_code == 200, result.text
        refs.append(result.json()["original_attachment"]["id"])
    foreign = await client.get(f"/chat/attachments/{refs[1]}/download")
    assert foreign.status_code == 404 and body not in foreign.content, foreign.text
    paths = []
    for company in (d.tenant, tenant):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, company)
            blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == company))
            assert blob.provider == "filesystem"
            paths.append(d.root.parent / ".storage_objects" / blob.bucket / blob.object_key)
    first, second = paths
    assert first.read_bytes() == second.read_bytes() == body
    assert first.stat().st_ino != second.stat().st_ino


@pytest.mark.parametrize("damage", ["corrupt", "missing", "foreign_location", "invalid_ref"])
async def test_unavailable_original_never_falls_back_to_mutable_workspace(original_api, app_user_sessionmaker, damage):
    from app.models.storage_blob import StorageBlob, StorageBlobRef

    d, client = original_api
    body = b"TRUSTED_ORIGINAL"
    result = await client.post("/chat/upload", files={"file": ("source.bin", body)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert result.status_code == 200, result.text
    projection = result.json()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))
        path = d.root.parent / ".storage_objects" / blob.bucket / blob.object_key
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"UNTRUSTED_DATA")
    else:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            if damage == "invalid_ref":
                ref = await db.get(StorageBlobRef, uuid.UUID(projection["original_attachment"]["id"]))
                ref.metadata_json = []
            else:
                blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))
                blob.object_key = f"{uuid.uuid4()}/{blob.content_sha256}"
            await db.commit()
    assert (d.root / projection["workspace_path"]).read_bytes() == body
    downloaded = await client.get(f"/chat/attachments/{projection['original_attachment']['id']}/download")
    assert downloaded.status_code == 503, downloaded.text
    assert downloaded.json()["detail"] == {"code": "original_attachment_unavailable", "retryable": True}
    assert body not in downloaded.content and b"UNTRUSTED_DATA" not in downloaded.content


async def test_unsupported_writer_provider_is_visible_before_mutable_upload_creation(original_api, monkeypatch):
    from app.config import get_settings

    d, client = original_api
    monkeypatch.setattr(get_settings(), "BLOB_STORAGE_PROVIDER", "unconfigured")
    result = await client.post("/chat/upload", files={"file": ("source.bin", b"UNAVAILABLE_PROVIDER")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["code"] == "original_attachment_storage_unavailable"
    assert not (d.root / "workspace/uploads").exists()
