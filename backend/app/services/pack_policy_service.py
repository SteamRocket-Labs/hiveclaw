"""Legacy tenant pack policy storage and filtering.

Runtime callers should use ``capability_group_policy_service``. This module
remains the migration-compatible backing store for existing SystemSetting keys
and installed plugin rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_settings import SystemSetting
from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs


_MANIFEST_DEFAULT_ENABLEMENT: dict[str, bool] | None = None
def tenant_pack_policy_key(tenant_id: uuid.UUID) -> str:
    return f"tenant:{tenant_id}:pack_policies"


async def get_tenant_pack_policies(db: AsyncSession, tenant_id: uuid.UUID | None) -> dict[str, bool]:
    if not tenant_id:
        return {}
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == tenant_pack_policy_key(tenant_id)))
    setting = result.scalar_one_or_none()
    value = getattr(setting, "value", None) or {}
    policies = value.get("packs", value)
    explicit = dict(policies) if isinstance(policies, dict) else {}

    # Step 5: an installed plugin (TenantInstalledPlugin, status=enabled) is enabled
    # unless the tenant explicitly overrode it. This is how a pack.yaml install
    # actually changes the runtime tool surface (e.g. installing mcp_admin_pack —
    # default_state=inactive — makes its tools turn-1 visible). An uninstalled pack
    # falls back to its manifest default, so no tenant is silently grayed out.
    #
    # Read on a DEDICATED tenant-scoped session so this merge never perturbs the
    # caller's session/transaction (and unit tests that mock the caller db keep
    # working). Falls back to explicit policies if the table is absent
    # (pre-migration) or there is no live DB.
    try:
        from app.database import tenant_scoped_session
        from app.models.installed_plugin import TenantInstalledPlugin

        async with tenant_scoped_session(tenant_id) as plugin_db:
            installed = (
                (
                    await plugin_db.execute(
                        select(TenantInstalledPlugin.plugin_key).where(
                            TenantInstalledPlugin.tenant_id == tenant_id,
                            TenantInstalledPlugin.status == "enabled",
                        )
                    )
                )
                .scalars()
                .all()
            )
        merged = dict(explicit)
        for key in installed:
            merged.setdefault(key, True)
        return merged
    except Exception:
        return explicit


async def get_agent_pack_policies(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
) -> dict[str, bool]:
    """Return pack policies for one agent.

    Installed plugins are only visible when the specific agent has an enabled
    ``AgentPluginAssignment``. Tenant SystemSetting policies remain as a legacy
    compatibility override, but they no longer grant plugin visibility by
    themselves.
    """
    if not tenant_id or not agent_id:
        return {}

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == tenant_pack_policy_key(tenant_id)))
    setting = result.scalar_one_or_none()
    value = getattr(setting, "value", None) or {}
    policies = value.get("packs", value)
    explicit = dict(policies) if isinstance(policies, dict) else {}

    try:
        from app.database import tenant_scoped_session
        from app.models.installed_plugin import AgentPluginAssignment, TenantInstalledPlugin

        async with tenant_scoped_session(tenant_id) as plugin_db:
            installed = (
                (
                    await plugin_db.execute(
                        select(TenantInstalledPlugin.plugin_key)
                        .join(
                            AgentPluginAssignment,
                            AgentPluginAssignment.installed_plugin_id == TenantInstalledPlugin.id,
                        )
                        .where(
                            TenantInstalledPlugin.tenant_id == tenant_id,
                            TenantInstalledPlugin.status == "enabled",
                            AgentPluginAssignment.tenant_id == tenant_id,
                            AgentPluginAssignment.agent_id == agent_id,
                            AgentPluginAssignment.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        installed = []

    merged = dict(explicit)
    for key in installed:
        merged.setdefault(key, True)
    return merged


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

    The governance taxonomy owns the CORE-vs-L2 boundary. Keep this function as
    the storage-layer compatibility entrypoint, but do not infer policy packs
    directly from runtime groups here.
    """
    from app.services.governance_capability_taxonomy import taxonomy_policy_pack_names_for_tool

    return taxonomy_policy_pack_names_for_tool(tool_name)
