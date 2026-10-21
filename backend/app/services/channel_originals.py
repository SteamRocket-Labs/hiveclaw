"""Original channel payloads reuse storage while retaining canonical source authority."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.core.resource_authority import authorize_resource_action, normalize_workspace_resource_path, workspace_resource_id
from app.models.storage_blob import StorageBlobRef
from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.blob_store import BlobStoreError

SCHEMA = "hive.channel_upload_original.v1"


async def archive_channel_original(db, *, manifest, filename, body, external_principal_id=None):
    from app.services.upload_originals import archive_original_blob

    ref_id = uuid.uuid5(manifest.id, "channel-original")
    previous = await db.get(StorageBlobRef, ref_id, populate_existing=True)
    if previous is not None:
        if (previous.tenant_id != manifest.tenant_id or previous.owner_type != "channel_upload"
            or previous.owner_id != str(manifest.id) or previous.purpose != "original_attachment"
            or previous.metadata_json.get("content_sha256") != manifest.content_hash):
            raise BlobStoreError("Channel original requires source reconciliation")
        return previous
    blob = await archive_original_blob(db, tenant_id=manifest.tenant_id, body=body)
    ref = StorageBlobRef(id=ref_id, tenant_id=manifest.tenant_id, blob_id=blob.id, owner_type="channel_upload",
        owner_id=str(manifest.id), purpose="original_attachment", metadata_json={"schema": SCHEMA,
            "agent_id": str(manifest.agent_id), "path": manifest.path,
            "owner_user_id": str(manifest.owner_user_id) if manifest.owner_user_id else None,
            "root_session_id": str(manifest.root_session_id) if manifest.root_session_id else None,
            "source": manifest.source, "filename": filename,
            "external_principal_id": str(external_principal_id) if external_principal_id else None,
            "content_sha256": blob.content_sha256, "size_bytes": blob.size_bytes})
    db.add(ref)
    await db.flush()
    return ref


async def channel_original_source(db, *, tenant_id, ref, inspection_user=None, runtime_principal=None):
    from app.services.original_source_lifecycle import require_original_source_active

    require_original_source_active(ref, user=inspection_user, runtime_principal=runtime_principal)
    metadata = ref.metadata_json
    try:
        if not isinstance(metadata, dict) or metadata.get("schema") != SCHEMA:
            raise ValueError()
        manifest_id = uuid.UUID(ref.owner_id)
        if ref.id != uuid.uuid5(manifest_id, "channel-original") or ref.tenant_id != tenant_id:
            raise ValueError()
        agent_id = uuid.UUID(metadata["agent_id"])
        path = normalize_workspace_resource_path(metadata["path"])
        if not path.startswith("workspace/") or path != metadata["path"] or not isinstance(metadata["filename"], str) or not metadata["filename"] or "\x00" in metadata["filename"]:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise BlobStoreError("Channel original authority requires reconciliation") from None
    manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.id == manifest_id,
        WorkspaceResourceManifest.tenant_id == tenant_id).execution_options(populate_existing=True))
    same_root = manifest is not None and (str(manifest.root_session_id) if manifest.root_session_id else None) == metadata.get("root_session_id")
    if manifest is not None and manifest.root_session_id is None and not same_root:
        from app.models.chat_session import ChatSession
        from app.services.original_reference_pins import closed_session_proof

        closed = closed_session_proof(ref)
        same_root = closed is not None and await db.scalar(select(ChatSession.id).where(
            ChatSession.tenant_id == tenant_id, ChatSession.id == uuid.UUID(closed["session_id"]))) is None
    if (manifest is None or manifest.deleted_at is not None or manifest.agent_id != agent_id or manifest.path != path
        # The manifest source records the latest writer; the immutable ref
        # records the upload origin. Editing a work copy changes only the former.
        or metadata.get("source") not in {"feishu_upload", "dingtalk_upload", "wecom_upload"}
        or (str(manifest.owner_user_id) if manifest.owner_user_id else None) != metadata.get("owner_user_id")
        or not same_root):
        raise BlobStoreError("Channel original authority requires reconciliation")
    return manifest


async def authorize_channel_original(db, *, user, ref, runtime_principal=None):
    manifest = await channel_original_source(db, tenant_id=user.tenant_id, ref=ref, inspection_user=user,
        runtime_principal=runtime_principal)
    await authorize_resource_action(db, user, agent_id=manifest.agent_id, resource_kind="workspace_file",
        resource_id=workspace_resource_id(manifest.agent_id, manifest.path), action="read", owner_user_id=manifest.owner_user_id,
        root_session_id=manifest.root_session_id, authority_state=manifest.authority_state, runtime_principal=runtime_principal)


async def find_channel_original(db, *, user, agent_id, path):
    try:
        normalized = normalize_workspace_resource_path(path)
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_workspace_path"}) from None
    manifest_id = await db.scalar(select(WorkspaceResourceManifest.id).where(WorkspaceResourceManifest.tenant_id == user.tenant_id,
        WorkspaceResourceManifest.agent_id == agent_id, WorkspaceResourceManifest.path == normalized))
    ref = await db.get(StorageBlobRef, uuid.uuid5(manifest_id, "channel-original")) if manifest_id else None
    if ref is None or ref.tenant_id != user.tenant_id or ref.owner_type != "channel_upload" or ref.purpose != "original_attachment":
        raise HTTPException(404, detail={"code": "original_attachment_not_recorded", "retryable": False})
    if (isinstance(ref.metadata_json, dict) and ref.metadata_json.get("owner_user_id") == str(user.id)
        and "withdrawal" in ref.metadata_json):
        from app.services.original_source_lifecycle import original_reference_status

        # The owner may still discover a withdrawn source's status, without
        # restoring the content read or changing any workspace authority.
        await original_reference_status(db, user=user, ref_id=ref.id)
    else:
        await authorize_channel_original(db, user=user, ref=ref)
    return {"id": str(ref.id), "content_sha256": ref.metadata_json["content_sha256"], "size_bytes": ref.metadata_json["size_bytes"]}
