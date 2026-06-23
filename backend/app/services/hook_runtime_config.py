"""Durable hook runtime configuration loader."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, enter_rls_bypass
from app.models.system_settings import SystemSetting
from app.runtime.hooks import configure_hook_runtime


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
    configs = await read_agent_hook_runtime_configs(db, agent_id=agent_id)
    configs[str(key)] = dict(config)
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == agent_hook_runtime_config_key(agent_id)))
    setting = result.scalar_one_or_none()
    payload = {"hooks": configs}
    if setting is None:
        db.add(SystemSetting(key=agent_hook_runtime_config_key(agent_id), value=payload))
    else:
        setting.value = payload
    await db.commit()


def apply_agent_hook_runtime_configs(agent_id: uuid.UUID, configs: dict[str, dict]) -> int:
    applied = 0
    for key, config in configs.items():
        configure_hook_runtime(
            key=key,
            agent_id=agent_id,
            enabled=config.get("enabled"),
            timeout_seconds=config.get("timeout_seconds"),
            failure_policy=config.get("failure_policy"),
        )
        applied += 1
    return applied


async def apply_all_persisted_hook_runtime_configs() -> int:
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="hook runtime config startup load"):
            rows = (
                (
                    await db.execute(
                        select(SystemSetting).where(SystemSetting.key.like("agent:%:hook_runtime"))
                    )
                )
                .scalars()
                .all()
            )
    applied = 0
    for row in rows:
        agent_id = parse_agent_hook_runtime_config_key(row.key)
        if agent_id is None:
            continue
        applied += apply_agent_hook_runtime_configs(agent_id, normalize_hook_runtime_configs(row.value))
    return applied
