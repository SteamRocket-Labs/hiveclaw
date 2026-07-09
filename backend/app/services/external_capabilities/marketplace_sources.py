from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
from typing import Any
import uuid
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_capability import ExternalMarketplaceEntry, ExternalMarketplaceSource
from app.services.external_capabilities.materializer import DEFAULT_ALLOWED_REMOTE_HOSTS
from app.services.external_capabilities.trust_gate import stage_external_capability_review
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle

MarketplaceFetchText = Callable[[str], Awaitable[str]]
REMOTE_MARKETPLACE_SOURCE_TYPES = {"github", "cc_marketplace", "codex_marketplace"}


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


async def sync_marketplace_source(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    fetch_text: MarketplaceFetchText | None = None,
) -> dict[str, Any]:
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
        entries = await _entries_from_source(source, fetch_text=fetch_text)
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


async def _entries_from_source(
    source: ExternalMarketplaceSource,
    *,
    fetch_text: MarketplaceFetchText | None,
) -> list[dict[str, Any]]:
    if source.source_type == "manual":
        return _manual_entries_from_source(source)
    if source.source_type in REMOTE_MARKETPLACE_SOURCE_TYPES:
        manifest_uri = _remote_manifest_uri(source)
        manifest_text = await (fetch_text or _default_fetch_text)(manifest_uri)
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"marketplace manifest is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("marketplace manifest must be a JSON object")
        return _remote_entries_from_manifest(source, manifest, manifest_uri=manifest_uri)
    raise ValueError("marketplace source type is not supported by this sync worker")


def _manual_entries_from_source(source: ExternalMarketplaceSource) -> list[dict[str, Any]]:
    config = _dict_or_empty(source.config_json)
    entries = config.get("entries")
    if not isinstance(entries, list):
        return []
    return [dict(item) for item in entries if isinstance(item, dict)]


def _remote_manifest_uri(source: ExternalMarketplaceSource) -> str:
    config = _dict_or_empty(source.config_json)
    explicit = _optional_string(config.get("manifest_uri"))
    if explicit:
        _assert_remote_marketplace_host_allowed(explicit)
        return explicit
    manifest_path = (_optional_string(config.get("manifest_path")) or "marketplace.json").lstrip("/")
    parsed = urlparse(source.source_uri)
    host = parsed.netloc.lower()
    _assert_remote_marketplace_host_allowed(source.source_uri)
    if host == "raw.githubusercontent.com":
        return source.source_uri
    if host == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("github marketplace source must include owner and repo")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        branch = "main"
        base_path = ""
        if len(parts) >= 4 and parts[2] == "tree":
            branch = parts[3]
            base_path = "/".join(parts[4:])
        path = "/".join(part for part in [base_path.strip("/"), manifest_path] if part)
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    return source.source_uri


def _remote_entries_from_manifest(
    source: ExternalMarketplaceSource,
    manifest: dict[str, Any],
    *,
    manifest_uri: str,
) -> list[dict[str, Any]]:
    raw_entries = manifest.get("entries")
    if isinstance(raw_entries, list):
        return [_normalize_remote_entry_payload(source, item, manifest_uri=manifest_uri) for item in raw_entries if isinstance(item, dict)]
    raw_plugins = manifest.get("plugins") or manifest.get("items") or manifest.get("extensions") or manifest.get("skills")
    if not isinstance(raw_plugins, list):
        return []
    return [_normalize_remote_plugin_payload(source, item, manifest_uri=manifest_uri) for item in raw_plugins if isinstance(item, dict)]


def _normalize_remote_entry_payload(
    source: ExternalMarketplaceSource,
    payload: dict[str, Any],
    *,
    manifest_uri: str,
) -> dict[str, Any]:
    entry = dict(payload)
    compatibility = _dict_or_empty(entry.get("compatibility"))
    compatibility.setdefault("marketplace_source_type", source.source_type)
    compatibility.setdefault("manifest_uri", manifest_uri)
    entry["compatibility"] = compatibility
    entry.setdefault("source_format", _default_source_format(source))
    entry.setdefault("manifest", _dict_or_empty(payload.get("manifest")))
    return entry


def _normalize_remote_plugin_payload(
    source: ExternalMarketplaceSource,
    payload: dict[str, Any],
    *,
    manifest_uri: str,
) -> dict[str, Any]:
    name = _optional_string(payload.get("name")) or _optional_string(payload.get("id")) or _required_string(
        payload.get("source_uri") or payload.get("repo") or payload.get("url"),
        field_name="plugin name or source_uri",
    )
    source_uri = _optional_string(payload.get("source_uri")) or _optional_string(payload.get("repo")) or _optional_string(
        payload.get("url")
    )
    if not source_uri:
        source_uri = f"{source.source_uri.rstrip('#')}#{name}"
    source_format = _optional_string(payload.get("source_format")) or _default_source_format(source)
    manifest = _dict_or_empty(payload.get("manifest"))
    if not manifest:
        manifest = {
            "plugin_name": name,
            "version": _optional_string(payload.get("version")),
            "description": _optional_string(payload.get("description") or payload.get("summary")),
            "source_format": source_format,
            "source_uri": source_uri,
            "components": list(payload.get("components") or []),
            "unsupported_components": list(payload.get("unsupported_components") or []),
            "credential_requirements": list(payload.get("credential_requirements") or []),
            "admission_notes": list(payload.get("admission_notes") or []),
        }
    return {
        "external_key": _optional_string(payload.get("external_key")) or name,
        "display_name": _optional_string(payload.get("display_name") or payload.get("title")) or name,
        "description": _optional_string(payload.get("description") or payload.get("summary")),
        "source_format": source_format,
        "source_uri": source_uri,
        "source_ref": _optional_string(payload.get("source_ref") or payload.get("ref") or payload.get("version")),
        "manifest": manifest,
        "compatibility": {
            **_dict_or_empty(payload.get("compatibility")),
            "marketplace_source_type": source.source_type,
            "manifest_uri": manifest_uri,
        },
    }


def _default_source_format(source: ExternalMarketplaceSource) -> str:
    if source.source_type == "codex_marketplace":
        return "codex_plugin"
    return "cc_plugin"


def _assert_remote_marketplace_host_allowed(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in DEFAULT_ALLOWED_REMOTE_HOSTS:
        raise ValueError("marketplace source host is not allowed")


async def _default_fetch_text(uri: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(uri)
        response.raise_for_status()
        return response.text


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
