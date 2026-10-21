"""Persist exact closed-work evidence before Session deletion removes its rows."""

from datetime import datetime, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.storage_blob import StorageBlobRef

RETIREMENT_SCHEMA = "hive.original_session_retirement.v1"


def reference_identity(ref):
    metadata = {key: value for key, value in ref.metadata_json.items()
        if key not in {"session_retirement", "withdrawal", "draft_state"}}
    return {"id": str(ref.id), "tenant_id": str(ref.tenant_id), "blob_id": str(ref.blob_id),
        "owner_type": ref.owner_type, "owner_id": ref.owner_id, "purpose": ref.purpose,
        "created_at": ref.created_at.isoformat(), "metadata": metadata}


def identity_hash(ref):
    return hashlib.sha256(json.dumps(reference_identity(ref), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def closed_session_proof(ref):
    try:
        value = ref.metadata_json["session_retirement"]
        if (value["schema"] != RETIREMENT_SCHEMA or value["snapshot_sha256"] != identity_hash(ref)
            or value["agent_id"] != ref.metadata_json["agent_id"]
            or value["session_id"] != (ref.metadata_json.get("session_id") or ref.metadata_json.get("root_session_id"))
            or datetime.fromisoformat(value["closed_at"]).tzinfo is None):
            return None
        uuid.UUID(value["session_id"])
        return value
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def pin_source_id(ref):
    metadata = ref.metadata_json
    source_id = uuid.UUID(metadata["source_ref_id"])
    if ref.owner_type == "session_input":
        input_id = uuid.UUID(metadata["input_id"])
        revision = metadata["revision"]
        if (metadata["schema"] != "hive.original_input_pin.v1" or type(revision) is not int or revision < 1
            or ref.id != uuid.uuid5(source_id, f"session-input:{input_id}:revision:{revision}")
            or ref.owner_id != f"{input_id}:{revision}:{source_id}"):
            raise ValueError("unverified_input_pin")
    elif ref.owner_type == "session_tool_read":
        invocation_id = uuid.UUID(metadata["invocation_id"])
        if (metadata["schema"] != "hive.original_tool_pin.v1" or ref.owner_id != str(invocation_id)
            or ref.id != uuid.uuid5(invocation_id, "original-read")):
            raise ValueError("unverified_tool_pin")
    else:
        raise ValueError("unknown_original_pin")
    uuid.UUID(metadata["session_id"])
    uuid.UUID(metadata["agent_id"])
    return source_id


async def mark_closed_original_references(db, *, session):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.channel_originals import SCHEMA
    from app.services.original_tool_sources import original_tool_result_sources

    rows = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.tenant_id == session.tenant_id,
        StorageBlobRef.purpose == "original_attachment", or_(
            (StorageBlobRef.owner_type.in_(("session_input", "session_tool_read")))
                & (StorageBlobRef.metadata_json["session_id"].astext == str(session.id)),
            (StorageBlobRef.owner_type == "channel_upload")
                & (StorageBlobRef.metadata_json["root_session_id"].astext == str(session.id)),
        )).order_by(StorageBlobRef.id).with_for_update().execution_options(populate_existing=True))).all()
    for ref in rows:
        try:
            metadata = ref.metadata_json
            if metadata["agent_id"] != str(session.agent_id):
                continue
            if ref.owner_type == "channel_upload":
                manifest_id = uuid.UUID(ref.owner_id)
                manifest = await db.get(WorkspaceResourceManifest, manifest_id, populate_existing=True)
                if (metadata["schema"] != SCHEMA or ref.id != uuid.uuid5(manifest_id, "channel-original")
                    or manifest is None or manifest.tenant_id != session.tenant_id or manifest.agent_id != session.agent_id
                    or manifest.root_session_id != session.id or manifest.path != metadata["path"]
                    or (str(manifest.owner_user_id) if manifest.owner_user_id else None) != metadata.get("owner_user_id")):
                    continue
            else:
                source_id = pin_source_id(ref)
                source = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.id == source_id,
                    StorageBlobRef.tenant_id == session.tenant_id, StorageBlobRef.blob_id == ref.blob_id,
                    StorageBlobRef.owner_type.in_(("chat_upload", "channel_upload")), StorageBlobRef.purpose == "original_attachment"))
                if source is None:
                    continue
                if ref.owner_type == "session_input":
                    item = await db.get(SessionTurnInput, uuid.UUID(metadata["input_id"]), populate_existing=True)
                    if (item is None or item.tenant_id != session.tenant_id or item.session_id != session.id
                        or item.revision < metadata["revision"]):
                        continue
                else:
                    invocation = await db.get(SessionToolInvocation, uuid.UUID(metadata["invocation_id"]), populate_existing=True)
                    if invocation is None or invocation.tenant_id != session.tenant_id or invocation.session_id != session.id:
                        continue
                    event = await db.get(ChatTranscriptEvent, invocation.result_event_id) if invocation.result_event_id else None
                    if (event is None or event.tenant_id != session.tenant_id or event.agent_id != session.agent_id
                        or event.session_id != session.id or event.invocation_id != invocation.id or event.item_kind != "tool_result"):
                        continue
                    payload = (event.metadata_json or {}).get("v2_payload", {})
                    sources, complete = await original_tool_result_sources(db, tenant_id=session.tenant_id,
                        agent_id=session.agent_id, invocation=invocation, content_hash=payload.get("content_hash"))
                    if not complete or sources != [{"source_kind": "original_attachment", "source_id": str(source.id),
                        "source_version": source.metadata_json["content_sha256"]}]:
                        continue
            ref.metadata_json = {**metadata, "session_retirement": {"schema": RETIREMENT_SCHEMA,
                "session_id": str(session.id), "agent_id": str(session.agent_id),
                "snapshot_sha256": identity_hash(ref), "closed_at": datetime.now(timezone.utc).isoformat()}}
        except (KeyError, ValueError, TypeError, AttributeError, HTTPException):
            # Preserve legacy/unverifiable holdings; absence is not a receipt.
            continue
