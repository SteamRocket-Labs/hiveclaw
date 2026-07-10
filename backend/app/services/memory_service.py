"""Unified Memory Service — session summaries, retrieval, and compaction only.

The durable MD-first memory pipeline is handled elsewhere:
  - Extractor: T0 -> T2
  - Heartbeat: T2 -> T3
  - Dream: T3 -> soul

This service must not bypass that pipeline by writing long-term memory directly.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.memory.activation import ActivationContext
from app.memory import MemoryAssembler, MemoryRetriever
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.runtime.context_budget import ContextBudget, compute_context_budget
from app.services.agency_charter import AgentAccountabilityContext, build_default_accountability_context
from app.services.conversation_summarizer import estimate_tokens

logger = logging.getLogger(__name__)


CompactionCallback = Callable[[dict], Awaitable[None] | None]


# ============================================================================
# Public API
# ============================================================================


async def on_conversation_start(
    agent_id: uuid.UUID,
    session_id: str,
    tenant_id: uuid.UUID,
) -> str:
    """Backward-compatible wrapper for loading runtime memory context."""
    return await build_memory_context(agent_id, tenant_id, session_id=session_id)


async def build_memory_snapshot(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    session_id: str | None = None,
    context_window_tokens: int | None = None,
    budget_profile: ContextBudget | None = None,
    current_user_id: uuid.UUID | str | None = None,
    current_user_name: str | None = None,
) -> str:
    """Build a session-start memory snapshot for frozen prompt prefixes."""
    return await build_memory_context(
        agent_id,
        tenant_id,
        session_id=session_id,
        query="",
        context_window_tokens=context_window_tokens,
        budget_profile=budget_profile,
        current_user_id=current_user_id,
        current_user_name=current_user_name,
    )


async def build_memory_context(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    session_id: str | None = None,
    query: str = "",
    context_window_tokens: int | None = None,
    budget_profile: ContextBudget | None = None,
    current_user_id: uuid.UUID | str | None = None,
    current_user_name: str | None = None,
    legacy_compatibility: bool = False,
) -> str:
    """Build a self-consistent memory context for any runtime entrypoint.

    Uses the four-layer retrieval pipeline (working, episodic, semantic, external)
    followed by the assembler. Returns empty string on failure (caller decides).
    """
    retrieval_profile = budget_profile or compute_context_budget(
        context_window_tokens=context_window_tokens,
        query=query,
        active_pack_count=0,
    )
    try:
        data_root_settings = Path(get_settings().AGENT_DATA_DIR)
        retriever = MemoryRetriever(data_root=data_root_settings)
        retrieve_kwargs = {
            "limit": max(50, retrieval_profile.semantic_limit * 2),
        }
        retrieve_params = inspect.signature(retriever.retrieve).parameters
        if "retrieval_profile" in retrieve_params:
            retrieve_kwargs["retrieval_profile"] = retrieval_profile
        activation_context = None
        if "activation_context" in retrieve_params and not legacy_compatibility:
            activation_context = await _resolve_activation_context(
                agent_id=agent_id,
                tenant_id=tenant_id,
                query=query,
                current_user_id=current_user_id,
                current_user_name=current_user_name,
            )
        elif not legacy_compatibility:
            activation_context = await _resolve_activation_context(
                agent_id=agent_id,
                tenant_id=tenant_id,
                query=query,
                current_user_id=current_user_id,
                current_user_name=current_user_name,
            )
        # TaskModulation deterministic tier (design §4.4, M6): the active
        # goal's objective joins recall as a second term channel.
        if session_id and activation_context is not None:
            activation_context = await _attach_goal_terms(
                activation_context, agent_id=agent_id, tenant_id=tenant_id, session_id=str(session_id)
            )
        # Session working set W_t (design §4.2): load the evolving activation
        # state for this session so ContextBoost sees what the conversation is
        # already about; advanced + persisted after retrieval below.
        working_set_state = None
        if session_id and activation_context is not None:
            from dataclasses import replace as _dc_replace

            from app.memory.session_working_set import load_working_set

            working_set_state = load_working_set(data_root_settings, agent_id, str(session_id))
            if working_set_state.items:
                activation_context = _dc_replace(activation_context, working_set=working_set_state.as_pairs())
        if not legacy_compatibility and activation_context is None:
            logger.warning(
                "Memory activation principal unresolved; suppressing prompt memory for agent %s",
                agent_id,
                extra={
                    "metric": "memory_activation_fail_closed",
                    "agent_id": str(agent_id),
                    "tenant_id": str(tenant_id),
                },
            )
            return ""
        if "activation_context" in retrieve_params and activation_context:
            retrieve_kwargs["activation_context"] = activation_context

        # Resident profile plane (spec §4.2): self + profiles + explicit
        # overlay load WHOLE ahead of retrieval — never trimmed per-entry.
        # Over-budget raises a one-shot write-side convergence alert.
        from app.memory.profile_plane import (
            DEFAULT_RESIDENT_BUDGET_CHARS,
            check_resident_budget,
            load_resident_memory,
        )

        settings = get_settings()
        data_root = Path(settings.AGENT_DATA_DIR)
        resident = load_resident_memory(
            agent_id=agent_id,
            data_root=data_root,
            budget_chars=float(getattr(settings, "MEMORY_RESIDENT_BUDGET_CHARS", DEFAULT_RESIDENT_BUDGET_CHARS)),
        )
        try:
            await check_resident_budget(agent_id=agent_id, data_root=data_root, resident=resident)
        except Exception as budget_exc:  # noqa: BLE001 - budget telemetry must not block prompt assembly
            logger.warning("Resident budget check failed for %s: %s", agent_id, budget_exc)

        items = await retriever.retrieve(
            agent_id,
            query,
            session_id,
            str(tenant_id) if tenant_id else None,
            **retrieve_kwargs,
        )
        if working_set_state is not None:
            from app.memory.session_working_set import advance_working_set, save_working_set

            activated_refs: list[str] = []
            for item in items:
                item_metadata = getattr(item, "metadata", None)
                if not isinstance(item_metadata, dict):
                    continue
                ref = str(item_metadata.get("entry_id") or item_metadata.get("page_id") or "").strip()
                if ref:
                    activated_refs.append(ref)
            save_working_set(
                data_root_settings,
                agent_id,
                str(session_id),
                advance_working_set(working_set_state, activated_refs),
            )
        if resident.text:
            # Overlay entries already sit in the resident block — drop the
            # retriever's duplicate explicit-overlay items for this assembly.
            items = [item for item in items if item.metadata.get("source_type") != "explicit_overlay"]
        assembler = MemoryAssembler()
        assemble_kwargs = {}
        if "budget_chars" in inspect.signature(assembler.assemble).parameters:
            # Profile plane holds a fixed allowance; retrieval fills the rest.
            retrieval_budget = max(2_000, retrieval_profile.memory_budget_chars - resident.chars)
            assemble_kwargs["budget_chars"] = retrieval_budget
        assembled = assembler.assemble(items, **assemble_kwargs) or ""
        if resident.text and assembled:
            return f"{resident.text}\n\n{assembled}"
        return resident.text or assembled
    except Exception as exc:
        logger.warning("Retrieval pipeline failed, returning empty memory context: %s", exc)
        return ""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_activation_context(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    query: str,
    current_user_id: uuid.UUID | str | None,
    current_user_name: str | None,
) -> ActivationContext | None:
    accountability = await _resolve_accountability_context(
        agent_id=agent_id,
        tenant_id=tenant_id,
        current_user_id=current_user_id,
        current_user_name=current_user_name,
    )
    if not accountability:
        return None
    return ActivationContext(
        query=query,
        principal_stack=accountability.principal_stack,
        owner_terms=_label_terms(
            accountability.owner_charter.owner_id,
            accountability.owner_charter.owner_name,
        ),
        company_terms=_label_terms(
            accountability.company_charter.company_id,
            accountability.company_charter.company_name,
        ),
    )


async def _load_active_goal_objective(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session_id: str,
) -> str | None:
    """Fetch the active session goal's objective for TaskModulation (M6)."""
    try:
        session_uuid = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None
    try:
        from app.models.agent_session_goal import AgentSessionGoal

        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(
                select(AgentSessionGoal.objective).where(
                    AgentSessionGoal.agent_id == agent_id,
                    AgentSessionGoal.chat_session_id == session_uuid,
                    AgentSessionGoal.status == "active",
                )
            )
            objective = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - goal modulation is additive, recall must not fail on it
        logger.warning("Active goal lookup failed for agent %s: %s", agent_id, exc)
        return None
    text = str(objective or "").strip()
    return text or None


