"""Role Template management API (ARCHITECTURE.md Phase 5).

Role Templates define the default agent configuration for a department.
When an employee first logs in, their Main Agent is auto-provisioned
from the matching Role Template.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.database import get_db
from app.models.agent import AgentTemplate
from app.models.user import User
from app.services.sync_service import bump_sync_version

router = APIRouter(tags=["role-templates"])


# ─── Schemas ────────────────────────────────────────────


class RoleTemplateCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    category: str = "general"
    soul_template: str = ""
    default_skills: list = []
    department_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None


class RoleTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    category: str | None = None
    soul_template: str | None = None
    default_skills: list | None = None
    department_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None


class RoleTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    icon: str
    category: str
    soul_template: str
    default_skills: list
    department_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    config_version: int

    model_config = {"from_attributes": True}


# ─── Endpoints ──────────────────────────────────────────


@router.get("/role-templates", response_model=list[RoleTemplateOut])
async def list_role_templates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List Role Templates visible to the current tenant."""
    result = await db.execute(
        select(AgentTemplate).where(
            (AgentTemplate.tenant_id == current_user.tenant_id) | (AgentTemplate.is_builtin.is_(True))
        )
    )
    return [RoleTemplateOut.model_validate(t) for t in result.scalars().all()]


@router.post("/role-templates", response_model=RoleTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_role_template(
    body: RoleTemplateCreate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a Role Template for the current tenant (admin only)."""
    template = AgentTemplate(
        name=body.name,
        description=body.description,
        icon=body.icon,
        category=body.category,
        soul_template=body.soul_template,
        default_skills=body.default_skills,
        department_id=body.department_id,
        model_id=body.model_id,
        tenant_id=current_user.tenant_id,
        created_by=current_user.id,
        config_version=1,
    )
    db.add(template)
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)

    return RoleTemplateOut.model_validate(template)


@router.patch("/role-templates/{template_id}", response_model=RoleTemplateOut)
async def update_role_template(
    template_id: uuid.UUID,
    body: RoleTemplateUpdate,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a Role Template (admin only)."""
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    if template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your tenant's template")
    if template.is_builtin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify builtin templates")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    template.config_version += 1
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)

    return RoleTemplateOut.model_validate(template)


@router.delete("/role-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a Role Template (admin only)."""
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    if template.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your tenant's template")
    if template.is_builtin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete builtin templates")

    await db.delete(template)
    await db.flush()

    if current_user.tenant_id:
        await bump_sync_version(db, current_user.tenant_id)
