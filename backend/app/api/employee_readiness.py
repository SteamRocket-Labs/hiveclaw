"""Authenticated owner/team entry points for no-inference readiness jobs."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.business_teams import _bind, _http
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.business_teams import BusinessTeamError
from app.services import employee_readiness as service

router = APIRouter(tags=["employee-readiness"])


class ReadinessCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID


async def _perform(operation, db, user, agent_id, team_id=None, **kwargs):
    await _bind(db, user)
    try:
        return await operation(db, user=user, agent_id=agent_id, team_id=team_id, **kwargs)
    except BusinessTeamError as exc:
        raise _http(exc) from exc


@router.post("/agents/{agent_id}/readiness-checks", status_code=202)
async def start_employee_check(agent_id: uuid.UUID, body: ReadinessCheckIn,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.start_check, db, user, agent_id, request_id=body.request_id)


@router.post("/business-teams/{team_id}/agents/{agent_id}/readiness-checks", status_code=202)
async def start_team_employee_check(team_id: uuid.UUID, agent_id: uuid.UUID, body: ReadinessCheckIn,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.start_check, db, user, agent_id, team_id, request_id=body.request_id)


@router.get("/agents/{agent_id}/readiness-checks/latest")
async def latest_employee_check(agent_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.read_check, db, user, agent_id)


@router.get("/business-teams/{team_id}/agents/{agent_id}/readiness-checks/latest")
async def latest_team_employee_check(team_id: uuid.UUID, agent_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.read_check, db, user, agent_id, team_id)


@router.get("/agents/{agent_id}/readiness-checks/{check_id}")
async def get_employee_check(agent_id: uuid.UUID, check_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.read_check, db, user, agent_id, check_id=check_id)


@router.get("/business-teams/{team_id}/agents/{agent_id}/readiness-checks/{check_id}")
async def get_team_employee_check(team_id: uuid.UUID, agent_id: uuid.UUID, check_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.read_check, db, user, agent_id, team_id, check_id=check_id)


@router.post("/agents/{agent_id}/readiness-checks/{check_id}/cancel")
async def cancel_employee_check(agent_id: uuid.UUID, check_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.cancel_check, db, user, agent_id, check_id=check_id)


@router.post("/business-teams/{team_id}/agents/{agent_id}/readiness-checks/{check_id}/cancel")
async def cancel_team_employee_check(team_id: uuid.UUID, agent_id: uuid.UUID, check_id: uuid.UUID,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _perform(service.cancel_check, db, user, agent_id, team_id, check_id=check_id)
