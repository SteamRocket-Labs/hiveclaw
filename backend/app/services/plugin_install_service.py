"""Tenant plugin install service (Step 5).

Installs a capability pack (pack.yaml manifest) into a tenant: validates the
manifest fail-closed, enforces source policy (builtin/local installable; remote
fail-closed), pins a dependency lockfile, and persists a ``TenantInstalledPlugin``
(+ declarative ``PluginHookRegistration`` rows) through ``tenant_scoped_session``
so RLS binds every write. Generalizes the MCPServer install primitive to any pack.
"""

from __future__ import annotations

import logging
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.installed_plugin import (
    AgentPluginAssignment,
    PluginDependencyEdge,
    PluginHookRegistration,
    TenantInstalledPlugin,
)
from app.packs.catalog_reader import HOOK_HANDLER_ALLOWLIST, PackCatalogReader, PackManifest, find_pack_dirs

logger = logging.getLogger(__name__)

_INSTALLABLE_SOURCE_KINDS = frozenset({"builtin", "local"})
_ACTIVE_STATES = frozenset({"active", "enabled", "on", "true", "1"})


class PluginInstallError(Exception):
    """A plugin cannot be installed (validation / source policy / dependency)."""


@dataclass(frozen=True, slots=True)
class ResolvedPlugin:
    manifest: PackManifest
    lockfile: dict
    source_kind: str
    source_ref: str | None


def load_manifest(plugin_key: str) -> PackManifest | None:
    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        manifest = reader.get_pack(plugin_key)
        if manifest is not None:
            return manifest
    return None


