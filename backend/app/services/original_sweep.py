"""Exact-generation retirement with durable intent and explicit effect recovery."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun
from app.models.tenant_setting import TenantSetting
from app.services.blob_store import BlobDeleteReceipt, BlobIntegrityError, BlobLocation, BlobStoreError
from app.services.original_retention import POLICY_KEY, _project_policy, original_grace_deadline, original_manifest, original_retention_hold
from app.services.storage_intents import durable_storage_intent
from app.services.upload_originals import _recorded_store


class OriginalSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    expected_content_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    expected_manifest_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    expected_policy_version: int = Field(strict=True, ge=0)
    action: Literal["sweep", "reconcile"]
    retry_operation_id: uuid.UUID | None = None


def ledger_identity(tenant_id, operation_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"hive.original-sweep:{tenant_id}:{operation_id}")


async def _locked_state(db, tenant_id, blob_id, *, nowait=False):
    blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == tenant_id, StorageBlob.id == blob_id,
        StorageBlob.kind == "chat_upload").with_for_update(nowait=nowait).execution_options(populate_existing=True))
    if blob is None:
        raise HTTPException(404, detail={"code": "original_not_found"})
    row = await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == tenant_id, TenantSetting.key == POLICY_KEY)
        .with_for_update(nowait=nowait).execution_options(populate_existing=True))
    policy = _project_policy(row.value if row is not None else {"version": 0})
    referenced = await db.scalar(select(StorageBlobRef.id).where(StorageBlobRef.tenant_id == tenant_id,
        StorageBlobRef.blob_id == blob_id).limit(1)) is not None
    return blob, policy, referenced


async def _active_intent(db, tenant_id, blob_id):
    rows = list((await db.scalars(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id,
        StorageGCRun.mode == "sweep", StorageGCRun.status.in_(("running", "partial")),
        StorageGCRun.receipt_json["request"]["blob_id"].astext == str(blob_id)).with_for_update())).all())
    if len(rows) != 1:
        raise HTTPException(409, detail={"code": "original_delete_intent_requires_repair"})
    return rows[0]


def _hold(blob, policy, referenced, now):
    hold = original_retention_hold(blob, policy, referenced=referenced, now=now, states=("quarantined", "deleting"))
    deadline = original_grace_deadline(blob, policy)
    return hold or ("quarantine_evidence_unavailable" if deadline is None else "grace_period" if deadline > now else None)


def _result(blob, policy, request, *, outcome, hold=None, parent=None):
    return {"operation_id": str(request.operation_id), "blob_id": str(blob.id), "state": blob.state, "outcome": outcome,
        "hold_reason": hold, "manifest_sha256": original_manifest(blob, policy), "policy_version": policy["version"],
        "physical_bytes_reclaimed": None, "observed_at": datetime.now(timezone.utc).isoformat(), "replayed": False,
        "previous_operation_id": parent.receipt_json["request"]["operation_id"] if parent is not None else None,
        "recovery_action": "none" if outcome == "absent" else "reconcile_with_new_operation" if blob.state == "deleting"
            else "inspect_original_and_policy"}


async def _record(db, ledger, blob, result, evidence):
    from app.core.policy import write_audit_event

    ledger.status = "partial" if (blob.state == "deleting" and ledger.mode == "sweep"
        and ledger.receipt_json["evidence"]["dispatch_authorized"]) else "completed"
    ledger.processed_count = int(result["outcome"] == "absent")
    ledger.processed_bytes = blob.size_bytes if ledger.processed_count else 0
    ledger.failed_count = int(result["outcome"] in {"unknown", "changed", "unavailable", "denied"})
    ledger.finished_at = datetime.now(timezone.utc)
    ledger.receipt_json = {**ledger.receipt_json, "result": result, "evidence": {**ledger.receipt_json["evidence"], **evidence}}
    await write_audit_event(db, event_type="storage.original_sweep_observed", severity="info", actor_type="user",
        actor_id=uuid.UUID(ledger.receipt_json["request"]["actor_id"]), tenant_id=blob.tenant_id,
        resource_type="storage_blob", resource_id=blob.id, action=ledger.receipt_json["request"]["action"],
        details={key: result[key] for key in ("operation_id", "outcome", "state", "hold_reason")})
    await db.flush()


async def _prepare(db, *, tenant_id, actor_id, blob_id, request, authorize):
    ledger_id = ledger_identity(tenant_id, request.operation_id)
    identity = {**request.model_dump(mode="json"), "tenant_id": str(tenant_id), "blob_id": str(blob_id), "actor_id": str(actor_id)}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    async with durable_storage_intent(db, tenant_id) as intent:
        try:
            acquired = await intent.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(ledger_id.bytes[:8], signed=True)})
            if not acquired:
                raise HTTPException(409, detail={"code": "original_sweep_busy", "retryable": True})
            await authorize(intent)
            blob, policy, referenced = await _locked_state(intent, tenant_id, blob_id, nowait=True)
            prior = await intent.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id, StorageGCRun.id == ledger_id))
            if prior is not None:
                if prior.manifest_sha256 != fingerprint:
                    raise HTTPException(409, detail={"code": "original_sweep_operation_conflict"})
                return ledger_id, {**prior.receipt_json["result"], "replayed": True}
            if (request.expected_policy_version != policy["version"] or request.expected_content_sha256 != blob.content_sha256
                or request.expected_manifest_sha256 != original_manifest(blob, policy)):
                raise HTTPException(409, detail={"code": "original_sweep_manifest_changed", "recovery_action": "inspect_original_and_policy"})
            hold, parent = None, None
            if request.action == "reconcile":
                if blob.state != "deleting" or request.retry_operation_id is not None:
                    hold = "unresolved_state"
                else:
                    parent = await _active_intent(intent, tenant_id, blob_id)
            else:
                hold = _hold(blob, policy, referenced, datetime.now(timezone.utc))
                if not hold and blob.state == "deleting":
                    parent = await _active_intent(intent, tenant_id, blob_id)
                    if (parent.status != "partial" or parent.receipt_json.get("resolution", {}).get("outcome") != "present"
                        or str(request.retry_operation_id) != parent.receipt_json["request"]["operation_id"]):
                        hold = "explicit_inspected_retry_required"
                    else:
                        parent.status = "completed"
                        parent.receipt_json = {**parent.receipt_json, "resolution": {"authorized_retry": str(request.operation_id)}}
                elif request.retry_operation_id is not None:
                    hold = hold or "retry_predecessor_changed"
                if not hold:
                    blob.state = "deleting"
            result = _result(blob, policy, request, outcome="held" if hold else "unknown", hold=hold, parent=parent)
            ledger = StorageGCRun(id=ledger_id, tenant_id=tenant_id, policy_version=f"hive.original_sweep.v1:{policy['version']}",
                mode="sweep" if request.action == "sweep" else "inventory", status="running", manifest_sha256=fingerprint,
                candidate_count=1, candidate_bytes=blob.size_bytes, receipt_json={"request": identity, "result": result,
                    "evidence": {"provider": blob.provider, "bucket": blob.bucket, "object_key": blob.object_key,
                        "intent_manifest": original_manifest(blob, policy), "parent_id": str(parent.id) if parent is not None else None,
                        "prior_uncertain": parent is not None and request.action == "sweep",
                        "dispatch_authorized": hold is None and request.action == "sweep"}})
            intent.add(ledger)
            if hold:
                await _record(intent, ledger, blob, result, {})
            else:
                await intent.flush()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                raise HTTPException(409, detail={"code": "original_sweep_busy", "retryable": True}) from None
            raise
    return ledger_id, result if hold else None


async def sweep_original(db, *, tenant_id, actor_id, blob_id, request, authorize):
    ledger_id, replay = await _prepare(db, tenant_id=tenant_id, actor_id=actor_id, blob_id=blob_id, request=request, authorize=authorize)
    if replay is not None:
        return replay
    blob, policy, referenced = await _locked_state(db, tenant_id, blob_id)
    ledger = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id, StorageGCRun.id == ledger_id)
        .with_for_update().execution_options(populate_existing=True))
    if ledger.status != "running":
        return {**ledger.receipt_json["result"], "replayed": True}
    parent = None
    evidence = {}
    if (blob.state != "deleting" or original_manifest(blob, policy) != ledger.receipt_json["evidence"]["intent_manifest"]):
        result = _result(blob, policy, request, outcome="held", hold="original_or_policy_changed")
        if request.action == "sweep" and blob.state == "deleting" and not ledger.receipt_json["evidence"]["prior_uncertain"]:
            blob.state = "quarantined"
            result = _result(blob, policy, request, outcome="held", hold="original_or_policy_changed")
        await _record(db, ledger, blob, result, evidence)
        return result
    hold = _hold(blob, policy, referenced, datetime.now(timezone.utc)) if request.action == "sweep" else None
    if hold:
        if not ledger.receipt_json["evidence"]["prior_uncertain"]:
            blob.state = "quarantined"
        result = _result(blob, policy, request, outcome="held", hold=hold)
    else:
        # Reuse the canonical live-user/selected-company authentication. The
        # route's trusted callback runs again after waiting for current locks.
        await authorize(db)
        location = BlobLocation(blob.provider, blob.bucket, blob.object_key)
        try:
            store = _recorded_store(blob, tenant_id)
        except BlobStoreError:
            store = None
        if request.action == "reconcile":
            parent = await _active_intent(db, tenant_id, blob_id)
            outcome = "unknown"
            try:
                if store is not None:
                    observed = await store.stat_for_delete(location, expected_size=blob.size_bytes)
                    evidence["observed"] = {"available": observed.available, "sha256": observed.sha256, "size_bytes": observed.size_bytes}
                    outcome = "absent" if not observed.available else "present" if (
                        observed.sha256, observed.size_bytes) == (blob.content_sha256, blob.size_bytes) else "changed"
            except BlobIntegrityError:
                outcome = "changed"
            except (BlobStoreError, OSError):
                pass
            parent.status = "completed" if outcome == "absent" else "partial"
            parent.receipt_json = {**parent.receipt_json, "resolution": {"operation_id": str(request.operation_id), "outcome": outcome}}
        else:
            try:
                effect = await store.delete(location, gc_receipt_id=str(ledger.id), expected_sha256=blob.content_sha256,
                    expected_size=blob.size_bytes) if store is not None else BlobDeleteReceipt("unavailable")
            except (BlobStoreError, OSError):
                effect = BlobDeleteReceipt("unknown", True)
            outcome = effect.outcome
            evidence["effect_attempted"] = effect.effect_attempted
            if outcome != "absent" and not effect.effect_attempted and not ledger.receipt_json["evidence"]["prior_uncertain"]:
                blob.state = "quarantined"
        if outcome == "absent":
            blob.state, blob.deleted_at = "deleted", datetime.now(timezone.utc)
        result = _result(blob, policy, request, outcome=outcome, parent=parent)
    await _record(db, ledger, blob, result, evidence)
    return result
