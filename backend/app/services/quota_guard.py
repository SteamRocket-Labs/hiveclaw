"""Usage quota guard — token-only enforcement.

The only usage limit is token consumption per employee (daily/monthly).
All other limits (message count, LLM calls, agent count, TTL) have been removed.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session


class QuotaExceeded(Exception):
    """Raised when a quota limit is reached."""

    def __init__(self, message: str, quota_type: str = "token"):
        self.message = message
        self.quota_type = quota_type
        super().__init__(message)


async def check_user_token_quota(user_id: uuid.UUID) -> None:
    """Check if user has remaining daily/monthly token budget.

    Admin users (org_admin, platform_admin) are exempt.
    Resets counters automatically when the period rolls over.
    """
    from app.models.user import User

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return

        # Admin users are exempt
        if user.role in ("platform_admin", "org_admin"):
            return

        now = datetime.now(timezone.utc)

        # Daily reset
        if user.tokens_reset_at and now.date() > user.tokens_reset_at.date():
            user.tokens_used_today = 0
            await db.commit()

        # Monthly reset
        if user.tokens_reset_at and now.month != user.tokens_reset_at.month:
            user.tokens_used_month = 0
            await db.commit()

        # Check daily token limit
        if user.quota_tokens_per_day and user.tokens_used_today >= user.quota_tokens_per_day:
            raise QuotaExceeded(
                f"Daily token limit reached ({user.tokens_used_today:,}/{user.quota_tokens_per_day:,}).",
                quota_type="tokens_daily",
            )

        # Check monthly token limit
        if user.quota_tokens_per_month and user.tokens_used_month >= user.quota_tokens_per_month:
            raise QuotaExceeded(
                f"Monthly token limit reached ({user.tokens_used_month:,}/{user.quota_tokens_per_month:,}).",
                quota_type="tokens_monthly",
            )


# ── Heartbeat floor enforcement (kept — pending discussion) ───────

async def enforce_heartbeat_floor(tenant_id: uuid.UUID, floor: int | None = None, db=None) -> int:
    """Enforce heartbeat floor on all agents in the tenant."""
    from app.models.agent import Agent
    from app.models.tenant import Tenant

    async def _enforce(session, floor_val):
        if floor_val is None:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            if not tenant:
                return 0
            floor_val = tenant.min_heartbeat_interval_minutes

        agents_result = await session.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.heartbeat_interval_minutes < floor_val,
            )
        )
        agents = agents_result.scalars().all()
        for agent in agents:
            agent.heartbeat_interval_minutes = floor_val

        if agents:
            await session.commit()
        return len(agents)

    if db is not None:
        return await _enforce(db, floor)
    else:
        async with async_session() as new_db:
            return await _enforce(new_db, floor)
