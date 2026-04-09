"""Resolve tool runtime context from current agent execution state."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.core.execution_context import get_execution_identity
from app.database import async_session
from app.models.agent import Agent
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
    ) -> ToolExecutionContext:
        tenant_id = None
        try:
            async with async_session() as db:
                result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
                tenant = result.scalar_one_or_none()
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
        )