def _manifest_content_hash(manifest: PackManifest) -> str | None:
    if manifest.manifest_path is None:
        return None
    try:
        return hashlib.sha256(manifest.manifest_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _source_ref(manifest: PackManifest) -> str | None:
    ref = str((manifest.source or {}).get("ref") or "").strip()
    return ref or None


def _assert_safe_local_source_ref(manifest: PackManifest) -> None:
    if _source_kind(manifest) != "local":
        return
    ref = _source_ref(manifest)
    if not ref:
        raise PluginInstallError(f"plugin {manifest.name!r} local source is missing a pinned ref")
    candidate = Path(ref)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PluginInstallError(f"plugin {manifest.name!r} local source ref {ref!r} is not allowed")


def _dependency_entries(manifest: PackManifest, resolved_by_key: dict[str, ResolvedPlugin]) -> list[dict]:
    entries: list[dict] = []
    for dep in manifest.dependencies:
        dep_name = str(dep.get("name") or "").strip()
        resolved = resolved_by_key[dep_name]
        entries.append(
            {
                "plugin_key": resolved.manifest.name,
                "version": resolved.manifest.version,
                "source_kind": resolved.source_kind,
                "source_ref": resolved.source_ref,
                "content_sha256": _manifest_content_hash(resolved.manifest),
            }
        )
    return entries


def _resolve_lockfile(manifest: PackManifest, resolved_by_key: dict[str, ResolvedPlugin] | None = None) -> dict:
    """Pin the resolved dependency closure with source/hash provenance."""
    resolved_by_key = resolved_by_key or {}
    return {
        "plugin_key": manifest.name,
        "version": manifest.version,
        "source_kind": _source_kind(manifest),
        "source_ref": _source_ref(manifest),
        "content_sha256": _manifest_content_hash(manifest),
        "dependencies": _dependency_entries(manifest, resolved_by_key),
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
    _assert_safe_local_source_ref(manifest)


def resolve_plugin_dependency_closure(plugin_key: str) -> list[ResolvedPlugin]:
    """Resolve a plugin and its dependencies in install order.

    The closure is fail-closed: every dependency must exist locally, be
    installable, match the exact pinned version, and must not create a cycle.
    """
    resolved: dict[str, ResolvedPlugin] = {}
    visiting: list[str] = []

    def visit(key: str) -> None:
        if key in resolved:
            return
        if key in visiting:
            cycle = " -> ".join([*visiting, key])
            raise PluginInstallError(f"plugin dependency cycle detected: {cycle}")
        manifest = load_manifest(key)
        if manifest is None:
            raise PluginInstallError(f"dependency manifest {key!r} not found")
        _assert_installable(manifest)
        visiting.append(key)
        for dep in manifest.dependencies:
            dep_name = str(dep.get("name") or "").strip()
            dep_version = str(dep.get("version") or "").strip()
            if not dep_name or not dep_version:
                raise PluginInstallError(f"plugin {manifest.name!r} has an unpinned dependency")
            dep_source = dep.get("source")
            if isinstance(dep_source, dict):
                dep_kind = str(dep_source.get("kind") or "builtin").lower()
                if dep_kind not in _INSTALLABLE_SOURCE_KINDS:
                    raise PluginInstallError(
                        f"dependency {dep_name!r} source kind {dep_kind!r} is not installable in v1"
                    )
            visit(dep_name)
            actual = resolved[dep_name].manifest.version
            if actual != dep_version:
                raise PluginInstallError(
                    f"dependency {dep_name!r} version mismatch: manifest requires {dep_version}, found {actual}"
                )
        visiting.pop()
        resolved[key] = ResolvedPlugin(
            manifest=manifest,
            lockfile=_resolve_lockfile(manifest, resolved),
            source_kind=_source_kind(manifest),
            source_ref=_source_ref(manifest),
        )

    visit(plugin_key)
    return list(resolved.values())


def _approved_hook_key(manifest_name: str, hook: dict) -> str:
    return f"{manifest_name}:{str(hook.get('event') or '').strip()}:{str(hook.get('handler') or '').strip()}"


def validate_hook_install_approval(manifest: PackManifest, *, config: dict | None) -> None:
    approved = set(str(item) for item in ((config or {}).get("approved_enforce_hooks") or []))
    for hook in manifest.hooks:
        event = str(hook.get("event") or "").strip()
        mode = str(hook.get("mode") or "observe").strip().lower()
        if event == "pre_tool_use" and mode == "enforce":
            key = _approved_hook_key(manifest.name, hook)
            if key not in approved and "*" not in approved:
                raise PluginInstallError(
                    f"PRE_TOOL_USE enforce hook {key!r} requires explicit approved_enforce_hooks admin approval"
                )


async def _upsert_plugin_record(db, tenant_id: uuid.UUID, resolved: ResolvedPlugin, *, config: dict | None) -> TenantInstalledPlugin:
    manifest = resolved.manifest
    existing = (
        await db.execute(
            select(TenantInstalledPlugin).where(
                TenantInstalledPlugin.tenant_id == tenant_id,
                TenantInstalledPlugin.plugin_key == manifest.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.version = manifest.version
        existing.source_kind = resolved.source_kind
        existing.source_ref = resolved.source_ref
        existing.status = "enabled"
        if config is not None:
            existing.config_json = config
        existing.lockfile_json = resolved.lockfile
        plugin = existing
    else:
        plugin = TenantInstalledPlugin(
            tenant_id=tenant_id,
            plugin_key=manifest.name,
            version=manifest.version,
            source_kind=resolved.source_kind,
            source_ref=resolved.source_ref,
            status="enabled",
            config_json=config or {},
            lockfile_json=resolved.lockfile,
        )
        db.add(plugin)
    await db.flush()
    return plugin


async def _sync_plugin_hooks(db, tenant_id: uuid.UUID, plugin: TenantInstalledPlugin, manifest: PackManifest) -> None:
    await db.execute(
        delete(PluginHookRegistration).where(
            PluginHookRegistration.tenant_id == tenant_id,
            PluginHookRegistration.installed_plugin_id == plugin.id,
        )
    )
    for hook in manifest.hooks:
        handler = str(hook.get("handler") or "").strip()
        if handler not in HOOK_HANDLER_ALLOWLIST:
            raise PluginInstallError(f"hook handler {handler!r} is not in the platform allowlist")
        db.add(
            PluginHookRegistration(
                tenant_id=tenant_id,
                installed_plugin_id=plugin.id,
                event=str(hook.get("event") or ""),
                handler=handler,
                matcher_json=hook.get("matcher") or {},
                mode=str(hook.get("mode") or "observe"),
            )
        )


async def _sync_dependency_edges(
    db,
    tenant_id: uuid.UUID,
    plugin: TenantInstalledPlugin,
    manifest: PackManifest,
    installed_by_key: dict[str, TenantInstalledPlugin],
    resolved_by_key: dict[str, ResolvedPlugin],
) -> None:
    await db.execute(
        delete(PluginDependencyEdge).where(
            PluginDependencyEdge.tenant_id == tenant_id,
            PluginDependencyEdge.installed_plugin_id == plugin.id,
        )
    )
    for dep in manifest.dependencies:
        dep_name = str(dep.get("name") or "").strip()
        dependency = installed_by_key[dep_name]
        resolved = resolved_by_key[dep_name]
        db.add(
            PluginDependencyEdge(
                tenant_id=tenant_id,
                installed_plugin_id=plugin.id,
                dependency_plugin_id=dependency.id,
                dependency_key=dependency.plugin_key,
                dependency_version=dependency.version,
                source_kind=resolved.source_kind,
            )
        )


async def _sync_agent_assignments(
    db,
    tenant_id: uuid.UUID,
    plugin: TenantInstalledPlugin,
    *,
    agent_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None,
) -> None:
    if agent_ids is None:
        ids = (
            (await db.execute(select(Agent.id).where(Agent.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    else:
        ids = [uuid.UUID(str(agent_id)) for agent_id in agent_ids]
    if not ids:
        return
    existing = (
        (
            await db.execute(
                select(AgentPluginAssignment).where(
                    AgentPluginAssignment.tenant_id == tenant_id,
                    AgentPluginAssignment.installed_plugin_id == plugin.id,
                    AgentPluginAssignment.agent_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_agent = {row.agent_id: row for row in existing}
    for agent_id in ids:
        current = by_agent.get(agent_id)
        if current is not None:
            continue
        db.add(
            AgentPluginAssignment(
                tenant_id=tenant_id,
                agent_id=agent_id,
                installed_plugin_id=plugin.id,
                enabled=True,
            )
        )


async def install_plugin(
    tenant_id: uuid.UUID,
    plugin_key: str,
    *,
    config: dict | None = None,
    agent_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None = None,
) -> dict:
    """Install (or idempotently re-install) a pack into a tenant. RLS-bound."""
    if load_manifest(plugin_key) is None:
        raise PluginInstallError(f"no manifest found for plugin {plugin_key!r}")
    closure = resolve_plugin_dependency_closure(plugin_key)
    for resolved in closure:
        validate_hook_install_approval(resolved.manifest, config=config)

    async with tenant_scoped_session(tenant_id) as db:
        installed_by_key: dict[str, TenantInstalledPlugin] = {}
        resolved_by_key = {resolved.manifest.name: resolved for resolved in closure}
        for resolved in closure:
            plugin = await _upsert_plugin_record(
                db,
                tenant_id,
                resolved,
                config=config if resolved.manifest.name == plugin_key else None,
            )
            installed_by_key[resolved.manifest.name] = plugin
        for resolved in closure:
            plugin = installed_by_key[resolved.manifest.name]
            await _sync_plugin_hooks(db, tenant_id, plugin, resolved.manifest)
            await _sync_dependency_edges(db, tenant_id, plugin, resolved.manifest, installed_by_key, resolved_by_key)
            await _sync_agent_assignments(
                db,
                tenant_id,
                plugin,
                agent_ids=agent_ids,
            )
        target = installed_by_key[plugin_key]
    try:
        from app.services.plugin_hook_service import refresh_plugin_hooks_for_tenant

        await refresh_plugin_hooks_for_tenant(tenant_id)
    except Exception as exc:
        logger.warning("[plugin-install] hook refresh failed for tenant %s: %s", tenant_id, exc)
    return {
        "id": str(target.id),
        "plugin_key": plugin_key,
        "version": target.version,
        "status": target.status,
        "source_kind": target.source_kind,
        "installed_dependencies": [r.manifest.name for r in closure if r.manifest.name != plugin_key],
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
                "lockfile": r.lockfile_json,
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
        blockers = (
            (
                await db.execute(
                    select(TenantInstalledPlugin.plugin_key)
                    .join(
                        PluginDependencyEdge,
                        PluginDependencyEdge.installed_plugin_id == TenantInstalledPlugin.id,
                    )
                    .where(
                        PluginDependencyEdge.tenant_id == tenant_id,
                        PluginDependencyEdge.dependency_plugin_id == existing.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if blockers:
            raise PluginInstallError(
                f"plugin {plugin_key!r} is required by installed plugin(s): {', '.join(sorted(blockers))}"
            )
        await db.delete(existing)  # CASCADE removes assignments + hook registrations
    try:
        from app.services.plugin_hook_service import refresh_plugin_hooks_for_tenant

        await refresh_plugin_hooks_for_tenant(tenant_id)
    except Exception as exc:
        logger.warning("[plugin-uninstall] hook refresh failed for tenant %s: %s", tenant_id, exc)
    return True


async def set_agent_plugin_assignment(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    plugin_key: str,
    *,
    enabled: bool,
) -> dict:
    async with tenant_scoped_session(tenant_id) as db:
        plugin = (
            await db.execute(
                select(TenantInstalledPlugin).where(
                    TenantInstalledPlugin.tenant_id == tenant_id,
                    TenantInstalledPlugin.plugin_key == plugin_key,
                    TenantInstalledPlugin.status == "enabled",
                )
            )
        ).scalar_one_or_none()
        if plugin is None:
            raise PluginInstallError(f"plugin {plugin_key!r} is not installed")
        assignment = (
            await db.execute(
                select(AgentPluginAssignment).where(
                    AgentPluginAssignment.tenant_id == tenant_id,
                    AgentPluginAssignment.agent_id == agent_id,
                    AgentPluginAssignment.installed_plugin_id == plugin.id,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            assignment = AgentPluginAssignment(
                tenant_id=tenant_id,
                agent_id=agent_id,
                installed_plugin_id=plugin.id,
                enabled=enabled,
            )
            db.add(assignment)
        else:
            assignment.enabled = enabled
        await db.flush()
        assignment_id = assignment.id
    try:
        from app.services.plugin_hook_service import refresh_plugin_hooks_for_tenant

        await refresh_plugin_hooks_for_tenant(tenant_id)
    except Exception as exc:
        logger.warning("[plugin-assignment] hook refresh failed for tenant %s: %s", tenant_id, exc)
    return {
        "id": str(assignment_id),
        "agent_id": str(agent_id),
        "plugin_key": plugin_key,
        "enabled": enabled,
    }


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
