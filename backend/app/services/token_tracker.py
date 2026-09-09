"""Token usage tracking — records consumption against both Agent (stats) and User (enforcement).

All LLM call paths (web chat, heartbeat, triggers, A2A) go through this module.
"""

import uuid
import re
from datetime import datetime, timezone
from typing import Any

from loguru import logger

_DEFAULT_CHARS_PER_TOKEN = 3.5
_CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens_from_chars(total_chars: int) -> int:
    """Legacy rough estimate when only a character count is available."""
    return max(int(total_chars / _DEFAULT_CHARS_PER_TOKEN), 1)


def estimate_tokens_from_text(text: str, *, chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN) -> int:
    """Estimate tokens from actual text, preserving CJK density.

    Character-count-only callers cannot know script density and keep the legacy
    ASCII-leaning ratio. Callers with the text must use this path so Chinese,
    Japanese, and Korean content is not undercounted by ~3.5x.
    """
    if not text:
        return 0
    cpt = chars_per_token if chars_per_token > 0 else _DEFAULT_CHARS_PER_TOKEN
    cjk_chars = len(_CJK_CHAR_RE.findall(text))
    other_chars = max(len(text) - cjk_chars, 0)
    return max(int(cjk_chars + (other_chars / cpt)), 1)


def _usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _nested_usage_int(usage: dict[str, Any], parent: str, key: str) -> int:
    value = usage.get(parent)
    if not isinstance(value, dict):
        return 0
    return _usage_int(value, key)


def _cache_read_tokens(usage: dict[str, Any]) -> int:
    return max(
        _usage_int(usage, "prompt_cache_hit_tokens"),
        _usage_int(usage, "cache_read_input_tokens"),
        _usage_int(usage, "cached_tokens"),
        _usage_int(usage, "cachedContentTokenCount"),
        _nested_usage_int(usage, "prompt_tokens_details", "cached_tokens"),
        _nested_usage_int(usage, "input_tokens_details", "cached_tokens"),
    )


def _cache_creation_tokens(usage: dict[str, Any]) -> int:
    nested_creation = usage.get("cache_creation")
    nested_total = 0
    if isinstance(nested_creation, dict):
        nested_total = sum(_usage_int(nested_creation, key) for key in nested_creation)
    return max(
        _usage_int(usage, "cache_creation_input_tokens"),
        _usage_int(usage, "cacheCreationInputTokens"),
        nested_total,
    )


def _completion_tokens(usage: dict[str, Any]) -> int:
    return max(
        _usage_int(usage, "completion_tokens"),
        _usage_int(usage, "output_tokens"),
        _usage_int(usage, "candidatesTokenCount"),
    )


def extract_usage_tokens(usage: dict | None) -> int | None:
    """Extract effective token count from an LLM response usage dict.

    Cached prompt reads are excluded from the returned count. The runtime turn
    budget should guard new work in this turn, not repeatedly count the same
    cache-hit prompt prefix on every tool round.
    """
    if not usage:
        return None
    if not isinstance(usage, dict):
        return None
    cache_read = _cache_read_tokens(usage)
    cache_creation = _cache_creation_tokens(usage)
    prompt_cache_miss = _usage_int(usage, "prompt_cache_miss_tokens")
    if prompt_cache_miss:
        return prompt_cache_miss + _completion_tokens(usage)

    if "cache_read_input_tokens" in usage and ("input_tokens" in usage or "output_tokens" in usage):
        # Anthropic native usage keeps cache reads in a separate field: input_tokens
        # is the non-cache-read input. Count fresh input + cache writes + output.
        return _usage_int(usage, "input_tokens") + cache_creation + _usage_int(usage, "output_tokens")

    if "total_tokens" in usage:
        return max(_usage_int(usage, "total_tokens") - cache_read, 0)
    if "totalTokenCount" in usage:
        return max(_usage_int(usage, "totalTokenCount") - cache_read, 0)
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        total = _usage_int(usage, "prompt_tokens") + _usage_int(usage, "completion_tokens")
        return max(total - cache_read, 0)
    if "input_tokens" in usage or "output_tokens" in usage:
        total = _usage_int(usage, "input_tokens") + cache_creation + _usage_int(usage, "output_tokens")
        if cache_read:
            return max(total - cache_read, 0)
        return total
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


