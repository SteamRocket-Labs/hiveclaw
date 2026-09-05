"""RLS helpers for public channel webhook entry points."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant import Tenant


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

    A channel row whose company is retired or missing resolves like a missing
    row: a previously valid signed/configured channel must not carry provider
    ingress into a deactivated company's Agent runtime. Channel credentials are
    only scrubbed on tenant deletion — the admin toggle deactivates without
    scrubbing, so this liveness read is the boundary that holds there.
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
                # Legacy rows predate ChannelConfig.tenant_id.  The public
                # webhook has resolved authority through the owning Agent, so
                # expose that same trusted tenant to the downstream durable
                # inbox instead of leaving a fail-open/None identity gap.
                config.tenant_id = tenant_id

        if tenant_id is not None:
            tenant_active = (
                await bypass_db.execute(select(Tenant.is_active).where(Tenant.id == tenant_id))
            ).scalar_one_or_none()
            if tenant_active is not True:
                return None

    if tenant_id is not None:
        await pin_rls_tenant_context(db, tenant_id)
    return config
