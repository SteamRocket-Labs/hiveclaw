"""Invocation-bound original read receipts and current source revalidation."""

from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.invocation_span import InvocationSpan
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionToolInvocation
from app.models.storage_blob import StorageBlobRef
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor
from app.services.session_model_inputs import _hash

ORIGINAL_READ_TOOLS = frozenset({"read_file", "read_document", "fs_read"})


async def validate_original_dependency(db, tenant_id, dependency, *, user=None, runtime_request=None):
    from app.services.blob_store import BlobStoreError
    from app.services.upload_originals import resolve_original_download

    try:
        principal = None
        if runtime_request is not None:
            principal, user = await _runtime_input_actor(db, tenant_id, runtime_request)
        if user is None or user.tenant_id != tenant_id:
            raise ContentInputReceiptError("original_source_access_denied", "The original reader is unavailable.", 403)
        blob, metadata = await resolve_original_download(db, user, uuid.UUID(dependency["source_id"]), runtime_principal=principal)
        if (blob.state != "available" or blob.content_sha256 != dependency["source_version"]
            or metadata["content_sha256"] != blob.content_sha256 or metadata["size_bytes"] != blob.size_bytes):
            raise ContentInputReceiptError("original_source_changed", "The original source requires reconciliation.", 503)
        return blob, metadata
    except HTTPException as exc:
        raise ContentInputReceiptError("original_source_access_denied", "The original source is no longer accessible.", 403) from exc
    except (BlobStoreError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContentInputReceiptError):
            raise
        raise ContentInputReceiptError("original_source_unavailable", "The original source requires reconciliation.", 503) from exc


async def record_original_tool_result(*, context, arguments, tool_name, result):
    from app.core.policy import _lock_audit_chain
    from app.database import tenant_scoped_session
    from app.tools.runtime import ToolExecutionRequest

    if context is None or not context.runtime_task_id or not context.session_id or not context.tool_call_id:
        return
    tenant_id = uuid.UUID(str(context.tenant_id))
    async with tenant_scoped_session(tenant_id, require_tenant=True, source="original_read_receipt") as db:
        invocation = await db.scalar(select(SessionToolInvocation).join(ChatSession, ChatSession.id == SessionToolInvocation.session_id).where(
            SessionToolInvocation.tenant_id == tenant_id, ChatSession.tenant_id == tenant_id, ChatSession.agent_id == context.agent_id,
            SessionToolInvocation.session_id == uuid.UUID(str(context.session_id)),
            SessionToolInvocation.run_id == uuid.UUID(str(context.runtime_task_id)),
            SessionToolInvocation.provider_tool_use_id == context.tool_call_id, SessionToolInvocation.tool_name == tool_name))
        if invocation is None:
            return
        actual_arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        if tool_name not in ORIGINAL_READ_TOOLS or actual_arguments != arguments:
            raise ContentInputReceiptError("original_read_invocation_changed", "The original read invocation changed.", 503)
        source = result.metadata.get("original_attachment")
        dependencies = []
        if source is not None:
            await _lock_audit_chain(db, tenant_id)
            ref = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.id == uuid.UUID(source["reference_id"]),
                StorageBlobRef.tenant_id == tenant_id).with_for_update().execution_options(populate_existing=True))
            if ref is None:
                raise ContentInputReceiptError("original_source_access_denied", "The original reference was released.", 403)
            dependency = {"source_kind": "original_attachment", "source_id": str(ref.id), "source_version": source["content_sha256"]}
            await validate_original_dependency(db, tenant_id, dependency,
                runtime_request=ToolExecutionRequest(tool_name=tool_name, arguments=arguments, context=context))
            dependencies.append(dependency)
            pin_id = uuid.uuid5(invocation.id, "original-read")
            if await db.get(StorageBlobRef, pin_id) is None:
                db.add(StorageBlobRef(id=pin_id, tenant_id=tenant_id, blob_id=ref.blob_id,
                    owner_type="session_tool_read", owner_id=str(invocation.id), purpose="original_attachment",
                    metadata_json={"schema": "hive.original_tool_pin.v1", "source_ref_id": str(ref.id),
                        "invocation_id": str(invocation.id), "session_id": str(invocation.session_id), "agent_id": str(context.agent_id)}))
        identity = uuid.uuid5(invocation.id, "original-read-proof")
        proof = {"schema": "hive.original_tool_read.v1", "invocation_id": str(invocation.id),
            "arguments_hash": _hash(arguments), "content_hash": _hash(str(result)), "sources": dependencies,
            "status": "verified" if source is not None else result.metadata["original_read_status"]}
        row = await db.get(InvocationSpan, identity)
        if row is None:
            db.add(InvocationSpan(id=identity, tenant_id=tenant_id, agent_id=context.agent_id, user_id=context.user_id,
                session_id=str(invocation.session_id), runtime_task_id=invocation.run_id, trace_id=f"session-run:{invocation.run_id}",
                span_id=f"original-read:{identity}", span_type="original_attachment_read", name=tool_name,
                status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"original_read": proof}))
        elif row.metadata_json != {"original_read": proof}:
            raise ContentInputReceiptError("original_read_needs_reconciliation", "The original read must be reconciled.", 503)
        await db.flush()


async def original_tool_result_sources(db, *, tenant_id, agent_id, invocation, content_hash):
    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "original-read-proof"), populate_existing=True)
    proof = (row.metadata_json or {}).get("original_read") if row else None
    arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
    session = await db.get(ChatSession, invocation.session_id)
    valid = (row is not None and row.tenant_id == tenant_id and row.agent_id == agent_id
        and invocation.tenant_id == tenant_id and session is not None and session.tenant_id == tenant_id and session.agent_id == agent_id
        and row.runtime_task_id == invocation.run_id and row.session_id == str(invocation.session_id)
        and row.span_type == "original_attachment_read" and row.name == invocation.tool_name and row.status == "captured"
        and isinstance(proof, dict) and proof.get("schema") == "hive.original_tool_read.v1"
        and proof.get("invocation_id") == str(invocation.id) and proof.get("content_hash") == content_hash
        and proof.get("arguments_hash") == _hash(arguments) and proof.get("status") in {"verified", "denied", "unavailable"}
        and isinstance(proof.get("sources"), list) and invocation.tool_name in ORIGINAL_READ_TOOLS
        and ((proof["status"] == "verified" and len(proof["sources"]) == 1 and invocation.effect_state == "effect_committed")
            or (proof["status"] in {"denied", "unavailable"} and proof["sources"] == []
                and invocation.result_event_id is not None and invocation.effect_state in {"failed", "prepared_not_started", "effect_committed"})))
    return (proof["sources"], True) if valid else ([], False)
