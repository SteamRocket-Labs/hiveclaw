from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_capability import ExternalCapabilityReview, ExternalCapabilitySnapshot
from app.services.external_capabilities.types import NormalizedExternalPluginBundle

_BLOCKING_NOTE_CODES = {"component_path_escape"}


async def stage_external_capability_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    bundle: NormalizedExternalPluginBundle,
) -> dict[str, Any]:
    """Persist a Trust Gate review record without activating runtime capabilities."""
    manifest = _bundle_manifest(bundle)
    admission_report = _build_admission_report(bundle)
    admission_class = admission_report["admission_class"]
    governance_projection = _build_governance_projection(bundle, admission_class=admission_class)
    status = "blocked" if admission_class == "blocked" else "review_required"
    source_hash = bundle.manifest_sha256 or _stable_digest(manifest)

    row = ExternalCapabilityReview(
        tenant_id=tenant_id,
        source_format=bundle.source_format,
        source_uri=bundle.source_uri,
        source_hash=source_hash,
        normalized_name=bundle.plugin_name,
        status=status,
        admission_class=admission_class,
        admission_report_json=admission_report,
        governance_projection_json=governance_projection,
        normalized_manifest_json=manifest,
        created_by_user_id=created_by_user_id,
    )
    try:
        db.add(row)
        await db.flush()
        await db.commit()
        return _review_to_dict(row)
    except Exception:
        await db.rollback()
        raise


async def approve_external_capability_snapshot(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    review_id: uuid.UUID,
    approved_by_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Create an approved immutable snapshot from a staged review."""
    try:
        result = await db.execute(
            select(ExternalCapabilityReview).where(
                ExternalCapabilityReview.id == review_id,
                ExternalCapabilityReview.tenant_id == tenant_id,
            )
        )
        review = result.scalar_one_or_none()
        if review is None:
            raise ValueError("external capability review not found")
        if review.admission_class == "blocked" or review.status == "blocked":
            raise ValueError("blocked external capability review cannot be approved")

        snapshot = ExternalCapabilitySnapshot(
            tenant_id=tenant_id,
            review_id=review.id,
            snapshot_key=_snapshot_key(review),
            source_format=review.source_format,
            source_uri=review.source_uri,
            source_ref=getattr(review, "source_ref", None),
            source_hash=review.source_hash,
            normalized_name=review.normalized_name,
            status="approved",
            admission_class=review.admission_class,
            admission_report_json=review.admission_report_json or {},
            governance_projection_json=review.governance_projection_json or {},
            component_manifest_json=review.normalized_manifest_json or {},
            approved_by_user_id=approved_by_user_id,
        )
        review.status = "approved"
        db.add(snapshot)
        await db.flush()
        await db.commit()
        return _snapshot_to_dict(snapshot)
    except Exception:
        await db.rollback()
        raise


async def list_external_capability_reviews(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalCapabilityReview)
        .where(ExternalCapabilityReview.tenant_id == tenant_id)
        .order_by(ExternalCapabilityReview.created_at.desc())
    )
    return [_review_to_dict(row) for row in result.scalars().all()]


def _bundle_manifest(bundle: NormalizedExternalPluginBundle) -> dict[str, Any]:
    return {
        "source_format": bundle.source_format,
        "source_uri": bundle.source_uri,
        "plugin_name": bundle.plugin_name,
        "version": bundle.version,
        "description": bundle.description,
        "manifest_sha256": bundle.manifest_sha256,
        "components": [asdict(component) for component in bundle.components],
        "unsupported_components": list(bundle.unsupported_components),
        "credential_requirements": list(bundle.credential_requirements),
        "admission_notes": list(bundle.admission_notes),
    }


def _build_admission_report(bundle: NormalizedExternalPluginBundle) -> dict[str, Any]:
    notes = list(bundle.admission_notes)
    note_codes = {str(note.get("code")) for note in notes if isinstance(note, dict)}
    component_counts = Counter(component.component_type for component in bundle.components)
    if note_codes & _BLOCKING_NOTE_CODES:
        admission_class = "blocked"
    elif not bundle.components:
        admission_class = "metadata_only"
    elif component_counts.get("hook") or component_counts.get("mcp_server"):
        admission_class = "admin_scoped"
    else:
        admission_class = "governed_runtime"
    return {
        "admission_class": admission_class,
        "component_counts": dict(component_counts),
        "notes": notes,
        "unsupported_components": list(bundle.unsupported_components),
        "credential_requirements": list(bundle.credential_requirements),
    }


def _build_governance_projection(
    bundle: NormalizedExternalPluginBundle,
    *,
    admission_class: str,
) -> dict[str, Any]:
    component_counts = Counter(component.component_type for component in bundle.components)
    return {
        "activation_boundary": "approved_snapshot_required",
        "runtime_governance": "existing_governance_after_activation",
        "requires_admin_approval": admission_class in {"admin_scoped", "blocked"},
        "components_by_type": dict(component_counts),
        "credential_requirements": list(bundle.credential_requirements),
    }


def _snapshot_key(review: ExternalCapabilityReview) -> str:
    digest = _stable_digest(
        {
            "review_id": str(review.id),
            "source_hash": review.source_hash,
            "manifest": review.normalized_manifest_json or {},
        }
    )[:24]
    return f"{review.source_format}:{review.normalized_name}:{digest}"


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_to_dict(row: ExternalCapabilityReview) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "source_format": row.source_format,
        "source_uri": row.source_uri,
        "source_hash": row.source_hash,
        "normalized_name": row.normalized_name,
        "status": row.status,
        "admission_class": row.admission_class,
        "admission_report": row.admission_report_json or {},
        "governance_projection": row.governance_projection_json or {},
        "normalized_manifest": row.normalized_manifest_json or {},
    }


def _snapshot_to_dict(row: ExternalCapabilitySnapshot) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "review_id": str(row.review_id),
        "snapshot_key": row.snapshot_key,
        "source_format": row.source_format,
        "source_uri": row.source_uri,
        "source_hash": row.source_hash,
        "normalized_name": row.normalized_name,
        "status": row.status,
        "admission_class": row.admission_class,
        "admission_report": row.admission_report_json or {},
        "governance_projection": row.governance_projection_json or {},
        "component_manifest": row.component_manifest_json or {},
    }

