"""Tool config resolution service — per-tenant tool configuration isolation.

Merge hierarchy:
  Tool.config (platform default) < TenantToolConfig.config (tenant override) < AgentTool.config (agent override)

Every runtime tool config read MUST go through resolve_tool_config() to
ensure tenant isolation. Direct reads from Tool.config leak API keys
across tenants.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool

logger = logging.getLogger(__name__)


async def resolve_tool_config(
    tool_name: str,
    tenant_id: uuid.UUID | None = None,
    *,
    agent_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Resolve effective tool config with full tenant isolation.

    If tenant_id is not provided but agent_id is, resolves tenant_id from agent.
    Merges: Tool.config → TenantToolConfig.config → AgentTool.config (if agent_id given).
    """
    should_close = db is None
    if db is None:
        db = async_session()

    try:
        # 1. Get base tool
        result = await db.execute(
            select(Tool).where(Tool.name == tool_name, Tool.tenant_id.is_(None)).limit(1)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            # Try tenant-scoped tool
            if tenant_id:
                result = await db.execute(
                    select(Tool).where(Tool.name == tool_name, Tool.tenant_id == tenant_id).limit(1)
                )
                tool = result.scalar_one_or_none()
            if not tool:
                return {}

        base_config = dict(tool.config or {})

        # 2. Resolve tenant_id from agent if needed
        effective_tenant_id = tenant_id
        if not effective_tenant_id and agent_id:
            from app.models.agent import Agent

            agent_r = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
            effective_tenant_id = agent_r.scalar_one_or_none()

        # 3. Merge tenant-level override
        if effective_tenant_id:
            ttc_r = await db.execute(
                select(TenantToolConfig).where(
                    TenantToolConfig.tenant_id == effective_tenant_id,
                    TenantToolConfig.tool_id == tool.id,
                )
            )
            ttc = ttc_r.scalar_one_or_none()
            if ttc and ttc.config:
                base_config.update(ttc.config)

        # 4. Merge agent-level override (if requested)
        if agent_id:
            at_r = await db.execute(
                select(AgentTool.config).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool.id,
                )
            )
            agent_config = at_r.scalar_one_or_none()
            if agent_config:
                base_config.update(agent_config)

        return base_config
    finally:
        if should_close:
            await db.close()


async def get_or_create_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_id: uuid.UUID,
) -> TenantToolConfig:
    """Get existing or create new TenantToolConfig record."""
    result = await db.execute(
        select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id == tool_id,
        )
    )
    ttc = result.scalar_one_or_none()
    if ttc:
        return ttc

    ttc = TenantToolConfig(
        tenant_id=tenant_id,
        tool_id=tool_id,
        config={},
        enabled=True,
    )
    db.add(ttc)
    await db.flush()
    return ttc


async def update_tenant_tool_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    tool_id: uuid.UUID,
    *,
    config: dict | None = None,
    enabled: bool | None = None,
) -> TenantToolConfig:
    """Create or update tenant-level tool config."""
    ttc = await get_or_create_tenant_tool_config(db, tenant_id, tool_id)
    if config is not None:
        ttc.config = config
    if enabled is not None:
        ttc.enabled = enabled
    await db.flush()
    return ttc


async def resolve_tool_config_for_tenant_display(
    db: AsyncSession,
    tool: Tool,
    tenant_id: uuid.UUID | None,
) -> tuple[dict, bool]:
    """Resolve effective config and enabled status for frontend display.

    Returns (merged_config, effective_enabled).
    """
    base_config = dict(tool.config or {})
    effective_enabled = tool.enabled

    if not tenant_id:
        return base_config, effective_enabled

    result = await db.execute(
        select(TenantToolConfig).where(
            TenantToolConfig.tenant_id == tenant_id,
            TenantToolConfig.tool_id == tool.id,
        )
    )
    ttc = result.scalar_one_or_none()
    if ttc:
        if ttc.config:
            base_config.update(ttc.config)
        effective_enabled = ttc.enabled

    return base_config, effective_enabled
