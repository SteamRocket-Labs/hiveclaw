"""Usage quota guard — token-only enforcement.

Token limits exist at three scopes:
* tenant/company hard cap
* agent hard cap
* employee/user cap

All other limits (message count, LLM calls, agent count, TTL) have been removed.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session, tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent, resolve_tenant_for_user


class QuotaExceeded(Exception):
    """Raised when a quota limit is reached."""

    def __init__(self, message: str, quota_type: str = "token"):
        self.message = message
        self.quota_type = quota_type
        super().__init__(message)


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _reset_user_or_tenant_counter(subject, *, now: datetime) -> bool:
    changed = False
    reset_at = getattr(subject, "tokens_reset_at", None)
    if reset_at is None:
        subject.tokens_reset_at = now
        return True
    if now.date() > reset_at.date():
        subject.tokens_used_today = 0
        changed = True
    if now.month != reset_at.month or now.year != reset_at.year:
        subject.tokens_used_month = 0
        changed = True
    if changed:
        subject.tokens_reset_at = now
    return changed


def _reset_agent_counter(agent, *, now: datetime) -> bool:
    changed = False
    if agent.last_daily_reset is None:
        agent.last_daily_reset = now
        changed = True
    elif now.date() > agent.last_daily_reset.date():
        agent.tokens_used_today = 0
        agent.last_daily_reset = now
        changed = True
    if agent.last_monthly_reset is None:
        agent.last_monthly_reset = now
        changed = True
    elif now.month != agent.last_monthly_reset.month or now.year != agent.last_monthly_reset.year:
        agent.tokens_used_month = 0
        agent.last_monthly_reset = now
        changed = True
    return changed


def _raise_if_limit_reached(
    *,
    scope_label: str,
    quota_type: str,
    used: int | None,
    limit: int | None,
) -> None:
    if limit and (used or 0) >= limit:
        raise QuotaExceeded(
            f"{scope_label} token limit reached ({(used or 0):,}/{limit:,}).",
            quota_type=quota_type,
        )


async def check_user_token_quota(
    user_id: uuid.UUID | str | None,
    *,
    agent_id: uuid.UUID | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
) -> None:
    """Check if the invocation has remaining daily/monthly token budget.

    Admin users (org_admin, platform_admin) are exempt only from their personal
    user-level cap. Tenant and agent hard caps are never bypassed by role.
    Resets counters automatically when the period rolls over.
    """
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User

    checked_user_id = _coerce_uuid(user_id)
    checked_agent_id = _coerce_uuid(agent_id)
    checked_tenant_id = _coerce_uuid(tenant_id)
    if checked_user_id is None and checked_agent_id is None and checked_tenant_id is None:
        return

    if checked_tenant_id is None and checked_agent_id is not None:
        checked_tenant_id = await resolve_tenant_for_agent(
            checked_agent_id,
            session_factory=async_session,
        )
    if checked_tenant_id is None and checked_user_id is not None:
        checked_tenant_id = await resolve_tenant_for_user(
            checked_user_id,
            session_factory=async_session,
        )
    if checked_tenant_id is None:
        return

    async with tenant_scoped_session(
        checked_tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="token_quota_check",
    ) as db:
        user = None
        if checked_user_id is not None:
            user = (
                await db.execute(
                    select(User).where(
                        User.id == checked_user_id,
                        User.tenant_id == checked_tenant_id,
                    )
                )
            ).scalar_one_or_none()

        agent = None
        if checked_agent_id is not None:
            agent = (
                await db.execute(
                    select(Agent).where(
                        Agent.id == checked_agent_id,
                        Agent.tenant_id == checked_tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if agent is not None:
                checked_tenant_id = checked_tenant_id or agent.tenant_id
                if user is None:
                    checked_user_id = agent.owner_user_id or agent.creator_id
                    user = (
                        await db.execute(
                            select(User).where(
                                User.id == checked_user_id,
                                User.tenant_id == checked_tenant_id,
                            )
                        )
                    ).scalar_one_or_none()

        if checked_tenant_id is None and user is not None:
            checked_tenant_id = user.tenant_id

        tenant = None
        if checked_tenant_id is not None:
            tenant = (await db.execute(select(Tenant).where(Tenant.id == checked_tenant_id))).scalar_one_or_none()

        if user is None and agent is None and tenant is None:
            return
        now = datetime.now(timezone.utc)
        changed = False

        if tenant is not None:
            changed = _reset_user_or_tenant_counter(tenant, now=now) or changed
            _raise_if_limit_reached(
                scope_label="Tenant daily",
                quota_type="tenant_tokens_daily",
                used=tenant.tokens_used_today,
                limit=tenant.quota_tokens_per_day,
            )
            _raise_if_limit_reached(
                scope_label="Tenant monthly",
                quota_type="tenant_tokens_monthly",
                used=tenant.tokens_used_month,
                limit=tenant.quota_tokens_per_month,
            )

        if agent is not None:
            changed = _reset_agent_counter(agent, now=now) or changed
            _raise_if_limit_reached(
                scope_label="Agent daily",
                quota_type="agent_tokens_daily",
                used=agent.tokens_used_today,
                limit=agent.quota_tokens_per_day,
            )
            _raise_if_limit_reached(
                scope_label="Agent monthly",
                quota_type="agent_tokens_monthly",
                used=agent.tokens_used_month,
                limit=agent.quota_tokens_per_month,
            )

        if user is not None:
            changed = _reset_user_or_tenant_counter(user, now=now) or changed
            if user.role not in ("platform_admin", "org_admin"):
                _raise_if_limit_reached(
                    scope_label="Daily",
                    quota_type="tokens_daily",
                    used=user.tokens_used_today,
                    limit=user.quota_tokens_per_day,
                )
                _raise_if_limit_reached(
                    scope_label="Monthly",
                    quota_type="tokens_monthly",
                    used=user.tokens_used_month,
                    limit=user.quota_tokens_per_month,
                )

        # tenant_scoped_session commits counter resets atomically on exit.
