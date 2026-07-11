"""Agent-scoped autonomy overview and diagnostics endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.execution_context import ExecutionPrincipal
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.audit import AuditLog
from app.models.user import User
from app.services.autonomy_overview import (
    build_agent_autonomy_overview,
    list_agent_runtime_task_views,
    read_agent_trigger_artifact_view,
)
from app.services.agent_work_ledger import read_agent_work_ledger_view, read_latest_session_work_ledger_view

router = APIRouter(prefix="/agents", tags=["autonomy"])


def _can_manage_sessions(user: User, agent: object, access_level: str) -> bool:
    return (
        getattr(user, "role", None) in ("platform_admin", "org_admin")
        or str(getattr(agent, "creator_id", "")) == str(getattr(user, "id", ""))
        or access_level == "manage"
    )


async def _get_accessible_session_for_work_ledger(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User,
) -> ChatSession:
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            or_(
                ChatSession.agent_id == agent_id,
                (ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent"),
            ),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized to view this session work ledger")
    return session


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
        principal=ExecutionPrincipal(
            tenant_id=agent.tenant_id,
            source_agent_id=agent.id,
            requester_user_id=current_user.id,
            origin="rest",
        ),
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
        principal=ExecutionPrincipal(
            tenant_id=agent.tenant_id,
            source_agent_id=agent.id,
            requester_user_id=current_user.id,
            origin="rest",
        ),
    )


@router.get("/{agent_id}/runtime-tasks")
async def list_agent_runtime_tasks(
    agent_id: uuid.UUID,
    task_type: str | None = Query(default=None),
    trigger_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    diagnostics: bool = Query(default=False),
    root_session_id: uuid.UUID | None = Query(default=None),
    operator_override: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List display-safe RuntimeTask attempts for one accessible agent."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    reason = str(operator_reason or "").strip()
    if operator_override:
        if access_level != "manage":
            raise HTTPException(status_code=403, detail="RuntimeTask operator override requires manage access")
        if not reason:
            raise HTTPException(status_code=422, detail="operator_reason is required for RuntimeTask override")
        db.add(
            AuditLog(
                user_id=current_user.id,
                agent_id=agent_id,
                tenant_id=agent.tenant_id,
                action="runtime_task:operator_list_override",
                details={
                    "operator_reason": reason,
                    "root_session_filter": str(root_session_id) if root_session_id else None,
                },
            )
        )
        await db.flush()
    principal = ExecutionPrincipal(
        tenant_id=agent.tenant_id,
        source_agent_id=agent.id,
        requester_user_id=current_user.id,
        root_session_id=str(root_session_id) if root_session_id else None,
        origin="rest",
    )
    return await list_agent_runtime_task_views(
        db=db,
        agent_id=agent_id,
        task_type=task_type,
        trigger_id=trigger_id,
        status=status,
        limit=limit,
        include_diagnostics=diagnostics,
        principal=principal,
        allow_operator_override=operator_override,
        operator_user_id=current_user.id if operator_override else None,
        operator_reason=reason if operator_override else None,
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


@router.get("/{agent_id}/runtime-work-ledgers/{runtime_task_id}")
async def get_agent_runtime_work_ledger(
    agent_id: uuid.UUID,
    runtime_task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the chat-safe Work Ledger view for a running RuntimeTask."""
    await check_agent_access(db, current_user, agent_id)
    ledger = read_agent_work_ledger_view(agent_id=agent_id, runtime_task_id=runtime_task_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail="Runtime work ledger not found")
    return ledger


@router.get("/{agent_id}/sessions/{session_id}/work-ledger")
async def get_agent_session_work_ledger(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the latest chat-safe Work Ledger for the current chat session."""
    await _get_accessible_session_for_work_ledger(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    ledger = await read_latest_session_work_ledger_view(db=db, agent_id=agent_id, session_id=session_id)
    if ledger is None:
        return {
            "schema": "agent_work_ledger_view.v1",
            "session_id": str(session_id),
            "runtime_task_id": None,
            "status": "empty",
            "current_phase": None,
            "todo_items": [],
            "counts": {"todos_total": 0, "todos_complete": 0, "todos_open": 0},
        }
    return ledger
