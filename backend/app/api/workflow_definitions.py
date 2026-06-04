"""Registered workflow definition REST API (§9 P6) — thin shell over
:class:`WorkflowDefinitionService`. Tenant comes from the authenticated user;
typed service errors map onto status codes (PermissionError → 403,
lifecycle/visibility violations → 409, not-found phrasing → 404)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.workflow_definitions import WorkflowDefinitionError, WorkflowDefinitionService

router = APIRouter(prefix="/workflow-definitions", tags=["workflow-definitions"])


def get_workflow_definition_service() -> WorkflowDefinitionService:
    return WorkflowDefinitionService()


class DefinitionCreateRequest(BaseModel):
    definition: dict[str, Any]
    visibility_scope: str = "agent"
    owner_type: str = "user"
    owner_id: uuid.UUID | None = None
    call_policy: dict[str, Any] | None = None


class PromotionApprovalRequest(BaseModel):
    pass


class ForkRequest(BaseModel):
    agent_id: uuid.UUID
    version: int | None = None
    patch: dict[str, Any] = Field(default_factory=dict)


def _record_payload(record) -> dict:
    return {
        "id": str(record.id),
        "name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "status": record.status,
        "visibility_scope": record.visibility_scope,
        "owner_type": record.owner_type,
        "owner_id": str(record.owner_id) if record.owner_id else None,
        "call_policy": record.call_policy,
        "promoted_from_run_id": str(record.promoted_from_run_id) if record.promoted_from_run_id else None,
    }


def _raise_mapped(exc: WorkflowDefinitionError) -> None:
    message = str(exc)
    if "not found" in message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message) from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message) from exc


def _is_definition_admin(user: User) -> bool:
    return getattr(user, "role", None) in ("platform_admin", "org_admin")


def _require_definition_admin(user: User) -> None:
    if not _is_definition_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workflow definition management requires admin or owning-agent manage access",
        )


async def _require_agent_manage(db: AsyncSession, user: User, agent_id: uuid.UUID) -> None:
    _agent, access_level = await check_agent_access(db, user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Managing this workflow requires agent manage access")


async def _authorize_create_definition(
    db: AsyncSession,
    user: User,
    *,
    visibility_scope: str,
    owner_type: str,
    owner_id: uuid.UUID | None,
) -> None:
    if _is_definition_admin(user):
        return
    if visibility_scope == "agent" and owner_type == "agent" and owner_id is not None:
        await _require_agent_manage(db, user, owner_id)
        return
    _require_definition_admin(user)


async def _authorize_record_management(db: AsyncSession, user: User, record) -> None:
    if _is_definition_admin(user):
        return
    if record.visibility_scope == "agent" and record.owner_type == "agent" and record.owner_id is not None:
        await _require_agent_manage(db, user, record.owner_id)
        return
    _require_definition_admin(user)


@router.post("")
async def create_definition_draft(
    payload: DefinitionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    owner_id = payload.owner_id
    await _authorize_create_definition(
        db,
        current_user,
        visibility_scope=payload.visibility_scope,
        owner_type=payload.owner_type,
        owner_id=owner_id,
    )
    try:
        record = await service.create_draft(
            tenant_id=current_user.tenant_id,
            definition_data=payload.definition,
            created_by_user_id=current_user.id,
            visibility_scope=payload.visibility_scope,
            owner_type=payload.owner_type,
            owner_id=owner_id,
            call_policy=payload.call_policy,
        )
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return _record_payload(record)


@router.get("")
async def list_definitions(
    agent_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> list[dict]:
    if agent_id is not None:
        await check_agent_access(db, current_user, agent_id)
    else:
        _require_definition_admin(current_user)
    records = await service.list_definitions(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
    )
    return [_record_payload(record) for record in records]


@router.post("/{definition_id}/activate")
async def activate_definition(
    definition_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    try:
        existing = await service.get_record(definition_id, tenant_id=current_user.tenant_id)
        await _authorize_record_management(db, current_user, existing)
        record = await service.activate(definition_id, tenant_id=current_user.tenant_id, actor_user_id=current_user.id)
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return _record_payload(record)


@router.post("/{definition_id}/deprecate")
async def deprecate_definition(
    definition_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    try:
        existing = await service.get_record(definition_id, tenant_id=current_user.tenant_id)
        await _authorize_record_management(db, current_user, existing)
        record = await service.deprecate(definition_id, tenant_id=current_user.tenant_id)
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return _record_payload(record)


@router.post("/{definition_id}/revoke")
async def revoke_definition(
    definition_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    try:
        existing = await service.get_record(definition_id, tenant_id=current_user.tenant_id)
        await _authorize_record_management(db, current_user, existing)
        record = await service.revoke(definition_id, tenant_id=current_user.tenant_id)
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return _record_payload(record)


@router.post("/{definition_id}/approve-promotion")
async def approve_promotion(
    definition_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    try:
        existing = await service.get_record(definition_id, tenant_id=current_user.tenant_id)
        await _authorize_record_management(db, current_user, existing)
        record = await service.approve_promotion(
            definition_id, tenant_id=current_user.tenant_id, approver_user_id=current_user.id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return _record_payload(record)


@router.post("/{definition_id}/fork")
async def fork_definition(
    definition_id: uuid.UUID,
    payload: ForkRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    service: WorkflowDefinitionService = Depends(get_workflow_definition_service),
) -> dict:
    try:
        if not _is_definition_admin(current_user):
            await check_agent_access(db, current_user, payload.agent_id)
        forked = await service.fork_record_to_ephemeral(
            tenant_id=current_user.tenant_id,
            definition_id=definition_id,
            agent_id=payload.agent_id,
            expected_version=payload.version,
            patch=payload.patch or None,
        )
    except WorkflowDefinitionError as exc:
        _raise_mapped(exc)
    return {"definition": forked, "source_definition_id": str(definition_id)}
