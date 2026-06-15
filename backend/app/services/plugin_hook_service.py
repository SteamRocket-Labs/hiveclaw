"""Runtime registration for declarative plugin hooks.

Plugins may only bind platform-owned handler names from
``catalog_reader.HOOK_HANDLER_ALLOWLIST``. This module turns tenant DB rows into
``HookRegistrationSpec`` entries and registers them on the shared runtime
``HookRegistry``. Raw code/import paths/webhooks are never executed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.installed_plugin import AgentPluginAssignment, PluginHookRegistration, TenantInstalledPlugin
from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult, hook_registry

logger = logging.getLogger(__name__)

PluginHookHandler = Callable[[HookContext, dict[str, Any]], Awaitable[HookResult | None] | HookResult | None]
_PLUGIN_KEY_PREFIX = "plugin:"


def _row_meta(row: PluginHookRegistration, plugin: TenantInstalledPlugin) -> dict[str, Any]:
    matcher = dict(row.matcher_json or {})
    return {
        "tenant_id": str(row.tenant_id),
        "plugin_key": plugin.plugin_key,
        "installed_plugin_id": str(row.installed_plugin_id),
        "hook_id": str(row.id),
        "mode": str(row.mode or "observe").lower(),
        "matcher": matcher,
        "timeout_seconds": min(float(matcher.get("timeout_seconds") or 2.0), 5.0),
    }


async def _audit_handler(_ctx: HookContext, _meta: dict[str, Any]) -> HookResult | None:
    return None


async def _block_handler(_ctx: HookContext, meta: dict[str, Any]) -> HookResult | None:
    if meta["mode"] != "enforce":
        return None
    matcher = meta.get("matcher") or {}
    reason = str(matcher.get("reason") or f"blocked by plugin {meta['plugin_key']}")
    return HookResult(block=True, reason=reason)


async def _args_overlay_handler(ctx: HookContext, meta: dict[str, Any]) -> HookResult | None:
    if meta["mode"] != "enforce" or ctx.event != HookEvent.PRE_TOOL_USE:
        return None
    matcher = meta.get("matcher") or {}
    overlay = matcher.get("args_overlay")
    if not isinstance(overlay, dict) or not overlay:
        return None
    next_args = dict(ctx.tool_args or {})
    next_args.update(overlay)
    return HookResult(block=False, reason=f"modified by plugin {meta['plugin_key']}", modified_args=next_args)


PLUGIN_HOOK_HANDLERS: dict[str, PluginHookHandler] = {
    "plugin.audit": _audit_handler,
    "plugin.block": _block_handler,
    "plugin.args_overlay": _args_overlay_handler,
}


def _make_handler(handler: PluginHookHandler, meta: dict[str, Any]):
    async def _wrapped(ctx: HookContext) -> HookResult | None:
        guard_key = f"_plugin_hook_active:{meta['hook_id']}"
        if ctx.metadata.get(guard_key):
            return None
        previous = ctx.metadata.get(guard_key)
        ctx.metadata[guard_key] = True
        try:
            result = handler(ctx, meta)
            if asyncio.iscoroutine(result):
                return await asyncio.wait_for(result, timeout=float(meta["timeout_seconds"]))
            return result
        except asyncio.TimeoutError:
            logger.warning("[plugin-hooks] hook %s timed out", meta["hook_id"])
            return None
        finally:
            if previous is None:
                ctx.metadata.pop(guard_key, None)
            else:
                ctx.metadata[guard_key] = previous

    return _wrapped


def _matcher_for(row: PluginHookRegistration, plugin: TenantInstalledPlugin, agent_ids: list[str]) -> dict[str, Any]:
    matcher = dict(row.matcher_json or {})
    spec = dict(matcher.get("matcher_spec") or matcher)
    spec["tenant_ids"] = [str(row.tenant_id)]
    if agent_ids:
        declared_agents = [str(agent_id) for agent_id in spec.get("agent_ids", []) if agent_id]
        if declared_agents:
            allowed = sorted(set(declared_agents).intersection(agent_ids))
        else:
            allowed = sorted(agent_ids)
        spec["agent_ids"] = allowed
    else:
        spec["agent_ids"] = []
    spec.setdefault("metadata_equals", {})
    spec["metadata_equals"] = {
        **dict(spec.get("metadata_equals") or {}),
        "tenant_id": str(row.tenant_id),
    }
    return spec


def _unregister_plugin_keys(registry: HookRegistry, *, tenant_id: uuid.UUID | None = None) -> None:
    prefix = f"{_PLUGIN_KEY_PREFIX}{tenant_id}:" if tenant_id else _PLUGIN_KEY_PREFIX
    registry.unregister_key_prefix(prefix)


async def _load_rows_for_tenant(tenant_id: uuid.UUID) -> tuple[list[tuple[PluginHookRegistration, TenantInstalledPlugin]], dict[uuid.UUID, list[str]]]:
    async with tenant_scoped_session(tenant_id) as db:
        rows = (
            (
                await db.execute(
                    select(PluginHookRegistration, TenantInstalledPlugin)
                    .join(TenantInstalledPlugin, TenantInstalledPlugin.id == PluginHookRegistration.installed_plugin_id)
                    .where(
                        PluginHookRegistration.tenant_id == tenant_id,
                        PluginHookRegistration.enabled.is_(True),
                        TenantInstalledPlugin.tenant_id == tenant_id,
                        TenantInstalledPlugin.status == "enabled",
                    )
                )
            )
            .all()
        )
        assignments = (
            (
                await db.execute(
                    select(AgentPluginAssignment).where(
                        AgentPluginAssignment.tenant_id == tenant_id,
                        AgentPluginAssignment.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_plugin: dict[uuid.UUID, list[str]] = {}
    for assignment in assignments:
        by_plugin.setdefault(assignment.installed_plugin_id, []).append(str(assignment.agent_id))
    return rows, by_plugin


async def refresh_plugin_hooks_for_tenant(tenant_id: uuid.UUID, *, registry: HookRegistry | None = None) -> int:
    target = registry or hook_registry
    _unregister_plugin_keys(target, tenant_id=tenant_id)
    rows, assignments_by_plugin = await _load_rows_for_tenant(tenant_id)
    registered = 0
    for row, plugin in rows:
        handler = PLUGIN_HOOK_HANDLERS.get(row.handler)
        if handler is None:
            logger.warning("[plugin-hooks] skip unknown allowlisted handler %s", row.handler)
            continue
        agent_ids = assignments_by_plugin.get(row.installed_plugin_id, [])
        if not agent_ids:
            continue
        try:
            event = HookEvent(str(row.event))
        except ValueError:
            logger.warning("[plugin-hooks] skip unknown event %s", row.event)
            continue
        meta = _row_meta(row, plugin)
        target.register_spec(
            event,
            _make_handler(handler, meta),
            _matcher_for(row, plugin, agent_ids),
            key=f"{_PLUGIN_KEY_PREFIX}{tenant_id}:{plugin.plugin_key}:{row.id}",
            handler_name=row.handler,
            profile_name=f"plugin:{plugin.plugin_key}",
        )
        registered += 1
    return registered


async def register_installed_plugin_hooks(*, registry: HookRegistry | None = None) -> int:
    """Register all enabled plugin hooks at startup using an audited BYPASS read."""
    target = registry or hook_registry
    _unregister_plugin_keys(target)
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="plugin hook startup registration"):
            tenant_ids = (
                (
                    await db.execute(
                        select(PluginHookRegistration.tenant_id)
                        .join(TenantInstalledPlugin, TenantInstalledPlugin.id == PluginHookRegistration.installed_plugin_id)
                        .where(
                            PluginHookRegistration.enabled.is_(True),
                            TenantInstalledPlugin.status == "enabled",
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
    total = 0
    for tenant_id in tenant_ids:
        total += await refresh_plugin_hooks_for_tenant(uuid.UUID(str(tenant_id)), registry=target)
    logger.info("[plugin-hooks] Registered %d plugin hook binding(s)", total)
    return total
