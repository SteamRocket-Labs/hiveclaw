"""Mounted business-team routes for self-service and scoped-admin recovery."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import _platform_admin_selected_tenant_id
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.user import User
from app.schemas.schemas import TaskHandoverIn
from app.services.business_teams import (
    BusinessTeamError,
    add_member,
    affiliate_agent,
    create_admin_team,
    create_self_service_team,
    list_lead_candidates,
    list_member_candidates,
    list_teams,
    read_team,
    revoke_affiliation,
    update_membership,
    update_team,
)

router = APIRouter(tags=["business-teams"])


class SelfServiceTeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    department_id: uuid.UUID | None = None


class AdminTeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    first_lead_user_id: uuid.UUID


class TeamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "suspended", "archived"] | None = None


class MemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID | None = None
    org_member_id: uuid.UUID | None = None
    role: Literal["member", "lead"] = "member"


class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["member", "lead"] | None = None
    status: Literal["active", "suspended", "left"] | None = None


class AgentAffiliationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID


def _http(exc: BusinessTeamError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


async def _bind(db: AsyncSession, current_user: User) -> None:
    tenant_id = getattr(current_user, "tenant_id", None)
    if getattr(current_user, "role", None) == "platform_admin":
        tenant_id = _platform_admin_selected_tenant_id(current_user) or tenant_id
    if tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "tenant_required", "message": "Select a company before managing business teams."},
        )
    await pin_rls_tenant_context(db, tenant_id)


@router.get("/business-teams")
async def get_business_teams(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await list_teams(db, current_user)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/business-teams", status_code=201)
async def post_business_team(
    body: SelfServiceTeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await create_self_service_team(
            db, current_user, name=body.name, department_id=body.department_id
        )
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/enterprise/business-teams", status_code=201)
async def post_enterprise_business_team(
    body: AdminTeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await create_admin_team(
            db, current_user, name=body.name, first_lead_user_id=body.first_lead_user_id
        )
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.get("/enterprise/business-teams/lead-candidates")
async def get_lead_candidates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await list_lead_candidates(db, current_user)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.get("/business-teams/{team_id}")
async def get_business_team(
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await read_team(db, current_user, team_id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.patch("/business-teams/{team_id}")
async def patch_business_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await update_team(db, current_user, team_id, name=body.name, status=body.status)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.get("/business-teams/{team_id}/member-candidates")
async def get_member_candidates(
    team_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await list_member_candidates(db, current_user, team_id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/business-teams/{team_id}/members")
async def post_business_team_member(
    team_id: uuid.UUID,
    body: MemberCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        payload, created = await add_member(
            db,
            current_user,
            team_id,
            user_id=body.user_id,
            org_member_id=body.org_member_id,
            role=body.role,
        )
    except BusinessTeamError as exc:
        raise _http(exc) from exc
    return JSONResponse(status_code=201 if created else 200, content=payload)


@router.patch("/business-teams/{team_id}/members/{membership_id}")
async def patch_business_team_member(
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: MemberUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await update_membership(
            db,
            current_user,
            team_id,
            membership_id,
            role=body.role,
            status=body.status,
        )
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/business-teams/{team_id}/agents")
async def post_business_team_agent(
    team_id: uuid.UUID,
    body: AgentAffiliationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await affiliate_agent(db, current_user, team_id, body.agent_id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.delete("/business-teams/{team_id}/agents/{agent_id}")
async def delete_business_team_agent(
    team_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _bind(db, current_user)
    try:
        return await revoke_affiliation(db, current_user, team_id, agent_id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


class TeamBudgetIncreaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    approval_episode_id: uuid.UUID
    limits: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/business-teams/{team_id}/runtime-budget-runs")
async def get_team_runtime_budgets(team_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0), current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_runtime_budgets import list_team_budget_runs

    await _bind(db, current_user)
    try:
        return await list_team_budget_runs(db, user=current_user, team_id=team_id, limit=limit, offset=offset)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.get("/business-teams/{team_id}/tasks")
async def get_team_tasks(team_id: uuid.UUID, limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0), current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_business_tasks import list_team_tasks

    await _bind(db, current_user)
    try:
        return await list_team_tasks(db, user=current_user, team_id=team_id, limit=limit, offset=offset)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/business-teams/{team_id}/tasks/{task_id}/handover")
async def post_team_task_handover(team_id: uuid.UUID, task_id: uuid.UUID, body: TaskHandoverIn,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_business_tasks import handover_team_task

    await _bind(db, current_user)
    try:
        return await handover_team_task(db, user=current_user, team_id=team_id, task_id=task_id, body=body)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.get("/business-teams/{team_id}/runtime-budget-runs/{run_id}")
async def get_team_runtime_budget(team_id: uuid.UUID, run_id: uuid.UUID,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_runtime_budgets import load_team_budget_run, team_budget_projection

    await _bind(db, current_user)
    try:
        actor, team, run = await load_team_budget_run(db, user=current_user, team_id=team_id, run_id=run_id)
        return await team_budget_projection(db, run=run, actor=actor, team_id=team.id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/business-teams/{team_id}/runtime-budget-runs/{run_id}/increase-requests", status_code=201)
async def post_team_budget_increase(team_id: uuid.UUID, run_id: uuid.UUID, body: TeamBudgetIncreaseIn,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_runtime_budgets import request_budget_increase
    from app.services.runtime_budget_service import RuntimeBudgetStateConflict

    await _bind(db, current_user)
    try:
        return await request_budget_increase(db, user=current_user, team_id=team_id, run_id=run_id, body=body)
    except BusinessTeamError as exc:
        raise _http(exc) from exc
    except RuntimeBudgetStateConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.delete("/business-teams/{team_id}/runtime-budget-runs/{run_id}/increase-requests/{request_id}")
async def delete_team_budget_increase(team_id: uuid.UUID, run_id: uuid.UUID, request_id: uuid.UUID,
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.team_runtime_budgets import withdraw_budget_increase
    from app.services.runtime_budget_service import RuntimeBudgetStateConflict

    await _bind(db, current_user)
    try:
        return await withdraw_budget_increase(db, user=current_user, team_id=team_id, run_id=run_id, request_id=request_id)
    except BusinessTeamError as exc:
        raise _http(exc) from exc
    except RuntimeBudgetStateConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
