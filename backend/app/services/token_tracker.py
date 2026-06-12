"""Token usage tracking — records consumption against both Agent (stats) and User (enforcement).

All LLM call paths (web chat, heartbeat, triggers, A2A) go through this module.
"""

import uuid
from typing import Any
from datetime import datetime, timezone

from loguru import logger


def estimate_tokens_from_chars(total_chars: int) -> int:
    """Rough token estimate when real usage is unavailable. ~3.5 chars per token."""
    return max(int(total_chars / 3.5), 1)


def extract_usage_tokens(usage: dict | None) -> int | None:
    """Extract total token count from an LLM response usage dict.

    Supports both OpenAI format (prompt_tokens + completion_tokens)
    and Anthropic format (input_tokens + output_tokens).
    """
    if not usage:
        return None
    if "total_tokens" in usage:
        return usage["total_tokens"]
    if "input_tokens" in usage or "output_tokens" in usage:
        return (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
    return None


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _record_token_usage_event(
    *,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    source: str,
    provider: str | None,
    model: str | None,
    tokens: int,
    usage: dict | None,
    details: dict | None,
) -> None:
    if tokens <= 0:
        return
    try:
        from app.database import tenant_scoped_session
        from app.models.token_usage_event import TokenUsageEvent

        async with tenant_scoped_session(tenant_id) as db:
            db.add(
                TokenUsageEvent(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    source=source or "unknown",
                    provider=provider,
                    model=model,
                    tokens=tokens,
                    usage=usage,
                    details=details,
                )
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to record token usage event: {e}")


async def record_token_usage(
    agent_id: uuid.UUID,
    tokens: int,
    user_id: uuid.UUID | None = None,
    *,
    source: str = "kernel",
    provider: str | None = None,
    model: str | None = None,
    tenant_id: uuid.UUID | None = None,
    usage: dict | None = None,
    details: dict | None = None,
) -> None:
    """Record token consumption for an agent and its owner user.

    Updates Agent stats (tokens_used_today/month/total) and
    User enforcement counters (tokens_used_today/month/total).
    Uses an independent DB session to avoid interfering with the caller's transaction.
    """
    if tokens <= 0:
        return

    try:
        from app.database import tenant_scoped_session
        from app.models.agent import Agent
        from app.models.user import User
        from app.services.tenant_resolver import resolve_tenant_for_agent
        from sqlalchemy import select

        # Independent session (avoids the caller's tx) with no tenant in scope:
        # under enforced RLS a bare session sees neither the agent nor user row,
        # so the billing/quota counters would silently stop. Resolve the owning
        # tenant from the agent and pin the GUC — agent and its owner user share
        # the same tenant.
        tenant_id = tenant_id or await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            # Agent stats (tracking only)
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if agent:
                agent.tokens_used_today = (agent.tokens_used_today or 0) + tokens
                agent.tokens_used_month = (agent.tokens_used_month or 0) + tokens
                agent.tokens_used_total = (agent.tokens_used_total or 0) + tokens

                # Resolve user_id from agent if not provided
                if not user_id and agent.owner_user_id:
                    user_id = agent.owner_user_id
                elif not user_id:
                    user_id = agent.creator_id

            # User enforcement counters
            if user_id:
                user_result = await db.execute(select(User).where(User.id == user_id))
                user = user_result.scalar_one_or_none()
                if user:
                    now = datetime.now(timezone.utc)
                    # Daily reset
                    if user.tokens_reset_at and now.date() > user.tokens_reset_at.date():
                        user.tokens_used_today = 0
                    # Monthly reset
                    if user.tokens_reset_at and now.month != user.tokens_reset_at.month:
                        user.tokens_used_month = 0

                    user.tokens_used_today = (user.tokens_used_today or 0) + tokens
                    user.tokens_used_month = (user.tokens_used_month or 0) + tokens
                    user.tokens_used_total = (user.tokens_used_total or 0) + tokens
                    user.tokens_reset_at = now

            from app.models.token_usage_event import TokenUsageEvent

            db.add(
                TokenUsageEvent(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    source=source or "kernel",
                    provider=provider,
                    model=model,
                    tokens=tokens,
                    usage=usage,
                    details=details,
                )
            )
            await db.commit()
            logger.debug(f"Recorded {tokens:,} tokens for agent {agent_id}" + (f" / user {user_id}" if user_id else ""))
    except Exception as e:
        logger.warning(f"Failed to record token usage: {e}")


async def record_autonomous_llm_token_usage(
    *,
    source: str,
    usage: dict | None,
    provider: str | None = None,
    model: str | None = None,
    agent_id: uuid.UUID | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record usage for LLM calls that bypass AgentKernel.invoke_agent().

    When an agent_id is available, this updates the same Agent/User counters as
    the kernel path and appends a source-attributed event. Tenant-only calls
    still append an event, so platform cost charts do not lose background spend.
    """
    tokens = extract_usage_tokens(usage)
    if not tokens:
        return

    coerced_agent_id = _coerce_uuid(agent_id)
    coerced_tenant_id = _coerce_uuid(tenant_id)
    coerced_user_id = _coerce_uuid(user_id)
    if coerced_agent_id:
        await record_token_usage(
            coerced_agent_id,
            tokens,
            coerced_user_id,
            source=source,
            provider=provider,
            model=model,
            tenant_id=coerced_tenant_id,
            usage=usage,
            details=metadata,
        )
        return

    await _record_token_usage_event(
        tenant_id=coerced_tenant_id,
        agent_id=None,
        user_id=coerced_user_id,
        source=source,
        provider=provider,
        model=model,
        tokens=tokens,
        usage=usage,
        details=metadata,
    )
