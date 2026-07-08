from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.user import User
from app.services.external_capabilities.activation import (
    activate_external_extension_for_agent,
    deactivate_external_extension_for_agent,
)
from app.services.external_capabilities.trust_gate import (
    approve_external_capability_snapshot,
    list_external_capability_reviews,
    list_external_extension_catalog_entries,
    reject_external_capability_review,
    revoke_external_capability_snapshot,
    stage_external_capability_review,
)
from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle

router = APIRouter(tags=["external-capabilities"])


class ExternalCapabilityComponentIn(BaseModel):
    component_type: Literal["slash_command", "skill", "subagent", "hook", "mcp_server"]
    local_name: str
    qualified_name: str
    source_path: str
    content_sha256: str
    runtime_projection: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ignored_fields: list[str] = Field(default_factory=list)

    def to_component(self) -> ExternalCapabilityComponent:
        return ExternalCapabilityComponent(
            component_type=self.component_type,
            local_name=self.local_name,
            qualified_name=self.qualified_name,
            source_path=self.source_path,
            content_sha256=self.content_sha256,
            runtime_projection=dict(self.runtime_projection),
            metadata=dict(self.metadata),
            ignored_fields=tuple(self.ignored_fields),
        )


class ExternalCapabilityReviewIn(BaseModel):
    source_format: str
    source_uri: str
    plugin_name: str
    version: str | None = None
    description: str | None = None
    manifest_sha256: str | None = None
    components: list[ExternalCapabilityComponentIn] = Field(default_factory=list)
    unsupported_components: list[dict[str, Any]] = Field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = Field(default_factory=list)
    admission_notes: list[dict[str, Any]] = Field(default_factory=list)

    def to_bundle(self) -> NormalizedExternalPluginBundle:
        return NormalizedExternalPluginBundle(
            source_format=self.source_format,
            source_uri=self.source_uri,
            plugin_name=self.plugin_name,
            version=self.version,
            description=self.description,
            manifest_sha256=self.manifest_sha256,
            components=[component.to_component() for component in self.components],
            unsupported_components=list(self.unsupported_components),
            credential_requirements=list(self.credential_requirements),
            admission_notes=list(self.admission_notes),
        )


class ExternalCapabilityRejectIn(BaseModel):
    reason: str | None = None


@router.get("/enterprise/external-capabilities/reviews")
async def list_external_capability_reviews_route(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return await list_external_capability_reviews(db, tenant_id=current_user.tenant_id)


@router.get("/enterprise/external-capabilities/catalog")
async def list_external_extension_catalog_route(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return await list_external_extension_catalog_entries(db, tenant_id=current_user.tenant_id)


@router.post("/enterprise/external-capabilities/reviews")
async def stage_external_capability_review_route(
    data: ExternalCapabilityReviewIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return await stage_external_capability_review(
        db,
        tenant_id=current_user.tenant_id,
        created_by_user_id=current_user.id,
        bundle=data.to_bundle(),
    )


@router.post("/enterprise/external-capabilities/reviews/{review_id}/approve")
async def approve_external_capability_review_route(
    review_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await approve_external_capability_snapshot(
            db,
            tenant_id=current_user.tenant_id,
            review_id=review_id,
            approved_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/enterprise/external-capabilities/reviews/{review_id}/reject")
async def reject_external_capability_review_route(
    review_id: uuid.UUID,
    data: ExternalCapabilityRejectIn,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await reject_external_capability_review(
            db,
            tenant_id=current_user.tenant_id,
            review_id=review_id,
            rejected_by_user_id=current_user.id,
            reason=data.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/enterprise/external-capabilities/snapshots/{snapshot_id}/revoke")
async def revoke_external_capability_snapshot_route(
    snapshot_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    try:
        return await revoke_external_capability_snapshot(
            db,
            tenant_id=current_user.tenant_id,
            snapshot_id=snapshot_id,
            revoked_by_user_id=current_user.id,
            agent_data_root=Path(get_settings().AGENT_DATA_DIR),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/external-extensions/catalog")
async def list_agent_external_extension_catalog_route(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    tenant_id = getattr(agent, "tenant_id", None) or current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    return await list_external_extension_catalog_entries(db, tenant_id=tenant_id)


@router.post("/agents/{agent_id}/external-extensions/{snapshot_id}/activate")
async def activate_external_extension_route(
    agent_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    tenant_id = getattr(agent, "tenant_id", None) or current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    try:
        return await activate_external_extension_for_agent(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            snapshot_id=snapshot_id,
            workspace=workspace,
            activated_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/agents/{agent_id}/external-extensions/{snapshot_id}/deactivate")
async def deactivate_external_extension_route(
    agent_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    tenant_id = getattr(agent, "tenant_id", None) or current_user.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="No tenant assigned")
    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    try:
        return await deactivate_external_extension_for_agent(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            snapshot_id=snapshot_id,
            workspace=workspace,
            deactivated_by_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
