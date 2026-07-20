"""Recoverable User offboarding with atomic Agent ownership transfer."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import agent_owned_by_clause
from app.models.agent import Agent, AgentPermission
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.models.knowledge import KnowledgeGrant
from app.models.local_agent_channel import LocalAgentChannel, LocalAgentChannelSession, LocalAgentChannelWsTicket
from app.models.local_bridge import LocalAgentBridgeConnection, LocalAgentBridgePairingSession
from app.models.refresh_token import RefreshToken
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.task import Task
from app.models.user import User
from app.services.agent_ownership_service import transfer_loaded_agent_owner
from app.services.external_principal_service import unlink_external_principal


@dataclass(frozen=True, slots=True)
class RuntimeTaskRevocationSignal:
    task_id: uuid.UUID
    task_type: str
    parent_agent_id: uuid.UUID | None = None
    parent_session_id: str | None = None
    business_task_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthorityRevocationReceipt:
    agent_permissions: int = 0
    resource_permissions: int = 0
    knowledge_grants: int = 0
    refresh_tokens: int = 0
    external_principals: int = 0
    local_bridge_connections: int = 0
    runtime_tasks: int = 0
    pending_approvals: int = 0
    runtime_task_signals: tuple[RuntimeTaskRevocationSignal, ...] = ()


@dataclass(frozen=True, slots=True)
class UserOffboardingReceipt:
    status: str
    user_id: uuid.UUID
    successor_user_id: uuid.UUID
    transferred_agent_ids: list[uuid.UUID]
    revocations: AuthorityRevocationReceipt
    request_id: str


@dataclass(frozen=True, slots=True)
class UserOffboardingPreview:
    user_id: uuid.UUID
    display_name: str
    is_active: bool
    owned_agents: list[dict[str, str]]
    eligible_successors: list[dict[str, str]]
    default_successor_id: uuid.UUID | None
    agent_permissions: int
    resource_permissions: int
    knowledge_grants: int
    refresh_tokens: int
    external_principals: int
    local_bridge_connections: int
    runtime_tasks: int
    pending_approvals: int


def _rowcount(result: object) -> int:
    return max(0, int(getattr(result, "rowcount", 0) or 0))


def _runtime_task_signal_from_dict(value: dict[str, Any]) -> RuntimeTaskRevocationSignal:
    return RuntimeTaskRevocationSignal(
        task_id=uuid.UUID(str(value["task_id"])),
        task_type=str(value.get("task_type") or ""),
        parent_agent_id=(uuid.UUID(str(value["parent_agent_id"])) if value.get("parent_agent_id") else None),
        parent_session_id=str(value["parent_session_id"]) if value.get("parent_session_id") else None,
        business_task_id=(uuid.UUID(str(value["business_task_id"])) if value.get("business_task_id") else None),
    )


def _runtime_task_signal_to_dict(value: RuntimeTaskRevocationSignal) -> dict[str, str | None]:
    return {
        "task_id": str(value.task_id),
        "task_type": value.task_type,
        "parent_agent_id": str(value.parent_agent_id) if value.parent_agent_id else None,
        "parent_session_id": value.parent_session_id,
        "business_task_id": str(value.business_task_id) if value.business_task_id else None,
    }


async def find_user_offboarding_replay(
    db: AsyncSession,
    *,
    target_user: User,
    successor_user_id: uuid.UUID,
    expected_agent_ids: list[uuid.UUID],
    reason: str,
    request_id: str,
) -> UserOffboardingReceipt | None:
    """Return a committed receipt for an exact network retry.

    The target User row is locked by the API before this lookup. That lock
    serializes concurrent attempts for one member, while the append-only audit
    receipt makes a response-lost retry recoverable without repeating effects.
    """

    clean_request_id = str(request_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_request_id:
        raise HTTPException(status_code=400, detail="Offboarding request_id is required")
    if not clean_reason:
        raise HTTPException(status_code=400, detail="Offboarding reason is required")

    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == target_user.tenant_id,
            AuditLog.action == "user:offboarded",
            AuditLog.details["target_user_id"].as_string() == str(target_user.id),
            AuditLog.details["request_id"].as_string() == clean_request_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    audit = result.scalar_one_or_none()
    if audit is None:
        return None

    details = dict(audit.details or {})
    recorded_expected = {str(value) for value in details.get("expected_agent_ids", [])}
    current_expected = {str(value) for value in expected_agent_ids}
    same_input = (
        str(details.get("successor_user_id") or "") == str(successor_user_id)
        and str(details.get("reason") or "").strip() == clean_reason
        and recorded_expected == current_expected
    )
    if not same_input:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "offboarding_idempotency_conflict",
                "request_id": clean_request_id,
                "target_user_id": str(target_user.id),
            },
        )

    revocations = dict(details.get("revocations") or {})
    return UserOffboardingReceipt(
        status="already_inactive" if details.get("already_inactive") else "deactivated",
        user_id=target_user.id,
        successor_user_id=uuid.UUID(str(details["successor_user_id"])),
        transferred_agent_ids=[uuid.UUID(str(value)) for value in details.get("transferred_agent_ids", [])],
        revocations=AuthorityRevocationReceipt(
            agent_permissions=int(revocations.get("agent_permissions") or 0),
            resource_permissions=int(revocations.get("resource_permissions") or 0),
            knowledge_grants=int(revocations.get("knowledge_grants") or 0),
            refresh_tokens=int(revocations.get("refresh_tokens") or 0),
            external_principals=int(revocations.get("external_principals") or 0),
            local_bridge_connections=int(revocations.get("local_bridge_connections") or 0),
            runtime_tasks=int(revocations.get("runtime_tasks") or 0),
            pending_approvals=int(revocations.get("pending_approvals") or 0),
            runtime_task_signals=tuple(
                _runtime_task_signal_from_dict(value)
                for value in details.get("runtime_task_signals", [])
                if isinstance(value, dict) and value.get("task_id")
            ),
        ),
        request_id=clean_request_id,
    )


async def _lock_owned_agents(
    db: AsyncSession,
    *,
    target_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> list[Agent]:
    result = await db.execute(
        select(Agent)
        .where(Agent.tenant_id == tenant_id, agent_owned_by_clause(target_user_id))
        .order_by(Agent.id.asc())
        .with_for_update()
    )
    return list(result.scalars().all())


async def _revoke_user_authority(
    db: AsyncSession,
    *,
    target_user: User,
    actor_user: User,
    now: datetime,
) -> AuthorityRevocationReceipt:
    tenant_id = target_user.tenant_id
    target_user_id = target_user.id

    runtime_tasks = list(
        (
            await db.execute(
                select(RuntimeTask)
                .where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.root_user_id == target_user_id,
                    RuntimeTask.status.in_(("pending", "running", "suspended", "resumable")),
                )
                .order_by(RuntimeTask.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    runtime_task_signals: list[RuntimeTaskRevocationSignal] = []
    business_tasks_by_runtime_id: dict[uuid.UUID, Task] = {}
    if runtime_tasks:
        business_tasks_by_runtime_id = {
            business_task.active_runtime_task_id: business_task
            for business_task in (
                await db.execute(
                    select(Task)
                    .where(
                        Task.tenant_id == tenant_id,
                        Task.active_runtime_task_id.in_([task.id for task in runtime_tasks]),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
            if business_task.active_runtime_task_id is not None
        }
    from app.services.runtime_root_ledger import transition_runtime_root_item_by_task

    for task in runtime_tasks:
        previous_status = str(task.status)
        original_metadata = dict(task.metadata_json or {})
        business_task = business_tasks_by_runtime_id.get(task.id)
        business_binding_matches = bool(
            business_task is not None
            and task.task_type == "business_task"
            and str(original_metadata.get("business_task_id") or "") == str(business_task.id)
            and task.parent_agent_id == business_task.agent_id
        )
        if business_binding_matches:
            from app.services.business_task_runtime import apply_business_task_cancellation

            apply_business_task_cancellation(
                db=db,
                task=business_task,
                runtime_task=task,
                cancelled_by_user_id=actor_user.id,
                reason="Root User was offboarded; previous execution authority was revoked.",
                completed_at=now,
            )
        else:
            task.status = "needs_reconciliation" if previous_status == "running" else "killed"
            task.claim_version = int(task.claim_version or 0) + 1
            task.claimed_by = None
            task.claim_expires_at = None
            task.completed_at = now
        task.budget_terminal_reason = "root_user_offboarded"
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "authority_revoked": True,
            "authority_revocation_reason": "root_user_offboarded",
            "authority_revoked_user_id": str(target_user_id),
            "authority_revoked_by_user_id": str(actor_user.id),
            "authority_revoked_at": now.isoformat(),
            "pre_revocation_status": previous_status,
            "side_effect_state": "unknown" if previous_status == "running" else "not_started",
        }
        business_task_id = business_task.id if business_binding_matches else None
        raw_business_task_id = original_metadata.get("business_task_id")
        if raw_business_task_id:
            try:
                business_task_id = uuid.UUID(str(raw_business_task_id))
            except ValueError:
                business_task_id = None
        runtime_task_signals.append(
            RuntimeTaskRevocationSignal(
                task_id=task.id,
                task_type=str(task.task_type),
                parent_agent_id=task.parent_agent_id,
                parent_session_id=task.parent_session_id,
                business_task_id=business_task_id,
            )
        )
        await transition_runtime_root_item_by_task(
            db,
            runtime_task_id=task.id,
            requested_state=task.status,
            reason_code="root_user_offboarded",
            metadata={"authority_revoked_user_id": str(target_user_id)},
        )

    pending_approvals = list(
        (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.requested_by == target_user_id,
                    ApprovalRequest.status == "pending",
                )
                .order_by(ApprovalRequest.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for approval in pending_approvals:
        approval.status = "rejected"
        approval.execution_status = "rejected"
        approval.resolved_at = now
        approval.resolved_by = actor_user.id
        approval.execution_result = "Request rejected because the requesting User was offboarded."
        approval.execution_receipt = {
            **dict(approval.execution_receipt or {}),
            "status": "rejected",
            "side_effect_state": "not_started",
            "reason": "requesting_user_offboarded",
            "resolved_by_user_id": str(actor_user.id),
        }

    agent_permissions = _rowcount(
        await db.execute(
            delete(AgentPermission).where(
                AgentPermission.tenant_id == tenant_id,
                AgentPermission.scope_type == "user",
                AgentPermission.scope_id == target_user_id,
            )
        )
    )
    resource_permissions = _rowcount(
        await db.execute(
            delete(ResourcePermission).where(
                ResourcePermission.tenant_id == tenant_id,
                ResourcePermission.principal_type == "user",
                ResourcePermission.principal_id == target_user_id,
            )
        )
    )
    knowledge_grants = _rowcount(
        await db.execute(
            update(KnowledgeGrant)
            .where(
                KnowledgeGrant.tenant_id == tenant_id,
                KnowledgeGrant.revoked_at.is_(None),
                (
                    ((KnowledgeGrant.scope_type == "person") & (KnowledgeGrant.scope_id == target_user_id))
                    | ((KnowledgeGrant.grantee_type == "user") & (KnowledgeGrant.grantee_id == target_user_id))
                    | (KnowledgeGrant.requester_user_id == target_user_id)
                ),
            )
            .values(revoked_at=now, revoked_by_user_id=actor_user.id)
        )
    )
    refresh_tokens = _rowcount(
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == target_user_id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
    )

    principal_ids = list(
        (
            await db.execute(
                select(ExternalPrincipal.id).where(
                    ExternalPrincipal.tenant_id == tenant_id,
                    ExternalPrincipal.linked_user_id == target_user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for principal_id in principal_ids:
        await unlink_external_principal(
            db,
            tenant_id=tenant_id,
            principal_id=principal_id,
            actor_user_id=actor_user.id,
            reason=f"User offboarding: {target_user_id}",
        )

    # Fail closed even for legacy channel bindings that predate an
    # ExternalPrincipal row.
    channel_configs = list(
        (
            await db.execute(
                select(ChannelConfig)
                .where(
                    ChannelConfig.tenant_id == tenant_id,
                    ChannelConfig.self_identity_user_id == target_user_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for config in channel_configs:
        config.self_identity_user_id = None
        config.self_identity_verified_at = None
        config.is_connected = False
        if config.channel_type == "feishu":
            config.is_configured = False
        config.extra_config = {
            **dict(config.extra_config or {}),
            "connection_status": "identity_rebind_required",
            "identity_status": "rebind_required",
        }

    local_bridge_connections = _rowcount(
        await db.execute(
            update(LocalAgentBridgeConnection)
            .where(
                LocalAgentBridgeConnection.tenant_id == tenant_id,
                LocalAgentBridgeConnection.user_id == target_user_id,
                LocalAgentBridgeConnection.status == "active",
            )
            .values(status="revoked", revoked_at=now)
        )
    )
    await db.execute(
        update(LocalAgentBridgePairingSession)
        .where(
            LocalAgentBridgePairingSession.tenant_id == tenant_id,
            LocalAgentBridgePairingSession.user_id == target_user_id,
            LocalAgentBridgePairingSession.status.in_(("pending", "approved")),
        )
        .values(status="rejected")
    )
    await db.execute(
        update(LocalAgentChannel)
        .where(LocalAgentChannel.tenant_id == tenant_id, LocalAgentChannel.owner_user_id == target_user_id)
        .values(status="offline")
    )
    await db.execute(
        update(LocalAgentChannelSession)
        .where(
            LocalAgentChannelSession.tenant_id == tenant_id,
            LocalAgentChannelSession.owner_user_id == target_user_id,
            LocalAgentChannelSession.status == "active",
        )
        .values(status="closed")
    )
    await db.execute(
        update(LocalAgentChannelWsTicket)
        .where(
            LocalAgentChannelWsTicket.tenant_id == tenant_id,
            LocalAgentChannelWsTicket.user_id == target_user_id,
            LocalAgentChannelWsTicket.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    return AuthorityRevocationReceipt(
        agent_permissions=agent_permissions,
        resource_permissions=resource_permissions,
        knowledge_grants=knowledge_grants,
        refresh_tokens=refresh_tokens,
        external_principals=len(principal_ids),
        local_bridge_connections=local_bridge_connections,
        runtime_tasks=len(runtime_tasks),
        pending_approvals=len(pending_approvals),
        runtime_task_signals=tuple(runtime_task_signals),
    )


def _validate_successor(*, target_user: User, successor: User) -> None:
    if successor.id == target_user.id:
        raise HTTPException(status_code=400, detail="Successor must be a different user")
    if not successor.is_active:
        raise HTTPException(status_code=400, detail="Successor must be active")
    if successor.tenant_id != target_user.tenant_id:
        raise HTTPException(status_code=400, detail="Successor must belong to the same company")
    if successor.role not in ("org_admin", "platform_admin"):
        raise HTTPException(status_code=400, detail="Successor must be a company administrator")


async def offboard_loaded_user(
    db: AsyncSession,
    *,
    target_user: User,
    successor: User,
    actor: User,
    expected_agent_ids: list[uuid.UUID],
    reason: str,
    request_id: str,
) -> UserOffboardingReceipt:
    """Transfer responsibilities, revoke authority, then deactivate one User."""

    clean_reason = str(reason or "").strip()
    clean_request_id = str(request_id or "").strip()
    if not clean_reason:
        raise HTTPException(status_code=400, detail="Offboarding reason is required")
    if not clean_request_id:
        raise HTTPException(status_code=400, detail="Offboarding request_id is required")
    if target_user.tenant_id is None:
        raise HTTPException(status_code=409, detail="Target user has no company")
    if target_user.role == "platform_admin":
        raise HTTPException(status_code=400, detail="Platform administrators cannot be offboarded from a tenant")
    if target_user.id == actor.id:
        raise HTTPException(status_code=400, detail="Administrators cannot offboard their own account")
    _validate_successor(target_user=target_user, successor=successor)

    agents = await _lock_owned_agents(
        db,
        target_user_id=target_user.id,
        tenant_id=target_user.tenant_id,
    )
    current_agent_ids = [agent.id for agent in agents]
    if set(current_agent_ids) != set(expected_agent_ids):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "offboarding_preview_stale",
                "expected_agent_ids": sorted(str(value) for value in expected_agent_ids),
                "current_agent_ids": sorted(str(value) for value in current_agent_ids),
            },
        )

    transferred_agent_ids: list[uuid.UUID] = []
    for agent in agents:
        receipt = await transfer_loaded_agent_owner(
            db,
            agent=agent,
            new_owner=successor,
            actor=actor,
            reason=clean_reason,
            expected_owner_id=target_user.id,
            mode="user_offboarding",
            request_id=clean_request_id,
            flush=False,
        )
        if receipt.status == "transferred":
            transferred_agent_ids.append(agent.id)

    now = datetime.now(timezone.utc)
    revocations = await _revoke_user_authority(
        db,
        target_user=target_user,
        actor_user=actor,
        now=now,
    )
    was_active = bool(target_user.is_active)
    target_user.is_active = False
    db.add(
        AuditLog(
            user_id=actor.id,
            tenant_id=target_user.tenant_id,
            action="user:offboarded",
            details={
                "schema": "hive.user_offboarding.v1",
                "target_user_id": str(target_user.id),
                "successor_user_id": str(successor.id),
                "expected_agent_ids": [str(value) for value in expected_agent_ids],
                "transferred_agent_ids": [str(value) for value in transferred_agent_ids],
                "revocations": {
                    "agent_permissions": revocations.agent_permissions,
                    "resource_permissions": revocations.resource_permissions,
                    "knowledge_grants": revocations.knowledge_grants,
                    "refresh_tokens": revocations.refresh_tokens,
                    "external_principals": revocations.external_principals,
                    "local_bridge_connections": revocations.local_bridge_connections,
                    "runtime_tasks": revocations.runtime_tasks,
                    "pending_approvals": revocations.pending_approvals,
                },
                "runtime_task_signals": [
                    _runtime_task_signal_to_dict(value) for value in revocations.runtime_task_signals
                ],
                "reason": clean_reason,
                "request_id": clean_request_id,
                "already_inactive": not was_active,
            },
        )
    )
    await db.flush()
    return UserOffboardingReceipt(
        status="deactivated" if was_active else "already_inactive",
        user_id=target_user.id,
        successor_user_id=successor.id,
        transferred_agent_ids=transferred_agent_ids,
        revocations=revocations,
        request_id=clean_request_id,
    )


async def build_user_offboarding_preview(
    db: AsyncSession,
    *,
    target_user: User,
    actor: User,
) -> UserOffboardingPreview:
    if target_user.tenant_id is None:
        raise HTTPException(status_code=409, detail="Target user has no company")
    tenant_id = target_user.tenant_id
    agents = list(
        (
            await db.execute(
                select(Agent)
                .where(Agent.tenant_id == tenant_id, agent_owned_by_clause(target_user.id))
                .order_by(Agent.name.asc(), Agent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    successors = list(
        (
            await db.execute(
                select(User)
                .where(
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                    User.role.in_(("org_admin", "platform_admin")),
                    User.id != target_user.id,
                )
                .order_by(User.display_name.asc(), User.username.asc())
            )
        )
        .scalars()
        .all()
    )

    async def count_where(model, *clauses) -> int:
        result = await db.execute(select(func.count()).select_from(model).where(*clauses))
        return int(result.scalar() or 0)

    knowledge_clause = (
        ((KnowledgeGrant.scope_type == "person") & (KnowledgeGrant.scope_id == target_user.id))
        | ((KnowledgeGrant.grantee_type == "user") & (KnowledgeGrant.grantee_id == target_user.id))
        | (KnowledgeGrant.requester_user_id == target_user.id)
    )
    default_successor_id = actor.id if actor.tenant_id == tenant_id and actor.id != target_user.id else None
    if default_successor_id not in {user.id for user in successors}:
        default_successor_id = None
    return UserOffboardingPreview(
        user_id=target_user.id,
        display_name=target_user.display_name,
        is_active=target_user.is_active,
        owned_agents=[
            {
                "id": str(agent.id),
                "name": agent.name,
                "status": str(agent.status),
                "agent_class": str(agent.agent_class),
            }
            for agent in agents
        ],
        eligible_successors=[
            {
                "id": str(user.id),
                "display_name": user.display_name,
                "email": user.email,
                "role": user.role,
            }
            for user in successors
        ],
        default_successor_id=default_successor_id,
        agent_permissions=await count_where(
            AgentPermission,
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.scope_type == "user",
            AgentPermission.scope_id == target_user.id,
        ),
        resource_permissions=await count_where(
            ResourcePermission,
            ResourcePermission.tenant_id == tenant_id,
            ResourcePermission.principal_type == "user",
            ResourcePermission.principal_id == target_user.id,
        ),
        knowledge_grants=await count_where(
            KnowledgeGrant,
            KnowledgeGrant.tenant_id == tenant_id,
            KnowledgeGrant.revoked_at.is_(None),
            knowledge_clause,
        ),
        refresh_tokens=await count_where(
            RefreshToken,
            RefreshToken.user_id == target_user.id,
            RefreshToken.revoked.is_(False),
        ),
        external_principals=await count_where(
            ExternalPrincipal,
            ExternalPrincipal.tenant_id == tenant_id,
            ExternalPrincipal.linked_user_id == target_user.id,
        ),
        local_bridge_connections=await count_where(
            LocalAgentBridgeConnection,
            LocalAgentBridgeConnection.tenant_id == tenant_id,
            LocalAgentBridgeConnection.user_id == target_user.id,
            LocalAgentBridgeConnection.status == "active",
        ),
        runtime_tasks=await count_where(
            RuntimeTask,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.root_user_id == target_user.id,
            RuntimeTask.status.in_(("pending", "running", "suspended", "resumable")),
        ),
        pending_approvals=await count_where(
            ApprovalRequest,
            ApprovalRequest.tenant_id == tenant_id,
            ApprovalRequest.requested_by == target_user.id,
            ApprovalRequest.status == "pending",
        ),
    )


async def publish_user_offboarding_runtime_cancellations(receipt: UserOffboardingReceipt) -> None:
    """Best-effort wakeups after the revocation transaction is committed.

    The DB status and claim-version fence are authoritative. Redis signals only
    shorten the stop latency for work already executing in another process.
    """

    from app.services.runtime_control_bus import (
        publish_business_task_cancel,
        publish_delegation_cancel,
        publish_subagent_cancel,
        publish_web_chat_cancel,
    )

    chat_task_types = {"web_chat_turn", "goal_continuation", "team_member", "advanced_plan"}
    signal_limit = asyncio.Semaphore(16)

    async def publish_one(signal: RuntimeTaskRevocationSignal) -> None:
        async with signal_limit:
            try:
                if (
                    signal.task_type in chat_task_types
                    and signal.parent_agent_id is not None
                    and signal.parent_session_id
                ):
                    await publish_web_chat_cancel(
                        run_id=signal.task_id,
                        agent_id=signal.parent_agent_id,
                        session_id=signal.parent_session_id,
                        user_id=receipt.user_id,
                    )
                elif signal.task_type == "business_task" and signal.business_task_id is not None:
                    await publish_business_task_cancel(
                        task_id=signal.business_task_id,
                        runtime_task_id=signal.task_id,
                    )
                elif signal.task_type in {"delegation", "a2a_delegation"}:
                    await publish_delegation_cancel(
                        task_id=signal.task_id.hex,
                        parent_agent_id=signal.parent_agent_id,
                    )
                elif signal.task_type == "subagent":
                    await publish_subagent_cancel(
                        run_id=signal.task_id,
                        parent_agent_id=signal.parent_agent_id,
                    )
            except Exception as exc:  # noqa: BLE001 - the durable fence already revoked authority.
                logger.warning(
                    "User offboarding runtime cancellation wakeup failed for %s: %s",
                    signal.task_id,
                    exc,
                )

    await asyncio.gather(*(publish_one(signal) for signal in receipt.revocations.runtime_task_signals))
