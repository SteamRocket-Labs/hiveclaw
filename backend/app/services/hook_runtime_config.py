"""Durable platform-extension hook runtime configuration.

Per-employee overrides are intentionally limited to registered plugin hooks.
Built-in runtime safeguards are platform invariants and are never restored from
the legacy ``agent:*:hook_runtime`` settings surface.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, enter_rls_bypass
from app.models.system_settings import SystemSetting
from app.runtime.hooks import configure_hook_runtime, hook_registry


_RETIRED_OVERRIDES_KEY = "retired_hook_runtime_overrides"
_PLUGIN_HOOK_KEY_PREFIX = "plugin:"


def agent_hook_runtime_config_key(agent_id: uuid.UUID) -> str:
    return f"agent:{agent_id}:hook_runtime"


def parse_agent_hook_runtime_config_key(key: str) -> uuid.UUID | None:
    prefix = "agent:"
    suffix = ":hook_runtime"
    raw = str(key or "")
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        return None
    try:
        return uuid.UUID(raw[len(prefix) : -len(suffix)])
    except ValueError:
        return None


def normalize_hook_runtime_configs(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    configs = value.get("hooks", value)
    if not isinstance(configs, dict):
        return {}
    return {str(key): dict(config) for key, config in configs.items() if isinstance(config, dict)}


def _config_sha256(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_retirement_history(value: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    history: dict[str, list[dict[str, Any]]] = {}
    for raw_key, raw_records in value.items():
        records = raw_records if isinstance(raw_records, list) else []
        normalized = [dict(record) for record in records if isinstance(record, dict)]
        if normalized:
            history[str(raw_key)] = normalized
    return history


def registered_extension_hook_keys(registrations: Iterable[dict[str, Any]] | None = None) -> set[str]:
    """Return mutable hook keys backed by a currently registered plugin.

    The stable ``plugin:`` prefix is issued by ``plugin_hook_service`` and the
    profile check prevents an unrelated registration from claiming the prefix.
    """

    items = registrations if registrations is not None else hook_registry.describe_registrations()
    keys: set[str] = set()
    for item in items:
        key = str(item.get("key") or "")
        profile_name = str(item.get("profile_name") or "")
        if key.startswith(_PLUGIN_HOOK_KEY_PREFIX) and profile_name.startswith("plugin:"):
            keys.add(key)
    return keys


def retire_disallowed_hook_runtime_overrides(
    value: object,
    *,
    mutable_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move legacy internal/stale overrides into a deterministic recovery area.

    No config bytes are deleted. Each distinct retired config is preserved with
    a content hash and reason. Re-running the cleanup is idempotent.
    """

    raw_value = dict(value) if isinstance(value, dict) else {}
    payload = dict(raw_value) if "hooks" in raw_value else {}
    configs = normalize_hook_runtime_configs(raw_value)
    active: dict[str, dict[str, Any]] = {}
    history = _normalized_retirement_history(raw_value.get(_RETIRED_OVERRIDES_KEY))
    retired_keys: list[str] = []

    for key in sorted(configs):
        config = dict(configs[key])
        if key in mutable_keys:
            active[key] = config
            continue
        reason = "extension_not_registered" if key.startswith(_PLUGIN_HOOK_KEY_PREFIX) else "built_in_hook_immutable"
        fingerprint = _config_sha256(config)
        records = history.setdefault(key, [])
        if not any(str(record.get("sha256") or "") == fingerprint for record in records):
            records.append(
                {
                    "schema": "hive.retired_hook_runtime_override.v1",
                    "reason": reason,
                    "sha256": fingerprint,
                    "config": config,
                }
            )
        retired_keys.append(key)

    payload["hooks"] = active
    if history:
        payload[_RETIRED_OVERRIDES_KEY] = history
    else:
        payload.pop(_RETIRED_OVERRIDES_KEY, None)
    return payload, {
        "active_extension_overrides": len(active),
        "retired_overrides": len(retired_keys),
        "retired_keys": retired_keys,
    }


