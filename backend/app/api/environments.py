"""Scoped observation and lifecycle control for employee command resources."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from app.core.security import get_current_user
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.audit import AuditLog
from app.models.user import User
from app.services.execution_environments import EnvironmentError, control_environment, project_environment

router = APIRouter(tags=["execution-environments"])


class EnvironmentControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    expected_user_id: uuid.UUID
    expected_tenant_id: uuid.UUID
    expected_lease_id: uuid.UUID | None = None


async def _scope(db, user, expected_user_id, expected_tenant_id):
    if expected_user_id != user.id:
        raise HTTPException(409, detail={"code": "environment_account_changed"})
    return await resolve_and_pin_tenant_scope(db, user, expected_tenant_id)


@router.get("/agents/{agent_id}/environment")
async def read_environment(
    agent_id: uuid.UUID,
    response: Response,
    expected_user_id: uuid.UUID,
    expected_tenant_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _scope(db, user, expected_user_id, expected_tenant_id)
    agent, access = await check_agent_access(db, user, agent_id)
    if agent.tenant_id != tenant_id:
        raise HTTPException(404)
    response.headers["Cache-Control"] = "no-store"
    try:
        environment = await project_environment(db, tenant_id=tenant_id, agent_id=agent_id, include_recovery=access == "manage")
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, detail={"code": "environment_state_unavailable"}, headers={"Cache-Control": "no-store"}
        ) from exc
    return {"tenant_id": tenant_id, "agent_id": agent_id, "can_manage": access == "manage", "environment": environment}


@router.post("/agents/{agent_id}/environment/{action}")
async def change_environment(
    agent_id: uuid.UUID,
    action: str,
    body: EnvironmentControl,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _scope(db, user, body.expected_user_id, body.expected_tenant_id)
    agent = await require_agent_manage_access(db, user, agent_id)
    if agent.tenant_id != tenant_id:
        raise HTTPException(404)
    try:
        result = await control_environment(
            db, tenant_id=tenant_id, agent_id=agent_id, action=action, expected_revision=body.expected_revision,
            expected_lease_id=body.expected_lease_id,
        )
    except EnvironmentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, detail={"code": "environment_state_unavailable"}, headers={"Cache-Control": "no-store"}
        ) from exc
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user.id,
            action=f"environment.{action}",
            details={
                "environment_id": result["id"],
                "generation": result["generation"],
                "revision": result["revision"],
                **({"lease_id": str(body.expected_lease_id), "reason_code": "worker_terminated_before_resource_creation"}
                   if action == "reconcile" else {}),
            },
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return {"tenant_id": tenant_id, "agent_id": agent_id, "can_manage": True, "environment": result}
