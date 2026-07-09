from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_capability import (
    ExternalCapabilityReview,
    ExternalCapabilitySnapshot,
    ExternalExtensionActivation,
    ExternalExtensionCatalogEntry,
    ExternalExtensionComponent,
    ExternalExtensionHookRegistration,
)
from app.services.external_capabilities.activation_cleanup import deactivate_activation_components
from app.services.external_capabilities.types import NormalizedExternalPluginBundle

_BLOCKING_NOTE_CODES = {
    "component_path_escape",
    "install_time_commands_require_isolated_worker",
    "materialized_path_escape",
    "missing_skill_md",
    "remote_directory_depth_exceeded",
    "remote_fetch_failed",
    "remote_file_count_exceeded",
    "remote_file_decode_failed",
    "remote_file_unreadable",
    "remote_host_not_allowed",
    "remote_manifest_unreadable",
    "remote_source_format_unsupported",
    "remote_total_size_exceeded",
    "skill_guard_blocked",
}


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
    source_hash = bundle.artifact_sha256 or bundle.manifest_sha256 or _stable_digest(manifest)

    row = ExternalCapabilityReview(
        tenant_id=tenant_id,
        source_format=bundle.source_format,
        source_uri=bundle.source_uri,
        source_ref=bundle.resolved_ref or bundle.source_ref,
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
        if review.status != "review_required":
            raise ValueError("external capability review must be review_required before approval")

        superseded_result = await db.execute(
            select(ExternalCapabilitySnapshot).where(
                ExternalCapabilitySnapshot.tenant_id == tenant_id,
                ExternalCapabilitySnapshot.source_format == review.source_format,
                ExternalCapabilitySnapshot.normalized_name == review.normalized_name,
                ExternalCapabilitySnapshot.status == "approved",
            )
        )
        superseded_snapshots = list(superseded_result.scalars().all())
        for old_snapshot in superseded_snapshots:
            old_snapshot.status = "superseded"

        catalog_entries_superseded = 0
        if superseded_snapshots:
            superseded_snapshot_ids = [old_snapshot.id for old_snapshot in superseded_snapshots]
            superseded_catalog_result = await db.execute(
                select(ExternalExtensionCatalogEntry).where(
                    ExternalExtensionCatalogEntry.tenant_id == tenant_id,
                    ExternalExtensionCatalogEntry.snapshot_id.in_(superseded_snapshot_ids),
                    ExternalExtensionCatalogEntry.status == "available",
                )
            )
            superseded_catalog_entries = list(superseded_catalog_result.scalars().all())
            for entry in superseded_catalog_entries:
                entry.status = "superseded"
            catalog_entries_superseded = len(superseded_catalog_entries)

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
        component_records = _component_records_for_snapshot(snapshot)
        for component_record in component_records:
            db.add(component_record)
        if component_records:
            await db.flush()
        hook_registrations = _hook_registrations_for_snapshot(snapshot, component_records=component_records)
        for hook_registration in hook_registrations:
            db.add(hook_registration)
        catalog_entries = _catalog_entries_for_snapshot(snapshot)
        for entry in catalog_entries:
            db.add(entry)
        if catalog_entries or hook_registrations:
            await db.flush()
        await db.commit()
        payload = _snapshot_to_dict(
            snapshot,
            catalog_entries=catalog_entries,
            component_records=component_records,
            hook_registrations=hook_registrations,
        )
        payload["superseded_snapshots"] = [str(old_snapshot.id) for old_snapshot in superseded_snapshots]
        payload["catalog_entries_superseded"] = catalog_entries_superseded
        return payload
    except Exception:
        await db.rollback()
        raise


async def reject_external_capability_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    review_id: uuid.UUID,
    rejected_by_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark a staged review rejected so it cannot later mint an approved snapshot."""
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
        if review.status == "approved":
            raise ValueError("approved external capability review cannot be rejected")

        normalized_reason = str(reason or "").strip()
        admission_report = dict(review.admission_report_json or {})
        admission_report["rejection"] = {
            "reason": normalized_reason,
            "rejected_by_user_id": str(rejected_by_user_id) if rejected_by_user_id else None,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
        }
        review.admission_report_json = admission_report
        review.status = "rejected"
        await db.flush()
        await db.commit()
        return {
            "review_id": str(review_id),
            "status": "rejected",
            "reason": normalized_reason,
        }
    except Exception:
        await db.rollback()
        raise


async def revoke_external_capability_snapshot(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    revoked_by_user_id: uuid.UUID | None,
    agent_data_root: Path | None = None,
) -> dict[str, Any]:
    """Revoke an approved snapshot and hide its catalog entries from future activation."""
    try:
        result = await db.execute(
            select(ExternalCapabilitySnapshot).where(
                ExternalCapabilitySnapshot.id == snapshot_id,
                ExternalCapabilitySnapshot.tenant_id == tenant_id,
            )
        )
        snapshot = result.scalar_one_or_none()
        if snapshot is None:
            raise ValueError("external capability snapshot not found")
        if snapshot.status == "revoked":
            catalog_entries_revoked = 0
        else:
            snapshot.status = "revoked"
            snapshot.revoked_by_user_id = revoked_by_user_id
            snapshot.revoked_at = datetime.now(timezone.utc)
            catalog_result = await db.execute(
                select(ExternalExtensionCatalogEntry).where(
                    ExternalExtensionCatalogEntry.tenant_id == tenant_id,
                    ExternalExtensionCatalogEntry.snapshot_id == snapshot_id,
                )
            )
            catalog_entries = list(catalog_result.scalars().all())
            for entry in catalog_entries:
                entry.status = "revoked"
            catalog_entries_revoked = len(catalog_entries)
        active_activation_result = await db.execute(
            select(ExternalExtensionActivation).where(
                ExternalExtensionActivation.tenant_id == tenant_id,
                ExternalExtensionActivation.snapshot_id == snapshot_id,
                ExternalExtensionActivation.status == "active",
            )
        )
        active_activations = list(active_activation_result.scalars().all())
        for activation in active_activations:
            activation_payload = dict(activation.activation_result_json or {})
            workspace = agent_data_root / str(activation.agent_id) if agent_data_root is not None else None
            revoked_components = (
                deactivate_activation_components(activation_payload.get("components"), workspace=workspace)
                if workspace is not None
                else _db_only_revoked_components(activation_payload.get("components"))
            )
            activation.status = "revoked"
            activation.activation_result_json = {
                **activation_payload,
                "revocation": {
                    "components": revoked_components,
                    "revoked_by_user_id": str(revoked_by_user_id) if revoked_by_user_id else None,
                    "revoked_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        await db.flush()
        await db.commit()
        return {
            "snapshot_id": str(snapshot_id),
            "status": "revoked",
            "catalog_entries_revoked": catalog_entries_revoked,
            "activations_revoked": len(active_activations),
        }
    except Exception:
        await db.rollback()
        raise


def _db_only_revoked_components(components: Any) -> list[dict[str, Any]]:
    if not isinstance(components, list):
        return []
    revoked: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        revoked.append(
            {
                "component_type": str(component.get("component_type") or "unknown"),
                "name": str(component.get("name") or component.get("qualified_name") or "unknown"),
                "status": "db_revoked_artifact_cleanup_not_attempted",
            }
        )
    return revoked


async def list_external_capability_reviews(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalCapabilityReview)
        .where(ExternalCapabilityReview.tenant_id == tenant_id)
        .order_by(ExternalCapabilityReview.created_at.desc())
    )
    return [_review_to_dict(row) for row in result.scalars().all()]


async def list_external_extension_catalog_entries(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalExtensionCatalogEntry)
        .where(
            ExternalExtensionCatalogEntry.tenant_id == tenant_id,
            ExternalExtensionCatalogEntry.status == "available",
        )
        .order_by(ExternalExtensionCatalogEntry.component_type, ExternalExtensionCatalogEntry.component_name)
    )
    return [_catalog_entry_to_dict(row) for row in result.scalars().all()]


def _bundle_manifest(bundle: NormalizedExternalPluginBundle) -> dict[str, Any]:
    return {
        "source_format": bundle.source_format,
        "source_uri": bundle.source_uri,
        "plugin_name": bundle.plugin_name,
        "version": bundle.version,
        "description": bundle.description,
        "source_ref": bundle.source_ref,
        "resolved_ref": bundle.resolved_ref,
        "artifact_sha256": bundle.artifact_sha256,
        "manifest_sha256": bundle.manifest_sha256,
        "materialization": dict(bundle.materialization_report or {}),
        "sandbox": dict(bundle.sandbox_report or {}),
        "lockfile": dict(bundle.lockfile or {}),
        "components": [asdict(component) for component in bundle.components],
        "unsupported_components": list(bundle.unsupported_components),
        "credential_requirements": list(bundle.credential_requirements),
        "admission_notes": list(bundle.admission_notes),
        "manifest_metadata": dict(bundle.manifest_metadata or {}),
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
        "materialization": dict(bundle.materialization_report or {}),
        "sandbox": dict(bundle.sandbox_report or {}),
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


def _catalog_entries_for_snapshot(snapshot: ExternalCapabilitySnapshot) -> list[ExternalExtensionCatalogEntry]:
    manifest = snapshot.component_manifest_json or {}
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, list):
        return []

    entries: list[ExternalExtensionCatalogEntry] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("component_type") or "").strip()
        qualified_name = str(component.get("qualified_name") or "").strip()
        if not component_type or not qualified_name:
            continue
        local_name = str(component.get("local_name") or "").strip()
        component_name = local_name or qualified_name.rsplit(":", maxsplit=1)[-1]
        entries.append(
            ExternalExtensionCatalogEntry(
                tenant_id=snapshot.tenant_id,
                snapshot_id=snapshot.id,
                component_type=component_type,
                component_name=component_name,
                qualified_name=qualified_name,
                policy="optional",
                status="available",
                source_format=snapshot.source_format,
                source_uri=snapshot.source_uri,
                metadata_json={
                    "runtime_projection": component.get("runtime_projection") or {},
                    "metadata": component.get("metadata") or {},
                    "ignored_fields": component.get("ignored_fields") or [],
                },
            )
        )
    return entries


def _component_records_for_snapshot(snapshot: ExternalCapabilitySnapshot) -> list[ExternalExtensionComponent]:
    manifest = snapshot.component_manifest_json or {}
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, list):
        return []

    records: list[ExternalExtensionComponent] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("component_type") or "").strip()
        qualified_name = str(component.get("qualified_name") or "").strip()
        if not component_type or not qualified_name:
            continue
        local_name = str(component.get("local_name") or "").strip()
        component_name = local_name or qualified_name.rsplit(":", maxsplit=1)[-1]
        records.append(
            ExternalExtensionComponent(
                tenant_id=snapshot.tenant_id,
                snapshot_id=snapshot.id,
                component_type=component_type,
                component_name=component_name,
                qualified_name=qualified_name,
                source_path=_optional_string(component.get("source_path")),
                content_sha256=_optional_string(component.get("content_sha256")),
                status="approved",
                runtime_projection_json=_dict_or_empty(component.get("runtime_projection")),
                metadata_json={
                    "metadata": _dict_or_empty(component.get("metadata")),
                    "ignored_fields": list(component.get("ignored_fields") or []),
                },
            )
        )
    return records


def _hook_registrations_for_snapshot(
    snapshot: ExternalCapabilitySnapshot,
    *,
    component_records: list[ExternalExtensionComponent],
) -> list[ExternalExtensionHookRegistration]:
    by_qualified_name = {record.qualified_name: record for record in component_records}
    registrations: list[ExternalExtensionHookRegistration] = []
    for record in component_records:
        if record.component_type != "hook":
            continue
        runtime_projection = _dict_or_empty(record.runtime_projection_json)
        event = _optional_string(runtime_projection.get("event")) or "unknown"
        handler = _optional_string(runtime_projection.get("handler")) or record.component_name
        matcher = runtime_projection.get("matcher")
        registrations.append(
            ExternalExtensionHookRegistration(
                tenant_id=snapshot.tenant_id,
                snapshot_id=snapshot.id,
                component_id=getattr(by_qualified_name.get(record.qualified_name), "id", None),
                qualified_name=record.qualified_name,
                event=event,
                handler=handler,
                mode=_optional_string(runtime_projection.get("mode")) or "observe",
                matcher_json=matcher if isinstance(matcher, dict) else {},
                approval_required=True,
                status="pending_approval",
                approval_json={
                    "reason": "external_hook_requires_hive_runtime_binding_approval",
                    "runtime_activation": "fail_closed",
                },
            )
        )
    return registrations


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


def _catalog_entry_to_dict(row: ExternalExtensionCatalogEntry) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "snapshot_id": str(row.snapshot_id),
        "component_type": row.component_type,
        "component_name": row.component_name,
        "qualified_name": row.qualified_name,
        "policy": row.policy,
        "status": row.status,
        "source_format": row.source_format,
        "source_uri": row.source_uri,
        "metadata": row.metadata_json or {},
    }


def _component_record_to_dict(row: ExternalExtensionComponent) -> dict[str, Any]:
    payload = {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "snapshot_id": str(row.snapshot_id),
        "component_type": row.component_type,
        "component_name": row.component_name,
        "qualified_name": row.qualified_name,
        "source_path": row.source_path,
        "content_sha256": row.content_sha256,
        "status": row.status,
        "runtime_projection": row.runtime_projection_json or {},
        "metadata": row.metadata_json or {},
    }
    if row.component_type == "hook":
        # Explicit installed-list state: hooks are catalogued but their runtime
        # execution chain is not wired yet (aligns with A2A `not_exposed`).
        payload["runtime_execution"] = "not_yet_supported"
    return payload


def _hook_registration_to_dict(row: ExternalExtensionHookRegistration) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "snapshot_id": str(row.snapshot_id),
        "component_id": str(row.component_id) if row.component_id else None,
        "qualified_name": row.qualified_name,
        "event": row.event,
        "handler": row.handler,
        "mode": row.mode,
        "matcher": row.matcher_json or {},
        "approval_required": row.approval_required,
        "status": row.status,
        "approval": row.approval_json or {},
    }


def _snapshot_to_dict(
    row: ExternalCapabilitySnapshot,
    *,
    catalog_entries: list[ExternalExtensionCatalogEntry] | None = None,
    component_records: list[ExternalExtensionComponent] | None = None,
    hook_registrations: list[ExternalExtensionHookRegistration] | None = None,
) -> dict[str, Any]:
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
        "catalog_entries": [_catalog_entry_to_dict(entry) for entry in catalog_entries or []],
        "components": [_component_record_to_dict(component) for component in component_records or []],
        "hook_registrations": [_hook_registration_to_dict(hook) for hook in hook_registrations or []],
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
