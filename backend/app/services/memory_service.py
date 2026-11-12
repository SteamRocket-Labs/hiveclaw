"""Unified Memory Service — session summaries, resident context, and compaction.

The durable MD-first memory pipeline is handled elsewhere:
  - Extractor: T0 -> T2
  - Heartbeat: T2 -> T3
  - Dream: T3 -> soul

This service must not bypass that pipeline by writing long-term memory directly.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Literal

from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.agency_charter import AgentAccountabilityContext, build_default_accountability_context
from app.services.conversation_summarizer import estimate_tokens

logger = logging.getLogger(__name__)


CompactionCallback = Callable[[dict], Awaitable[None] | None]
MemoryContextStatus = Literal["ready", "degraded", "unavailable", "blocked_authority"]


@dataclass(frozen=True, slots=True)
class MemoryContextResult:
    content: str
    status: MemoryContextStatus
    code: str
    user_message: str
    retryable: bool = False
    conversation_available: bool = True
    authority_context_available: bool = True
    durable_write_available: bool = True
    external_effects_available: bool = True
    degraded_components: tuple[str, ...] = ()
    error_class: str | None = None
    source_refs: tuple[dict[str, str], ...] = ()
    source_unbound: tuple[str, ...] = ("memory_source_inventory_missing",)
    sections: tuple[dict, ...] = ()


# ============================================================================
# Public API
# ============================================================================


async def build_memory_context(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    session_id: str | None = None,
    runtime_task_id: str | None = None,
    query: str = "",
    current_user_id: uuid.UUID | str | None = None,
    current_user_name: str | None = None,
    return_result: bool = False,
) -> str | MemoryContextResult:
    """Build the resident memory context for any runtime entrypoint.

    Resident identity memory plus the authorized explicit-memory index go into
    the prompt with zero model calls; the conversation model fetches memory
    bodies on demand through ``search_memory`` / ``load_memory``. Runtime
    callers request the typed result so failure cannot collapse to an
    indistinguishable empty context.
    """
    result = await _build_memory_context_result(
        agent_id,
        tenant_id,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        query=query,
        current_user_id=current_user_id,
        current_user_name=current_user_name,
    )
    return result if return_result else result.content


async def _build_memory_context_result(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    session_id: str | None,
    runtime_task_id: str | None,
    query: str,
    current_user_id: uuid.UUID | str | None,
    current_user_name: str | None,
) -> MemoryContextResult:
    try:
        settings = get_settings()
        data_root = Path(settings.AGENT_DATA_DIR)
    except Exception as exc:
        return MemoryContextResult(
            content="",
            status="unavailable",
            code="memory_storage_unavailable",
            user_message="Agent memory storage is temporarily unavailable. Conversation can continue in degraded mode.",
            retryable=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
            error_class=type(exc).__name__,
        )
    try:
        accountability = await _resolve_accountability_context(
            agent_id=agent_id,
            tenant_id=tenant_id,
            current_user_id=current_user_id,
            current_user_name=current_user_name,
        )
    except Exception as exc:
        logger.warning("Memory authority resolution failed for agent %s: %s", agent_id, type(exc).__name__)
        return MemoryContextResult(
            content="",
            status="unavailable",
            code="memory_authority_unavailable",
            user_message="Memory authority could not be verified. Conversation can continue, but governed effects are frozen.",
            retryable=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
            error_class=type(exc).__name__,
        )

    if accountability is None:
        logger.warning(
            "Memory activation principal unresolved; blocking prompt memory for agent %s",
            agent_id,
            extra={
                "metric": "memory_activation_fail_closed",
                "agent_id": str(agent_id),
                "tenant_id": str(tenant_id),
            },
        )
        return MemoryContextResult(
            content="",
            status="blocked_authority",
            code="memory_authority_unresolved",
            user_message="Memory access authority could not be verified. Conversation can continue, but governed effects are frozen.",
            retryable=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
        )

    from app.memory.profile_plane import (
        DEFAULT_RESIDENT_BUDGET_CHARS,
        check_resident_budget,
        load_resident_memory,
    )

    try:
        base_resident = load_resident_memory(
            agent_id=agent_id,
            data_root=data_root,
            budget_chars=float(getattr(settings, "MEMORY_RESIDENT_BUDGET_CHARS", DEFAULT_RESIDENT_BUDGET_CHARS)),
            include_overlay=False,
        )
    except Exception as exc:
        logger.error("Resident profile unavailable for agent %s: %s", agent_id, type(exc).__name__)
        return MemoryContextResult(
            content="",
            status="unavailable",
            code="resident_profile_unavailable",
            user_message="Required agent memory is temporarily unavailable. Conversation can continue in degraded mode.",
            retryable=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
            error_class=type(exc).__name__,
        )

    critical_read_errors = {"self", "profiles/owner"}.intersection(base_resident.read_errors)
    if critical_read_errors:
        logger.error(
            "Resident identity sections unavailable for agent %s: %s",
            agent_id,
            sorted(critical_read_errors),
        )
        return MemoryContextResult(
            content="",
            status="unavailable",
            code="resident_identity_unavailable",
            user_message="Required agent identity memory is temporarily unavailable. Conversation can continue, but governed effects are frozen.",
            retryable=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
            degraded_components=tuple(sorted(critical_read_errors)),
            error_class="ResidentReadError",
        )
    degraded_components: list[str] = list(base_resident.read_errors)

    # The resident explicit index is authority-checked at read time; the model
    # loads the full bodies itself through load_memory(ids=[...]).
    resident, overlay_sections = base_resident, []
    withheld_note = ""
    try:
        from app.memory.explicit_overlay import load_explicit_overlay_entries
        from app.memory.profile_plane import with_explicit_memory_index
        from app.services.explicit_memory_authority import authorized_explicit_entries, source_dependency
        from app.services.personal_knowledge_access import AgentRuntimePrincipal

        principal = AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=uuid.UUID(str(current_user_id)),
            session_id=str(session_id), runtime_task_id=runtime_task_id, purpose="interactive_session")
        issues = []
        authorized = await authorized_explicit_entries(tenant_id=tenant_id, agent_id=agent_id, request=principal,
            entries=load_explicit_overlay_entries(data_root, agent_id), issue_observer=issues)
        if issues:
            degraded_components.append("explicit_memory_source_authority")
            # A4c in the prompt: only the requester's own entries (issues that
            # carry a memory_id) are named by count. An id-less group-outage
            # issue may belong to another member and stays silently omitted.
            own_issues = [issue for issue in issues if issue.get("memory_id")]
            if own_issues:
                withheld_note = f"有{len(own_issues)} 条记忆暂时无法核实，已略过，可继续使用其余内容。"
        overlay_sources = {entry.entry_id: (source_dependency(receipt),) for entry, receipt in authorized}
        resident = with_explicit_memory_index(base_resident, [entry for entry, _receipt in authorized],
            overlay_sources,
            section_observer=lambda entry, text: overlay_sections.append({
                "content": text, "sources": overlay_sources[entry.entry_id], "unbound": ()}))
    except Exception as exc:
        logger.warning("Resident explicit source authority unavailable: %s", type(exc).__name__)
        degraded_components.append("explicit_memory_source_authority")
        withheld_note = "有部分记忆暂时无法核实，已略过，可继续使用其余内容。"
    withheld_section = ()
    if withheld_note:
        noted = f"{resident.text}\n\n{withheld_note}" if resident.text else withheld_note
        resident = replace(resident, text=noted)
        # A complete section of its own: no sources to bind, no unbound
        # reasons to claim, so split_context_message keeps it intact even
        # when the surrounding compiled content carries other unbound data.
        withheld_section = ({"content": withheld_note, "sources": (), "unbound": ()},)

    resident_unbound = tuple(f"resident_source_unverified:{ref['source_id']}" for ref in resident.source_refs
        if ref["source_kind"] != "explicit_memory" and (
            ref["source_kind"] != "agent_resident_memory" or ref["source_id"] != f"{agent_id}\nmemory/self/self.md"))
    try:
        await check_resident_budget(agent_id=agent_id, data_root=data_root, resident=resident)
    except Exception as budget_exc:
        logger.warning("Resident budget check failed for %s: %s", agent_id, type(budget_exc).__name__)
        degraded_components.append("resident_budget_telemetry")

    common = {
        "source_refs": resident.source_refs,
        "source_unbound": resident_unbound,
        "sections": (
            ({"content": base_resident.text, "sources": base_resident.source_refs, "unbound": resident_unbound},)
            if base_resident.text else ()
        ) + tuple(overlay_sections) + withheld_section,
    }
    if degraded_components:
        return MemoryContextResult(
            content=resident.text,
            status="degraded",
            code="memory_auxiliary_degraded",
            user_message="Core memory is available, but an auxiliary memory component is temporarily degraded.",
            retryable=True,
            degraded_components=tuple(dict.fromkeys(degraded_components)),
            **common,
        )
    return MemoryContextResult(
        content=resident.text,
        status="ready",
        code="memory_context_ready",
        user_message="Memory context is ready.",
        **common,
    )


async def _resolve_accountability_context(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    current_user_id: uuid.UUID | str | None,
    current_user_name: str | None,
) -> AgentAccountabilityContext | None:
    try:
        async with tenant_scoped_session(tenant_id) as db:
            row = (await db.execute(select(Agent, Tenant).join(Tenant, Tenant.id == Agent.tenant_id)
                .where(Agent.id == agent_id, Agent.tenant_id == tenant_id))).first()
            agent, tenant = row if row is not None else (None, None)

            if agent is None or tenant is None:
                return None

            current_user_uuid = _coerce_uuid(current_user_id)
            owner_uuid = (
                getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None) or current_user_uuid
            )
            creator_uuid = getattr(agent, "creator_id", None)

            users = {user.id: user for user in await db.scalars(select(User).where(
                User.id.in_({value for value in (owner_uuid, creator_uuid, current_user_uuid) if value}))) }
            owner, creator, current_user = (users.get(value) for value in (owner_uuid, creator_uuid, current_user_uuid))

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
        logger.warning("Failed to resolve activation accountability context for agent %s: %s", agent_id, exc)
        raise


def _display_name(user: User | None) -> str | None:
    if not user:
        return None
    return getattr(user, "display_name", None) or getattr(user, "username", None) or getattr(user, "email", None)



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

    # Resident memory + explicit index in the dynamic suffix (~6K tokens for 256K models)
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

    Auto-compaction is implicit, so the wrapper keeps CC's continue-without-recap
    directive, scoped to the current request: it must not reopen a request the
    user interrupted. The recovery pointer is system-injected here
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
        "memory/t0/sessions/<session_id>/segments/<segment_id>/source.md in your workspace.\n"
        "Continue the current request directly; do not acknowledge or recap this summary, and do not ask the user about it. "
        "A request the user interrupted stays stopped unless the user asks to resume it."
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
    before_compaction: CompactionCallback | None = None,
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

    if before_compaction:
        maybe_result = before_compaction(
            {
                "estimated_tokens": current_tokens,
                "trigger_tokens": trigger_tokens,
                "context_limit": context_limit,
                "message_count": len(messages),
            }
        )
        if maybe_result is not None:
            await maybe_result

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
                from app.runtime.compaction_trace import CompactedMessages

                return CompactedMessages([_wrap_compressed_summary(summary)] + recent_messages,
                    source_messages=[dict(row) for row in old_messages if _summarizer._serialize_message_for_summary(row)])
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
    from app.runtime.context_candidates import ContextMessageOrigin

    marker = {"role": "system", "content": "[Older messages omitted: LLM semantic compaction summary unavailable]"}
    marker["context_origin"] = ContextMessageOrigin.capture(
        kind="compaction_status", role="system", content=marker["content"], sources=(), unbound=())
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

    # Persist cadence input and hand due work to the durable RuntimeTask owner.
    try:
        from app.services.auto_dream import record_session_end
        from app.services.dream_runtime import enqueue_due_dream

        record_session_end(agent_id)
        queued = await enqueue_due_dream(
            agent_id=agent_id,
            tenant_id=tenant_id,
            source="conversation_end",
        )
        if queued is not None:
            logger.info("[Memory] Durable %s Dream queued for agent %s", queued.mode, agent_id)
    except Exception as _dream_err:
        logger.debug("[Memory] Auto-dream check failed: %s", _dream_err)


async def persist_runtime_memory(
    *,
    agent_id: uuid.UUID,
    session_id: str | None,
    tenant_id: uuid.UUID,
    messages: list[dict],
) -> None:
    """Persist the session-list/search summary projection for any runtime entrypoint.

    Durable memory extraction is handled by hooks/extractor into T2 and must not
    be duplicated here. ``ChatSession.summary`` is a user-facing projection, not
    an episodic prompt-memory source.
    """
    if not _has_meaningful_messages(messages):
        return

    _MAX_RETRIES = 2
    _RETRY_DELAYS = (1.0, 3.0)
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            summary = await _generate_session_summary(
                messages,
                tenant_id,
                agent_id=agent_id,
                http_max_attempts=1,
            )
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
        "max_output_tokens": getattr(model, "max_output_tokens", None),
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


async def _generate_session_summary(
    messages: list[dict],
    tenant_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    http_max_attempts: int | None = None,
) -> str | None:
    """Generate the user-facing session-list/search summary using LLM only.

    Canonical durable memory is the T2 package read model. If the projection LLM
    is unavailable or fails, hold the projection write instead of persisting a
    mechanical extraction as a second semantic path.
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
                http_max_attempts=http_max_attempts,
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
            # Keep session-list/search ordering current; prompt memory reads T2.
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
