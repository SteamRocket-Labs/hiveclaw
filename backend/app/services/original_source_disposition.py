"""Current inactive source custody for explicit administrator withdrawal."""

from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.services.blob_store import BlobStoreError

SCHEMA = "hive.original_admin_withdrawal.v1"


def validate_disposition(ref, value):
    metadata = ref.metadata_json
    kind = "user" if metadata.get("owner_user_id") else "external_principal"
    owner_id = metadata.get("owner_user_id") or metadata.get("external_principal_id")
    if (value.get("schema") != SCHEMA or value.get("owner_kind") != kind or value.get("owner_id") != owner_id
        or value.get("authority") != "scoped_admin_inactive_source"
        or kind == "external_principal" and ref.owner_type != "channel_upload"
        or datetime.fromisoformat(value["withdrawn_at"]).tzinfo is None):
        raise ValueError("invalid_source_disposition")
    for key in ("owner_id", "actor_user_id", "operation_id"):
        uuid.UUID(value[key])


async def disposition_eligibility(db, *, blob, ref, lock=False):
    from app.models.external_principal import ExternalPrincipal
    from app.models.user import User
    from app.services.channel_originals import SCHEMA as CHANNEL_SCHEMA, channel_original_source
    from app.services.original_source_lifecycle import _withdrawal
    from app.services.upload_originals import REF_SCHEMA

    reason, custody = None, None
    try:
        metadata = ref.metadata_json
        if (ref.purpose != "original_attachment" or blob.state not in {"available", "quarantined"}
            or ref.owner_type not in {"chat_upload", "channel_upload"}
            or metadata["schema"] != (REF_SCHEMA if ref.owner_type == "chat_upload" else CHANNEL_SCHEMA)
            or metadata["content_sha256"] != blob.content_sha256 or type(metadata["size_bytes"]) is not int
            or metadata["size_bytes"] != blob.size_bytes):
            raise ValueError("unverified_original")
        if _withdrawal(ref) is not None:
            return "source_already_withdrawn", None
        if ref.owner_type == "channel_upload":
            await channel_original_source(db, tenant_id=blob.tenant_id, ref=ref)
        elif ref.owner_id != str(ref.id):
            raise ValueError("unverified_original")
        owner_id = metadata.get("owner_user_id")
        model = User if owner_id else ExternalPrincipal
        if not owner_id:
            if ref.owner_type != "channel_upload":
                raise ValueError("unverified_owner")
            owner_id = metadata["external_principal_id"]
        statement = select(model).where(model.tenant_id == blob.tenant_id, model.id == uuid.UUID(owner_id))
        if lock:
            statement = statement.with_for_update(read=True, nowait=True)
        owner = await db.scalar(statement.execution_options(populate_existing=True))
        if owner is None:
            raise ValueError("unverified_owner")
        if model is User:
            custody = {"kind": "user", "id": str(owner.id), "tenant_id": str(owner.tenant_id), "is_active": owner.is_active}
            if owner.is_active:
                reason = "source_owner_active"
        else:
            if metadata["source"] != f"{owner.provider}_upload":
                raise ValueError("unverified_provider")
            custody = {"kind": "external_principal", "id": str(owner.id), "tenant_id": str(owner.tenant_id),
                "status": owner.status, "linked_user_id": str(owner.linked_user_id) if owner.linked_user_id else None}
            if owner.status != "revoked":
                reason = "source_owner_active"
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise HTTPException(503, detail={"code": "original_source_custody_unavailable", "retryable": True}) from exc
    except (KeyError, ValueError, TypeError, AttributeError, HTTPException, BlobStoreError):
        reason = "source_custody_reconciliation_required"
    return reason, custody


def disposition_marker(*, custody, actor_id, operation_id):
    return {"schema": SCHEMA, "owner_kind": custody["kind"], "owner_id": custody["id"],
        "actor_user_id": str(actor_id), "operation_id": str(operation_id),
        "authority": "scoped_admin_inactive_source", "withdrawn_at": datetime.now(timezone.utc).isoformat()}
