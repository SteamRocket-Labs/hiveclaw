"""Exact source admission pins and owner-requested release of unused upload drafts."""

from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.storage_blob import StorageBlob, StorageBlobRef
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.blob_store import BlobStoreError


def unavailable(code="original_source_unavailable", status=503):
    return HTTPException(status, detail={"code": code, "message": "The original source could not be verified. Remove its reference or select a recorded upload.",
        "retryable": True, "recovery_action": "remove_or_reselect_original"})


async def _reader(db, authority, command):
    if authority.principal_type == "user":
        identity = authority.principal_id
    else:
        from app.models.external_principal import ExternalPrincipal
        from app.services.external_principal_service import load_external_runtime_actor

        principal = await db.scalar(select(ExternalPrincipal).where(ExternalPrincipal.id == authority.principal_id,
            ExternalPrincipal.tenant_id == authority.tenant_id).execution_options(populate_existing=True))
        if principal is None or principal.status != "active":
            raise unavailable("original_source_forbidden", 403)
        actor = await load_external_runtime_actor(db, tenant_id=authority.tenant_id, principal_id=principal.id,
            expected_user_id=principal.linked_user_id)
        identity = actor.id
    if identity is None:
        return None
    user = await db.scalar(select(User).where(User.id == identity, User.tenant_id == authority.tenant_id,
        User.is_active.is_(True)).execution_options(populate_existing=True))
    if user is None and authority.principal_type == "user":
        from app.models.chat_session import ChatSession
        from app.services.session_v2_persistence import resolve_session_command_authority

        # Reuse the existing audited canonical actor recovery, including its
        # current-role check, when the actor lives outside this company.
        try:
            context = await resolve_session_command_authority(db, command=command,
                session=await db.get(ChatSession, authority.session_id), action=authority.action)
        except (RuntimeError, PermissionError, ValueError):
            raise unavailable("original_source_forbidden", 403) from None
        actor = context.actor
        if (actor.id != identity or actor.role != "platform_admin" or not actor.is_active
            or context.authority.tenant_id != authority.tenant_id or context.authority.principal_id != identity):
            raise unavailable("original_source_forbidden", 403)
        db.expunge(actor)
        actor.tenant_id = authority.tenant_id
        user = actor
    if user is None:
        raise unavailable("original_source_forbidden", 403)
    return user


async def bind_original_input_refs(db, *, authority, command, input_id, revision, parts):
    from app.models.chat_session import ChatSession
    from app.services.channel_originals import authorize_channel_original, channel_original_source
    from app.services.upload_originals import authorize_upload_original, original_upload_projection

    selected = {}
    for part in parts:
        attachment = part.get("attachment") if isinstance(part, dict) and part.get("type") == "attachment_ref" else None
        if not isinstance(attachment, dict):
            continue
        claim = attachment.get("original_attachment")
        if claim is not None:
            try:
                if not isinstance(claim, dict):
                    raise ValueError()
                ref_id = uuid.UUID(claim["id"])
            except (ValueError, TypeError, KeyError, AttributeError):
                raise unavailable("invalid_original_reference", 422) from None
        else:
            # Only a canonical registered source can discover an omitted ref.
            manifest_id = await db.scalar(select(WorkspaceResourceManifest.id).where(
                WorkspaceResourceManifest.tenant_id == authority.tenant_id,
                WorkspaceResourceManifest.agent_id == authority.agent_id, WorkspaceResourceManifest.path == attachment.get("path")))
            if manifest_id is None:
                continue
            candidates = (await db.scalars(select(StorageBlobRef.id).where(StorageBlobRef.tenant_id == authority.tenant_id,
                StorageBlobRef.purpose == "original_attachment",
                ((StorageBlobRef.owner_type == "channel_upload") & (StorageBlobRef.owner_id == str(manifest_id)))
                | ((StorageBlobRef.owner_type == "chat_upload") & (StorageBlobRef.metadata_json["workspace_manifest_id"].astext == str(manifest_id)))))).all()
            if not candidates:
                continue
            if len(candidates) != 1:
                raise unavailable()
            ref_id = candidates[0]
        selected.setdefault(ref_id, []).append((attachment, claim))
    if not selected:
        return
    from app.core.policy import _lock_audit_chain

    # Source authorization also appends to this chain. Keep the same order
    # as command authority before taking source-reference row locks.
    await _lock_audit_chain(db, authority.tenant_id)
    user = await _reader(db, authority, command)
    session = await db.get(ChatSession, authority.session_id)
    for ref_id in sorted(selected):
        ref = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.id == ref_id,
            StorageBlobRef.tenant_id == authority.tenant_id, StorageBlobRef.purpose == "original_attachment",
            StorageBlobRef.owner_type.in_(("chat_upload", "channel_upload"))).with_for_update().execution_options(populate_existing=True))
        if ref is None:
            raise unavailable("original_reference_not_found", 409)
        from app.services.original_source_lifecycle import require_original_source_active

        # Admission is future employee source use, never browser inspection.
        require_original_source_active(ref)
        metadata = ref.metadata_json
        try:
            if ref.owner_type == "channel_upload":
                expected = {"id": str(ref.id), "content_sha256": metadata["content_sha256"], "size_bytes": metadata["size_bytes"]}
                path = metadata["path"]
                if user is not None:
                    await authorize_channel_original(db, user=user, ref=ref)
                else:
                    await channel_original_source(db, tenant_id=authority.tenant_id, ref=ref)
                    if (metadata.get("external_principal_id") != str(authority.principal_id)
                        or metadata.get("root_session_id") != str(session.root_session_id or session.id)):
                        raise unavailable("original_source_forbidden", 403)
                if metadata.get("agent_id") != str(authority.agent_id):
                    raise unavailable("original_source_mismatch", 409)
            else:
                if user is None:
                    raise unavailable("original_source_forbidden", 403)
                await authorize_upload_original(db, user, ref)
                expected, path = original_upload_projection(ref), metadata.get("workspace_path")
                if not path:
                    raise unavailable()
                if metadata.get("agent_id") not in {None, str(authority.agent_id)}:
                    raise unavailable("original_source_mismatch", 409)
                if metadata.get("agent_id") is not None:
                    manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.tenant_id == authority.tenant_id,
                        WorkspaceResourceManifest.id == uuid.UUID(metadata["workspace_manifest_id"])).execution_options(populate_existing=True))
                    if (manifest is None or manifest.agent_id != authority.agent_id or manifest.path != path
                        or str(manifest.owner_user_id) != metadata["owner_user_id"] or manifest.deleted_at is not None):
                        raise unavailable()
            for attachment, claim in selected[ref_id]:
                if attachment.get("path") != path or (claim is not None and claim != expected):
                    raise unavailable("original_source_mismatch", 409)
            blob = await db.get(StorageBlob, ref.blob_id, populate_existing=True)
            if blob is None or blob.state != "available" or blob.content_sha256 != metadata["content_sha256"] or blob.size_bytes != metadata["size_bytes"]:
                raise unavailable()
        except (BlobStoreError, ValueError, TypeError, KeyError):
            raise unavailable() from None
        pin_id = uuid.uuid5(ref.id, f"session-input:{input_id}:revision:{revision}")
        if await db.get(StorageBlobRef, pin_id) is None:
            db.add(StorageBlobRef(id=pin_id, tenant_id=authority.tenant_id, blob_id=ref.blob_id,
                owner_type="session_input", owner_id=f"{input_id}:{revision}:{ref_id}", purpose="original_attachment",
                metadata_json={"schema": "hive.original_input_pin.v1", "source_ref_id": str(ref.id), "input_id": str(input_id),
                    "revision": revision, "agent_id": str(authority.agent_id), "session_id": str(authority.session_id)}))
        if ref.owner_type == "chat_upload":
            ref.metadata_json = {**metadata, "draft_state": "bound"}
    await db.flush()


