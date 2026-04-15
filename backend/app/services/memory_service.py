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
from app.database import async_session
from app.memory import MemoryAssembler, MemoryRetriever
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tenant_setting import TenantSetting
from app.runtime.context_budget import ContextBudget, compute_context_budget
from app.services.conversation_summarizer import estimate_tokens, _extract_summary

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
) -> str:
    """Build a session-start memory snapshot for frozen prompt prefixes."""
    return await build_memory_context(
        agent_id,
        tenant_id,
        session_id=session_id,
        query="",
        context_window_tokens=context_window_tokens,
        budget_profile=budget_profile,
    )


async def build_memory_context(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    session_id: str | None = None,
    query: str = "",
    context_window_tokens: int | None = None,
    budget_profile: ContextBudget | None = None,
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
        retriever = MemoryRetriever(data_root=Path(get_settings().AGENT_DATA_DIR))
        rerank_model_config = None
        if query:
            rerank_model_config = await _maybe_await(_get_rerank_model_config(tenant_id))
        retrieve_kwargs = {
            "rerank_model_config": rerank_model_config,
            "limit": max(50, retrieval_profile.semantic_limit * 2),
        }
        if "retrieval_profile" in inspect.signature(retriever.retrieve).parameters:
            retrieve_kwargs["retrieval_profile"] = retrieval_profile
        items = await retriever.retrieve(
            agent_id,
            query,
            session_id,
            str(tenant_id) if tenant_id else None,
            **retrieve_kwargs,
        )
        assembler = MemoryAssembler()
        assemble_kwargs = {}
        if "budget_chars" in inspect.signature(assembler.assemble).parameters:
            assemble_kwargs["budget_chars"] = retrieval_profile.memory_budget_chars
        return assembler.assemble(items, **assemble_kwargs) or ""
    except Exception as exc:
        logger.warning("Retrieval pipeline failed, returning empty memory context: %s", exc)
        return ""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


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

        async with async_session() as db:
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
) -> list[dict]:
    """Compress old messages when approaching model context window.

    Returns potentially compressed message list with summary prepended.
    """
    # Resolve config from tenant settings
    config = await _get_memory_config(tenant_id) if tenant_id else {}
    # Default 82% — was 70%, too aggressive for 256K models (compressed with 77K tokens remaining)
    threshold = compress_threshold if compress_threshold is not None else config.get("compress_threshold", 82) / 100.0
    recent_count = keep_recent if keep_recent is not None else config.get("keep_recent", 10)

    # Resolve context window — reserve space for summary output (CC: MAX_OUTPUT_TOKENS_FOR_SUMMARY=20K)
    _SUMMARY_OUTPUT_RESERVE = 20000
    context_limit = _get_input_context_limit(model_provider, model_name, max_input_tokens_override)
    effective_limit = max(context_limit - _SUMMARY_OUTPUT_RESERVE, context_limit // 2)
    trigger_tokens = int(effective_limit * threshold)

    current_tokens = estimate_tokens(messages, provider=model_provider)
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

    # Try LLM-powered summarization
    summary_model = await _get_summary_model_config(tenant_id) if tenant_id else None
    if summary_model:
        try:
            from app.services.conversation_summarizer import _llm_summarize

            summary = await _llm_summarize(old_messages, summary_model)
            if summary:
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
                return [{"role": "system", "content": f"[Previous conversation summary]\n{summary}"}] + recent_messages
        except Exception as e:
            logger.warning("LLM summarization failed, falling back to extraction: %s", e)

    # Fallback: text extraction
    summary = _extract_summary(old_messages)
    if not summary:
        # G4: Last-resort trim — drop old messages with a marker, keep recent
        logger.warning("[Memory] Both LLM and extraction summaries empty — last-resort trim")
        marker = {"role": "system", "content": "[Older messages trimmed to fit context window]"}
        return [marker] + recent_messages
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
    return [{"role": "system", "content": f"[Previous conversation summary]\n{summary}"}] + recent_messages


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
            summary = await _generate_session_summary(messages, tenant_id)
            if summary and session_id:
                await _save_session_summary(session_id, summary)

            config = await _get_memory_config(tenant_id)
            if config.get("extract_to_viking", False) and summary:
                from app.services import viking_client

                if viking_client.is_configured():
                    await viking_client.add_resource(
                        content=summary,
                        to=f"viking://conversations/{agent_id}/{session_id or 'runtime'}",
                        tenant_id=str(tenant_id),
                        agent_id=str(agent_id),
                        reason="conversation_summary",
                    )
                    logger.info("Summary written to OpenViking for session %s", session_id or "runtime")

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
        async with async_session() as db:
            result = await db.execute(
                select(TenantSetting.value).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "memory_config",
                )
            )
            value = result.scalar_one_or_none()
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _get_summary_model_config(tenant_id: uuid.UUID) -> dict | None:
    """Resolve the LLM model to use for summarization from tenant config."""
    config = await _get_memory_config(tenant_id)
    model_id = config.get("summary_model_id")
    if not model_id:
        return None

    try:
        async with async_session() as db:
            result = await db.execute(
                select(LLMModel).where(LLMModel.id == uuid.UUID(str(model_id)), LLMModel.tenant_id == tenant_id)
            )
            model = result.scalar_one_or_none()
            if not model or not model.enabled:
                return None

            return {
                "provider": model.provider,
                "model": model.model,
                "api_key": model.api_key,
                "base_url": model.base_url,
            }
    except Exception as e:
        logger.warning("Failed to load summary model: %s", e)
        return None


async def _get_rerank_model_config(tenant_id: uuid.UUID) -> dict | None:
    """Resolve the optional LLM model to use for semantic memory reranking."""
    config = await _get_memory_config(tenant_id)
    model_id = config.get("rerank_model_id")
    if not model_id:
        return None

    try:
        async with async_session() as db:
            result = await db.execute(
                select(LLMModel).where(LLMModel.id == uuid.UUID(str(model_id)), LLMModel.tenant_id == tenant_id)
            )
            model = result.scalar_one_or_none()
            if not model or not model.enabled:
                return None

            return {
                "provider": model.provider,
                "model": model.model,
                "api_key": model.api_key,
                "base_url": model.base_url,
            }
    except Exception as e:
        logger.warning("Failed to load rerank model: %s", e)
        return None


async def _generate_session_summary(messages: list[dict], tenant_id: uuid.UUID) -> str | None:
    """Generate a summary for the session using LLM or fallback extraction."""
    summary_model = await _get_summary_model_config(tenant_id)
    if summary_model:
        try:
            from app.services.conversation_summarizer import _llm_summarize

            return await _llm_summarize(messages, summary_model)
        except Exception as e:
            logger.warning("LLM session summary failed, using extraction: %s", e)

    return _extract_summary(messages)


def _parse_session_uuid(session_id: str | None) -> uuid.UUID | None:
    if not session_id:
        return None
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, TypeError) as exc:
        logger.debug("Invalid session UUID %s: %s", session_id, exc)
        return None


async def _save_session_summary(session_id: str, summary: str) -> None:
    session_uuid = _parse_session_uuid(session_id)
    if not session_uuid:
        return

    async with async_session() as db:
        result = await db.execute(select(ChatSession).where(ChatSession.id == session_uuid))
        session = result.scalar_one_or_none()
        if session:
            session.summary = summary
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
