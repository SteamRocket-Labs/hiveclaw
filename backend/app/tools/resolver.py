"""Resolve tool runtime context from current agent execution state."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.execution_context import get_execution_identity
from app.database import async_session, enter_rls_bypass
from app.models.runtime_task import RuntimeTask
from app.services.tenant_resolver import resolve_tenant_for_agent
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
        async with async_session() as db:
            async with enter_rls_bypass(db, reason="tool runtime context — inherit RuntimeTask budget envelope"):
                task = await db.get(RuntimeTask, task_id)
                if task is None or task.budget_run_id is None:
                    return None
                return str(task.budget_run_id)
    except Exception as exc:
        logger.warning("[Governance] budget resolution failed for runtime task %s: %s", runtime_task_id, exc)
        return None


class ToolRuntimeResolver:
    """Build ToolExecutionContext from agent/user identifiers."""

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
        tenant_id = None
        try:
            tenant = await resolve_tenant_for_agent(agent_id)
            if tenant:
                tenant_id = str(tenant)
        except Exception as exc:
            # Cutover hardening (stage-3): after the role flip a resolve failure
            # here leaves the tool running with an empty tenant GUC — every
            # governed query then fail-closes. Surface it loudly (was a silent
            # debug line) instead of swallowing the make-or-break signal.
            logger.warning(
                "[Governance] tenant resolution failed for tool execution (agent=%s): %s", agent_id, exc
            )

        workspace = await ensure_workspace(agent_id, tenant_id=tenant_id)
        resolved_budget_run_id = str(budget_run_id) if budget_run_id else await _resolve_budget_run_id_from_runtime_task(runtime_task_id)
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
        )