async def read_agent_hook_runtime_configs(db: AsyncSession, *, agent_id: uuid.UUID) -> dict[str, dict]:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == agent_hook_runtime_config_key(agent_id)))
    setting = result.scalar_one_or_none()
    return normalize_hook_runtime_configs(getattr(setting, "value", None) or {})


async def persist_agent_hook_runtime_config(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    key: str,
    config: dict,
) -> None:
    """Stage one registered extension override in the caller transaction."""

    setting_key = agent_hook_runtime_config_key(agent_id)
    # The advisory transaction lock also serializes the first write, when no
    # SystemSetting row exists yet and SELECT ... FOR UPDATE cannot lock it.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": setting_key},
    )
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == setting_key).with_for_update())
    setting = result.scalar_one_or_none()
    existing = dict(getattr(setting, "value", None) or {}) if setting is not None else {}
    configs = normalize_hook_runtime_configs(existing)
    configs[str(key)] = dict(config)
    payload = dict(existing)
    payload["hooks"] = configs
    retired_history = existing.get("retired_hook_runtime_overrides")
    if retired_history is not None:
        payload[_RETIRED_OVERRIDES_KEY] = retired_history
    if setting is None:
        db.add(SystemSetting(key=setting_key, value=payload))
    else:
        setting.value = payload
    await db.flush()


def apply_agent_hook_runtime_configs(
    agent_id: uuid.UUID,
    configs: dict[str, dict],
    *,
    mutable_keys: set[str] | None = None,
) -> int:
    allowed = registered_extension_hook_keys() if mutable_keys is None else set(mutable_keys)
    applied = 0
    for key, config in configs.items():
        if key not in allowed:
            continue
        configure_hook_runtime(
            key=key,
            agent_id=agent_id,
            enabled=config.get("enabled"),
            timeout_seconds=config.get("timeout_seconds"),
            failure_policy=config.get("failure_policy"),
            migration_preview=config.get("migration_preview"),
        )
        applied += 1
    return applied


async def _audit_retired_overrides(*, changes: list[dict[str, Any]]) -> None:
    from app.services.audit_logger import write_platform_security_audit_event

    await write_platform_security_audit_event(
        event_type="hook_override_retirement",
        severity="warning",
        actor_type="system",
        actor_id=None,
        action="retire_legacy_per_employee_hook_overrides",
        resource_type="runtime_hooks",
        details={
            "schema": "hive.hook_override_retirement.v1",
            "agent_count": len(changes),
            "override_count": sum(int(item["retired_overrides"]) for item in changes),
            "changes": changes,
            "recovery_surface": _RETIRED_OVERRIDES_KEY,
        },
    )


async def apply_all_persisted_hook_runtime_configs() -> int:
    """Retire invalid legacy overrides and apply registered extensions only."""

    mutable_keys = registered_extension_hook_keys()
    applied = 0
    changes: list[dict[str, Any]] = []
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="hook runtime config startup load") as bypass_db:
            rows = (
                (
                    await bypass_db.execute(
                        select(SystemSetting).where(SystemSetting.key.like("agent:%:hook_runtime")).with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                agent_id = parse_agent_hook_runtime_config_key(row.key)
                if agent_id is None:
                    continue
                payload, receipt = retire_disallowed_hook_runtime_overrides(row.value, mutable_keys=mutable_keys)
                if receipt["retired_overrides"]:
                    row.value = payload
                    changes.append(
                        {
                            "agent_id": str(agent_id),
                            "retired_overrides": receipt["retired_overrides"],
                            "retired_keys": receipt["retired_keys"],
                        }
                    )
                applied += apply_agent_hook_runtime_configs(
                    agent_id,
                    normalize_hook_runtime_configs(payload),
                    mutable_keys=mutable_keys,
                )
            if changes:
                await _audit_retired_overrides(changes=changes)
                await bypass_db.commit()
                logger.warning(
                    "[hook-runtime] retired {} legacy per-employee overrides across {} agents",
                    sum(int(item["retired_overrides"]) for item in changes),
                    len(changes),
                )
    return applied
