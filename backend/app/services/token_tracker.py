"""Token usage tracking — records consumption against both Agent (stats) and User (enforcement).

All LLM call paths (web chat, heartbeat, triggers, A2A) go through this module.
"""

import uuid
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


async def record_token_usage(agent_id: uuid.UUID, tokens: int, user_id: uuid.UUID | None = None) -> None:
    """Record token consumption for an agent and its owner user.

    Updates Agent stats (tokens_used_today/month/total) and
    User enforcement counters (tokens_used_today/month/total).
    Uses an independent DB session to avoid interfering with the caller's transaction.
    """
    if tokens <= 0:
        return

    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.user import User
        from sqlalchemy import select

        async with async_session() as db:
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

            await db.commit()
            logger.debug(f"Recorded {tokens:,} tokens for agent {agent_id}" + (f" / user {user_id}" if user_id else ""))
    except Exception as e:
        logger.warning(f"Failed to record token usage: {e}")
