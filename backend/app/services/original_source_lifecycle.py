"""Owner withdrawal stops new source use while preserving retention evidence."""

from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models.storage_blob import StorageBlobRef


class OriginalWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_content_sha256: str = Field(pattern="^[0-9a-f]{64}$")


def _withdrawal(ref):
    metadata = ref.metadata_json
    if not isinstance(metadata, dict):
        raise HTTPException(503, detail={"code": "original_reference_requires_reconciliation", "retryable": True})
    if "withdrawal" not in metadata:
        return None
    value = metadata["withdrawal"]
    if isinstance(value, dict) and value.get("schema") == "hive.original_admin_withdrawal.v1":
        from app.services.original_source_disposition import validate_disposition

        try:
            validate_disposition(ref, value)
            return value
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HTTPException(503, detail={"code": "original_reference_requires_reconciliation", "retryable": True}) from None
    if (not isinstance(value, dict) or value.get("schema") != "hive.original_withdrawal.v1"
        or value.get("actor_user_id") != metadata.get("owner_user_id")
        or not isinstance(value.get("withdrawn_at"), str) or not value["withdrawn_at"]):
        raise HTTPException(503, detail={"code": "original_reference_requires_reconciliation", "retryable": True})
    return value


def _retained_inspection_allowed(ref, user, runtime_principal):
    from app.core.permissions import is_scoped_business_admin
    from app.core.source_read_purpose import business_inspection_allowed

    return (user is not None and runtime_principal is None and business_inspection_allowed()
        and is_scoped_business_admin(user, resource_tenant_id=ref.tenant_id))


def require_original_source_active(ref, *, user=None, runtime_principal=None):
    if _withdrawal(ref) is not None and not _retained_inspection_allowed(ref, user, runtime_principal):
        raise HTTPException(410, detail={"code": "original_source_withdrawn", "retryable": False,
            "recovery_action": "remove_or_reselect_original"})


async def audit_retained_original_inspection(db, *, user, ref, runtime_principal):
    from app.core.policy import write_audit_event

    if _withdrawal(ref) is not None and _retained_inspection_allowed(ref, user, runtime_principal):
        await write_audit_event(db, event_type="storage.original_retained_inspection_authorized", severity="info",
            actor_type="user", actor_id=user.id, tenant_id=ref.tenant_id, resource_type="storage_blob_ref",
            resource_id=ref.id, action="inspect_retained_original",
            details={"source_state": "withdrawn", "authority_source": "scoped_business_inspection"})


async def _reference(db, *, user, ref_id, lock=False):
    statement = select(StorageBlobRef).where(StorageBlobRef.id == ref_id, StorageBlobRef.tenant_id == user.tenant_id,
        StorageBlobRef.owner_type.in_(("chat_upload", "channel_upload")), StorageBlobRef.purpose == "original_attachment")
    ref = await db.scalar((statement.with_for_update() if lock else statement).execution_options(populate_existing=True))
    if ref is None:
        raise HTTPException(404, detail={"code": "original_attachment_not_recorded", "retryable": False})
    return ref


def _owned(ref, user):
    return isinstance(ref.metadata_json, dict) and ref.metadata_json.get("owner_user_id") == str(user.id)


async def original_reference_status(db, *, user, ref_id):
    from app.services.upload_originals import resolve_original_download

    ref = await _reference(db, user=user, ref_id=ref_id)
    own = _owned(ref, user)
    withdrawn = _withdrawal(ref)
    inspection = withdrawn is not None and _retained_inspection_allowed(ref, user, None)
    if not own or inspection:
        # Browser inspection or an explicit read grant can inspect status, but
        # neither enlarges the canonical owner's withdrawal authority.
        await resolve_original_download(db, user, ref_id)
    return {"reference_id": str(ref.id), "state": "withdrawn" if withdrawn else "active",
        "can_withdraw": own and withdrawn is None, "content_sha256": ref.metadata_json["content_sha256"],
        "size_bytes": ref.metadata_json["size_bytes"], "can_inspect_retained": inspection}


async def withdraw_original_source(db, *, user, ref_id, expected_content_sha256):
    from app.core.policy import _lock_audit_chain, write_audit_event

    await _lock_audit_chain(db, user.tenant_id)
    ref = await _reference(db, user=user, ref_id=ref_id, lock=True)
    if not _owned(ref, user):
        raise HTTPException(403, detail={"code": "original_source_owner_required", "retryable": False})
    if ref.metadata_json.get("content_sha256") != expected_content_sha256:
        raise HTTPException(409, detail={"code": "original_source_mismatch", "retryable": False,
            "recovery_action": "reload_original"})
    withdrawal = _withdrawal(ref)
    if withdrawal is None:
        withdrawal = {"schema": "hive.original_withdrawal.v1", "actor_user_id": str(user.id),
            "withdrawn_at": datetime.now(timezone.utc).isoformat()}
        ref.metadata_json = {**ref.metadata_json, "withdrawal": withdrawal}
        await write_audit_event(db, event_type="storage.original_source_withdrawn", severity="info", actor_type="user",
            actor_id=user.id, tenant_id=user.tenant_id, resource_type="storage_blob_ref", resource_id=ref.id,
            action="withdraw_original_source", details={"content_sha256": expected_content_sha256,
                "blob_id": str(ref.blob_id), "source_state": "withdrawn", "physical_bytes_reclaimed": None})
        await db.flush()
    return {"reference_id": str(ref.id), "state": "withdrawn", "withdrawn_at": withdrawal["withdrawn_at"],
        "physical_bytes_reclaimed": None}
