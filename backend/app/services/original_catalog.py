"""Operator-safe catalog and actor-bound readback of recorded original work."""

from typing import Literal
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

OriginalState = Literal["uploading", "available", "quarantined", "deleting", "deleted", "failed"]
OperationFamily = Literal["reconciliation", "retention", "sweep", "references"]
NAMESPACES = {"reconciliation": "hive.original-reconciliation", "retention": "hive.original-retention", "sweep": "hive.original-sweep"}
NAMESPACES["references"] = "hive.original-reference-retirement"
RESULT_FIELDS = {"operation_id", "blob_id", "outcome", "state", "recovered", "replayed", "observed_at", "retryable",
    "recovery_action", "policy_version", "changed", "hold_reason", "observation", "delete_after", "sweep_eligible",
    "manifest_sha256", "physical_bytes_reclaimed", "previous_operation_id"}
RESULT_FIELDS.add("reference_id")


async def original_catalog(db, *, tenant_id, limit, after=None, state=None, unreferenced=False):
    refs = select(func.count(StorageBlobRef.id)).where(StorageBlobRef.tenant_id == tenant_id,
        StorageBlobRef.blob_id == StorageBlob.id).correlate(StorageBlob).scalar_subquery()
    query = select(StorageBlob, refs.label("reference_count")).where(StorageBlob.tenant_id == tenant_id,
        StorageBlob.kind == "chat_upload")
    if after is not None:
        query = query.where(StorageBlob.id > after)
    if state is not None:
        query = query.where(StorageBlob.state == state)
    if unreferenced:
        query = query.where(refs == 0)
    rows = list((await db.execute(query.order_by(StorageBlob.id).limit(limit + 1))).all())
    page = rows[:limit]
    active = {}
    if page:
        ledgers = (await db.scalars(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id,
            StorageGCRun.mode == "sweep", StorageGCRun.status.in_(("running", "partial")),
            StorageGCRun.receipt_json["evidence"]["dispatch_authorized"].as_boolean().is_(True),
            StorageGCRun.receipt_json["request"]["blob_id"].astext.in_([str(row.id) for row, _ in page])))).all()
        for ledger in ledgers:
            identity = ledger.receipt_json["request"]
            active.setdefault(identity["blob_id"], []).append({"operation_id": identity["operation_id"], "status": ledger.status,
                "outcome": ledger.receipt_json.get("result", {}).get("outcome"),
                "reconciled_outcome": ledger.receipt_json.get("resolution", {}).get("outcome")})
    items = []
    for row, count in page:
        pending = active.get(str(row.id), [])
        item = {"blob_id": str(row.id), "provider": row.provider, "content_sha256": row.content_sha256,
            "size_bytes": row.size_bytes, "state": row.state, "reference_count": count,
            "active_sweep": pending[0] if len(pending) == 1 else None, "operation_conflict": len(pending) > 1}
        for key in ("created_at", "verified_at", "quarantined_at", "delete_after", "deleted_at"):
            value = getattr(row, key)
            item[key] = value.isoformat() if value is not None else None
        items.append(item)
    return {"items": items, "next_cursor": items[-1]["blob_id"] if len(rows) > limit else None}


async def read_original_operation(db, *, tenant_id, actor_id, blob_id, family, operation_id):
    ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{NAMESPACES[family]}:{tenant_id}:{operation_id}")
    ledger = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id, StorageGCRun.id == ledger_id,
        StorageGCRun.receipt_json["request"]["blob_id"].astext == str(blob_id),
        StorageGCRun.receipt_json["request"]["actor_id"].astext == str(actor_id)))
    if ledger is None:
        # Absence is not evidence that an earlier network request stopped.
        raise HTTPException(404, detail={"code": "original_operation_not_found", "retryable": True})
    result = ledger.receipt_json.get("result")
    if not isinstance(result, dict):
        raise HTTPException(503, detail={"code": "original_operation_receipt_unavailable", "retryable": True})
    return {"operation_id": str(operation_id), "status": ledger.status,
        "result": {key: value for key, value in result.items() if key in RESULT_FIELDS}}
