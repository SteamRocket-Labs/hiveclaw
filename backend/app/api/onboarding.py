"""Selected-company configuration facts; runtime readiness is checked separately."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_scoped_business_admin
from app.core.security import get_current_admin
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.llm import LLMModel
from app.models.org import OrgDepartment
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User

router = APIRouter(prefix="/enterprise", tags=["onboarding"])


@router.get("/onboarding-status")
async def get_onboarding_status(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Keep the legacy five configuration items without claiming runtime success."""
    try:
        target = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
        if not is_scoped_business_admin(current_user, resource_tenant_id=target):
            raise HTTPException(403, "Company administrator access required")
        tenant = await db.get(Tenant, target)
        if tenant is None:
            raise HTTPException(404, "Company not found")
        if not tenant.is_active:
            raise HTTPException(409, "Company is inactive")
        setting = await db.scalar(select(TenantSetting).where(
            TenantSetting.tenant_id == target, TenantSetting.key == "oidc_config"))
        oidc = (setting.value or {}) if setting else {}
        model_count = await db.scalar(select(func.count()).select_from(LLMModel).where(
            LLMModel.tenant_id == target, LLMModel.enabled.is_(True)))
        live_agents = (Agent.tenant_id == target, Agent.deleted_at.is_(None), Agent.agent_class != "internal_system")
        employees = (await db.execute(select(Agent.id, Agent.name, Agent.agent_type).where(
            *live_agents).order_by(Agent.name, Agent.id))).all()
        channel_count = await db.scalar(select(func.count()).select_from(ChannelConfig).where(
            ChannelConfig.tenant_id == target, ChannelConfig.is_configured.is_(True),
            ChannelConfig.agent_id.in_(select(Agent.id).where(*live_agents))))
        org_count = await db.scalar(select(func.count()).select_from(OrgDepartment).where(OrgDepartment.tenant_id == target))
    except SQLAlchemyError as exc:
        raise HTTPException(503, {"code": "onboarding_status_unavailable",
            "message": "Company setup could not be read. Refresh when the database is available."}) from exc

    items = [
        {"key": "sso", "title": "Configure SSO", "completed": bool(oidc.get("issuer_url") and oidc.get("client_id")),
            "required": False, "link": None},
        {"key": "llm", "title": "Add LLM Model", "completed": bool(model_count),
            "required": False, "link": "/enterprise/llm"},
        {"key": "agent", "title": "Create First Agent", "completed": bool(employees),
            "required": True, "link": "/agents/new"},
        {"key": "channel", "title": "Configure Channel", "completed": bool(channel_count),
            "required": False, "link": "/enterprise/org"},
        {"key": "org", "title": "Sync Org Structure", "completed": bool(org_count),
            "required": False, "link": "/enterprise/org"},
    ]
    return {"tenant_id": str(target), "tenant_name": tenant.name, "authenticated_user_id": str(current_user.id),
        "configuration_only": True, "items": items, "completed": sum(row["completed"] for row in items), "total": len(items),
        "employees": [{"id": str(row.id), "name": row.name, "agent_type": row.agent_type} for row in employees]}