_GOAL_TERM_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "that", "this", "then", "from", "into", "onto", "our", "your", "their", "all", "any"}
)


def _goal_terms_from_objective(objective: str) -> list[str]:
    import re as _re

    terms = {
        term for term in _re.split(r"\W+", objective.lower()) if len(term) >= 3 and term not in _GOAL_TERM_STOPWORDS
    }
    return sorted(terms)


async def _attach_goal_terms(
    context: ActivationContext,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session_id: str,
) -> ActivationContext:
    """TaskModulation deterministic tier (design §4.4, M6).

    First real population of ``goal_terms``: the active goal's objective joins
    the query as a second term channel, so goal_relevance stops being a
    misnomer for plain query overlap. Zero LLM, fail-open.
    """
    objective = await _load_active_goal_objective(agent_id=agent_id, tenant_id=tenant_id, session_id=session_id)
    if not objective:
        return context
    terms = _goal_terms_from_objective(objective)
    if not terms:
        return context
    from dataclasses import replace as _dc_replace

    return _dc_replace(context, goal_terms=terms)


async def _resolve_accountability_context(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    current_user_id: uuid.UUID | str | None,
    current_user_name: str | None,
) -> AgentAccountabilityContext | None:
    try:
        async with tenant_scoped_session(tenant_id) as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
            agent = agent_result.scalar_one_or_none()

            tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = tenant_result.scalar_one_or_none()

            current_user_uuid = _coerce_uuid(current_user_id)
            owner_uuid = (
                getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None) or current_user_uuid
            )
            creator_uuid = getattr(agent, "creator_id", None)

            owner = await _fetch_user(db, owner_uuid)
            creator = await _fetch_user(db, creator_uuid)
            current_user = await _fetch_user(db, current_user_uuid)

            owner_id = str(owner_uuid or agent_id)
            owner_name = _display_name(owner) or owner_id
            creator_id = str(creator_uuid) if creator_uuid else None
            creator_name = _display_name(creator) if creator else None
            resolved_current_user_id = str(current_user_uuid) if current_user_uuid else None
            resolved_current_user_name = current_user_name or _display_name(current_user)

            return build_default_accountability_context(
                company_id=str(tenant_id),
                company_name=getattr(tenant, "name", None) or str(tenant_id),
                owner_id=owner_id,
                owner_name=owner_name,
                current_user_id=resolved_current_user_id,
                current_user_name=resolved_current_user_name,
                creator_id=creator_id,
                creator_name=creator_name,
            )
    except Exception as exc:
        logger.debug("Failed to resolve activation accountability context for agent %s: %s", agent_id, exc)
        return None


