"""Immutable original uploads with tenant-local payload reuse and private source refs."""

from datetime import datetime, timezone
import asyncio
import hashlib
from pathlib import Path
import tempfile
import uuid
from weakref import WeakKeyDictionary

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.resource_authority import authorize_resource_action
from app.models.storage_blob import StorageBlob, StorageBlobRef
from app.models.user import User
from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobPutRequest, BlobStoreError, FilesystemBlobStore
from app.services.original_blob_identity import original_object_key

REF_SCHEMA = "hive.chat_upload_original.v1"
_INTENT_LOCKS = WeakKeyDictionary()


def _store(provider=None, *, metadata=None, bucket=None):
    selected = provider or get_settings().BLOB_STORAGE_PROVIDER
    if selected == "s3":
        from app.services.s3_blob_store import s3_store_from_values

        store = s3_store_from_values(vars(get_settings()))
        if metadata is not None and (metadata != store.identity or bucket != store.bucket):
            raise BlobStoreError("Recorded object storage is unavailable")
        return store
    if selected != "filesystem":
        raise BlobStoreError("Original attachment provider is unavailable")
    return FilesystemBlobStore(Path(get_settings().AGENT_DATA_DIR) / ".storage_objects")


def _location(tenant_id, digest, provider="filesystem", bucket="durable", *, generation_id=None):
    return BlobLocation(provider=provider, bucket=bucket,
        object_key=original_object_key(tenant_id, digest, generation_id=generation_id))


def _recorded_store(blob, tenant_id):
    try:
        keys = {original_object_key(tenant_id, blob.content_sha256),
            original_object_key(tenant_id, blob.content_sha256, generation_id=blob.id)}
    except (ValueError, TypeError):
        raise BlobStoreError("Original attachment location requires reconciliation") from None
    if (blob.provider not in {"filesystem", "s3"} or blob.object_key not in keys
        or (blob.provider == "filesystem" and blob.bucket != "durable")):
        raise BlobStoreError("Original attachment location requires reconciliation")
    return _store(blob.provider, metadata=blob.metadata_json if blob.provider == "s3" else None, bucket=blob.bucket)


async def _commit_upload_intent(db, *, tenant_id, digest, size):
    from app.database import pin_rls_tenant_context

    # This transaction owns only mechanical intent, never the caller's pending
    # workspace, authorship or permission changes. It precedes provider effects.
    if not isinstance(db.bind, AsyncEngine):
        raise BlobStoreError("Independent original attachment persistence is unavailable")
    # Reserve at most one short-lived connection per bound engine. Sharing its
    # request pool can deadlock when every request is holding a connection.
    async with _INTENT_LOCKS.setdefault(db.bind, asyncio.Lock()):
        source = db.bind.sync_engine
        independent = AsyncEngine(Engine(source.pool.recreate(), source.dialect, source.url,
            execution_options=source.get_execution_options(), hide_parameters=source.hide_parameters))
        try:
            async with async_sessionmaker(independent, expire_on_commit=False)() as intent:
                await pin_rls_tenant_context(intent, tenant_id)
                existing = await intent.scalar(select(StorageBlob.id).where(StorageBlob.tenant_id == tenant_id,
                    StorageBlob.content_sha256 == digest, StorageBlob.state.in_(("uploading", "available"))))
                if existing is None:
                    store = _store()
                    provider = get_settings().BLOB_STORAGE_PROVIDER
                    generation_id = uuid.uuid4()
                    location = _location(tenant_id, digest, provider, store.bucket if provider == "s3" else "durable",
                        generation_id=generation_id)
                    existing = await intent.scalar(insert(StorageBlob).values(id=generation_id, tenant_id=tenant_id, scope_type="tenant",
                        kind="chat_upload", retention_class="original", state="uploading", provider=location.provider,
                        bucket=location.bucket, object_key=location.object_key, content_sha256=digest, size_bytes=size,
                        metadata_json=store.identity if provider == "s3" else {})
                        .on_conflict_do_nothing(index_elements=[StorageBlob.tenant_id, StorageBlob.content_sha256],
                            index_where=text("state IN ('uploading','available')")).returning(StorageBlob.id))
                    if existing is None:
                        existing = await intent.scalar(select(StorageBlob.id).where(StorageBlob.tenant_id == tenant_id,
                            StorageBlob.content_sha256 == digest, StorageBlob.state.in_(("uploading", "available"))))
                if existing is None:
                    raise BlobStoreError("Original attachment generation requires reconciliation")
                await intent.commit()
                return existing
        except SQLAlchemyError:
            raise BlobStoreError("Durable original attachment intent is unavailable") from None
        finally:
            await independent.dispose()


