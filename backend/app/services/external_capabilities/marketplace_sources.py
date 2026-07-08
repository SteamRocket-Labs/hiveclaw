from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_capability import ExternalMarketplaceEntry, ExternalMarketplaceSource
from app.services.external_capabilities.trust_gate import stage_external_capability_review
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle


async def list_marketplace_sources(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalMarketplaceSource)
        .where(ExternalMarketplaceSource.tenant_id == tenant_id)
        .order_by(ExternalMarketplaceSource.created_at.desc())
    )
    return [_source_to_dict(row) for row in result.scalars().all()]


async def create_marketplace_source(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    row = ExternalMarketplaceSource(
        tenant_id=tenant_id,
        name=_required_string(data.get("name"), field_name="name"),
        source_type=_optional_string(data.get("source_type")) or "manual",
        source_uri=_required_string(data.get("source_uri"), field_name="source_uri"),
        status=_optional_string(data.get("status")) or "enabled",
        config_json=_dict_or_empty(data.get("config")),
        created_by_user_id=created_by_user_id,
    )
    try:
        db.add(row)
        await db.flush()
        await db.commit()
        return _source_to_dict(row)
    except Exception:
        await db.rollback()
        raise


async def sync_marketplace_source(db: AsyncSession, *, tenant_id: uuid.UUID, source_id: uuid.UUID) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalMarketplaceSource).where(
            ExternalMarketplaceSource.id == source_id,
            ExternalMarketplaceSource.tenant_id == tenant_id,
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise ValueError("marketplace source not found")
    if source.status != "enabled":
        raise ValueError("marketplace source is not enabled")

    try:
        entries = _manual_entries_from_source(source)
        existing_result = await db.execute(
            select(ExternalMarketplaceEntry).where(
                ExternalMarketplaceEntry.tenant_id == tenant_id,
                ExternalMarketplaceEntry.source_id == source_id,
            )
        )
        existing_by_key = {row.external_key: row for row in existing_result.scalars().all()}
        created = 0
        updated = 0
        now = datetime.now(timezone.utc)
        for payload in entries:
            external_key = _required_string(payload.get("external_key"), field_name="external_key")
            row = existing_by_key.get(external_key)
            if row is None:
                row = ExternalMarketplaceEntry(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    external_key=external_key,
                    display_name=_required_string(payload.get("display_name"), field_name="display_name"),
                    description=_optional_string(payload.get("description")),
                    source_format=_optional_string(payload.get("source_format")) or "cc_plugin",
                    source_uri=_required_string(payload.get("source_uri"), field_name="source_uri"),
                    source_ref=_optional_string(payload.get("source_ref")),
                    status="available",
                    manifest_json=_dict_or_empty(payload.get("manifest")),
                    compatibility_json=_dict_or_empty(payload.get("compatibility")),
                    last_seen_at=now,
                )
                db.add(row)
                created += 1
            else:
                row.display_name = _required_string(payload.get("display_name"), field_name="display_name")
                row.description = _optional_string(payload.get("description"))
                row.source_format = _optional_string(payload.get("source_format")) or row.source_format
                row.source_uri = _required_string(payload.get("source_uri"), field_name="source_uri")
                row.source_ref = _optional_string(payload.get("source_ref"))
                row.status = "available" if row.status == "missing" else row.status
                row.manifest_json = _dict_or_empty(payload.get("manifest"))
                row.compatibility_json = _dict_or_empty(payload.get("compatibility"))
                row.last_seen_at = now
                updated += 1
        source.sync_status = "synced"
        source.last_sync_error = None
        source.last_sync_at = now
        await db.flush()
        await db.commit()
        return {
            "source_id": str(source_id),
            "sync_status": source.sync_status,
            "entries_seen": len(entries),
            "entries_created": created,
            "entries_updated": updated,
        }
    except Exception as exc:
        source.sync_status = "failed"
        source.last_sync_error = str(exc)
        await db.rollback()
        raise


async def list_marketplace_entries(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(ExternalMarketplaceEntry).where(ExternalMarketplaceEntry.tenant_id == tenant_id)
    if source_id is not None:
        stmt = stmt.where(ExternalMarketplaceEntry.source_id == source_id)
    if status is not None:
        stmt = stmt.where(ExternalMarketplaceEntry.status == status)
    result = await db.execute(stmt.order_by(ExternalMarketplaceEntry.updated_at.desc()))
    return [_entry_to_dict(row) for row in result.scalars().all()]


async def submit_marketplace_entry_for_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    submitted_by_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalMarketplaceEntry).where(
            ExternalMarketplaceEntry.id == entry_id,
            ExternalMarketplaceEntry.tenant_id == tenant_id,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ValueError("marketplace entry not found")
    bundle = _bundle_from_entry(entry)
    review = await stage_external_capability_review(
        db,
        tenant_id=tenant_id,
        created_by_user_id=submitted_by_user_id,
        bundle=bundle,
    )
    entry.status = "review_required"
    review_id = _uuid_or_none(review.get("id"))
    if review_id is not None:
        entry.review_id = review_id
    await db.flush()
    await db.commit()
    return {"entry": _entry_to_dict(entry), "review": review}


def _manual_entries_from_source(source: ExternalMarketplaceSource) -> list[dict[str, Any]]:
    if source.source_type != "manual":
        raise ValueError("marketplace source type is not supported by this sync worker")
    config = _dict_or_empty(source.config_json)
    entries = config.get("entries")
    if not isinstance(entries, list):
        return []
    return [dict(item) for item in entries if isinstance(item, dict)]


def _bundle_from_entry(entry: ExternalMarketplaceEntry) -> NormalizedExternalPluginBundle:
    manifest = _dict_or_empty(entry.manifest_json)
    components = [
        ExternalCapabilityComponent(
            component_type=str(component.get("component_type") or "skill"),  # type: ignore[arg-type]
            local_name=_required_string(component.get("local_name"), field_name="local_name"),
            qualified_name=_required_string(component.get("qualified_name"), field_name="qualified_name"),
            source_path=_optional_string(component.get("source_path")) or "",
            content_sha256=_optional_string(component.get("content_sha256")) or "",
            runtime_projection=_dict_or_empty(component.get("runtime_projection")),
            metadata=_dict_or_empty(component.get("metadata")),
            ignored_fields=tuple(component.get("ignored_fields") or ()),
        )
        for component in manifest.get("components", [])
        if isinstance(component, dict)
    ]
    return NormalizedExternalPluginBundle(
        source_format=_optional_string(manifest.get("source_format")) or entry.source_format,
        source_uri=_optional_string(manifest.get("source_uri")) or entry.source_uri,
        plugin_name=_optional_string(manifest.get("plugin_name")) or entry.display_name,
        version=_optional_string(manifest.get("version")),
        description=_optional_string(manifest.get("description")) or entry.description,
        source_ref=entry.source_ref,
        manifest_sha256=_optional_string(manifest.get("manifest_sha256")),
        components=components,
        unsupported_components=list(manifest.get("unsupported_components") or []),
        credential_requirements=list(manifest.get("credential_requirements") or []),
        admission_notes=list(manifest.get("admission_notes") or []),
    )


def _source_to_dict(row: ExternalMarketplaceSource) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "name": row.name,
        "source_type": row.source_type,
        "source_uri": row.source_uri,
        "status": row.status,
        "sync_status": row.sync_status,
        "last_sync_error": row.last_sync_error,
        "config": row.config_json or {},
        "last_sync_at": _isoformat_or_none(row.last_sync_at),
    }


def _entry_to_dict(row: ExternalMarketplaceEntry) -> dict[str, Any]:
    source_id = getattr(row, "source_id", None)
    snapshot_id = getattr(row, "snapshot_id", None)
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "source_id": str(source_id) if source_id else None,
        "external_key": getattr(row, "external_key", row.display_name),
        "display_name": row.display_name,
        "description": row.description,
        "source_format": row.source_format,
        "source_uri": row.source_uri,
        "source_ref": row.source_ref,
        "status": row.status,
        "manifest": row.manifest_json or {},
        "compatibility": getattr(row, "compatibility_json", None) or {},
        "review_id": str(row.review_id) if row.review_id else None,
        "snapshot_id": str(snapshot_id) if snapshot_id else None,
        "last_seen_at": _isoformat_or_none(getattr(row, "last_seen_at", None)),
    }


def _required_string(value: Any, *, field_name: str) -> str:
    text = _optional_string(value)
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _isoformat_or_none(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