def _reset_user_or_tenant_counter(subject: Any, *, now: datetime) -> None:
    reset_at = getattr(subject, "tokens_reset_at", None)
    if reset_at is None:
        subject.tokens_reset_at = now
        return
    if now.date() > reset_at.date():
        subject.tokens_used_today = 0
    if now.month != reset_at.month or now.year != reset_at.year:
        subject.tokens_used_month = 0
    subject.tokens_reset_at = now


def _reset_agent_counter(agent: Any, *, now: datetime) -> None:
    if agent.last_daily_reset is None:
        agent.last_daily_reset = now
    elif now.date() > agent.last_daily_reset.date():
        agent.tokens_used_today = 0
        agent.last_daily_reset = now
    if agent.last_monthly_reset is None:
        agent.last_monthly_reset = now
    elif now.month != agent.last_monthly_reset.month or now.year != agent.last_monthly_reset.year:
        agent.tokens_used_month = 0
        agent.last_monthly_reset = now


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
    raise_on_error: bool = False,
) -> None:
    if tokens <= 0:
        return
    try:
        from app.database import tenant_scoped_session
        from app.models.tenant import Tenant
        from app.models.token_usage_event import TokenUsageEvent
        from app.models.user import User
        from sqlalchemy import select

        async with tenant_scoped_session(tenant_id) as db:
            now = datetime.now(timezone.utc)
            if tenant_id is not None:
                tenant = (
                    await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True))
                ).scalar_one_or_none()
                if tenant:
                    _reset_user_or_tenant_counter(tenant, now=now)
                    tenant.tokens_used_today = (tenant.tokens_used_today or 0) + tokens
                    tenant.tokens_used_month = (tenant.tokens_used_month or 0) + tokens
                    tenant.tokens_used_total = (tenant.tokens_used_total or 0) + tokens
            if user_id is not None:
                user = (
                    await db.execute(
                        select(User)
                        .where(
                            User.id == user_id,
                            User.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if user:
                    _reset_user_or_tenant_counter(user, now=now)
                    user.tokens_used_today = (user.tokens_used_today or 0) + tokens
                    user.tokens_used_month = (user.tokens_used_month or 0) + tokens
                    user.tokens_used_total = (user.tokens_used_total or 0) + tokens
                    user.tokens_reset_at = now
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
        if raise_on_error:
            raise
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
    idempotency_key: str | None = None,
    raise_on_error: bool = False,
) -> bool | None:
    """Record token consumption for an agent and its owner user.

    Updates Agent stats (tokens_used_today/month/total) and
    User enforcement counters (tokens_used_today/month/total).
    Uses an independent DB session to avoid interfering with the caller's transaction.

    ``idempotency_key`` carries an exact charge identity (e.g. the durable
    provider request of one model round).  When set, the TokenUsageEvent row is
    both the charge evidence and the dedupe fence: the existence check, the
    counter updates, and the event insert commit in ONE transaction, so a crash
    can never leave "counters bumped without evidence" or vice versa.  Returns
    ``True`` when this call committed the charge, ``False`` when the key's
    charge already committed durably, and ``None`` on suppressed failure.
    Round execution is serialized upstream by the claim/prepare fences, so only
    the current claim owner can ever reach a keyed charge for its round.
    """
    if tokens <= 0:
        return None

    try:
        from app.database import tenant_scoped_session
        from app.models.agent import Agent
        from app.models.tenant import Tenant
        from app.models.token_usage_event import TokenUsageEvent
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
            now = datetime.now(timezone.utc)
            # Serialize charges on the shared counter rows BEFORE any dedupe
            # read: two concurrent callers of the SAME key (a stale worker and
            # the reclaiming worker) or of DIFFERENT keys sharing these rows
            # must never interleave read-modify-write counters or race the
            # keyed existence check.
            #
            # Lock order is Tenant -> User -> Agent.  User offboarding locks
            # the target User (FOR UPDATE) and then the user's Agent rows
            # (FOR UPDATE, ``_lock_owned_agents``); tenant retirement locks
            # Tenant first.  Ordering every counter row Tenant -> User ->
            # Agent keeps ONE consistent global order with BOTH authenticated
            # production paths — the inverse order (Agent before User) is a
            # reachable ABBA deadlock whenever an admin offboards an employee
            # whose Agent is mid-turn.
            #
            # The order is not only about the explicit ``with_for_update``
            # calls: no row may be dirtied before its lock is held, because
            # the session's autoflush would emit that row's UPDATE (and take
            # its row lock) at the NEXT select — silently re-introducing the
            # inverse order even when the explicit Agent lock is removed.
            # All counter mutations below therefore happen only after every
            # lock AND the dedupe read.  The strength is FOR NO KEY UPDATE:
            # it still serializes concurrent charges against each other
            # (mutual conflict) and against offboarding's FOR UPDATE, but
            # remains compatible with the foreign-key KEY SHARE row locks
            # any open transaction holds after inserting child rows — a
            # full FOR UPDATE here deadlocks whenever a charge runs while
            # the caller's own transaction (e.g. the restart loader) is
            # still open, because that transaction awaits this charge's
            # result on the same event loop.
            tenant = (
                await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True))
            ).scalar_one_or_none()

            # Resolve the owning user BEFORE taking the Agent lock: the User
            # row must be locked first (order above).  This snapshot read
            # mutates nothing, so autoflush cannot reorder it.  The snapshot
            # owner is DELIBERATELY the charge principal for the whole
            # transaction: an ownership transfer that commits mid-charge must
            # not split the charge across principals (for example, debit the
            # snapshot user's counter while writing the new owner's identity
            # into the evidence event).  The User row is locked under exactly
            # that principal.
            resolved_user_id = user_id
            if not resolved_user_id:
                owner_snapshot = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
                if owner_snapshot is not None:
                    resolved_user_id = owner_snapshot.owner_user_id or owner_snapshot.creator_id

            user = None
            if resolved_user_id:
                user = (
                    await db.execute(
                        select(User)
                        .where(
                            User.id == resolved_user_id,
                            User.tenant_id == tenant_id,
                        )
                        .with_for_update(key_share=True)
                    )
                ).scalar_one_or_none()

            # The locking Agent re-read refreshes the row state
            # (``populate_existing``) so the counter read-modify-write below
            # uses the values as of THIS lock, not a stale identity-map
            # instance from the mutation-free snapshot above.
            agent = (
                await db.execute(
                    select(Agent)
                    .where(Agent.id == agent_id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()

            if idempotency_key:
                # Only after the counter rows are locked: the competing charge
                # (if any) has either committed its evidence row — visible to
                # this fresh READ COMMITTED snapshot — or not started, so the
                # check-then-insert is a real fence, not a race window.
                already = await db.scalar(
                    select(TokenUsageEvent.id)
                    .where(
                        TokenUsageEvent.tenant_id == tenant_id,
                        TokenUsageEvent.agent_id == agent_id,
                        TokenUsageEvent.details["idempotency_key"].astext == str(idempotency_key),
                    )
                    .limit(1)
                )
                if already is not None:
                    await db.rollback()
                    return False
            if tenant:
                _reset_user_or_tenant_counter(tenant, now=now)
                tenant.tokens_used_today = (tenant.tokens_used_today or 0) + tokens
                tenant.tokens_used_month = (tenant.tokens_used_month or 0) + tokens
                tenant.tokens_used_total = (tenant.tokens_used_total or 0) + tokens
            if agent:
                _reset_agent_counter(agent, now=now)
                agent.tokens_used_today = (agent.tokens_used_today or 0) + tokens
                agent.tokens_used_month = (agent.tokens_used_month or 0) + tokens
                agent.tokens_used_total = (agent.tokens_used_total or 0) + tokens

                # The evidence event carries the SAME principal that was
                # locked and debited above (the snapshot owner) — never a
                # mid-charge ownership transfer observed on the refreshed
                # Agent row, which would split counter and evidence.
                if not user_id:
                    user_id = resolved_user_id

            # User enforcement counters
            if user:
                _reset_user_or_tenant_counter(user, now=now)
                user.tokens_used_today = (user.tokens_used_today or 0) + tokens
                user.tokens_used_month = (user.tokens_used_month or 0) + tokens
                user.tokens_used_total = (user.tokens_used_total or 0) + tokens
                user.tokens_reset_at = now

            event_details = dict(details or {})
            if idempotency_key:
                event_details["idempotency_key"] = str(idempotency_key)
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
                    details=event_details or None,
                )
            )
            await db.commit()
            logger.debug(f"Recorded {tokens:,} tokens for agent {agent_id}" + (f" / user {user_id}" if user_id else ""))
            return True
    except Exception as e:
        if raise_on_error:
            raise
        logger.warning(f"Failed to record token usage: {e}")
        return None


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
    raise_on_error: bool = False,
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
            raise_on_error=raise_on_error,
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
        raise_on_error=raise_on_error,
    )
