"""Explicit company policy and reversible quarantine of unreferenced originals."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun
from app.models.tenant_setting import TenantSetting
from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobStoreError
from app.services.upload_originals import _recorded_store

POLICY_KEY = "original_attachment_retention"


def original_manifest(blob, policy):
    identity = {key: getattr(blob, key) for key in ("provider", "bucket", "object_key", "content_sha256", "size_bytes", "state", "metadata_json")}
    identity.update(id=str(blob.id), tenant_id=str(blob.tenant_id), policy=policy)
    for key in ("created_at", "verified_at", "quarantined_at", "delete_after"):
        value = getattr(blob, key)
        identity[key] = value.isoformat() if value is not None else None
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def original_grace_deadline(blob, policy):
    if blob.quarantined_at is None or blob.delete_after is None:
        return None
    try:
        return max(blob.delete_after, blob.quarantined_at + timedelta(days=policy["grace_days"]))
    except OverflowError:
        raise ValueError("Original retention grace is out of range") from None


def original_retention_hold(blob, policy, *, referenced, now, states=("available", "quarantined")):
    if not policy["enabled"]:
        return "policy_disabled"
    if referenced:
        return "referenced"
    if blob.state not in states or blob.verified_at is None:
        return "unresolved_state"
    if (now - max(blob.created_at, blob.verified_at)).total_seconds() < policy["retention_days"] * 86400:
        return "retention_age"
    return None


class OriginalRetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    retention_days: float = Field(strict=True, ge=0, allow_inf_nan=False)
    grace_days: float = Field(strict=True, ge=0, allow_inf_nan=False)


class OriginalRetentionPolicyUpdate(OriginalRetentionPolicy):
    expected_version: int = Field(strict=True, ge=0)


class OriginalRetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    expected_content_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    expected_policy_version: int = Field(strict=True, ge=0)
    action: Literal["inspect", "quarantine", "restore"]


def _project_policy(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("Invalid original retention policy")
    if value == {"version": 0}:
        return {"enabled": False, "retention_days": 30.0, "grace_days": 7.0, "version": 0, "source": "unset"}
    if value["version"] < 1:
        raise ValueError("Invalid original retention policy")
    policy = OriginalRetentionPolicy.model_validate({key: value.get(key) for key in ("enabled", "retention_days", "grace_days")})
    return {**policy.model_dump(), "version": value["version"], "source": "company_admin"}


async def _policy_row(db, tenant_id, *, lock=False):
    if lock:
        await db.execute(insert(TenantSetting).values(tenant_id=tenant_id, key=POLICY_KEY, value={"version": 0})
            .on_conflict_do_nothing(index_elements=[TenantSetting.tenant_id, TenantSetting.key]))
    query = select(TenantSetting).where(TenantSetting.tenant_id == tenant_id, TenantSetting.key == POLICY_KEY)
    return await db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)


async def read_original_retention(db, tenant_id):
    row = await _policy_row(db, tenant_id)
    return _project_policy(row.value if row is not None else {"version": 0})


async def save_original_retention(db, tenant_id, actor_id, data):
    from app.core.policy import write_audit_event

    row = await _policy_row(db, tenant_id, lock=True)
    before = _project_policy(row.value)
    if before["version"] != data.expected_version:
        raise HTTPException(409, detail={"code": "original_retention_version_conflict", "recovery_action": "reload_policy"})
    try:
        datetime.now(timezone.utc) + timedelta(days=data.grace_days)
    except OverflowError:
        raise HTTPException(422, detail={"code": "original_retention_grace_out_of_range"}) from None
    row.value = {**data.model_dump(exclude={"expected_version"}), "version": before["version"] + 1}
    after = _project_policy(row.value)
    await write_audit_event(db, event_type="storage.original_retention_updated", severity="info", actor_type="user",
        actor_id=actor_id, tenant_id=tenant_id, resource_type="storage_retention", action="update_original_retention",
        details={"before": before, "after": after})
    await db.flush()
    return after


async def apply_original_retention(db, *, tenant_id, actor_id, blob_id, request):
    from app.core.policy import write_audit_event

    ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hive.original-retention:{tenant_id}:{request.operation_id}")
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(ledger_id.bytes[:8], signed=True)})
    blob = await db.scalar(select(StorageBlob).where(StorageBlob.tenant_id == tenant_id, StorageBlob.id == blob_id,
        StorageBlob.kind == "chat_upload").with_for_update().execution_options(populate_existing=True))
    if blob is None:
        raise HTTPException(404, detail={"code": "original_not_found"})
    identity = {**request.model_dump(mode="json"), "tenant_id": str(tenant_id), "blob_id": str(blob_id), "actor_id": str(actor_id)}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == tenant_id, StorageGCRun.id == ledger_id))
    if prior is not None:
        if prior.manifest_sha256 != fingerprint or not isinstance(prior.receipt_json.get("result"), dict):
            raise HTTPException(409, detail={"code": "original_retention_operation_conflict"})
        return {**prior.receipt_json["result"], "replayed": True}
    row = await _policy_row(db, tenant_id, lock=True)
    policy = _project_policy(row.value)
    if policy["version"] != request.expected_policy_version or blob.content_sha256 != request.expected_content_sha256:
        raise HTTPException(409, detail={"code": "original_retention_changed", "recovery_action": "reload_original_and_policy"})
    now = datetime.now(timezone.utc)
    referenced = await db.scalar(select(StorageBlobRef.id).where(StorageBlobRef.tenant_id == tenant_id,
        StorageBlobRef.blob_id == blob_id).limit(1)) is not None
    hold, changed, observation = None, False, None
    evidence = {"provider": blob.provider, "bucket": blob.bucket, "object_key": blob.object_key,
        "content_sha256": blob.content_sha256, "size_bytes": blob.size_bytes, "policy": policy}
    if request.action == "restore":
        active = await db.scalar(select(StorageBlob.id).where(StorageBlob.tenant_id == tenant_id,
            StorageBlob.content_sha256 == blob.content_sha256, StorageBlob.id != blob_id,
            StorageBlob.state.in_(("uploading", "available"))).limit(1))
        if blob.state != "quarantined":
            hold = "unresolved_state"
        elif active is not None:
            hold = "active_generation_exists"
        else:
            try:
                store = _recorded_store(blob, tenant_id)
                observed = await store.stat(BlobLocation(blob.provider, blob.bucket, blob.object_key), expected_size=blob.size_bytes)
                observation = "missing" if not observed.available else (
                    "present" if (observed.sha256, observed.size_bytes) == (blob.content_sha256, blob.size_bytes) else "changed")
                evidence["observed"] = {"available": observed.available, "sha256": observed.sha256, "size_bytes": observed.size_bytes}
            except BlobIntegrityError:
                observation = "changed"
            except (BlobStoreError, OSError):
                observation = "unknown"
            if observation == "present":
                try:
                    async with db.begin_nested():
                        blob.state, blob.verified_at = "available", now
                        blob.quarantined_at = blob.delete_after = None
                        await db.flush()
                except IntegrityError:
                    # A concurrent upload may have committed a new generation
                    # after our read; the unique active index remains decisive.
                    raise HTTPException(409, detail={"code": "original_active_generation_conflict",
                        "recovery_action": "reload_original"}) from None
                changed = True
            else:
                hold = "provider_" + observation
    else:
        hold = original_retention_hold(blob, policy, referenced=referenced, now=now)
        if not hold and request.action == "quarantine" and blob.state == "available":
            try:
                deadline = now + timedelta(days=policy["grace_days"])
            except OverflowError:
                raise ValueError("Original retention grace is out of range") from None
            blob.state, blob.quarantined_at, blob.delete_after = "quarantined", now, deadline
            changed = True
    deadline = original_grace_deadline(blob, policy)
    eligible = not hold and blob.state == "quarantined" and deadline is not None and deadline <= now
    result = {"operation_id": str(request.operation_id), "blob_id": str(blob_id), "state": blob.state,
        "policy_version": policy["version"], "changed": changed, "hold_reason": hold, "observation": observation,
        "delete_after": deadline.isoformat() if deadline else None, "sweep_eligible": bool(eligible),
        "manifest_sha256": original_manifest(blob, policy),
        "observed_at": now.isoformat(), "replayed": False,
        "recovery_action": "reload_active_original" if hold == "active_generation_exists" else
            "repair_provider_and_retry" if hold and hold.startswith("provider_") else "inspect_with_new_operation" if hold else "none"}
    db.add(StorageGCRun(id=ledger_id, tenant_id=tenant_id, policy_version=f"hive.original_retention.v1:{policy['version']}",
        mode={"inspect": "dry_run", "quarantine": "quarantine", "restore": "restore"}[request.action],
        status="completed", manifest_sha256=fingerprint, candidate_count=1, candidate_bytes=blob.size_bytes,
        processed_count=int(changed), processed_bytes=blob.size_bytes if changed else 0,
        failed_count=int(observation in {"changed", "unknown"}), finished_at=now,
        receipt_json={"request": identity, "result": result, "evidence": evidence}))
    await write_audit_event(db, event_type="storage.original_retention_applied", severity="info", actor_type="user",
        actor_id=actor_id, tenant_id=tenant_id, resource_type="storage_blob", resource_id=blob_id, action=request.action,
        details={"operation_id": str(request.operation_id), "manifest_sha256": fingerprint, "state": blob.state,
            "changed": changed, "hold_reason": hold})
    await db.flush()
    return result
