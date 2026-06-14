"""Tenant plugin install/management APIs (Step 5).

Enterprise-admin routes to install / list / uninstall capability packs (pack.yaml
manifests) for a tenant, mirroring the MCP server-first surface. Install is
fail-closed (validated manifest + builtin/local source only). Backfill installs
all default-active packs so existing tenants keep their current capabilities.

The service layer owns its own ``tenant_scoped_session`` (RLS-bound writes), so
these handlers do not take a ``get_db`` dependency.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import get_current_admin
from app.models.user import User
from app.services.plugin_install_service import (
    PluginInstallError,
    backfill_tenant_plugins,
    install_plugin,
    list_installed_plugins,
    uninstall_plugin,
)

router = APIRouter(tags=["plugins"])


class PluginInstallIn(BaseModel):
    plugin_key: str
    config: dict | None = None


class PluginUninstallIn(BaseModel):
    plugin_key: str


@router.get("/enterprise/plugins")
async def list_enterprise_plugins(current_user: User = Depends(get_current_admin)):
    """List capability packs installed for the tenant."""
    if not current_user.tenant_id:
        return []
    return await list_installed_plugins(current_user.tenant_id)


@router.post("/enterprise/plugins/install")
async def install_enterprise_plugin(data: PluginInstallIn, current_user: User = Depends(get_current_admin)):
    """Install (or idempotently re-install) a capability pack for the tenant."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await install_plugin(current_user.tenant_id, data.plugin_key, config=data.config)
    except PluginInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/enterprise/plugins/uninstall")
async def uninstall_enterprise_plugin(data: PluginUninstallIn, current_user: User = Depends(get_current_admin)):
    """Uninstall a capability pack from the tenant (CASCADE removes assignments/hooks)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    removed = await uninstall_plugin(current_user.tenant_id, data.plugin_key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"plugin {data.plugin_key!r} not installed")
    return {"ok": True, "plugin_key": data.plugin_key}


@router.post("/enterprise/plugins/backfill")
async def backfill_enterprise_plugins(current_user: User = Depends(get_current_admin)):
    """Install all default-active packs the tenant does not yet have (idempotent)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    newly = await backfill_tenant_plugins(current_user.tenant_id)
    return {"ok": True, "installed": newly}
