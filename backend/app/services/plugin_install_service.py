"""Tenant plugin install service (Step 5).

Installs a capability pack (pack.yaml manifest) into a tenant: validates the
manifest fail-closed, enforces source policy (builtin/local installable; remote
fail-closed), pins a dependency lockfile, and persists a ``TenantInstalledPlugin``
(+ declarative ``PluginHookRegistration`` rows) through ``tenant_scoped_session``
so RLS binds every write. Generalizes the MCPServer install primitive to any pack.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.installed_plugin import PluginHookRegistration, TenantInstalledPlugin
from app.packs.catalog_reader import HOOK_HANDLER_ALLOWLIST, PackCatalogReader, PackManifest, find_pack_dirs

logger = logging.getLogger(__name__)

_INSTALLABLE_SOURCE_KINDS = frozenset({"builtin", "local"})
_ACTIVE_STATES = frozenset({"active", "enabled", "on", "true", "1"})


class PluginInstallError(Exception):
    """A plugin cannot be installed (validation / source policy / dependency)."""


def load_manifest(plugin_key: str) -> PackManifest | None:
    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        manifest = reader.get_pack(plugin_key)
        if manifest is not None:
            return manifest
    return None


def _resolve_lockfile(manifest: PackManifest) -> dict:
    """Pin the declared dependency closure (governed inclusion). The validator
    already enforced name+version on each dependency; record them as the lock."""
    return {
        "dependencies": [{"name": d.get("name"), "version": d.get("version")} for d in manifest.dependencies],
        "resolved_at_version": manifest.version,
    }


def _source_kind(manifest: PackManifest) -> str:
    return str((manifest.source or {}).get("kind") or "builtin").lower()


def _assert_installable(manifest: PackManifest) -> None:
    if manifest.validation_errors:
        raise PluginInstallError(f"manifest {manifest.name!r} invalid: {list(manifest.validation_errors)}")
    kind = _source_kind(manifest)
    if kind not in _INSTALLABLE_SOURCE_KINDS:
        raise PluginInstallError(
            f"plugin {manifest.name!r} source kind {kind!r} is not installable in v1 "
            "(needs signature + sandbox infra) — fail-closed"
        )


async def install_plugin(tenant_id: uuid.UUID, plugin_key: str, *, config: dict | None = None) -> dict:
    """Install (or idempotently re-install) a pack into a tenant. RLS-bound."""
    manifest = load_manifest(plugin_key)
    if manifest is None:
        raise PluginInstallError(f"no manifest found for plugin {plugin_key!r}")
    _assert_installable(manifest)
    source_kind = _source_kind(manifest)
    source_ref = str((manifest.source or {}).get("ref") or "") or None
    lockfile = _resolve_lockfile(manifest)

    async with tenant_scoped_session(tenant_id) as db:
        existing = (
            await db.execute(
                select(TenantInstalledPlugin).where(
                    TenantInstalledPlugin.tenant_id == tenant_id,
                    TenantInstalledPlugin.plugin_key == plugin_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.version = manifest.version
            existing.source_kind = source_kind
            existing.source_ref = source_ref
            existing.status = "enabled"
            if config is not None:
                existing.config_json = config
            existing.lockfile_json = lockfile
            plugin = existing
        else:
            plugin = TenantInstalledPlugin(
                tenant_id=tenant_id,
                plugin_key=plugin_key,
                version=manifest.version,
                source_kind=source_kind,
                source_ref=source_ref,
                status="enabled",
                config_json=config or {},
                lockfile_json=lockfile,
            )
            db.add(plugin)
        await db.flush()
        plugin_id = plugin.id

        # Re-register declarative hooks idempotently (platform allowlist enforced;
        # a plugin can never bind raw code — governed inclusion §6.7).
        await db.execute(
            PluginHookRegistration.__table__.delete().where(
                PluginHookRegistration.tenant_id == tenant_id,
                PluginHookRegistration.installed_plugin_id == plugin_id,
            )
        )
        for hook in manifest.hooks:
            handler = str(hook.get("handler") or "").strip()
            if handler not in HOOK_HANDLER_ALLOWLIST:
                raise PluginInstallError(f"hook handler {handler!r} is not in the platform allowlist")
            db.add(
                PluginHookRegistration(
                    tenant_id=tenant_id,
                    installed_plugin_id=plugin_id,
                    event=str(hook.get("event") or ""),
                    handler=handler,
                    matcher_json=hook.get("matcher") or {},
                    mode=str(hook.get("mode") or "observe"),
                )
            )
        return {
            "id": str(plugin_id),
            "plugin_key": plugin_key,
            "version": manifest.version,
            "status": "enabled",
            "source_kind": source_kind,
        }


async def list_installed_plugins(tenant_id: uuid.UUID) -> list[dict]:
    async with tenant_scoped_session(tenant_id) as db:
        rows = (
            (await db.execute(select(TenantInstalledPlugin).where(TenantInstalledPlugin.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        return [
            {
                "id": str(r.id),
                "plugin_key": r.plugin_key,
                "version": r.version,
                "status": r.status,
                "source_kind": r.source_kind,
            }
            for r in rows
        ]


async def uninstall_plugin(tenant_id: uuid.UUID, plugin_key: str) -> bool:
    async with tenant_scoped_session(tenant_id) as db:
        existing = (
            await db.execute(
                select(TenantInstalledPlugin).where(
                    TenantInstalledPlugin.tenant_id == tenant_id,
                    TenantInstalledPlugin.plugin_key == plugin_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return False
        await db.delete(existing)  # CASCADE removes assignments + hook registrations
        return True


async def backfill_tenant_plugins(tenant_id: uuid.UUID) -> list[str]:
    """Install all default-active manifest packs a tenant does not yet have.

    Preserves the historical "no-manifest pack = enabled" behavior so flipping
    pack policy to "installed = available" never silently disables a tenant's
    capabilities (critic §5.3). Idempotent: skips already-installed packs.
    """
    installed_keys = {p["plugin_key"] for p in await list_installed_plugins(tenant_id)}
    newly: list[str] = []
    seen: set[str] = set()
    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        for manifest in reader.list_packs():
            if manifest.name in seen or manifest.name in installed_keys:
                continue
            seen.add(manifest.name)
            state = str((manifest.activation or {}).get("default_state") or "active").strip().lower()
            if state not in _ACTIVE_STATES or manifest.validation_errors:
                continue
            try:
                await install_plugin(tenant_id, manifest.name)
                newly.append(manifest.name)
            except PluginInstallError as exc:
                logger.warning("[plugin-backfill] skip %s for tenant %s: %s", manifest.name, tenant_id, exc)
    return newly
