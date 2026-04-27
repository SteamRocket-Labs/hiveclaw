"""Agent-scoped autonomy overview and diagnostics endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.autonomy_overview import (
    build_agent_autonomy_overview,
    list_agent_runtime_task_views,
    read_agent_trigger_artifact_view,
)

router = APIRouter(prefix="/agents", tags=["autonomy"])


@router.get("/{agent_id}/autonomy/overview")
async def get_agent_autonomy_overview(
    agent_id: uuid.UUID,
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the operator-facing autonomy state for one accessible agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    return await build_agent_autonomy_overview(
        db=db,
        agent=agent,
        lookback_hours=lookback_hours,
        include_diagnostics=False,
    )


@router.get("/{agent_id}/autonomy/diagnostics")
async def get_agent_autonomy_diagnostics(
    agent_id: uuid.UUID,
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return diagnostics for one accessible agent without requiring platform-admin scope."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    return await build_agent_autonomy_overview(
        db=db,
        agent=agent,
        lookback_hours=lookback_hours,
        include_diagnostics=True,
    )


@router.get("/{agent_id}/runtime-tasks")
async def list_agent_runtime_tasks(
    agent_id: uuid.UUID,
    task_type: str | None = Query(default=None),
    trigger_id: str | None = Query(default=None),
    objective_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    diagnostics: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List display-safe RuntimeTask attempts for one accessible agent."""
    await check_agent_access(db, current_user, agent_id)
    return await list_agent_runtime_task_views(
        db=db,
        agent_id=agent_id,
        task_type=task_type,
        trigger_id=trigger_id,
        objective_id=objective_id,
        status=status,
        limit=limit,
        include_diagnostics=diagnostics,
    )


@router.get("/{agent_id}/runtime-artifacts/{runtime_task_id}")
async def get_agent_runtime_artifact(
    agent_id: uuid.UUID,
    runtime_task_id: str,
    diagnostics: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read a display-safe trigger output artifact for one accessible agent."""
    await check_agent_access(db, current_user, agent_id)
    artifact = await read_agent_trigger_artifact_view(
        agent_id=agent_id,
        runtime_task_id=runtime_task_id,
        include_diagnostics=diagnostics,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Runtime artifact not found")
    return artifact
