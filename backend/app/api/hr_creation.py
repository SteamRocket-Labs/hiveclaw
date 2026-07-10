"""Authenticated user decisions for canonical HR creation drafts."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.hr_creation_service import (
    HrCreationConflict,
    confirm_hr_creation_draft_record,
    hr_creation_draft_payload,
    load_hr_creation_draft,
    reject_hr_creation_draft_record,
)

router = APIRouter(prefix="/agents", tags=["hr-creation"])


class HrCreationConfirmIn(BaseModel):
    blueprint_version: int = Field(ge=1)
    blueprint_hash: str = Field(min_length=8, max_length=80)


class HrCreationDraftOut(BaseModel):
    blueprint_id: str
    blueprint_version: int
    blueprint_hash: str
    draft_status: str
    blueprint: dict[str, Any]
    summary: dict[str, Any] = Field(default_factory=dict)
    ready_now: list[str] = Field(default_factory=list)
    will_install: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)
    confirmed_by_user_id: str | None = None
    confirmed_at: str | None = None
    created_agent_id: str | None = None
    provisioning: dict[str, Any] = Field(default_factory=dict)
    failure: dict[str, Any] | None = None


def _out(payload: dict[str, Any]) -> HrCreationDraftOut:
    return HrCreationDraftOut.model_validate(payload)


async def _load_for_user(
    *,
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    for_update: bool,
):
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if agent.agent_class != "internal_system" or agent.name != "__system_hr__":
        raise HTTPException(status_code=404, detail="HR creation draft not found")
    try:
        return await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=agent.tenant_id,
            hr_agent_id=agent_id,
            requested_by_user_id=current_user.id,
            for_update=for_update,
        )
    except HrCreationConflict as exc:
        status_code = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status_code, detail={"error": exc.code, "message": exc.message}) from exc


@router.get("/{agent_id}/hr-creation-drafts/{draft_id}", response_model=HrCreationDraftOut)
async def get_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=False
    )
    return _out(hr_creation_draft_payload(draft))


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/confirm", response_model=HrCreationDraftOut)
async def confirm_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: HrCreationConfirmIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=True
    )
    try:
        confirm_hr_creation_draft_record(
            draft,
            confirming_user_id=current_user.id,
            blueprint_version=payload.blueprint_version,
            blueprint_hash=payload.blueprint_hash,
        )
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc

    from app.core.policy import write_audit_event

    await write_audit_event(
        db,
        event_type="hr.creation_blueprint_confirmed",
        severity="info",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=draft.tenant_id,
        action="confirm_hr_creation_blueprint",
        resource_type="hr_creation_draft",
        resource_id=draft.id,
        details={"blueprint_version": draft.blueprint_version, "blueprint_hash": draft.blueprint_hash},
    )
    await db.commit()
    return _out(hr_creation_draft_payload(draft))


@router.post("/{agent_id}/hr-creation-drafts/{draft_id}/reject", response_model=HrCreationDraftOut)
async def reject_hr_creation_draft(
    agent_id: uuid.UUID,
    draft_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await _load_for_user(
        db=db, current_user=current_user, agent_id=agent_id, draft_id=draft_id, for_update=True
    )
    try:
        reject_hr_creation_draft_record(draft, rejecting_user_id=current_user.id)
    except HrCreationConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": exc.message}) from exc
    await db.commit()
    return _out(hr_creation_draft_payload(draft))