async def release_unused_original_draft(db, *, user, ref_id, digest):
    from app.core.policy import _lock_audit_chain
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    await _lock_audit_chain(db, user.tenant_id)
    ref = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.id == ref_id,
        StorageBlobRef.tenant_id == user.tenant_id).with_for_update().execution_options(populate_existing=True))
    if ref is None:
        return {"status": "already_absent", "reference_id": str(ref_id)}
    if ref.owner_type != "chat_upload" or not isinstance(ref.metadata_json, dict) or ref.metadata_json.get("owner_user_id") != str(user.id):
        raise unavailable("original_draft_forbidden", 403)
    if ref.metadata_json.get("content_sha256") != digest:
        raise unavailable("original_source_mismatch", 409)
    reason = None
    if ref.legal_hold or (ref.pinned_until is not None and ref.pinned_until > datetime.now(timezone.utc)):
        reason = "retention_hold"
    elif ref.metadata_json.get("draft_state") == "bound":
        reason = "work_reference"
    elif ref.metadata_json.get("draft_state") != "unbound" or not ref.metadata_json.get("workspace_path"):
        reason = "source_reconciliation_required"
    else:
        needle = [{"type": "attachment_ref", "attachment": {"original_attachment": {"id": str(ref.id)}}}]
        queries = [select(StorageBlobRef.id).where(StorageBlobRef.tenant_id == user.tenant_id,
            StorageBlobRef.metadata_json["source_ref_id"].astext == str(ref.id)),
            select(SessionTurnInput.id).where(SessionTurnInput.tenant_id == user.tenant_id, SessionTurnInput.content_parts_json.contains(needle)),
            select(SessionCommand.id).where(SessionCommand.tenant_id == user.tenant_id, SessionCommand.request_json.contains({"content_parts": needle})),
            select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.tenant_id == user.tenant_id,
                ChatTranscriptEvent.metadata_json["v2_payload"].contains({"content_parts": needle}))]
        for query in queries:
            if await db.scalar(query.limit(1)) is not None:
                reason = "work_reference"
                break
    if reason:
        return {"status": "retained", "reference_id": str(ref_id), "reason": reason}
    from app.core.policy import write_audit_event

    await write_audit_event(db, event_type="storage.original_draft_released", severity="info", actor_type="user",
        actor_id=user.id, tenant_id=user.tenant_id, resource_type="storage_blob_ref", resource_id=ref.id,
        action="release_unused_original_draft", details={"content_sha256": digest, "blob_id": str(ref.blob_id),
            "reference_state": "released", "physical_bytes_reclaimed": None})
    await db.delete(ref)
    return {"status": "released", "reference_id": str(ref_id), "physical_bytes_reclaimed": None}