async def archive_upload(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID,
    agent_id: uuid.UUID | None, filename: str, body: bytes) -> StorageBlobRef:
    digest = hashlib.sha256(body).hexdigest()
    blob_id = await _commit_upload_intent(db, tenant_id=tenant_id, digest=digest, size=len(body))
    # The unique digest and row lock also serialize two first-time uploads until
    # their actual metadata/reference commit; no filename proves ownership.
    blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == tenant_id,
        StorageBlob.id == blob_id).with_for_update().execution_options(populate_existing=True))
    if blob is None or blob.state not in {"uploading", "available"} or blob.size_bytes != len(body):
        raise BlobStoreError("Original attachment requires storage reconciliation")
    store = _recorded_store(blob, tenant_id)
    location = BlobLocation(blob.provider, blob.bucket, blob.object_key)
    with tempfile.TemporaryDirectory(prefix="hive-upload-source-") as temporary:
        source = Path(temporary) / "body"
        source.write_bytes(body)
        await store.put_verified(BlobPutRequest(tenant_id=str(tenant_id), kind="chat_upload", retention_class="original",
            location=location, source_path=source, expected_sha256=digest, expected_size=len(body)))
    blob.state, blob.verified_at = "available", datetime.now(timezone.utc)
    ref_id = uuid.uuid4()
    ref = StorageBlobRef(id=ref_id, tenant_id=tenant_id, blob_id=blob.id, owner_type="chat_upload",
        owner_id=str(ref_id), purpose="original_attachment", metadata_json={"schema": REF_SCHEMA,
            "owner_user_id": str(user_id), "agent_id": str(agent_id) if agent_id else None, "filename": filename,
            "content_sha256": digest, "size_bytes": len(body)})
    db.add(ref)
    await db.flush()
    return ref


def original_upload_projection(ref):
    return {"id": str(ref.id), "owner_user_id": ref.metadata_json["owner_user_id"],
        "content_sha256": ref.metadata_json["content_sha256"], "size_bytes": ref.metadata_json["size_bytes"]}


async def capture_original_download(db: AsyncSession, user: User, ref_id: uuid.UUID):
    row = (await db.execute(select(StorageBlobRef, StorageBlob).join(StorageBlob,
        (StorageBlob.id == StorageBlobRef.blob_id) & (StorageBlob.tenant_id == StorageBlobRef.tenant_id)).where(
            StorageBlobRef.id == ref_id, StorageBlobRef.tenant_id == user.tenant_id,
            StorageBlobRef.owner_type == "chat_upload", StorageBlobRef.purpose == "original_attachment"))).one_or_none()
    if row is None:
        raise HTTPException(404, "Original attachment not found")
    ref, blob = row
    metadata = ref.metadata_json
    try:
        if not isinstance(metadata, dict) or metadata.get("schema") != REF_SCHEMA:
            raise ValueError()
        owner = uuid.UUID(metadata["owner_user_id"])
        agent = uuid.UUID(metadata["agent_id"]) if metadata.get("agent_id") else None
        if not isinstance(metadata.get("filename"), str) or not metadata["filename"] or "\x00" in metadata["filename"]:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise BlobStoreError("Original attachment authority is unavailable")
    if agent is not None:
        await authorize_resource_action(db, user, agent_id=agent, resource_kind="upload_attachment",
            resource_id=ref.id, action="read", owner_user_id=owner, authority_state="owned")
    elif owner != user.id:
        raise HTTPException(403, "Original attachment access denied")
    if (blob.state != "available" or blob.size_bytes != metadata.get("size_bytes")
        or blob.content_sha256 != metadata.get("content_sha256")):
        raise BlobStoreError("Original attachment requires storage reconciliation")
    store = _recorded_store(blob, user.tenant_id)
    temporary = tempfile.TemporaryDirectory(prefix="hive-upload-download-")
    target = Path(temporary.name) / "body"
    try:
        digest, size = hashlib.sha256(), 0
        with target.open("xb") as output:
            async for chunk in store.open_stream(BlobLocation(blob.provider, blob.bucket, blob.object_key)):
                size += len(chunk)
                if size > blob.size_bytes:
                    raise BlobIntegrityError("Original attachment size mismatch")
                digest.update(chunk)
                output.write(chunk)
        if size != blob.size_bytes or digest.hexdigest() != blob.content_sha256:
            raise BlobIntegrityError("Original attachment content mismatch")
        return temporary, target, metadata["filename"]
    except BaseException:
        temporary.cleanup()
        raise
