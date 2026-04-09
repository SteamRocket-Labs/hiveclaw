"""Tenant pack policy storage and filtering."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting


def tenant_pack_policy_key(tenant_id: uuid.UUID) -> str:
    return f"tenant:{tenant_id}:pack_policies"


async def get_tenant_pack_policies(db: AsyncSession, tenant_id: uuid.UUID | None) -> dict[str, bool]:
    if not tenant_id:
        return {}
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == tenant_pack_policy_key(tenant_id)))
    setting = result.scalar_one_or_none()
    value = getattr(setting, "value", None) or {}
    policies = value.get("packs", value)
    return policies if isinstance(policies, dict) else {}


async def set_tenant_pack_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    pack_name: str,
    *,
    enabled: bool,
) -> dict[str, bool]:
    key = tenant_pack_policy_key(tenant_id)
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    existing = {}
    if setting and isinstance(setting.value, dict):
        existing = setting.value.get("packs", setting.value)
        if not isinstance(existing, dict):
            existing = {}
    policies = {**existing, pack_name: enabled}
    payload = {"packs": policies}
    if setting:
        setting.value = payload
    else:
        db.add(SystemSetting(key=key, value=payload))
    await db.commit()
    return policies


def is_pack_enabled(pack_policies: dict[str, bool], pack_name: str) -> bool:
    return pack_policies.get(pack_name, True)