async def _fetch_user(db, user_id: uuid.UUID | None) -> User | None:
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def _display_name(user: User | None) -> str | None:
    if not user:
        return None
    return getattr(user, "display_name", None) or getattr(user, "username", None) or getattr(user, "email", None)


def _label_terms(*values: str | None) -> list[str]:
    terms: list[str] = []
    for value in values:
        for token in str(value or "").replace("-", " ").replace("_", " ").split():
            normalized = token.strip().lower()
            if normalized and normalized not in terms:
                terms.append(normalized)
    return terms


def compute_history_limit(
    provider: str,
    model_name: str,
    max_input_tokens_override: int | None = None,
    *,
    system_prompt_tokens: int = 0,
    tool_definitions_tokens: int = 0,
) -> int:
    """Compute how many history messages to load from DB based on model context window.

    Dynamic budget allocation: subtracts known token consumers (system prompt,
    tool definitions, generation headroom) before allocating to history.

    If system_prompt_tokens and tool_definitions_tokens are provided, uses real
    values; otherwise falls back to conservative estimates.
    """
    context_limit = _get_input_context_limit(provider, model_name, max_input_tokens_override)

    # Reserve tokens for known consumers
    # System prompt: use real value, or estimate based on context window
    # For 256K models, system prompt can be ~51K tokens; 3K was a severe underestimate.
    if system_prompt_tokens > 0:
        prompt_reserve = system_prompt_tokens
    else:
        # Estimate: 20% of context window (matches _SYSTEM_PROMPT_CONTEXT_RATIO)
        prompt_reserve = max(3000, int(context_limit * 0.20))

    # Tool definitions: use real value or estimate ~1500 tokens (15 tools × ~100 tokens each)
    tools_reserve = tool_definitions_tokens if tool_definitions_tokens > 0 else 1500
    # Generation headroom: ~8K tokens for model output
    generation_reserve = 8000

    # Memory context assembled by MemoryAssembler (20K+ chars ≈ 6K tokens for 256K models)
    memory_context_reserve = 6000
    total_reserved = prompt_reserve + tools_reserve + generation_reserve + memory_context_reserve
    history_token_budget = max(context_limit - total_reserved, context_limit // 4)

    avg_tokens_per_message = 300
    computed = history_token_budget // avg_tokens_per_message
    # Clamp: at least 20 (usable minimum), at most 800 (256K models can hold 800+ messages)
    return max(20, min(computed, 800))


async def compute_history_limit_for_agent(agent_id: uuid.UUID) -> int:
    """Resolve model info from DB and compute history limit for an agent.

    Convenience wrapper for channel handlers that don't have the model loaded.
    Falls back to 128k context (213 messages) if model lookup fails.
    """
    try:
        from app.models.agent import Agent
        from app.models.llm import LLMModel
        from app.services.tenant_resolver import resolve_tenant_for_agent

        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            agent_r = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_r.scalar_one_or_none()
            if agent and agent.primary_model_id:
                model_r = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_r.scalar_one_or_none()
                if model:
                    return compute_history_limit(
                        model.provider,
                        model.model,
                        getattr(model, "max_input_tokens", None),
                    )
    except Exception:
        logger.warning("Failed to resolve model for history limit (agent=%s), using default", agent_id)
    return compute_history_limit("openai", "")


# ── LLM summary circuit breaker (docs/compaction-cc-alignment.md §3 P1-2) ──
# Operational state only, NOT business state: mirrors CC's autoCompact
# consecutive-failure breaker (added there after unbounded retries burned
# ~250K API calls in a day). Lost on restart by design — worst case is one
# extra LLM attempt after a process bounce.
_SUMMARY_BREAKER_MAX_CONSECUTIVE_FAILURES = 3
_SUMMARY_BREAKER_RETRY_AFTER_SECONDS = 600  # half-open: allow one probe after TTL
_summary_breaker: dict[uuid.UUID, tuple[int, float]] = {}  # tenant_id → (failures, last_failure_ts)


def _summary_breaker_is_open(tenant_id: uuid.UUID) -> bool:
    import time

    entry = _summary_breaker.get(tenant_id)
    if not entry:
        return False
    failures, last_failure_ts = entry
    if failures < _SUMMARY_BREAKER_MAX_CONSECUTIVE_FAILURES:
        return False
    return (time.time() - last_failure_ts) < _SUMMARY_BREAKER_RETRY_AFTER_SECONDS


def _summary_breaker_record_failure(tenant_id: uuid.UUID) -> None:
    import time

    failures, _ = _summary_breaker.get(tenant_id, (0, 0.0))
    _summary_breaker[tenant_id] = (failures + 1, time.time())


def _summary_breaker_record_success(tenant_id: uuid.UUID) -> None:
    _summary_breaker.pop(tenant_id, None)


def _wrap_compressed_summary(summary: str) -> dict:
    """Build the post-compaction summary message (CC getCompactUserSummaryMessage).

    Auto-compaction is implicit — the user must not perceive a break, hence the
    resume-directly directive. The recovery pointer is system-injected here
    (CC transcriptPath pattern) rather than LLM-written: the summary model has
    no knowledge of real log paths and would only hallucinate them.
    """
    content = (
        "[Previous conversation summary]\n"
        "This session is being continued from an earlier portion of the conversation "
        "that was compacted to fit the context window. The summary below covers that "
        "earlier portion.\n\n"
        f"{summary}\n\n"
        "If you need specific pre-compaction detail, raw session evidence lives under "
        "memory/t0/sessions/<session_id>/segments/<segment_id>/source.md in your workspace; "
        "legacy/import compatibility pointers may also appear under logs/.\n"
        "Continue the conversation from where it left off without asking the user any "
        "further questions. Resume directly — do not acknowledge the summary, do not "
        "recap what was happening. Pick up the last task as if the break never happened."
    )
    return {"role": "system", "content": content}


async def maybe_compress_messages(
    messages: list[dict],
    model_provider: str,
    model_name: str,
    max_input_tokens_override: int | None,
    tenant_id: uuid.UUID | None,
    *,
    compress_threshold: float | None = None,
    keep_recent: int | None = None,
    on_compaction: CompactionCallback | None = None,
    usage_anchor_tokens: int | None = None,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[dict]:
    """Compress old messages when approaching model context window.

    Returns potentially compressed message list with summary prepended.
    """
    # Resolve config from tenant settings
    config = await _get_memory_config(tenant_id) if tenant_id else {}
    # Default 82% — was 70%, too aggressive for 256K models (compressed with 77K tokens remaining)
    threshold = compress_threshold if compress_threshold is not None else config.get("compress_threshold", 82) / 100.0
    recent_count = keep_recent if keep_recent is not None else config.get("keep_recent", 10)

    # Resolve context window — reserve space for summary output.
    _SUMMARY_OUTPUT_RESERVE = 20000
    context_limit = _get_input_context_limit(model_provider, model_name, max_input_tokens_override)
    effective_limit = max(context_limit - _SUMMARY_OUTPUT_RESERVE, context_limit // 2)
    trigger_tokens = int(effective_limit * threshold)

    estimated_tokens = estimate_tokens(messages, provider=model_provider)
    # ``usage_anchor_tokens`` is turn-level cumulative usage in AgentKernel. It
    # is valid for spend/budget controls, but it is not current context size:
    # a 90K-token prompt repeated across many tool rounds can exceed 750K
    # cumulative usage while the actual context is still far below a 1M window.
    # Compaction must therefore key off the current message payload estimate.
    if usage_anchor_tokens:
        logger.debug(
            "Memory compression ignoring cumulative usage anchor for threshold: estimate=%d anchor=%d threshold=%d",
            estimated_tokens,
            int(usage_anchor_tokens),
            trigger_tokens,
        )
    current_tokens = estimated_tokens
    if current_tokens <= trigger_tokens:
        return messages

    if len(messages) <= recent_count:
        return messages

    old_messages = messages[:-recent_count]
    recent_messages = messages[-recent_count:]

    # Ensure we don't break tool_call/tool_result pairs at the split point
    old_messages, recent_messages = _safe_split(old_messages, recent_messages)

    logger.info(
        "Memory compress: %d tokens > %d threshold (context=%d), summarizing %d old messages",
        current_tokens,
        trigger_tokens,
        context_limit,
        len(old_messages),
    )

    # Try LLM-powered summarization — defaults to the main conversation model (P1-1)
    summary_model = (
        await _get_summary_model_config(tenant_id, main_provider=model_provider, main_model=model_name)
        if tenant_id
        else None
    )
    if summary_model and tenant_id is not None and _summary_breaker_is_open(tenant_id):
        logger.warning(
            "[Memory] LLM summary breaker open for tenant %s — skipping semantic compaction summary",
            tenant_id,
            extra={"metric": "compaction_llm_breaker_open", "tenant_id": str(tenant_id)},
        )
        summary_model = None
    if summary_model:
        try:
            import app.services.conversation_summarizer as _summarizer

            summary = await _summarizer._llm_summarize(
                old_messages,
                summary_model,
                usage_source="compaction_summary",
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if summary:
                if tenant_id is not None:
                    _summary_breaker_record_success(tenant_id)
                if on_compaction:
                    maybe_result = on_compaction(
                        {
                            "summary": summary,
                            "original_message_count": len(messages),
                            "kept_message_count": len(recent_messages) + 1,
                        }
                    )
                    if maybe_result is not None:
                        await maybe_result
                return [_wrap_compressed_summary(summary)] + recent_messages
            # Empty LLM response counts as a failure for the breaker (CC: no-text-response = failure)
            if tenant_id is not None:
                _summary_breaker_record_failure(tenant_id)
            logger.warning(
                "LLM summarization returned empty; holding semantic compaction summary",
                extra={"metric": "compaction_llm_hold", "tenant_id": str(tenant_id), "reason": "empty"},
            )
        except Exception as e:
            if tenant_id is not None:
                _summary_breaker_record_failure(tenant_id)
            logger.warning(
                "LLM summarization failed; holding semantic compaction summary: %s",
                e,
                extra={"metric": "compaction_llm_hold", "tenant_id": str(tenant_id), "reason": "error"},
            )

    # No mechanical semantic summary fallback. Keep the prompt honest: the model
    # sees that older context was omitted instead of a platform-inferred summary.
    logger.warning("[Memory] Semantic compaction summary unavailable — using degraded trim marker")
    marker = {"role": "system", "content": "[Older messages omitted: LLM semantic compaction summary unavailable]"}
    return [marker] + recent_messages


async def on_conversation_end(
    agent_id: uuid.UUID,
    session_id: str,
    tenant_id: uuid.UUID,
    messages: list[dict],
) -> None:
    """Backward-compatible wrapper for persisting runtime memory state."""
    await persist_runtime_memory(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        messages=messages,
    )

    # P3.1: Auto-dream gate check — fire-and-forget if conditions met
    try:
        from app.services.auto_dream import (
            record_session_end,
            should_dream,
            run_dream,
            should_soft_dream,
            run_soft_dream,
        )
        import asyncio

        record_session_end(agent_id)
        if should_dream(agent_id):
            asyncio.create_task(run_dream(agent_id, tenant_id))
            logger.info("[Memory] Auto-dream triggered for agent %s", agent_id)
        elif should_soft_dream(agent_id):
            asyncio.create_task(run_soft_dream(agent_id))
            logger.info("[Memory] Soft dream triggered for agent %s", agent_id)
    except Exception as _dream_err:
        logger.debug("[Memory] Auto-dream check failed: %s", _dream_err)


async def persist_runtime_memory(
    *,
    agent_id: uuid.UUID,
    session_id: str | None,
    tenant_id: uuid.UUID,
    messages: list[dict],
) -> None:
    """Persist session summary for any runtime entrypoint.

    Durable memory extraction is handled by hooks/extractor into T2 and must not
    be duplicated here. This path only writes session summaries plus optional
    external journal copies.
    """
    if not _has_meaningful_messages(messages):
        return

    _MAX_RETRIES = 2
    _RETRY_DELAYS = (1.0, 3.0)
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            summary = await _generate_session_summary(messages, tenant_id, agent_id=agent_id)
            if summary and session_id:
                await _save_session_summary(session_id, summary, tenant_id)

            return  # success

        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                import asyncio

                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "persist_runtime_memory attempt %d/%d failed, retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "persist_runtime_memory failed after %d attempts (non-fatal): %s",
                    _MAX_RETRIES + 1,
                    last_exc,
                    exc_info=True,
                )


# ============================================================================
# Internal Helpers
# ============================================================================


def _get_input_context_limit(provider: str, model_name: str, override: int | None) -> int:
    """Resolve model input context window. Priority: override > ProviderSpec > 128000."""
    if override and override > 0:
        return override

    from app.services.llm_client import get_provider_spec

    spec = get_provider_spec(provider)
    if spec:
        return spec.max_input_tokens

    return 128000


async def _get_memory_config(tenant_id: uuid.UUID) -> dict:
    """Load memory configuration from TenantSetting(key='memory_config')."""
    try:
        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(
                select(TenantSetting.value).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "memory_config",
                )
            )
            value = result.scalar_one_or_none()
            return value if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("[Memory] tenant memory_config load failed for %s, using defaults: %s", tenant_id, exc)
        return {}


def _coerce_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _model_config(model: LLMModel) -> dict:
    return {
        "provider": model.provider,
        "model": model.model,
        "api_key": model.api_key,
        "base_url": model.base_url,
        # Window threads through to _llm_summarize input budgeting; consumers
        # that expand this dict into create_llm_client() must pop it first.
        "max_input_tokens": getattr(model, "max_input_tokens", None),
    }


async def _get_enabled_model_config_by_id(db, tenant_id: uuid.UUID, model_id: object) -> dict | None:
    model_uuid = _coerce_uuid(model_id)
    if not model_uuid:
        return None

    result = await db.execute(
        select(LLMModel).where(
            LLMModel.id == model_uuid,
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
    )
    model = result.scalar_one_or_none()
    return _model_config(model) if model else None


async def _get_default_model_config(db, tenant_id: uuid.UUID) -> dict | None:
    result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    value = result.scalar_one_or_none()
    if isinstance(value, dict) and value.get("model_id"):
        model_config = await _get_enabled_model_config_by_id(db, tenant_id, value["model_id"])
        if model_config:
            return model_config

    result = await db.execute(
        select(LLMModel)
        .where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.enabled.is_(True),
        )
        .order_by(LLMModel.created_at.desc())
        .limit(1)
    )
    model = result.scalar_one_or_none()
    return _model_config(model) if model else None


async def _get_memory_model_config(tenant_id: uuid.UUID, configured_model_id: object, purpose: str) -> dict | None:
    try:
        async with tenant_scoped_session(tenant_id) as db:
            if configured_model_id:
                model_config = await _get_enabled_model_config_by_id(db, tenant_id, configured_model_id)
                if model_config:
                    return model_config
                logger.warning("Configured %s model is unavailable for tenant %s", purpose, tenant_id)

            return await _get_default_model_config(db, tenant_id)
    except Exception as e:
        logger.warning("Failed to load %s model: %s", purpose, e)
        return None


async def _get_main_model_config(db, tenant_id: uuid.UUID, provider: str, model_name: str) -> dict | None:
    """Find the enabled LLMModel record matching the main conversation model."""
    if not provider or not model_name:
        return None
    result = await db.execute(
        select(LLMModel)
        .where(
            LLMModel.tenant_id == tenant_id,
            LLMModel.provider == provider,
            LLMModel.model == model_name,
            LLMModel.enabled.is_(True),
        )
        .limit(1)
    )
    model = result.scalar_one_or_none()
    return _model_config(model) if model else None


async def _get_summary_model_config(
    tenant_id: uuid.UUID,
    *,
    main_provider: str = "",
    main_model: str = "",
) -> dict | None:
    """Resolve the LLM model to use for summarization.

    Priority (docs/compaction-cc-alignment.md §3 P1-1, CC mainLoopModel philosophy):
    1. Tenant-configured summary_model_id (explicit operator choice)
    2. The current main conversation model (window + behavior consistency)
    3. Default chain (default_model_id → newest enabled model)
    """
    config = await _get_memory_config(tenant_id)
    configured_id = config.get("summary_model_id")
    try:
        async with tenant_scoped_session(tenant_id) as db:
            if configured_id:
                model_config = await _get_enabled_model_config_by_id(db, tenant_id, configured_id)
                if model_config:
                    return model_config
                logger.warning("Configured summary model is unavailable for tenant %s", tenant_id)

            main_config = await _get_main_model_config(db, tenant_id, main_provider, main_model)
            if main_config:
                return main_config

            return await _get_default_model_config(db, tenant_id)
    except Exception as e:
        logger.warning("Failed to load summary model: %s", e)
        return None


async def _get_rerank_model_config(tenant_id: uuid.UUID) -> dict | None:
    """Resolve the optional LLM model to use for semantic memory reranking."""
    config = await _get_memory_config(tenant_id)
    # Consumers create clients via create_llm_client_from_config, which filters
    # non-client hints (max_input_tokens) — no per-caller pop needed.
    return await _get_memory_model_config(tenant_id, config.get("rerank_model_id"), "rerank")


async def _generate_session_summary(
    messages: list[dict],
    tenant_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> str | None:
    """Generate a session summary using LLM only.

    ChatSession.summary is user-related semantic memory. If the summary LLM is
    unavailable or fails, hold the write instead of persisting a mechanical
    extraction as a second memory path.
    """
    summary_model = await _get_summary_model_config(tenant_id)
    if summary_model:
        try:
            from app.services.conversation_summarizer import _llm_summarize

            return await _llm_summarize(
                messages,
                summary_model,
                usage_source="session_summary",
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("LLM session summary failed; holding summary write: %s", e)

    logger.info("Session summary held because no summary LLM was available")
    return None


def _parse_session_uuid(session_id: str | None) -> uuid.UUID | None:
    if not session_id:
        return None
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, TypeError) as exc:
        logger.debug("Invalid session UUID %s: %s", session_id, exc)
        return None


async def _save_session_summary(session_id: str, summary: str, tenant_id: uuid.UUID) -> None:
    session_uuid = _parse_session_uuid(session_id)
    if not session_uuid:
        return

    safe_summary = summary.replace("\x00", "")

    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
        session = result.scalar_one_or_none()
        if session:
            session.summary = safe_summary
            # Update last_message_at so episodic retriever ranks this session correctly
            from datetime import datetime, timezone

            session.last_message_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Session summary saved for %s", session_id)


def _has_meaningful_messages(messages: list[dict]) -> bool:
    for msg in messages:
        if msg.get("role") not in {"user", "assistant"}:
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return True
    return False


def _safe_split(old: list[dict], recent: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ensure tool_call/tool_result pairs aren't split between old and recent.

    Handles three boundary cases:
    1. recent starts with tool results → move them to old (keep with their call)
    2. old ends with tool_calls but results are in recent → move call to recent
    3. old ends with tool_calls and no results anywhere → move call to recent

    Returns (old, recent) — same order as parameters.
    """
    if not recent or not old:
        return old, recent

    # Case 1: recent starts with tool results → pull them into old
    while recent and recent[0].get("role") == "tool":
        old.append(recent.pop(0))

    # Case 2+3: old ends with assistant+tool_calls but tool results are now
    # in recent or missing entirely → move the whole call into recent
    if old and old[-1].get("tool_calls"):
        # Count how many tool results should follow this call
        expected = len(old[-1].get("tool_calls", []))
        # Count trailing tool results already in old after the call
        trailing_tools = 0
        for i in range(len(old) - 1, -1, -1):
            if old[i].get("role") == "tool":
                trailing_tools += 1
            else:
                break
        if trailing_tools < expected:
            # Not all results present → move call (and any trailing results) to recent
            orphan = old.pop()  # assistant with tool_calls
            # Also move any trailing tool results that belong to this call
            moved_tools = []
            while old and old[-1].get("role") == "tool":
                moved_tools.insert(0, old.pop())
            recent = [orphan] + moved_tools + recent

    return old, recent
