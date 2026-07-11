"""Resolve tool runtime context from current agent execution state."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.core.execution_context import get_execution_identity
from app.database import async_session, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.tenant_admission import RuntimeTenantPreconditionError
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_runtime_task
from app.tools.runtime import ToolExecutionContext
from app.tools.workspace import ensure_workspace

logger = logging.getLogger(__name__)


async def _resolve_budget_run_id_from_runtime_task(runtime_task_id: str | None) -> str | None:
    if not runtime_task_id:
        return None
    try:
        task_id = uuid.UUID(str(runtime_task_id))
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        tenant_id = await resolve_tenant_for_runtime_task(
            task_id,
            session_factory=async_session,
        )
        if tenant_id is None:
            return None
        async with tenant_scoped_session(
            tenant_id,
            session_factory=async_session,
            require_tenant=True,
            source="tool_runtime_budget_envelope",
        ) as db:
            result = await db.execute(
                select(RuntimeTask.budget_run_id).where(
                    RuntimeTask.id == task_id,
                    RuntimeTask.tenant_id == tenant_id,
                )
            )
            budget_run_id = result.scalar_one_or_none()
            return str(budget_run_id) if budget_run_id is not None else None
    except Exception as exc:
        logger.warning("[Governance] budget resolution failed for runtime task %s: %s", runtime_task_id, exc)
        return None


class ToolRuntimeResolver:
    """Build ToolExecutionContext from agent/user identifiers."""

    def __init__(self, *, workspace_authority_loader=None) -> None:
        self.workspace_authority_loader = workspace_authority_loader or _load_workspace_authority_scope

    async def resolve(
        self,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str | None = None,
        permission_profile: Any | None = None,
        turn_id: str | None = None,
        runtime_task_id: str | None = None,
        budget_run_id: str | None = None,
        origin_channel: str | None = None,
        round_state: dict[str, Any] | None = None,
        t0_refs: tuple[str, ...] = (),
    ) -> ToolExecutionContext:
        # Tool-execution tenant chokepoint (RLS 阶段1). We hold only ``agent_id``
        # but need its tenant to scope every downstream governed query — and a
        # bare read of ``agents`` fails closed once the app connects as a
        # non-owner role. ``resolve_tenant_for_agent`` is the sanctioned narrow
        # ``enter_rls_bypass`` lookup (single agent row by PK, audited).
        try:
            tenant = await resolve_tenant_for_agent(agent_id)
        except Exception as exc:
            logger.warning("[Governance] tenant resolution failed for tool execution (agent=%s): %s", agent_id, exc)
            raise RuntimeTenantPreconditionError(
                reason_code="agent_tenant_resolution_failed",
                message=f"tool runtime is blocked because tenant resolution failed for agent {agent_id}.",
                source="tool_runtime",
                agent_id=agent_id,
            ) from exc
        if tenant is None:
            raise RuntimeTenantPreconditionError(
                reason_code="agent_tenant_missing",
                message=f"tool runtime is blocked because agent {agent_id} has no tenant.",
                source="tool_runtime",
                agent_id=agent_id,
            )
        tenant_id = str(tenant)

        workspace = await ensure_workspace(agent_id, tenant_id=tenant_id)
        try:
            session_uuid = uuid.UUID(str(session_id)) if session_id else None
        except (TypeError, ValueError):
            session_uuid = None
        workspace_authority_scope = await self.workspace_authority_loader(
            tenant_id=tenant,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_uuid,
        )
        resolved_budget_run_id = (
            str(budget_run_id) if budget_run_id else await _resolve_budget_run_id_from_runtime_task(runtime_task_id)
        )
        return ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=tenant_id,
            workspace=workspace,
            execution_identity=get_execution_identity(),
            session_id=session_id,
            permission_profile=permission_profile,
            turn_id=turn_id,
            runtime_task_id=runtime_task_id,
            budget_run_id=resolved_budget_run_id,
            origin_channel=origin_channel,
            round_state=dict(round_state or {}),
            t0_refs=tuple(str(ref) for ref in (t0_refs or ()) if str(ref).strip()),
            workspace_authority_scope=workspace_authority_scope,
        )


async def _load_workspace_authority_scope(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
):
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="tool_workspace_resource_authority",
    ) as authority_db:
        user = (await authority_db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            raise RuntimeTenantPreconditionError(
                reason_code="tool_requester_not_found",
                message="tool runtime is blocked because the authenticated requester could not be resolved.",
                source="tool_runtime",
                agent_id=agent_id,
            )
        from app.services.workspace_resource_authority import load_workspace_authority_scope

        return await load_workspace_authority_scope(
            authority_db,
            user,
            agent_id=agent_id,
            session_id=session_id,
        )
