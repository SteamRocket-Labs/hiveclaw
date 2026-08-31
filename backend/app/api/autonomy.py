"""Agent-scoped autonomy overview and diagnostics endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can_manage_agent_sessions, check_agent_access
from app.core.execution_context import ExecutionPrincipal
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.audit import AuditLog
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.autonomy_overview import (
    build_agent_autonomy_overview,
    list_agent_runtime_task_views,
    read_agent_trigger_artifact_view,
)
from app.services.agent_work_ledger import read_agent_work_ledger_view, read_latest_session_work_ledger_view
from app.services.direct_invocation_terminal_boundary_processor import trigger_artifact_projection_delivered
from app.services.action_preflight import CharterZone
from app.services.config_versioning import get_history
from app.services.owner_action_policy import (
    OWNER_ACTION_POLICY_ENTITY_TYPE,
    OwnerActionPolicyRevisionNotFound,
    OwnerActionPolicyValidationError,
    OwnerActionPolicyVersionConflict,
    load_owner_action_policy,
    rollback_owner_action_policy,
    save_owner_action_policy,
    validate_owner_action_policy_actions,
)
from app.services.runtime_task_authority import authorize_runtime_task_record

router = APIRouter(prefix="/agents", tags=["autonomy"])


class OwnerActionPolicyUpdateRequest(BaseModel):
    actions: dict[str, CharterZone]
    expected_version: int = Field(ge=1)


class OwnerActionPolicyRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


def _runtime_task_authority_record(task: RuntimeTask) -> dict:
    return {
        "task_id": str(task.id),
        "tenant_id": str(task.tenant_id) if task.tenant_id else None,
        "parent_agent_id": str(task.parent_agent_id) if task.parent_agent_id else None,
        "root_user_id": str(task.root_user_id) if task.root_user_id else None,
        "root_session_id": task.root_session_id,
        "root_runtime_task_id": str(task.root_runtime_task_id) if task.root_runtime_task_id else None,
        "delegation_chain": list(task.delegation_chain_json or []),
        "metadata": task.metadata_json or {},
    }


async def _authorize_runtime_task_read(
    *,
    db: AsyncSession,
    agent,
    access_level: str,
    current_user: User,
    runtime_task_id: str,
    operator_override: bool,
    operator_reason: str | None,
):
    try:
        task_id = uuid.UUID(str(runtime_task_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Runtime task not found") from exc
    task = (
        await db.execute(
            select(RuntimeTask).where(
                RuntimeTask.id == task_id,
                RuntimeTask.parent_agent_id == agent.id,
                RuntimeTask.tenant_id == agent.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Runtime task not found")
    reason = str(operator_reason or "").strip()
    if operator_override and access_level != "manage":
        raise HTTPException(status_code=403, detail="RuntimeTask operator override requires manage access")
    decision = authorize_runtime_task_record(
        _runtime_task_authority_record(task),
        principal=ExecutionPrincipal(
            tenant_id=agent.tenant_id,
            source_agent_id=agent.id,
            requester_user_id=current_user.id,
            origin="rest",
        ),
        action="api_read_resource",
        allow_operator_override=operator_override,
        operator_user_id=current_user.id if operator_override else None,
        operator_reason=reason if operator_override else None,
        require_root_session=False,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "runtime_task_resource_forbidden", "reason": decision.reason},
        )
    if decision.authority_source == "operator_override":
        db.add(
            AuditLog(
                user_id=current_user.id,
                agent_id=agent.id,
                tenant_id=agent.tenant_id,
                action="runtime_task:operator_resource_override",
                details={
                    "runtime_task_id": str(task.id),
                    "operator_reason": reason,
                    "authority_evidence": decision.evidence,
                },
            )
        )
        await db.flush()
    return task, decision


async def _get_accessible_session_for_work_ledger(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User,
    operator_override: bool = False,
    operator_reason: str | None = None,
) -> tuple[ChatSession, str]:
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
    if str(session.user_id) == str(current_user.id):
        return session, "session_owner"
    reason = str(operator_reason or "").strip()
    if can_manage_agent_sessions(access_level) and operator_override and reason:
        from app.services.audit_logger import write_audit_log

        await write_audit_log(
            "session_authority_override",
            details={
                "session_id": str(session.id),
                "session_user_id": str(session.user_id),
                "action": "read_work_ledger",
                "reason": reason,
                "authority_source": "manager_override",
            },
            agent_id=agent_id,
            user_id=current_user.id,
        )
        return session, "manager_override"
    raise HTTPException(status_code=403, detail="Not authorized to view this session work ledger")


@router.get("/{agent_id}/autonomy/overview")
async def get_agent_autonomy_overview(
    agent_id: uuid.UUID,
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the operator-facing autonomy state for one accessible agent."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
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
        resource_user=current_user,
        agent_access=(agent, access_level),
    )


@router.get("/{agent_id}/autonomy/diagnostics")
async def get_agent_autonomy_diagnostics(
    agent_id: uuid.UUID,
    lookback_hours: int = Query(default=24, ge=1, le=720),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return diagnostics for one accessible agent without requiring platform-admin scope."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
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
        resource_user=current_user,
        agent_access=(agent, access_level),
    )


@router.get("/{agent_id}/autonomy/action-policy")
async def get_agent_owner_action_policy(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the business-level action policy; never expose runtime Hook internals."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    policy = await load_owner_action_policy(
        db,
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        create_default=True,
    )
    return policy.response_payload(can_manage=access_level == "manage")


@router.put("/{agent_id}/autonomy/action-policy")
async def update_agent_owner_action_policy(
    agent_id: uuid.UUID,
    data: OwnerActionPolicyUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the exact typed policy through a manage-only, versioned boundary."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access is required to update action policy")
    try:
        actions = validate_owner_action_policy_actions(data.actions)
        policy = await save_owner_action_policy(
            db,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            actions=actions,
            changed_by_user_id=current_user.id,
            expected_version=data.expected_version,
        )
    except OwnerActionPolicyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OwnerActionPolicyVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            action="agent.owner_action_policy.updated",
            details={
                "revision_version": policy.version,
                "revision_id": str(policy.revision_id) if policy.revision_id else None,
                "content_hash": policy.content_hash,
                "action_ids": sorted(actions),
            },
        )
    )
    await db.flush()
    return policy.response_payload(can_manage=True)


@router.get("/{agent_id}/autonomy/action-policy/history")
async def get_agent_owner_action_policy_history(
    agent_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return immutable revision metadata for Owner recovery and audit."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access is required to view action policy history")
    items = await get_history(
        db,
        OWNER_ACTION_POLICY_ENTITY_TYPE,
        agent.id,
        limit=limit,
        tenant_id=agent.tenant_id,
    )
    return {"items": items}


@router.post("/{agent_id}/autonomy/action-policy/rollback")
async def rollback_agent_owner_action_policy(
    agent_id: uuid.UUID,
    data: OwnerActionPolicyRollbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore a historical policy as a new immutable revision."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access is required to roll back action policy")
    try:
        policy = await rollback_owner_action_policy(
            db,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            target_version=data.target_version,
            changed_by_user_id=current_user.id,
            expected_version=data.expected_version,
            reason=data.reason.strip(),
        )
    except OwnerActionPolicyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OwnerActionPolicyVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OwnerActionPolicyRevisionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    db.add(
        AuditLog(
            user_id=current_user.id,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            action="agent.owner_action_policy.rolled_back",
            details={
                "target_version": data.target_version,
                "revision_version": policy.version,
                "revision_id": str(policy.revision_id) if policy.revision_id else None,
                "content_hash": policy.content_hash,
                "reason": data.reason.strip(),
            },
        )
    )
    await db.flush()
    return policy.response_payload(can_manage=True)


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
    operator_override: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read a display-safe trigger output artifact for one accessible agent."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    task, decision = await _authorize_runtime_task_read(
        db=db,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        runtime_task_id=runtime_task_id,
        operator_override=operator_override,
        operator_reason=operator_reason,
    )
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    expected_artifact = trigger_output_artifact_ref(str(task.id))
    if (
        task.task_type != "trigger"
        or (task.metadata_json or {}).get("output_artifact") != expected_artifact
        or not await trigger_artifact_projection_delivered(db, task)
    ):
        raise HTTPException(status_code=404, detail="Runtime artifact not found")
    artifact = await read_agent_trigger_artifact_view(
        agent_id=agent_id,
        runtime_task_id=str(task.id),
        include_diagnostics=diagnostics,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Runtime artifact not found")
    artifact["authority_source"] = decision.authority_source
    artifact["operator_view"] = decision.authority_source == "operator_override"
    return artifact


@router.get("/{agent_id}/runtime-work-ledgers/{runtime_task_id}")
async def get_agent_runtime_work_ledger(
    agent_id: uuid.UUID,
    runtime_task_id: str,
    operator_override: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the chat-safe Work Ledger view for a running RuntimeTask."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    _task, decision = await _authorize_runtime_task_read(
        db=db,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        runtime_task_id=runtime_task_id,
        operator_override=operator_override,
        operator_reason=operator_reason,
    )
    ledger = read_agent_work_ledger_view(agent_id=agent_id, runtime_task_id=runtime_task_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail="Runtime work ledger not found")
    ledger["authority_source"] = decision.authority_source
    ledger["operator_view"] = decision.authority_source == "operator_override"
    return ledger


@router.get("/{agent_id}/sessions/{session_id}/work-ledger")
async def get_agent_session_work_ledger(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_override: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the latest chat-safe Work Ledger for the current chat session."""
    _session, authority_source = await _get_accessible_session_for_work_ledger(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        operator_override=operator_override,
        operator_reason=operator_reason,
    )
    ledger = await read_latest_session_work_ledger_view(db=db, agent_id=agent_id, session_id=session_id)
    if ledger is None:
        empty = {
            "schema": "agent_work_ledger_view.v1",
            "session_id": str(session_id),
            "runtime_task_id": None,
            "status": "empty",
            "current_phase": None,
            "todo_items": [],
            "counts": {"todos_total": 0, "todos_complete": 0, "todos_open": 0},
        }
        if authority_source == "manager_override":
            empty.update(authority_source=authority_source, operator_view=True)
        return empty
    if authority_source == "manager_override":
        ledger.update(authority_source=authority_source, operator_view=True)
    return ledger
