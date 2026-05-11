"""Tenant pack policy storage and filtering."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting
from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs


_MANIFEST_DEFAULT_ENABLEMENT: dict[str, bool] | None = None
_REGISTERED_TOOL_PACK_NAMES: dict[str, tuple[str, ...]] | None = None


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


def _manifest_default_enablement() -> dict[str, bool]:
    """Return default enablement from pack manifests.

    Static runtime packs without a manifest keep the historical default of
    enabled. Manifest-backed packs must honor `activation.default_state`; an
    inactive catalog package should not silently become available tenant-wide.
    """
    global _MANIFEST_DEFAULT_ENABLEMENT
    if _MANIFEST_DEFAULT_ENABLEMENT is not None:
        return _MANIFEST_DEFAULT_ENABLEMENT

    defaults: dict[str, bool] = {}
    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        for manifest in reader.list_packs():
            state = str((manifest.activation or {}).get("default_state") or "active").strip().lower()
            defaults.setdefault(manifest.name, state in {"active", "enabled", "on", "true", "1"})

    _MANIFEST_DEFAULT_ENABLEMENT = defaults
    return defaults


def is_pack_enabled(pack_policies: dict[str, bool], pack_name: str) -> bool:
    if pack_name in pack_policies:
        return bool(pack_policies[pack_name])
    return _manifest_default_enablement().get(pack_name, True)


def policy_pack_names_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return policy-relevant pack names for a tool.

    Static packs infer membership from `app.tools.packs`. Dedicated tool
    handlers also declare `ToolMeta.pack`; this keeps first-class tools such as
    `deep_research_run` attached to their pack without incorrectly tagging
    shared dependencies like `web_search` as Deep Research.
    """
    global _REGISTERED_TOOL_PACK_NAMES
    if _REGISTERED_TOOL_PACK_NAMES is None:
        from app.tools.collector import collect_tools
        from app.tools.packs import static_pack_names_for_tool

        by_tool: dict[str, set[str]] = {}
        for pack_name, names in collect_tools().pack_tool_groups.items():
            for name in names:
                by_tool.setdefault(name, set()).add(pack_name)
        for name in tuple(by_tool):
            by_tool[name].update(static_pack_names_for_tool(name))
        _REGISTERED_TOOL_PACK_NAMES = {
            name: tuple(sorted(pack_names))
            for name, pack_names in by_tool.items()
        }

    if tool_name in _REGISTERED_TOOL_PACK_NAMES:
        return _REGISTERED_TOOL_PACK_NAMES[tool_name]

    from app.tools.packs import static_pack_names_for_tool

    return static_pack_names_for_tool(tool_name)
