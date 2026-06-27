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
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.installed_plugin import AgentPluginAssignment, PluginHookRegistration, TenantInstalledPlugin
from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult, hook_registry
from app.services.invocation_trace import current_invocation_id, new_invocation_id, persist_invocation_span

logger = logging.getLogger(__name__)

PluginHookHandler = Callable[[HookContext, dict[str, Any]], Awaitable[HookResult | None] | HookResult | None]
_PLUGIN_KEY_PREFIX = "plugin:"
_GOVERNED_HOOK_HANDLER_TYPES = {
    "hook.agent": "agent",
    "hook.command": "command",
    "hook.http": "http",
    "hook.prompt": "prompt",
}


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


def _matcher_payload(row: PluginHookRegistration) -> dict[str, Any]:
    raw = dict(row.matcher_json or {})
    if isinstance(raw.get("matcher"), dict):
        return dict(raw["matcher"])
    if isinstance(raw.get("matcher_spec"), dict):
        return dict(raw["matcher_spec"])
    raw.pop("spec", None)
    return raw


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if key and item is not None}


def _governed_hook_spec_from_row(row: PluginHookRegistration, plugin: TenantInstalledPlugin) -> HookSpec:
    hook_type = _GOVERNED_HOOK_HANDLER_TYPES[str(row.handler)]
    raw = dict(row.matcher_json or {})
    declared = raw.get("spec") if isinstance(raw.get("spec"), dict) else {}
    spec = dict(declared)
    spec["type"] = hook_type
    return HookSpec(
        key=f"{plugin.plugin_key}:{row.id}",
        event=HookEvent(str(row.event)),
        type=hook_type,  # type: ignore[arg-type]
        command=str(spec.get("command") or "") or None,
        prompt=str(spec.get("prompt") or "") or None,
        url=str(spec.get("url") or "") or None,
        shell=str(spec.get("shell") or "bash"),
        timeout_seconds=_optional_int(spec.get("timeout_seconds") or spec.get("timeout")),
        status_message=str(spec.get("status_message") or "") or None,
        async_hook=bool(spec.get("async") or spec.get("async_hook")),
        async_rewake=bool(spec.get("async_rewake")),
        env=_string_map(spec.get("env")),
        headers=_string_map(spec.get("headers")),
        network_policy=str(spec.get("network_policy") or "deny"),
    )


def _hook_work_dir() -> Path:
    path = Path(get_settings().AGENT_DATA_DIR) / ".hive" / "hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _default_http_hook_executor(
    *,
    url: str | None,
    headers: dict[str, str],
    json_payload: dict[str, Any],
    timeout: int,
    hook_key: str,
    event: str,
    network_policy: str = "deny",
) -> dict[str, Any]:
    policy = str(network_policy or "deny").strip().lower()
    if policy not in {"allow", "allow_outbound", "internet", "public"}:
        raise RuntimeError(f"http hook {hook_key!r} for {event} denied by network_policy={network_policy!r}")
    if not url:
        raise RuntimeError("http hook requires url")
    parsed = httpx.URL(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise RuntimeError("http hook url must be http(s) without userinfo")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(str(parsed), headers=headers, json=json_payload)
    text = response.text
    parsed_body: Any = None
    try:
        parsed_body = response.json()
    except ValueError:
        parsed_body = None
    if isinstance(parsed_body, dict):
        return {
            **parsed_body,
            "status_code": response.status_code,
            "text": str(parsed_body.get("text") or parsed_body.get("content") or text),
        }
    return {"status_code": response.status_code, "text": text, "body": text}


async def _record_governed_hook_span(fact: dict[str, Any]) -> None:
    tenant_id = fact.get("tenant_id")
    if not tenant_id:
        return
    trace_id = str(fact.get("trace_id") or current_invocation_id() or new_invocation_id())
    hook_key = str(fact.get("hook_key") or "hook")
    await persist_invocation_span(
        tenant_id=tenant_id,
        trace_id=trace_id,
        span_id=f"hook-{uuid.uuid4().hex[:12]}",
        parent_span_id=fact.get("parent_span_id"),
        parent_trace_id=fact.get("parent_trace_id"),
        span_type="hook",
        name=f"hook:{hook_key}",
        status=str(fact.get("status") or "ok"),
        duration_ms=0.0,
        agent_id=fact.get("agent_id"),
        user_id=fact.get("user_id"),
        runtime_task_id=fact.get("runtime_task_id"),
        session_id=fact.get("session_id"),
        request_id=fact.get("request_id"),
        metadata=fact,
        error=str(fact.get("error") or "") or None,
    )


def _build_governed_hook_runner() -> GovernedHookRunner:
    return GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=_hook_work_dir()),
        http_executor=_default_http_hook_executor,
        span_recorder=_record_governed_hook_span,
    )


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
    spec = _matcher_payload(row)
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
        governed_handler = str(row.handler) in _GOVERNED_HOOK_HANDLER_TYPES
        if handler is None and not governed_handler:
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
        if governed_handler:
            spec = _governed_hook_spec_from_row(row, plugin)
            runner = _build_governed_hook_runner()
            target.register_spec(
                event,
                runner.build_handler(spec),
                _matcher_for(row, plugin, agent_ids),
                key=f"{_PLUGIN_KEY_PREFIX}{tenant_id}:{plugin.plugin_key}:{row.id}",
                handler_name="governed_hook_runner",
                profile_name=f"plugin:{plugin.plugin_key}:{row.handler}",
            )
        else:
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
