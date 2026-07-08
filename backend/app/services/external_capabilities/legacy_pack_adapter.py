from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.installed_plugin import AgentPluginAssignment, TenantInstalledPlugin
from app.services.external_capabilities.types import NormalizedExternalPluginBundle


def normalize_legacy_installed_plugin(plugin: TenantInstalledPlugin) -> NormalizedExternalPluginBundle:
    """Represent one legacy pack row as a migration-only normalized bundle."""
    plugin_key = _text(getattr(plugin, "plugin_key", None)) or "unknown"
    version = _text(getattr(plugin, "version", None)) or "0.0.0"
    lockfile = _dict_or_empty(getattr(plugin, "lockfile_json", None))
    source_kind = _text(getattr(plugin, "source_kind", None)) or "unknown"
    status = _text(getattr(plugin, "status", None)) or "unknown"
    content_sha256 = _text(lockfile.get("content_sha256") or lockfile.get("manifest_sha256"))
    return NormalizedExternalPluginBundle(
        source_format="legacy_pack",
        source_uri=f"legacy-pack:{plugin_key}",
        plugin_name=plugin_key,
        version=version,
        description="Legacy pack compatibility projection. Not a new external capability entrypoint.",
        source_ref=_text(getattr(plugin, "source_ref", None)),
        manifest_sha256=content_sha256,
        lockfile=lockfile,
        components=[],
        unsupported_components=[
            {
                "component_type": "legacy_pack_projection",
                "reason": "migration_only_projection",
                "plugin_key": plugin_key,
                "source_kind": source_kind,
                "status": status,
            }
        ],
        admission_notes=[
            {
                "code": "legacy_pack_migration_only",
                "migration_only": True,
                "new_entrypoint": False,
                "source_kind": source_kind,
                "status": status,
            }
        ],
    )


def build_legacy_pack_migration_report(
    plugins: list[TenantInstalledPlugin],
    assignments: list[AgentPluginAssignment],
) -> dict[str, Any]:
    """Build a dry-run projection report without mutating runtime or Trust Gate rows."""
    plugin_by_id = {getattr(plugin, "id", None): plugin for plugin in plugins}
    enabled_assignments = [assignment for assignment in assignments if bool(getattr(assignment, "enabled", False))]
    bundles = [normalize_legacy_installed_plugin(plugin) for plugin in plugins]
    return {
        "migration_only": True,
        "blocks_new_entrypoint": True,
        "runtime_writes": [],
        "counts": {
            "plugins": len(plugins),
            "assignments": len(assignments),
            "enabled_assignments": len(enabled_assignments),
        },
        "catalog_projections": [_catalog_projection(plugin) for plugin in plugins],
        "activation_projections": [
            _activation_projection(assignment, plugin_by_id[getattr(assignment, "installed_plugin_id", None)])
            for assignment in enabled_assignments
            if getattr(assignment, "installed_plugin_id", None) in plugin_by_id
        ],
        "normalized_bundles": [_bundle_summary(bundle) for bundle in bundles],
        "notes": [
            {
                "code": "legacy_pack_migration_only",
                "message": "Legacy pack rows are read as compatibility projections only; new external installs must use Trust Gate reviews.",
            }
        ],
    }


async def sweep_legacy_pack_migration_dry_run(db: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Read legacy pack rows for one tenant and return a no-write migration report."""
    plugins_result = await db.execute(
        select(TenantInstalledPlugin)
        .where(TenantInstalledPlugin.tenant_id == tenant_id)
        .order_by(TenantInstalledPlugin.installed_at.desc())
    )
    assignments_result = await db.execute(
        select(AgentPluginAssignment)
        .where(AgentPluginAssignment.tenant_id == tenant_id)
        .order_by(AgentPluginAssignment.created_at.desc())
    )
    return build_legacy_pack_migration_report(
        list(plugins_result.scalars().all()),
        list(assignments_result.scalars().all()),
    )


def _catalog_projection(plugin: TenantInstalledPlugin) -> dict[str, Any]:
    plugin_id = getattr(plugin, "id", None)
    plugin_key = _text(getattr(plugin, "plugin_key", None)) or "unknown"
    version = _text(getattr(plugin, "version", None)) or "0.0.0"
    status = _text(getattr(plugin, "status", None)) or "unknown"
    return {
        "plugin_id": str(plugin_id) if plugin_id else None,
        "plugin_key": plugin_key,
        "source_format": "legacy_pack",
        "snapshot_key": f"legacy:{plugin_key}:{version}",
        "policy": "approved_available" if status == "enabled" else "disabled",
        "migration_only": True,
        "source_kind": _text(getattr(plugin, "source_kind", None)) or "unknown",
        "source_ref": _text(getattr(plugin, "source_ref", None)),
        "status": status,
    }


def _activation_projection(assignment: AgentPluginAssignment, plugin: TenantInstalledPlugin) -> dict[str, Any]:
    return {
        "assignment_id": str(getattr(assignment, "id", "")),
        "plugin_id": str(getattr(plugin, "id", "")),
        "plugin_key": _text(getattr(plugin, "plugin_key", None)) or "unknown",
        "agent_id": str(getattr(assignment, "agent_id", "")),
        "activation_scope": "agent",
        "enabled": True,
        "migration_only": True,
    }


def _bundle_summary(bundle: NormalizedExternalPluginBundle) -> dict[str, Any]:
    return {
        "source_format": bundle.source_format,
        "source_uri": bundle.source_uri,
        "plugin_name": bundle.plugin_name,
        "version": bundle.version,
        "source_ref": bundle.source_ref,
        "manifest_sha256": bundle.manifest_sha256,
        "components": [],
        "unsupported_components": list(bundle.unsupported_components),
        "admission_notes": list(bundle.admission_notes),
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
