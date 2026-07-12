"""Helpers for idempotent AgentTool assignment updates."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.tool import AgentTool


async def ensure_agent_tool_assignment(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    enabled: bool = True,
    source: str = "system",
    installed_by_agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    config: dict | None = None,
    merge_config: bool = True,
    quarantine_for_mcp_trust: bool = False,
) -> tuple[AgentTool, bool]:
    """Create or update a single AgentTool row without duplicating assignments."""
    if tenant_id is None:
        tenant_result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        resolved_tenant_id = tenant_result.scalar_one_or_none()
        tenant_id = resolved_tenant_id if isinstance(resolved_tenant_id, uuid.UUID) else None

    result = await db.execute(
        select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id == tool_id,
        )
    )
    assignment = result.scalar_one_or_none()
    created = assignment is None

    if assignment is None:
        assignment = AgentTool(
            agent_id=agent_id,
            tenant_id=tenant_id,
            tool_id=tool_id,
            enabled=enabled,
            mcp_trust_requested_enabled=True if quarantine_for_mcp_trust else None,
            source=source,
            installed_by_agent_id=installed_by_agent_id,
            config=config or {},
        )
        db.add(assignment)
        return assignment, created

    if tenant_id is not None and getattr(assignment, "tenant_id", None) is None:
        assignment.tenant_id = tenant_id
    if quarantine_for_mcp_trust and getattr(assignment, "mcp_trust_requested_enabled", None) is None:
        assignment.mcp_trust_requested_enabled = bool(assignment.enabled)
    assignment.enabled = enabled
    if source and (assignment.source == "system" or source != "system"):
        assignment.source = source
    if installed_by_agent_id is not None:
        assignment.installed_by_agent_id = installed_by_agent_id
    if config is not None:
        if merge_config:
            merged = dict(assignment.config or {})
            merged.update(config)
            assignment.config = merged
        else:
            assignment.config = dict(config)
    return assignment, created
