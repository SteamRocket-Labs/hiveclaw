"""Legacy tenant plugin projection APIs.

Enterprise-admin compatibility routes for installed plugin projections backed by
legacy pack manifests. New external capability installs must enter the External
Capability Trust Gate and Catalog flow first. This surface remains fail-closed
for legacy builtin/local projections and migration backfill.

The service layer owns its own ``tenant_scoped_session`` (RLS-bound writes), so
these handlers do not take a ``get_db`` dependency.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import uuid

from app.core.security import get_current_admin
from app.models.user import User
from app.services.plugin_install_service import (
    PluginInstallError,
    backfill_tenant_plugins,
    install_plugin,
    list_installed_plugins,
    set_agent_plugin_assignment,
    uninstall_plugin,
)

router = APIRouter(tags=["plugins"])


class PluginInstallIn(BaseModel):
    plugin_key: str
    config: dict | None = None
    agent_ids: list[str] | None = None


class PluginUninstallIn(BaseModel):
    plugin_key: str


class AgentPluginAssignmentIn(BaseModel):
    enabled: bool


@router.get("/enterprise/plugins")
async def list_enterprise_plugins(current_user: User = Depends(get_current_admin)):
    """List legacy plugin projections installed for the tenant."""
    if not current_user.tenant_id:
        return []
    return await list_installed_plugins(current_user.tenant_id)


@router.post("/enterprise/plugins/install")
async def install_enterprise_plugin(data: PluginInstallIn, current_user: User = Depends(get_current_admin)):
    """Install or idempotently re-install a legacy builtin/local plugin projection."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await install_plugin(
            current_user.tenant_id,
            data.plugin_key,
            config=data.config,
            agent_ids=[uuid.UUID(agent_id) for agent_id in data.agent_ids] if data.agent_ids else None,
        )
    except PluginInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/enterprise/plugins/uninstall")
async def uninstall_enterprise_plugin(data: PluginUninstallIn, current_user: User = Depends(get_current_admin)):
    """Uninstall a legacy plugin projection from the tenant."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        removed = await uninstall_plugin(current_user.tenant_id, data.plugin_key)
    except PluginInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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


@router.put("/agents/{agent_id}/plugins/{plugin_key}")
async def set_agent_plugin(
    agent_id: str,
    plugin_key: str,
    data: AgentPluginAssignmentIn,
    current_user: User = Depends(get_current_admin),
):
    """Enable/disable one installed plugin for one agent."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        agent_uuid = uuid.UUID(agent_id)
        return await set_agent_plugin_assignment(
            current_user.tenant_id,
            agent_uuid,
            plugin_key,
            enabled=data.enabled,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid agent id")
    except PluginInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
