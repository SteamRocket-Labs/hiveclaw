"""Resolve tool runtime context from current agent execution state."""

from __future__ import annotations

import logging
import uuid

from app.core.execution_context import get_execution_identity
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.runtime import ToolExecutionContext
from app.tools.workspace import ensure_workspace

logger = logging.getLogger(__name__)


class ToolRuntimeResolver:
    """Build ToolExecutionContext from agent/user identifiers."""

    async def resolve(
        self,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str | None = None,
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
            logger.debug("Failed to resolve tenant_id for tool execution: %s", exc)

        workspace = await ensure_workspace(agent_id, tenant_id=tenant_id)
        return ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=tenant_id,
            workspace=workspace,
            execution_identity=get_execution_identity(),
            session_id=session_id,
        )
