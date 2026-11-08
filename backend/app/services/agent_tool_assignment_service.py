"""Helpers for idempotent AgentTool assignment updates."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.tool import AgentTool


async def lock_agent_tool_config_write(db: AsyncSession, agent_id: uuid.UUID, *, current_user=None) -> Agent:
    """Share offboarding's Agent -> session advisory -> AgentTool lock order."""
    from fastapi import HTTPException
    from app.core.permissions import require_agent_manage_access
    from app.services.runtime_user_identity import load_runtime_user

    if current_user is not None:
        await require_agent_manage_access(db, current_user, agent_id)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id).with_for_update(key_share=True)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "Agent not found")
    if current_user is not None:
        if await load_runtime_user(db, current_user.id) is None:
            raise HTTPException(403, "Manage access required")
    return agent


async def ensure_agent_tool_assignment(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    enabled: bool | None = True,
    source: str | None = "system",
    installed_by_agent_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    config: dict | Callable[[dict], dict] | None = None,
    merge_config: bool = True,
    quarantine_for_mcp_trust: bool = False,
    create: bool = True,
    locked_agent: Agent | None = None,
) -> tuple[AgentTool | None, bool]:
    """Create or update a single AgentTool row without duplicating assignments."""
    agent = locked_agent if locked_agent is not None else await lock_agent_tool_config_write(db, agent_id)
    if tenant_id is None:
        tenant_id = agent.tenant_id

    assignment, created, _changed = await db.run_sync(
        _write_agent_tool_config, agent_id=agent_id, tool_id=tool_id,
        tenant_id=tenant_id, enabled=enabled, source=source, installed_by_agent_id=installed_by_agent_id,
        config=config, merge_config=merge_config, quarantine_for_mcp_trust=quarantine_for_mcp_trust, create=create,
    )
    return assignment, created


def _write_agent_tool_config(
    db, *, agent_id, tool_id, tenant_id=None, enabled=True, source="system",
    installed_by_agent_id=None, config=None, merge_config=True, quarantine_for_mcp_trust=False, create=True,
) -> tuple[AgentTool | None, bool, bool]:
    """Mutate the assignment after its caller has locked the Agent row."""

    result = db.execute(
        select(AgentTool).where(
            AgentTool.agent_id == agent_id,
            AgentTool.tool_id == tool_id,
        ).with_for_update().execution_options(populate_existing=True)
    )
    assignment = result.scalar_one_or_none()
    created = assignment is None
    if created and not create:
        return None, False, False

    if assignment is None:
        assignment = AgentTool(
            agent_id=agent_id,
            tenant_id=tenant_id,
            tool_id=tool_id,
            enabled=enabled if enabled is not None else True,
            mcp_trust_requested_enabled=True if quarantine_for_mcp_trust else None,
            source=source or "system",
            installed_by_agent_id=installed_by_agent_id,
            config={},
        )
        db.add(assignment)

    if tenant_id is not None and getattr(assignment, "tenant_id", None) is None:
        assignment.tenant_id = tenant_id
    if quarantine_for_mcp_trust and getattr(assignment, "mcp_trust_requested_enabled", None) is None:
        assignment.mcp_trust_requested_enabled = bool(assignment.enabled)
    if enabled is not None:
        assignment.enabled = enabled
    if source and (assignment.source == "system" or source != "system"):
        assignment.source = source
    if installed_by_agent_id is not None:
        assignment.installed_by_agent_id = installed_by_agent_id
    previous = assignment.config
    if config is not None:
        if callable(config):
            assignment.config = config(dict(assignment.config or {}))
        else:
            from app.services.tool_config_service import merge_agent_tool_config_secrets

            merged = merge_agent_tool_config_secrets(config, assignment.config, {}, platform_tool=False)
            assignment.config = merged if merge_config else {key: merged[key] for key in config if key in merged}
    return assignment, created, created or assignment.config != previous
