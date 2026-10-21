"""Observe recorded upload intent bytes without granting original ownership."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.models.storage_blob import StorageBlob, StorageGCRun
from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobStoreError
from app.services.upload_originals import _recorded_store


class OriginalReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    expected_content_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    action: Literal["inspect", "recover"]


async def pending_originals(db, *, tenant_id, limit, after=None):
    query = select(StorageBlob).where(StorageBlob.tenant_id == tenant_id, StorageBlob.kind == "chat_upload",
        StorageBlob.state == "uploading")
    if after is not None:
        query = query.where(StorageBlob.id > after)
    rows = list((await db.scalars(query.order_by(StorageBlob.id).limit(limit + 1))).all())
    items = [{"blob_id": str(row.id), "provider": row.provider, "content_sha256": row.content_sha256,
        "size_bytes": row.size_bytes, "state": row.state, "created_at": row.created_at.isoformat()} for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["blob_id"] if len(rows) > limit else None}


async def reconcile_original(db, *, tenant_id, actor_id, blob_id, request):
    from app.core.policy import write_audit_event

    ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hive.original-reconciliation:{tenant_id}:{request.operation_id}")
    # Serialize the operation across blob IDs; its nonce remains tenant scoped.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(ledger_id.bytes[:8], signed=True)})
    blob = await db.scalar(select(StorageBlob).where(StorageBlob.id == blob_id, StorageBlob.tenant_id == tenant_id,
        StorageBlob.kind == "chat_upload").with_for_update().execution_options(populate_existing=True))
    if blob is None:
        raise HTTPException(404, detail={"code": "original_intent_not_found"})
    identity = {**request.model_dump(mode="json"), "tenant_id": str(tenant_id), "blob_id": str(blob_id), "actor_id": str(actor_id)}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = await db.scalar(select(StorageGCRun).where(StorageGCRun.id == ledger_id,
        StorageGCRun.tenant_id == tenant_id))
    if prior is not None:
        if prior.manifest_sha256 != fingerprint or not isinstance(prior.receipt_json.get("result"), dict):
            raise HTTPException(409, detail={"code": "original_reconciliation_operation_conflict"})
        return {**prior.receipt_json["result"], "replayed": True}
    if blob.content_sha256 != request.expected_content_sha256 or blob.state != "uploading":
        raise HTTPException(409, detail={"code": "original_intent_changed", "recovery_action": "reload_pending_originals"})
    evidence = {"provider": blob.provider, "bucket": blob.bucket, "object_key": blob.object_key,
        "expected_sha256": blob.content_sha256, "expected_size_bytes": blob.size_bytes}
    try:
        store = _recorded_store(blob, tenant_id)
        observed = await store.stat(BlobLocation(blob.provider, blob.bucket, blob.object_key), expected_size=blob.size_bytes)
        evidence["observed"] = {"available": observed.available, "sha256": observed.sha256, "size_bytes": observed.size_bytes}
        outcome = "missing" if not observed.available else (
            "present" if (observed.sha256, observed.size_bytes) == (blob.content_sha256, blob.size_bytes) else "changed")
    except BlobIntegrityError:
        evidence["error"] = "content_integrity_mismatch"
        outcome = "changed"
    except (BlobStoreError, OSError):
        evidence["error"] = "provider_unavailable"
        outcome = "unknown"
    recovered = outcome == "present" and request.action == "recover"
    now = datetime.now(timezone.utc)
    if recovered:
        blob.state, blob.verified_at = "available", now
    actions = {"present": "none" if recovered else "recover_with_new_operation", "missing": "retry_original_or_reinspect",
        "changed": "repair_original_object", "unknown": "repair_provider_and_reinspect"}
    result = {"operation_id": str(request.operation_id), "blob_id": str(blob_id), "outcome": outcome,
        "state": blob.state, "recovered": recovered, "replayed": False, "observed_at": now.isoformat(),
        "retryable": outcome in {"missing", "unknown"}, "recovery_action": actions[outcome]}
    db.add(StorageGCRun(id=ledger_id, tenant_id=tenant_id, policy_version="hive.original_reconciliation.v1",
        mode="restore" if request.action == "recover" else "inventory", status="completed", manifest_sha256=fingerprint,
        candidate_count=1, candidate_bytes=blob.size_bytes, processed_count=int(recovered), processed_bytes=blob.size_bytes if recovered else 0,
        failed_count=int(outcome in {"changed", "unknown"}), finished_at=now,
        receipt_json={"request": identity, "result": result, "evidence": evidence}))
    await write_audit_event(db, event_type="storage.original_reconciled", severity="info", actor_type="user", actor_id=actor_id,
        tenant_id=tenant_id, resource_type="storage_blob", resource_id=blob.id, action=request.action,
        details={"operation_id": str(request.operation_id), "outcome": outcome, "recovered": recovered,
            "manifest_sha256": fingerprint})
    await db.flush()
    return result
