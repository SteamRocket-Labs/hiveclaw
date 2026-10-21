"""Provider effects keep a durable catalog when the upload request rolls back."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from fastapi import Request
import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m4s_original_attachments_postgres import original_api, delivered  # noqa: F401
from tests.services.test_s3_blob_store import s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = b"RECOVERABLE_UPLOAD_EFFECT"


@pytest.fixture(params=("filesystem", "s3"))
def provider(request, s3_server, monkeypatch):  # noqa: F811
    from app.config import get_settings

    settings = get_settings()
    values = {"BLOB_STORAGE_PROVIDER": request.param}
    if request.param == "s3":
        values.update(BLOB_S3_ENDPOINT=s3_server.endpoint, BLOB_S3_REGION="us-east-1", BLOB_S3_BUCKET="private-originals",
            BLOB_S3_ACCESS_KEY_ID=s3_server.access, BLOB_S3_SECRET_ACCESS_KEY=s3_server.secret, BLOB_S3_SESSION_TOKEN=s3_server.token)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    return request.param, s3_server


@pytest.mark.parametrize("failure", ["workspace", "provider_feedback", "reference_commit"])
async def test_failed_request_retains_unacknowledged_intent_and_same_body_retry_recovers(
    original_api, provider, app_user_sessionmaker, owner_sessionmaker, monkeypatch, failure,  # noqa: F811
):
    from app.api import upload
    from app.core.security import decode_access_token
    from app.database import get_db
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.services.blob_store import BlobLocation, BlobStoreError, FilesystemBlobStore
    from app.services.upload_originals import _recorded_store

    d, client = original_api
    selected, remote = provider
    app = client._transport.app
    original_scope = app.dependency_overrides[get_db]
    write_copy = upload._write_upload_copy
    put = FilesystemBlobStore.put_verified

    async def send():
        return await client.post("/chat/upload", files={"file": ("original.bin", BODY)},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})

    if failure == "workspace":
        def unavailable_copy(*args):
            raise OSError("Synthetic workspace failure after original storage")

        monkeypatch.setattr(upload, "_write_upload_copy", unavailable_copy)
        with pytest.raises(OSError, match="Synthetic workspace"):
            await send()
    elif failure == "provider_feedback":
        if selected == "s3":
            remote.mode = "corrupt"
        else:
            async def lost_feedback(self, request):
                await put(self, request)
                raise BlobStoreError("Synthetic provider feedback failure")

            monkeypatch.setattr(FilesystemBlobStore, "put_verified", lost_feedback)
        result = await send()
        assert result.status_code == 503 and result.json()["detail"]["code"] == "original_attachment_storage_unavailable", result.text
    else:
        async def commit_failure_scope(request: Request):
            token = request.headers["authorization"].removeprefix("Bearer ")
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, uuid.UUID(decode_access_token(token)["tid"]))

                async def refused_commit():
                    raise RuntimeError("Synthetic reference commit failure")

                db.commit = refused_commit
                try:
                    yield db
                finally:
                    await db.rollback()

        app.dependency_overrides[get_db] = commit_failure_scope
        with pytest.raises(RuntimeError, match="Synthetic reference commit"):
            await send()
    monkeypatch.setattr(upload, "_write_upload_copy", write_copy)
    monkeypatch.setattr(FilesystemBlobStore, "put_verified", put)
    remote.mode = None
    app.dependency_overrides[get_db] = original_scope
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        assert tuple(role) == (False, False)
        rows = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        assert len(rows) == 1, "The provider effect must retain its durable catalog after the request rolls back"
        pending = rows[0]
        assert pending.state == "uploading" and pending.verified_at is None
        assert pending.provider == selected and pending.content_sha256 == hashlib.sha256(BODY).hexdigest()
        assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == pending.id))).all() == []
        store = _recorded_store(pending, d.tenant)
        actual = b"".join([chunk async for chunk in store.open_stream(BlobLocation(pending.provider, pending.bucket, pending.object_key))])
        assert actual == BODY
        pending_id = pending.id
        await db.rollback()
    if selected == "s3" and failure == "workspace":
        from tests.scripts.test_self_hosted_installation import installer

        source = owner_sessionmaker.kw["bind"].url
        profile = installer.configure(d.root.parent / "pending-profile", public_origin="http://127.0.0.1:3012", port=3012)
        values = installer.read_profile(profile)
        values["HIVE_INSTALL_SCHEMA_PASSWORD"] = source.password
        profile.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
        target = d.root.parent / "pending-original-backup"
        binary = Path(os.environ.get("M1E_PSQL") or shutil.which("psql")).parent
        result = subprocess.run([sys.executable, "-B", "-m", "app.scripts.self_hosted_recovery", "backup", "--offline",
            "--profile", str(profile), "--agents", str(d.root), "--output", str(target), "--pg-bin", str(binary)],
            cwd=Path(__file__).resolve().parents[2], env={**os.environ, "PGHOST": source.host, "PGPORT": str(source.port),
                "PGDATABASE": source.database, "PGUSER": source.username, "PGPASSWORD": source.password},
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 3 and json.loads(result.stdout)["code"] == "remote_upload_intents_require_reconciliation", result.stdout
        assert not target.exists(), "Unresolved remote effects must not publish a falsely complete backup"
    # The retry reuses the exact recorded bytes; the failed request never minted a ref.
    retried = await send()
    assert retried.status_code == 200, retried.text
    projection = retried.json()
    result = await client.get(f"/chat/attachments/{projection['original_attachment']['id']}/download")
    assert result.status_code == 200 and result.content == BODY, result.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        rows = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        refs = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == pending_id))).all()
        assert len(rows) == len(refs) == 1 and rows[0].id == pending_id and rows[0].state == "available"
        assert refs[0].metadata_json["owner_user_id"] == str(d.user.id)
        # Identical hashes do not make a tenant-local intent discoverable elsewhere.
        await db.rollback()
        await pin_rls_tenant_context(db, uuid.uuid4())
        assert await db.get(StorageBlob, pending_id) is None
    if selected == "s3":
        assert remote.writes == 1


async def test_intent_persistence_does_not_wait_for_its_own_request_pool_connection(
    original_api, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.core.security import decode_access_token
    from app.database import get_db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    d, client = original_api
    engine = create_async_engine(app_user_sessionmaker.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scoped_db(request: Request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        async with factory() as db:
            await pin_rls_tenant_context(db, uuid.UUID(decode_access_token(token)["tid"]))
            yield db
            await db.commit()

    client._transport.app.dependency_overrides[get_db] = scoped_db
    try:
        result = await client.post("/chat/upload", files={"file": ("single-connection.bin", BODY)},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert result.status_code == 200, result.text
    finally:
        await engine.dispose()


async def test_intent_commit_does_not_commit_unrelated_caller_changes(delivered, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.models.tenant_setting import TenantSetting
    from app.services.upload_originals import archive_upload

    d = delivered
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        db.add(TenantSetting(tenant_id=d.tenant, key="uncommitted_upload_setting", value={"value": "MUST_ROLL_BACK"}))
        # Flush makes an accidental commit observable in the independent reader.
        await db.flush()
        await archive_upload(db, tenant_id=d.tenant, user_id=d.user.id, agent_id=d.agent, filename="pending.bin", body=BODY)
        await db.rollback()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == d.tenant,
            TenantSetting.key == "uncommitted_upload_setting")) is None
        row = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))
        assert row.state == "uploading"
        assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == row.id))).all() == []
