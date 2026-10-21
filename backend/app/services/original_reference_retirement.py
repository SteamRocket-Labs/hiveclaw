"""Inspect and explicitly retire exact original holdings, without provider effects."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError

from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask, SEALED_RUNTIME_TASK_STATUSES
from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun
from app.services.original_reference_pins import closed_session_proof, pin_source_id

SCHEMA = "hive.original_reference_retirement.v1"
NAMESPACE = "hive.original-reference-retirement"


class OriginalReferenceRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    action: Literal["retire_reference", "withdraw_source"]
    expected_content_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    reference_id: uuid.UUID
    expected_reference_sha256: str = Field(pattern="^[0-9a-f]{64}$")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(ref):
    return {"id": str(ref.id), "tenant_id": str(ref.tenant_id), "blob_id": str(ref.blob_id),
        "owner_type": ref.owner_type, "owner_id": ref.owner_id, "purpose": ref.purpose,
        "created_at": ref.created_at.isoformat(), "pinned_until": ref.pinned_until.isoformat() if ref.pinned_until else None,
        "legal_hold": ref.legal_hold, "metadata": ref.metadata_json}


async def _blob(db, tenant_id, blob_id):
    blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == tenant_id,
        StorageBlob.id == blob_id, StorageBlob.kind == "chat_upload").execution_options(populate_existing=True))
    if blob is None:
        raise HTTPException(404, detail={"code": "original_not_found"})
    return blob


async def _active_work(db, *, tenant_id, session_id=None, agent_id=None):
    scopes = []
    if session_id:
        scopes.extend(column.in_((str(session_id), session_id.hex)) for column in
            (RuntimeTask.parent_session_id, RuntimeTask.child_session_id, RuntimeTask.root_session_id))
    if agent_id:
        scopes.extend((RuntimeTask.parent_agent_id == agent_id, RuntimeTask.child_agent_id == agent_id))
    return bool(scopes) and await db.scalar(select(RuntimeTask.id).where(RuntimeTask.tenant_id == tenant_id,
        RuntimeTask.status.not_in(SEALED_RUNTIME_TASK_STATUSES), or_(*scopes)).limit(1)) is not None


async def _root_has_work(db, *, tenant_id, source):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionModelResult, SessionTurnInput

    needle = [{"type": "attachment_ref", "attachment": {"original_attachment": {"id": str(source.id)}}}]
    dependency = [{"source_kind": "original_attachment", "source_id": str(source.id)}]
    queries = [select(StorageBlobRef.id).where(StorageBlobRef.tenant_id == tenant_id,
        StorageBlobRef.id != source.id, StorageBlobRef.metadata_json["source_ref_id"].astext == str(source.id)),
        select(SessionTurnInput.id).where(SessionTurnInput.tenant_id == tenant_id, SessionTurnInput.content_parts_json.contains(needle)),
        select(SessionCommand.id).where(SessionCommand.tenant_id == tenant_id,
            SessionCommand.request_json.contains({"content_parts": needle})),
        select(ChatTranscriptEvent.id).where(ChatTranscriptEvent.tenant_id == tenant_id, or_(
            ChatTranscriptEvent.metadata_json["v2_payload"].contains({"content_parts": needle}),
            ChatTranscriptEvent.metadata_json["v2_payload"]["payload"].contains({"content_parts": needle}))),
        select(SessionModelResult.id).where(SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.model_request_snapshot_json["input_bindings"]["dependencies"].contains(dependency))]
    for query in queries:
        if await db.scalar(query.limit(1)) is not None:
            return True
    return False


async def _eligibility(db, *, blob, ref):
    from app.services.channel_originals import SCHEMA as CHANNEL_SCHEMA
    from app.services.original_source_lifecycle import _withdrawal
    from app.services.upload_originals import REF_SCHEMA

    evidence = {"reference": _snapshot(ref)}
    reason = None
    if ref.legal_hold:
        reason = "legal_hold"
    elif ref.pinned_until and ref.pinned_until > datetime.now(timezone.utc):
        reason = "retention_hold"
    elif ref.purpose != "original_attachment" or blob.state not in {"available", "quarantined"}:
        reason = "reference_reconciliation_required"
    else:
        try:
            metadata = ref.metadata_json
            if ref.owner_type in {"session_input", "session_tool_read"}:
                source_id = pin_source_id(ref)
                source = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.id == source_id,
                    StorageBlobRef.tenant_id == blob.tenant_id, StorageBlobRef.blob_id == blob.id,
                    StorageBlobRef.purpose == "original_attachment", StorageBlobRef.owner_type.in_(("chat_upload", "channel_upload"))))
                closed = closed_session_proof(ref)
                session_id = uuid.UUID(metadata["session_id"])
                parent = await db.scalar(select(ChatSession.id).where(ChatSession.tenant_id == blob.tenant_id, ChatSession.id == session_id))
                evidence.update(source_reference=_snapshot(source) if source else None, closure=closed, parent_present=parent is not None)
                if parent is not None or await _active_work(db, tenant_id=blob.tenant_id, session_id=session_id):
                    reason = "work_reference"
                elif closed is None or source is None:
                    reason = "reference_reconciliation_required"
            elif ref.owner_type in {"chat_upload", "channel_upload"}:
                if (metadata["content_sha256"] != blob.content_sha256 or type(metadata["size_bytes"]) is not int
                    or metadata["size_bytes"] != blob.size_bytes
                    or metadata["schema"] != (REF_SCHEMA if ref.owner_type == "chat_upload" else CHANNEL_SCHEMA)):
                    raise ValueError("source_identity_changed")
                if ref.owner_type == "chat_upload" and ref.owner_id != str(ref.id):
                    raise ValueError("source_identity_changed")
                if ref.owner_type == "channel_upload" and ref.id != uuid.uuid5(uuid.UUID(ref.owner_id), "channel-original"):
                    raise ValueError("source_identity_changed")
                withdrawn = _withdrawal(ref)
                if withdrawn is None:
                    reason = "source_active"
                else:
                    uuid.UUID(withdrawn["actor_user_id"])
                    if datetime.fromisoformat(withdrawn["withdrawn_at"]).tzinfo is None:
                        raise ValueError("withdrawal_requires_reconciliation")
                    if await _root_has_work(db, tenant_id=blob.tenant_id, source=ref):
                        reason = "work_reference"
                    elif await _active_work(db, tenant_id=blob.tenant_id,
                        agent_id=uuid.UUID(metadata["agent_id"]) if metadata.get("agent_id") else None):
                        reason = "work_reference"
                    elif ref.owner_type == "channel_upload":
                        closed = closed_session_proof(ref)
                        session_id = uuid.UUID(metadata["root_session_id"])
                        parent = await db.scalar(select(ChatSession.id).where(ChatSession.tenant_id == blob.tenant_id,
                            ChatSession.id == session_id))
                        evidence.update(closure=closed, parent_present=parent is not None)
                        reason = "work_reference" if parent is not None else "reference_reconciliation_required" if closed is None else None
                    elif metadata.get("draft_state") == "bound":
                        retired = await db.scalar(select(StorageGCRun.id).where(StorageGCRun.tenant_id == blob.tenant_id,
                            StorageGCRun.receipt_json["schema"].astext == SCHEMA,
                            StorageGCRun.receipt_json["result"]["outcome"].astext == "reference_retired",
                            StorageGCRun.receipt_json["evidence"]["reference"]["metadata"]["source_ref_id"].astext == str(ref.id)).limit(1))
                        evidence["retired_pin_operation"] = str(retired) if retired else None
                        if retired is None:
                            reason = "reference_reconciliation_required"
                    elif metadata.get("draft_state") != "unbound":
                        reason = "reference_reconciliation_required"
            else:
                reason = "reference_reconciliation_required"
        except (KeyError, ValueError, TypeError, AttributeError, HTTPException):
            reason = "reference_reconciliation_required"
    evidence["hold_reason"] = reason
    return reason, evidence, _hash(evidence)


async def original_reference_catalog(db, *, tenant_id, blob_id, limit, after=None):
    blob = await _blob(db, tenant_id, blob_id)
    query = select(StorageBlobRef).where(StorageBlobRef.tenant_id == tenant_id, StorageBlobRef.blob_id == blob_id)
    if after:
        query = query.where(StorageBlobRef.id > after)
    rows = (await db.scalars(query.order_by(StorageBlobRef.id).limit(limit + 1))).all()
    items = []
    for ref in rows[:limit]:
        from app.services.original_source_disposition import disposition_eligibility

        reason, _, snapshot = await _eligibility(db, blob=blob, ref=ref)
        withdrawal_reason, custody = await disposition_eligibility(db, blob=blob, ref=ref)
        items.append({"reference_id": str(ref.id), "owner_type": ref.owner_type, "retirable": reason is None,
            "hold_reason": reason, "snapshot_sha256": snapshot, "created_at": ref.created_at.isoformat(),
            "pinned_until": ref.pinned_until.isoformat() if ref.pinned_until else None, "legal_hold": ref.legal_hold,
            "withdrawable": withdrawal_reason is None, "withdrawal_hold_reason": withdrawal_reason,
            "withdrawal_snapshot_sha256": _hash({"reference": _snapshot(ref), "custody": custody, "hold_reason": withdrawal_reason})})
    return {"items": items, "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None}


async def _current_administrator(db, *, tenant_id, actor_id):
    from app.core.permissions import is_scoped_business_admin
    from app.models.tenant import Tenant
    from app.services.runtime_user_identity import load_runtime_user

    try:
        actor = await load_runtime_user(db, actor_id, lock=True)
        active = await db.scalar(select(Tenant.is_active).where(Tenant.id == tenant_id)
            .with_for_update(read=True, nowait=True))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise HTTPException(503, detail={"code": "original_reference_authority_unavailable", "retryable": True}) from exc
    if actor is None or active is not True or not is_scoped_business_admin(actor, resource_tenant_id=tenant_id):
        raise HTTPException(403, detail={"code": "original_reference_admin_required", "retryable": False})


async def retire_original_reference(db, *, tenant_id, actor_id, blob_id, request):
    from app.core.policy import _lock_audit_chain, write_audit_event

    operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{NAMESPACE}:{tenant_id}:{request.operation_id}")
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(operation_id.bytes[:8], signed=True)})
    await _lock_audit_chain(db, tenant_id)
    blob = await _blob(db, tenant_id, blob_id)
    ref = await db.scalar(select(StorageBlobRef).where(StorageBlobRef.tenant_id == tenant_id, StorageBlobRef.blob_id == blob_id,
        StorageBlobRef.id == request.reference_id).with_for_update().execution_options(populate_existing=True))
    await _current_administrator(db, tenant_id=tenant_id, actor_id=actor_id)
    identity = {**request.model_dump(mode="json"), "tenant_id": str(tenant_id), "actor_id": str(actor_id), "blob_id": str(blob_id)}
    fingerprint = _hash(identity)
    previous = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id, StorageGCRun.id == operation_id))
    if previous:
        if previous.manifest_sha256 != fingerprint or not isinstance(previous.receipt_json.get("result"), dict):
            raise HTTPException(409, detail={"code": "original_reference_operation_conflict"})
        return {**previous.receipt_json["result"], "replayed": True}
    if blob.content_sha256 != request.expected_content_sha256:
        raise HTTPException(409, detail={"code": "original_reference_changed"})
    reason, evidence, changed = None, {}, False
    outcome = "reference_absent"
    if ref is not None:
        if request.action == "withdraw_source":
            from app.services.original_source_disposition import disposition_eligibility, disposition_marker

            reason, custody = await disposition_eligibility(db, blob=blob, ref=ref, lock=True)
            evidence = {"reference": _snapshot(ref), "custody": custody, "hold_reason": reason}
            snapshot = _hash(evidence)
        else:
            reason, evidence, snapshot = await _eligibility(db, blob=blob, ref=ref)
        if snapshot != request.expected_reference_sha256:
            reason = "reference_changed"
        if reason:
            outcome = "source_withdrawal_held" if request.action == "withdraw_source" else "reference_held"
        elif request.action == "withdraw_source":
            ref.metadata_json = {**ref.metadata_json, "withdrawal": disposition_marker(
                custody=custody, actor_id=actor_id, operation_id=request.operation_id)}
            await db.flush()
            changed, outcome = True, "source_withdrawn"
        else:
            await db.delete(ref)
            await db.flush()
            changed, outcome = True, "reference_retired"
    now = datetime.now(timezone.utc)
    result = {"operation_id": str(request.operation_id), "blob_id": str(blob_id), "reference_id": str(request.reference_id),
        "state": blob.state, "outcome": outcome, "changed": changed, "hold_reason": reason, "observed_at": now.isoformat(),
        "replayed": False, "physical_bytes_reclaimed": None,
        "recovery_action": "inspect_references" if reason else "inspect_original_retention"}
    db.add(StorageGCRun(id=operation_id, tenant_id=tenant_id, policy_version=SCHEMA, mode="inventory", status="completed",
        manifest_sha256=fingerprint, candidate_count=1, candidate_bytes=0, processed_count=int(changed), processed_bytes=0,
        failed_count=0, finished_at=now, receipt_json={"schema": SCHEMA, "request": identity, "result": result, "evidence": evidence}))
    await write_audit_event(db, event_type="storage.original_source_disposition" if request.action == "withdraw_source"
        else "storage.original_reference_retirement", severity="info", actor_type="user",
        actor_id=actor_id, tenant_id=tenant_id, resource_type="storage_blob_ref", resource_id=request.reference_id,
        action="withdraw_inactive_original_source" if request.action == "withdraw_source" else "retire_original_reference",
        details={"blob_id": str(blob_id), "outcome": outcome,
            "operation_id": str(request.operation_id), "physical_bytes_reclaimed": None})
    await db.flush()
    return result
