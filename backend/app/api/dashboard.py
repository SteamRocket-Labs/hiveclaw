"""Bounded workspace dashboard read model.

The dashboard is a single authenticated request regardless of Agent count.
Every collection query is globally capped and applies row authority before its
LIMIT so the browser cannot amplify one refresh into an unbounded SQL fanout.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.activity import load_visible_activity_rows
from app.core.permissions import agent_owned_by_clause
from app.core.resource_authority import load_explicit_resource_grant_ids
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent, AgentPermission
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.agent_identity_lifecycle import agent_lifecycle_active_clause
from app.services.tool_telemetry import summarize_tool_failure_logs

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)

_HIDDEN_SESSION_SOURCES = ("trigger", "task", "heartbeat")


async def _load_accessible_agent_ids(
    db: AsyncSession,
    current_user: User,
    tenant_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    if current_user.role == "org_admin":
        target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
        statement = select(Agent.id).where(
            Agent.tenant_id == target_tenant_id,
            Agent.agent_class != "internal_system",
            agent_lifecycle_active_clause(),
        )
    else:
        target_tenant_id = (
            await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
            if current_user.role == "platform_admin"
            else current_user.tenant_id
        )
        if target_tenant_id is None:
            return []
        if tenant_id is not None and tenant_id != target_tenant_id:
            raise HTTPException(status_code=403, detail="Dashboard tenant scope mismatch")
        permission_scopes = [(AgentPermission.scope_type == "user") & (AgentPermission.scope_id == current_user.id)]
        if current_user.role != "platform_admin":
            permission_scopes.append(AgentPermission.scope_type == "company")
        if current_user.role != "platform_admin" and current_user.department_id:
            permission_scopes.append(
                (AgentPermission.scope_type == "department") & (AgentPermission.scope_id == current_user.department_id)
            )
        permitted = exists(
            select(AgentPermission.id).where(
                AgentPermission.agent_id == Agent.id,
                AgentPermission.tenant_id == target_tenant_id,
                or_(*permission_scopes),
            )
        )
        statement = select(Agent.id).where(
            Agent.tenant_id == target_tenant_id,
            Agent.agent_class != "internal_system",
            agent_lifecycle_active_clause(),
            or_(agent_owned_by_clause(current_user.id), permitted),
        )
    return list((await db.execute(statement)).scalars().all())


async def _load_dashboard_sessions(
    db: AsyncSession,
    current_user: User,
    *,
    agent_ids: list[uuid.UUID],
    limit: int,
) -> tuple[list[dict], int]:
    has_user_message = exists(
        select(ChatMessage.id).where(
            ChatMessage.conversation_id == cast(ChatSession.id, String),
            ChatMessage.agent_id == ChatSession.agent_id,
            ChatMessage.role == "user",
        )
    )
    statement = (
        select(ChatSession, func.count(ChatSession.id).over().label("dashboard_session_count"))
        .where(
            ChatSession.agent_id.in_(agent_ids),
            ChatSession.user_id == current_user.id,
            ChatSession.listed_surface == "chat",
            ChatSession.source_channel.notin_(_HIDDEN_SESSION_SOURCES),
            ChatSession.session_kind == "human_chat",
            ChatSession.actor_type == "user",
            or_(ChatSession.source_channel == "web", has_user_message),
        )
        .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        .limit(max(1, int(limit)))
    )
    rows = list((await db.execute(statement)).all())
    session_count = int(rows[0][1]) if rows else 0
    payload = []
    for session, _total in rows:
        updated_at = session.last_message_at or session.created_at
        payload.append(
            {
                "id": str(session.id),
                "agent_id": str(session.agent_id),
                "title": session.title,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
                "session_kind": session.session_kind,
                "actor_type": session.actor_type,
                "listed_surface": session.listed_surface,
            }
        )
    return payload, session_count


def _activity_payload(row: AgentActivityLog, authority_source: str) -> dict:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "action_type": row.action_type,
        "summary": row.summary,
        "detail": row.detail_json,
        "related_id": str(row.related_id) if row.related_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "authority_source": authority_source,
        "operator_view": False,
    }


async def _load_dashboard_activities(
    db: AsyncSession,
    current_user: User,
    *,
    agent_ids: list[uuid.UUID],
    limit: int,
    explicit_grant_ids: set[uuid.UUID],
) -> list[dict]:
    query = (
        select(AgentActivityLog)
        .where(AgentActivityLog.agent_id.in_(agent_ids))
        .order_by(AgentActivityLog.created_at.desc())
    )
    rows = await load_visible_activity_rows(
        db,
        current_user,
        query=query,
        limit=limit,
        explicit_grant_ids=explicit_grant_ids,
    )
    return [_activity_payload(row, authority_source) for row, authority_source in rows]


async def _load_dashboard_failures(
    db: AsyncSession,
    current_user: User,
    *,
    agent_ids: list[uuid.UUID],
    hours: int,
    limit: int,
    explicit_grant_ids: set[uuid.UUID],
) -> tuple[dict[str, dict], bool, int]:
    since = datetime.now(UTC) - timedelta(hours=max(1, int(hours)))
    bounded_limit = max(1, int(limit))
    query = (
        select(AgentActivityLog)
        .where(
            AgentActivityLog.agent_id.in_(agent_ids),
            AgentActivityLog.action_type == "error",
            AgentActivityLog.created_at >= since,
        )
        .order_by(AgentActivityLog.created_at.desc())
    )
    visible = await load_visible_activity_rows(
        db,
        current_user,
        query=query,
        limit=bounded_limit + 1,
        explicit_grant_ids=explicit_grant_ids,
    )
    truncated = len(visible) > bounded_limit
    sampled = visible[:bounded_limit]
    rows_by_agent: dict[str, list[AgentActivityLog]] = defaultdict(list)
    for row, _authority_source in sampled:
        rows_by_agent[str(row.agent_id)].append(row)
    return (
        {agent_id: summarize_tool_failure_logs(rows) for agent_id, rows in rows_by_agent.items()},
        truncated,
        len(sampled),
    )


def _empty_overview(*, session_limit: int, activity_limit: int, failure_hours: int, failure_limit: int) -> dict:
    return {
        "recent_sessions": [],
        "session_count": 0,
        "recent_activities": [],
        "tool_failures": {},
        "query_evidence": {
            "agent_count": 0,
            "session_limit": session_limit,
            "activity_limit": activity_limit,
            "failure_hours": failure_hours,
            "failure_limit": failure_limit,
            "failure_rows_scanned": 0,
            "failure_rows_truncated": False,
        },
    }


@router.get("/overview")
async def get_dashboard_overview(
    tenant_id: uuid.UUID | None = None,
    session_limit: int = Query(4, ge=1, le=20),
    activity_limit: int = Query(20, ge=1, le=100),
    failure_hours: int = Query(24, ge=1, le=24 * 30),
    failure_limit: int = Query(500, ge=10, le=2000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    started = time.monotonic()
    agent_ids = await _load_accessible_agent_ids(db, current_user, tenant_id)
    if not agent_ids:
        return _empty_overview(
            session_limit=session_limit,
            activity_limit=activity_limit,
            failure_hours=failure_hours,
            failure_limit=failure_limit,
        )

    grant_ids = await load_explicit_resource_grant_ids(
        db,
        user=current_user,
        resource_kind="agent_activity",
        action="read",
    )
    sessions, session_count = await _load_dashboard_sessions(
        db,
        current_user,
        agent_ids=agent_ids,
        limit=session_limit,
    )
    activities = await _load_dashboard_activities(
        db,
        current_user,
        agent_ids=agent_ids,
        limit=activity_limit,
        explicit_grant_ids=grant_ids,
    )
    failures, failure_truncated, failure_rows_scanned = await _load_dashboard_failures(
        db,
        current_user,
        agent_ids=agent_ids,
        hours=failure_hours,
        limit=failure_limit,
        explicit_grant_ids=grant_ids,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    log = logger.warning if elapsed_ms >= 1000 else logger.debug
    log(
        "dashboard_overview elapsed_ms=%s agents=%s sessions=%s activities=%s failure_rows=%s truncated=%s",
        elapsed_ms,
        len(agent_ids),
        len(sessions),
        len(activities),
        failure_rows_scanned,
        failure_truncated,
    )
    return {
        "recent_sessions": sessions,
        "session_count": session_count,
        "recent_activities": activities,
        "tool_failures": failures,
        "query_evidence": {
            "agent_count": len(agent_ids),
            "session_limit": session_limit,
            "activity_limit": activity_limit,
            "failure_hours": failure_hours,
            "failure_limit": failure_limit,
            "failure_rows_scanned": failure_rows_scanned,
            "failure_rows_truncated": failure_truncated,
        },
    }
