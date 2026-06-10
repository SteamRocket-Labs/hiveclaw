"""Resolve governance context and dependencies for tool execution."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.core.policy import write_audit_event
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.services.approval_service import approval_service
from app.services.capability_gate import check_capability
from app.tools.governance import GovernanceDependencies, ToolGovernanceContext
from app.tools.runtime import ToolExecutionContext

logger = logging.getLogger(__name__)


class ToolGovernanceResolver:
    """Build governance context and dependency wrappers for tool runtime."""

    async def build_context(
        self,
        *,
        runtime_context: ToolExecutionContext,
        tool_name: str,
        arguments: dict,
        delegation_token: Any | None = None,
    ) -> ToolGovernanceContext:
        return ToolGovernanceContext(
            agent_id=runtime_context.agent_id,
            user_id=runtime_context.user_id,
            tenant_id=runtime_context.tenant_id,
            tool_name=tool_name,
            arguments=arguments,
            delegation_token=delegation_token,
        )

    def build_dependencies(self) -> GovernanceDependencies:
        async def _resolve_security_zone(agent_id: uuid.UUID) -> str:
            try:
                async with (
                    async_session() as db,
                    enter_rls_bypass(db, reason=f"security-zone resolution for agent {agent_id}"),
                ):
                    result = await db.execute(select(Agent).where(Agent.id == agent_id))
                    agent = result.scalar_one_or_none()
                    zone = getattr(agent, "security_zone", None)
                    if not zone:
                        logger.warning(
                            "[Governance] Agent %s has no security_zone set — defaulting to 'restricted'", agent_id
                        )
                    return zone or "restricted"
            except Exception as exc:
                logger.error(
                    "[Governance] Failed to resolve security zone for %s: %s — defaulting to 'restricted'",
                    agent_id,
                    exc,
                )
                return "restricted"

        async def _check_capability(tenant_id: uuid.UUID, agent_id: uuid.UUID, tool_name: str):
            async with tenant_scoped_session(tenant_id) as db:
                return await check_capability(db, tenant_id, agent_id, tool_name)

        async def _write_audit_event(**kwargs) -> None:
            async with async_session() as db:
                await write_audit_event(db, **kwargs)
                await db.commit()

        async def _request_approval(
            *,
            agent_id: uuid.UUID,
            user_id: uuid.UUID,
            tool_name: str,
            arguments: dict,
            capability: str,
            reason: str | None = None,
        ) -> dict:
            async with async_session() as db, enter_rls_bypass(db, reason=f"approval request for agent {agent_id}"):
                result = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = result.scalar_one_or_none()
                if not agent:
                    return {"allowed": False, "message": "Agent not found"}
                outcome = await approval_service.request_approval(
                    db,
                    agent,
                    action_type=capability,
                    details={
                        "tool": tool_name,
                        "args": arguments,
                        "requested_by": str(user_id),
                        "reason": reason,
                    },
                )
                await db.commit()
                return outcome

        async def _resolve_mcp_tool_mode(
            agent_id: uuid.UUID,
            tool_name: str,
            arguments: dict,
        ) -> str | None:
            # Closure A2: feed the governance MCP gate. call_mcp_tool is the
            # generic entry — the governed object is the target tool inside
            # its arguments; dynamic MCP tool names govern themselves.
            target = arguments.get("tool_name") if tool_name == "call_mcp_tool" else tool_name
            if not target or not isinstance(target, str):
                return None
            from app.models.tool import AgentTool, Tool
            from app.services.mcp_server_service import resolve_agent_mcp_tool_mode

            async with (
                async_session() as db,
                enter_rls_bypass(db, reason=f"MCP tool-mode resolution for agent {agent_id}"),
            ):
                result = await db.execute(
                    select(Tool)
                    .join(AgentTool, AgentTool.tool_id == Tool.id)
                    .where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.enabled.is_(True),
                        Tool.name == target,
                        Tool.type == "mcp",
                        Tool.enabled.is_(True),
                    )
                )
                tool = result.scalar_one_or_none()
                if tool is None:
                    return None  # not an MCP tool — fall through to the capability gate
                return await resolve_agent_mcp_tool_mode(db, agent_id, tool)

        return GovernanceDependencies(
            resolve_security_zone=_resolve_security_zone,
            check_capability=_check_capability,
            write_audit_event=_write_audit_event,
            request_approval=_request_approval,
            resolve_mcp_tool_mode=_resolve_mcp_tool_mode,
        )
