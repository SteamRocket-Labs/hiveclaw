"""Email Channel adapter — thin CRUD that stores config in agent tool config.

Provides the same /agents/{id}/email-channel API shape as other channels,
but persists to AgentTool.config (send_email tool) instead of ChannelConfig.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.tool import Tool, AgentTool
from app.models.user import User

router = APIRouter(tags=["email-channel"])


async def _get_email_agent_tool(db: AsyncSession, agent_id: uuid.UUID) -> AgentTool | None:
    """Find the AgentTool row for send_email."""
    r = await db.execute(select(Tool).where(Tool.name == "send_email"))
    tool = r.scalar_one_or_none()
    if not tool:
        return None
    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
    )
    return at_r.scalar_one_or_none()


@router.post("/agents/{agent_id}/email-channel", status_code=201)
async def configure_email_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure email")

    email_config = {
        "email_provider": data.get("email_provider", "gmail"),
        "email_address": data.get("email_address", ""),
        "auth_code": data.get("auth_code", ""),
    }

    # Save to all 3 email tools
    for tool_name in ("send_email", "read_emails", "reply_email"):
        r = await db.execute(select(Tool).where(Tool.name == tool_name))
        tool = r.scalar_one_or_none()
        if not tool:
            continue
        at_r = await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
        )
        at = at_r.scalar_one_or_none()
        if at:
            at.config = {**(at.config or {}), **email_config}
        else:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True, config=email_config))
    await db.flush()

    return {
        "id": str(agent_id),
        "agent_id": str(agent_id),
        "channel_type": "email",
        "is_configured": bool(email_config["email_address"] and email_config["auth_code"]),
        "app_id": email_config["email_provider"],
        "app_secret": "***" if email_config["auth_code"] else None,
        "extra_config": {"email_address": email_config["email_address"], "email_provider": email_config["email_provider"]},
    }


@router.get("/agents/{agent_id}/email-channel")
async def get_email_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    at = await _get_email_agent_tool(db, agent_id)
    cfg = (at.config or {}) if at else {}
    return {
        "id": str(agent_id),
        "agent_id": str(agent_id),
        "channel_type": "email",
        "is_configured": bool(cfg.get("email_address") and cfg.get("auth_code")),
        "app_id": cfg.get("email_provider", ""),
        "app_secret": "***" if cfg.get("auth_code") else None,
        "extra_config": {"email_address": cfg.get("email_address", ""), "email_provider": cfg.get("email_provider", "")},
    }


@router.delete("/agents/{agent_id}/email-channel", status_code=204)
async def delete_email_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove email config")
    for tool_name in ("send_email", "read_emails", "reply_email"):
        r = await db.execute(select(Tool).where(Tool.name == tool_name))
        tool = r.scalar_one_or_none()
        if not tool:
            continue
        at_r = await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
        )
        at = at_r.scalar_one_or_none()
        if at and at.config:
            at.config = {k: v for k, v in at.config.items() if k not in ("email_provider", "email_address", "auth_code")}
