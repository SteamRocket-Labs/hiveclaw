"""Tenant-admin control surface for external channel principals."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.external_principal import ExternalPrincipal
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.external_principal_service import (
    ExternalPrincipalAuthorityError,
    ExternalPrincipalRevokedError,
    unlink_external_principal,
)


router = APIRouter(tags=["external-principals"])


def _require_org_admin(current_user: User) -> None:
    """Company administrator gate (PDEC-013): org admins and scoped platform
    administrators both manage company external identities; tenant binding is
    enforced per-route by ``resolve_and_pin_tenant_scope``."""
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="Organization administrator access required")


class ExternalPrincipalOut(BaseModel):
    id: uuid.UUID
    provider: str
    installation_ref: str
    channel_config_id: uuid.UUID | None
    subject_id: str
    display_name: str
    linked_user_id: uuid.UUID | None
    binding_method: str | None
    binding_verified_at: datetime | None
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    linked_at: datetime | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class ExternalPrincipalUnlinkIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail="External principal not found")
    if isinstance(exc, ExternalPrincipalRevokedError):
        return HTTPException(status_code=409, detail="External principal is revoked")
    if isinstance(exc, ExternalPrincipalAuthorityError):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.get("/enterprise/external-principals", response_model=list[ExternalPrincipalOut])
async def list_external_principals(
    tenant_id: str | None = None,
    provider: str | None = Query(default=None, min_length=1, max_length=40),
    status: Literal["active", "revoked"] | None = None,
    linked: bool | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List external identities without projecting them as licensed members."""
    _require_org_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    query = select(ExternalPrincipal).where(ExternalPrincipal.tenant_id == target_tenant_id)
    if provider:
        query = query.where(ExternalPrincipal.provider == provider.strip().lower())
    if status:
        query = query.where(ExternalPrincipal.status == status)
    if linked is True:
        query = query.where(ExternalPrincipal.linked_user_id.is_not(None))
    elif linked is False:
        query = query.where(ExternalPrincipal.linked_user_id.is_(None))
    result = await db.execute(query.order_by(ExternalPrincipal.last_seen_at.desc(), ExternalPrincipal.id))
    return [ExternalPrincipalOut.model_validate(item) for item in result.scalars().all()]


@router.post("/enterprise/external-principals/{principal_id}/unlink", response_model=ExternalPrincipalOut)
async def unlink_external_principal_route(
    principal_id: uuid.UUID,
    data: ExternalPrincipalUnlinkIn,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_org_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        resolution = await unlink_external_principal(
            db,
            tenant_id=target_tenant_id,
            principal_id=principal_id,
            actor_user_id=current_user.id,
            reason=data.reason,
        )
    except (LookupError, ExternalPrincipalRevokedError, ExternalPrincipalAuthorityError) as exc:
        raise _service_error(exc) from exc
    # Stop the live transport only when this unlink actually invalidated the
    # channel self identity — the service's typed signal, not a route-side
    # reconstruction of that decision. A repeated or never-bound unlink keeps
    # a healthy channel configured and its live client running.
    transport_agent_id = None
    if (
        resolution.principal.provider in {"wechat_personal", "feishu"}
        and resolution.principal.channel_config_id
        and resolution.channel_identity_invalidated
    ):
        config = await db.get(ChannelConfig, resolution.principal.channel_config_id)
        transport_agent_id = config.agent_id if config is not None else None
    await db.commit()
    if transport_agent_id is not None and resolution.principal.provider == "wechat_personal":
        from app.services.wechat_personal_stream import wechat_personal_stream_manager

        await wechat_personal_stream_manager.stop_client(transport_agent_id)
    elif transport_agent_id is not None and resolution.principal.provider == "feishu":
        from app.services.feishu_ws import feishu_ws_manager

        await feishu_ws_manager.stop_client(transport_agent_id)
    return ExternalPrincipalOut.model_validate(resolution.principal)


__all__ = ["router"]
