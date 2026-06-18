"""RLS helpers for public channel webhook entry points."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig


async def load_public_agent_channel_config(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    channel_type: str,
) -> ChannelConfig | None:
    """Load an agent channel config from a public webhook and pin its tenant.

    Public channel webhooks authenticate with provider signatures/secrets rather
    than Hive JWTs, so `get_db()` starts fail-closed with no request tenant.
    We use a narrow audited bypass only to resolve the channel record and owning
    tenant from the untrusted URL agent id, then immediately return to normal
    tenant-scoped RLS before provider validation and message ingestion continue.
    """
    async with enter_rls_bypass(
        db,
        reason=f"public {channel_type} webhook channel lookup for agent {agent_id}",
    ) as bypass_db:
        result = await bypass_db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == channel_type,
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            return None

        tenant_id = getattr(config, "tenant_id", None)
        if tenant_id is None:
            agent_result = await bypass_db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
            tenant_id = agent_result.scalar_one_or_none()

    if tenant_id is not None:
        await pin_rls_tenant_context(db, tenant_id)
    return config
