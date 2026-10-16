"""Email Channel adapter — thin CRUD that stores config in agent tool config.

Provides the same /agents/{id}/email-channel API shape as other channels,
but persists to AgentTool.config (send_email tool) instead of ChannelConfig.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.tool import Tool, AgentTool
from app.models.user import User

router = APIRouter(tags=["email-channel"])


class MailboxCreateIn(BaseModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class MailboxSaveIn(BaseModel):
    expected_version: int = Field(ge=1)
    config: dict
    source_agent_id: uuid.UUID | None = None


class MailboxGrantIn(BaseModel):
    expected_version: int = Field(ge=0)
    enabled: bool


class MailboxRevokeIn(BaseModel):
    expected_version: int = Field(ge=1)


async def _owned_mailbox(db, user, account_id):
    from app.core.tenant_scope import resolve_and_pin_tenant_scope
    from app.services.email_accounts import owned_account

    tenant_id = await resolve_and_pin_tenant_scope(db, user)
    return await owned_account(db, user=user, tenant_id=tenant_id, account_id=account_id)


@router.get("/agents/{agent_id}/email-accounts")
async def list_email_accounts(agent_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.email_accounts import list_accounts

    agent, _access = await check_agent_access(db, current_user, agent_id)
    return await list_accounts(db, user=current_user, agent=agent)


@router.post("/agents/{agent_id}/email-accounts")
async def create_email_account(agent_id: uuid.UUID, data: MailboxCreateIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.permissions import require_agent_execution_access
    from app.models.email_account import EmailAccount
    from app.services.email_accounts import account_public, active_user

    agent, _access = await require_agent_execution_access(db, current_user, agent_id)
    await active_user(db, current_user.id, agent.tenant_id)
    name = data.name.strip()
    if not name:
        raise HTTPException(422, "Mailbox name required")
    account = await db.get(EmailAccount, data.id)
    if account is None:
        try:
            async with db.begin_nested():
                account = EmailAccount(id=data.id, tenant_id=agent.tenant_id, owner_user_id=current_user.id, name=name, config={})
                db.add(account)
                await db.flush()
        except IntegrityError as exc:
            account = await db.get(EmailAccount, data.id)
            if account is None:
                raise HTTPException(409, "Mailbox creation conflicts; read back before retrying") from exc
    if (account.tenant_id, account.owner_user_id, account.name) != (agent.tenant_id, current_user.id, name):
        raise HTTPException(409, "Mailbox creation identity has different inputs")
    await db.commit()
    return account_public(account)


@router.get("/email-accounts/{account_id}")
async def get_email_account(account_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.email_accounts import account_public, EMAIL_SCHEMA, personal_config_schema
    from app.services.tool_config_service import mask_agent_tool_config_secrets

    account = await _owned_mailbox(db, current_user, account_id)
    return {**account_public(account), "config": mask_agent_tool_config_secrets(account.config, EMAIL_SCHEMA), "config_schema": personal_config_schema()}


@router.put("/email-accounts/{account_id}")
async def save_email_account(account_id: uuid.UUID, data: MailboxSaveIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.email_accounts import save_account

    account = await _owned_mailbox(db, current_user, account_id)
    result = await save_account(db, account=account, config=data.config, expected_version=data.expected_version, source_agent_id=data.source_agent_id)
    await db.commit()
    return result


@router.put("/email-accounts/{account_id}/grants/{agent_id}/{requester_id}")
async def grant_email_account(account_id: uuid.UUID, agent_id: uuid.UUID, requester_id: uuid.UUID, data: MailboxGrantIn,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.core.permissions import require_agent_execution_access
    from app.services.email_accounts import owned_account, set_grant

    agent, _access = await require_agent_execution_access(db, current_user, agent_id)
    account = await owned_account(db, user=current_user, tenant_id=agent.tenant_id, account_id=account_id)
    result = await set_grant(db, account=account, agent_id=agent_id, requester_id=requester_id,
        enabled=data.enabled, expected_version=data.expected_version)
    await db.commit()
    return result


@router.post("/email-accounts/{account_id}/revoke")
async def revoke_email_account(account_id: uuid.UUID, data: MailboxRevokeIn, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.models.email_account import EmailAccountGrant
    from app.services.email_accounts import account_public

    account = await _owned_mailbox(db, current_user, account_id)
    grants = (await db.scalars(select(EmailAccountGrant).where(EmailAccountGrant.account_id == account.id))).all()
    if data.expected_version != account.version:
        if data.expected_version + 1 == account.version and not account.config.get("auth_code") and not any(grant.enabled for grant in grants):
            return account_public(account)
        raise HTTPException(409, "Mailbox changed; review its current version before deactivating it")
    account.config = {key: value for key, value in account.config.items() if key != "auth_code"}
    account.version += 1
    for grant in grants:
        grant.enabled = False
        grant.version += 1
    await db.commit()
    return account_public(account)


async def _get_email_agent_tool(db: AsyncSession, agent_id: uuid.UUID) -> AgentTool | None:
    """Find the AgentTool row for send_email."""
    r = await db.execute(select(Tool).where(Tool.name == "send_email"))
    tool = r.scalar_one_or_none()
    if not tool:
        return None
    at_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))
    return at_r.scalar_one_or_none()


@router.post("/agents/{agent_id}/email-channel", status_code=201)
async def configure_email_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await require_agent_manage_access(db, current_user, agent_id)

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
        at_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))
        at = at_r.scalar_one_or_none()
        if at:
            if at.tenant_id is None:
                at.tenant_id = agent.tenant_id
            at.config = {**(at.config or {}), **email_config}
        else:
            db.add(
                AgentTool(
                    agent_id=agent_id,
                    tenant_id=agent.tenant_id,
                    tool_id=tool.id,
                    enabled=True,
                    config=email_config,
                )
            )
    await db.flush()

    return {
        "id": str(agent_id),
        "agent_id": str(agent_id),
        "channel_type": "email",
        "is_configured": bool(email_config["email_address"] and email_config["auth_code"]),
        "app_id": email_config["email_provider"],
        "app_secret": "***" if email_config["auth_code"] else None,
        "extra_config": {
            "email_address": email_config["email_address"],
            "email_provider": email_config["email_provider"],
        },
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
        "extra_config": {
            "email_address": cfg.get("email_address", ""),
            "email_provider": cfg.get("email_provider", ""),
        },
    }


@router.delete("/agents/{agent_id}/email-channel", status_code=204)
async def delete_email_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_agent_manage_access(db, current_user, agent_id)
    for tool_name in ("send_email", "read_emails", "reply_email"):
        r = await db.execute(select(Tool).where(Tool.name == tool_name))
        tool = r.scalar_one_or_none()
        if not tool:
            continue
        at_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))
        at = at_r.scalar_one_or_none()
        if at and at.config:
            at.config = {
                k: v for k, v in at.config.items() if k not in ("email_provider", "email_address", "auth_code")
            }
