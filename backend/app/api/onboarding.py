"""Onboarding status API — 5-item checklist for new tenant setup."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise", tags=["onboarding"])


@router.get("/onboarding-status")
async def get_onboarding_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a 5-item onboarding checklist reflecting current tenant configuration.

    Items:
    1. SSO configured (OIDC or Feishu OAuth)
    2. At least one LLM model added
    3. At least one agent created
    4. At least one channel configured (Feishu/Slack/etc.)
    5. Org structure synced (at least one department or member)
    """
    tenant_id = current_user.tenant_id
    if not tenant_id:
        return {"items": [], "completed": 0, "total": 0}

    items = []

    # 1. SSO configured
    sso_ok = False
    try:
        from app.models.tenant_setting import TenantSetting

        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == "oidc_config",
            )
        )
        setting = result.scalar_one_or_none()
        if setting and setting.value and setting.value.get("issuer_url"):
            sso_ok = True
    except Exception:
        logger.debug("Failed to check SSO config", exc_info=True)
    items.append(
        {
            "key": "sso",
            "title": "Configure SSO",
            "completed": sso_ok,
            "link": "/enterprise?tab=sso",
        }
    )

    # 2. LLM model added
    llm_ok = False
    try:
        from app.models.llm import LLMModel

        result = await db.execute(
            select(func.count())
            .select_from(LLMModel)
            .where(
                LLMModel.tenant_id == tenant_id,
                LLMModel.enabled == True,  # noqa: E712
            )
        )
        llm_ok = (result.scalar() or 0) > 0
    except Exception:
        logger.debug("Failed to check LLM models", exc_info=True)
    items.append(
        {
            "key": "llm",
            "title": "Add LLM Model",
            "completed": llm_ok,
            "link": "/enterprise?tab=llm",
        }
    )

    # 3. Agent created
    agent_ok = False
    try:
        from app.models.agent import Agent

        result = await db.execute(select(func.count()).select_from(Agent).where(Agent.tenant_id == str(tenant_id)))
        agent_ok = (result.scalar() or 0) > 0
    except Exception:
        logger.debug("Failed to check agents", exc_info=True)
    items.append(
        {
            "key": "agent",
            "title": "Create First Agent",
            "completed": agent_ok,
            "link": "/agents/new",
        }
    )

    # 4. Channel configured
    channel_ok = False
    try:
        from app.models.channel_config import ChannelConfig
        from app.models.agent import Agent as AgentModel

        agent_ids = select(AgentModel.id).where(AgentModel.tenant_id == str(tenant_id))
        result = await db.execute(
            select(func.count())
            .select_from(ChannelConfig)
            .where(
                ChannelConfig.agent_id.in_(agent_ids),
                ChannelConfig.is_configured == True,  # noqa: E712
            )
        )
        channel_ok = (result.scalar() or 0) > 0
    except Exception:
        logger.debug("Failed to check channel configs", exc_info=True)
    items.append(
        {
            "key": "channel",
            "title": "Configure Channel",
            "completed": channel_ok,
            "link": "/enterprise?tab=org",
        }
    )

    # 5. Org structure synced
    org_ok = False
    try:
        from app.models.org import OrgDepartment

        result = await db.execute(
            select(func.count()).select_from(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id)
        )
        org_ok = (result.scalar() or 0) > 0
    except Exception:
        logger.debug("Failed to check org structure", exc_info=True)
    items.append(
        {
            "key": "org",
            "title": "Sync Org Structure",
            "completed": org_ok,
            "link": "/enterprise?tab=org",
        }
    )

    completed = sum(1 for i in items if i["completed"])
    return {"items": items, "completed": completed, "total": len(items)}
