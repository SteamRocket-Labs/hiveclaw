"""Memory system hook handler registration.

Phase 0: logging-only for SESSION_START, POST_COMPACTION, MEMORY_EXTRACTED.
Phase 1: T0 cursor-based log writers for SESSION_CLOSE/IDLE, TRIGGER_END,
         DELEGATION_END, HEARTBEAT_TICK_END, DREAM_END.
         Chat T0 uses cursor to write only new messages — safe across reconnects.
Phase 2: Extractor for RESPONSE_COMPLETE, PRE_COMPACTION, SESSION_CLOSE drain.
"""

from __future__ import annotations

import logging
import uuid

from app.runtime.hooks import (
    HookContext,
    HookEvent,
    describe_registration_specs,
    hook_registry,
    load_registration_specs,
)
from app.services.extract_agent import extract_agent
from app.services.pending_reply_service import OUTBOUND_TOOL_NAMES
from app.services.session_memory import (
    build_session_memory_payload_from_messages,
    update_session_memory,
    write_compaction_summary,
)
from app.services.t0_logger import write_t0_log

logger = logging.getLogger(__name__)


# ── Logging-only handlers (Phase 0, kept for events without active handler) ──


async def _log_session_start(ctx: HookContext) -> None:
    model = ctx.metadata.get("model", "?")
    logger.info(
        "[Hooks] SESSION_START: agent=%s source=%s model=%s",
        ctx.agent_id, ctx.source, model,
    )
    # Reset extractor cursor on new session
    agent_id = _parse_agent_id(ctx)
    if agent_id:
        extract_agent.reset_cursor(agent_id)


async def _log_post_compaction(ctx: HookContext) -> None:
    trigger = ctx.metadata.get("trigger", "?")
    summary_len = len(ctx.metadata.get("summary", ""))
    logger.info(
        "[Hooks] POST_COMPACTION: agent=%s trigger=%s summary_len=%d",
        ctx.agent_id, trigger, summary_len,
    )
    agent_id = _parse_agent_id(ctx)
    if agent_id and ctx.metadata.get("summary"):
        write_compaction_summary(
            agent_id,
            str(ctx.metadata.get("summary", "")),
            original_message_count=ctx.metadata.get("before_msgs"),
            kept_message_count=ctx.metadata.get("after_msgs"),
        )


async def _log_memory_extracted(ctx: HookContext) -> None:
    logger.info("[Hooks] MEMORY_EXTRACTED: agent=%s", ctx.agent_id)


# ── Extractor handlers (Phase 2) ──


async def _extract_on_response(ctx: HookContext) -> None:
    """RESPONSE_COMPLETE → fire-and-forget extraction to T2."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    turn = ctx.metadata.get("turn_count", "?")
    logger.info("[Hooks] RESPONSE_COMPLETE: agent=%s source=%s turn=%s", ctx.agent_id, ctx.source, turn)
    update_session_memory(
        agent_id,
        build_session_memory_payload_from_messages(ctx.messages or [], metadata=ctx.metadata),
    )
    # Fire-and-forget: don't block the response (tracked for drain at SESSION_CLOSE)
    tenant_id = ctx.metadata.get("tenant_id")
    agent_name = ctx.metadata.get("agent_name", "Agent")
    extract_agent.schedule_extract(
        agent_id=agent_id,
        messages=ctx.messages,
        source=ctx.source or "web",
        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
        agent_name=agent_name,
    )


async def _extract_on_pre_compaction(ctx: HookContext) -> None:
    """PRE_COMPACTION → synchronous extraction before context is lost."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    trigger = ctx.metadata.get("trigger", "?")
    logger.info("[Hooks] PRE_COMPACTION: agent=%s trigger=%s msgs=%d", ctx.agent_id, trigger, len(ctx.messages or []))
    update_session_memory(
        agent_id,
        build_session_memory_payload_from_messages(ctx.messages or [], metadata=ctx.metadata),
    )
    # Synchronous: must finish before compaction discards messages
    tenant_id = ctx.metadata.get("tenant_id")
    agent_name = ctx.metadata.get("agent_name", "Agent")
    await extract_agent.extract(
        agent_id=agent_id,
        messages=ctx.messages,
        source="compaction",
        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
        agent_name=agent_name,
    )


# ── T0 writers (Phase 1) ──


def _parse_agent_id(ctx: HookContext) -> uuid.UUID | None:
    """Parse agent_id from HookContext, return None on failure."""
    try:
        return uuid.UUID(str(ctx.agent_id))
    except (ValueError, AttributeError):
        logger.warning("[T0] Invalid agent_id: %s", ctx.agent_id)
        return None


_t0_cursors: dict[str, int] = {}  # "agent_id:session_id" → message index of last T0 write


async def _t0_session_close(ctx: HookContext) -> None:
    """SESSION_CLOSE → drain extractor + write incremental T0 (cursor-based)."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    reason = ctx.metadata.get("reason", "unknown")
    messages = ctx.messages or []
    logger.info("[Hooks] SESSION_CLOSE: agent=%s reason=%s msgs=%d", ctx.agent_id, reason, len(messages))
    update_session_memory(
        agent_id,
        build_session_memory_payload_from_messages(messages, metadata=ctx.metadata),
    )
    # Drain pending extractions before session ends
    await extract_agent.drain(agent_id, timeout_s=10.0)
    # Write only new messages since last T0 cursor
    session_key = f"{agent_id}:{ctx.session_id}"
    cursor = _t0_cursors.get(session_key, 0)
    new_messages = messages[cursor:]
    if not new_messages:
        logger.debug("[Hooks] SESSION_CLOSE: no new messages since cursor=%d, skipping T0", cursor)
        return
    write_t0_log(
        agent_id,
        behavior_type="chat",
        messages=new_messages,
        metadata={**ctx.metadata, "source": ctx.source or "web", "cursor_start": cursor},
    )
    _t0_cursors[session_key] = len(messages)


async def _t0_session_idle(ctx: HookContext) -> None:
    """SESSION_IDLE → write incremental T0 log (cursor-based, no duplication).

    Extraction is NOT triggered here — RESPONSE_COMPLETE already extracts
    after every agent response (cursor-based, no duplicates). SESSION_IDLE
    only writes the T0 snapshot and marks the session for dream gate counting.
    Aligned with Claude Code: CC has no idle-triggered extraction either.
    """
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    messages = ctx.messages or []
    idle_s = ctx.metadata.get("idle_seconds", "?")
    # Write only new messages since last T0 cursor
    session_key = f"{agent_id}:{ctx.session_id}"
    cursor = _t0_cursors.get(session_key, 0)
    new_messages = messages[cursor:]
    if not new_messages:
        logger.debug("[Hooks] SESSION_IDLE: agent=%s no new messages since cursor=%d", ctx.agent_id, cursor)
        return
    logger.info("[Hooks] SESSION_IDLE: agent=%s idle=%ss new_msgs=%d (cursor %d→%d)",
                ctx.agent_id, idle_s, len(new_messages), cursor, len(messages))
    write_t0_log(
        agent_id,
        behavior_type="chat",
        messages=new_messages,
        metadata={**ctx.metadata, "source": ctx.source or "web", "cursor_start": cursor},
    )
    _t0_cursors[session_key] = len(messages)


async def _t0_trigger_end(ctx: HookContext) -> None:
    """TRIGGER_END → write trigger T0 log."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] TRIGGER_END: agent=%s trigger=%s", ctx.agent_id, ctx.metadata.get("trigger_name", "?"))
    write_t0_log(
        agent_id,
        behavior_type="trigger",
        messages=ctx.messages or [],
        metadata=ctx.metadata,
    )


async def _t0_delegation_end(ctx: HookContext) -> None:
    """DELEGATION_END → write delegation T0 log."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] DELEGATION_END: agent=%s", ctx.agent_id)
    write_t0_log(
        agent_id,
        behavior_type="delegation",
        messages=ctx.messages or [],
        metadata=ctx.metadata,
    )


async def _t0_heartbeat_tick_end(ctx: HookContext) -> None:
    """HEARTBEAT_TICK_END → write heartbeat T0 log."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] HEARTBEAT_TICK_END: agent=%s", ctx.agent_id)
    write_t0_log(
        agent_id,
        behavior_type="heartbeat",
        messages=ctx.messages or [],
        metadata=ctx.metadata,
    )


async def _t0_dream_end(ctx: HookContext) -> None:
    """DREAM_END → write dream T0 log + reset heartbeat persistent session."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    logger.info("[Hooks] DREAM_END: agent=%s", ctx.agent_id)
    write_t0_log(
        agent_id,
        behavior_type="dream",
        messages=ctx.messages or [],
        metadata=ctx.metadata,
    )
    # Phase 5: Reset heartbeat KAIROS session after dream completes
    # so next heartbeat tick starts fresh with updated T3 memory.
    from app.services.heartbeat import _reset_heartbeat_session
    _reset_heartbeat_session(agent_id)


async def _capture_pending_reply(ctx: HookContext) -> None:
    """POST_TOOL_USE → auto-capture pending reply context for outbound messages."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return

    tool_result = ctx.tool_result or ""
    # Skip failed tool calls — error results start with known failure prefixes
    if tool_result.startswith(("❌", "⚠️", "[Tool execution error]", "Blocked by hook")):
        return
    if not tool_result:
        return

    from app.services.pending_reply_service import (
        capture_pending_reply,
        extract_recipient_info,
    )

    if not extract_recipient_info(ctx.tool_name or "", ctx.tool_args or {}):
        return

    # Extract originator info from conversation messages
    originator_name = ""
    originator_identity = ""
    messages = ctx.messages or []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                # Try to extract sender label from "[发送者: XXX]" prefix
                if content.startswith("[发送者:") or content.startswith("[发送者："):
                    bracket_end = content.find("]")
                    if bracket_end > 0:
                        originator_name = content[5:bracket_end].strip().split("(")[0].strip()
                # No fallback to agent_name — that's the bot, not the human originator

    try:
        from app.database import async_session

        async with async_session() as db:
            await capture_pending_reply(
                db,
                agent_id=agent_id,
                tool_name=ctx.tool_name or "",
                tool_args=ctx.tool_args or {},
                messages=messages,
                originator_name=originator_name,
                originator_identity=originator_identity,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("[PendingReply] Failed to capture: %s", exc)


def register_memory_hooks() -> None:
    """Register all memory system hook handlers.

    Called from main.py lifespan during startup.
    Phase 0: logging-only for SESSION_START, POST_COMPACTION, MEMORY_EXTRACTED.
    Phase 1: T0 cursor-based writers for SESSION_CLOSE/IDLE, TRIGGER_END, DELEGATION_END, HEARTBEAT_TICK_END, DREAM_END.
    Phase 2: Extractor for RESPONSE_COMPLETE, PRE_COMPACTION; drain on SESSION_CLOSE.
    Phase 3: Pending reply capture for outbound messages.
    """
    hook_registry.register_many(_MEMORY_HOOK_REGISTRATIONS)

    logger.info("[Hooks] Memory system hooks registered: %d handlers (3 log + 2 extract + 6 T0 + 1 pending_reply)", 12)


def export_memory_hook_plan() -> list[dict[str, object]]:
    """Return the declarative memory-hook registration plan for observability."""
    return describe_registration_specs(_MEMORY_HOOK_REGISTRATIONS)


_MEMORY_HOOK_REGISTRATIONS = [
    # Loaded from the declarative config below via load_registration_specs().
]

_MEMORY_HOOK_HANDLERS = {
    "log_session_start": _log_session_start,
    "log_post_compaction": _log_post_compaction,
    "log_memory_extracted": _log_memory_extracted,
    "extract_on_response": _extract_on_response,
    "extract_on_pre_compaction": _extract_on_pre_compaction,
    "t0_session_close": _t0_session_close,
    "t0_session_idle": _t0_session_idle,
    "t0_trigger_end": _t0_trigger_end,
    "t0_delegation_end": _t0_delegation_end,
    "t0_heartbeat_tick_end": _t0_heartbeat_tick_end,
    "t0_dream_end": _t0_dream_end,
    "capture_pending_reply": _capture_pending_reply,
}

_MEMORY_HOOK_CONFIGURATION = [
    {"event": HookEvent.SESSION_START.value, "handler": "log_session_start", "key": "memory.session_start.log"},
    {
        "event": HookEvent.POST_COMPACTION.value,
        "handler": "log_post_compaction",
        "key": "memory.post_compaction.log",
    },
    {
        "event": HookEvent.MEMORY_EXTRACTED.value,
        "handler": "log_memory_extracted",
        "key": "memory.extracted.log",
    },
    {
        "event": HookEvent.RESPONSE_COMPLETE.value,
        "handler": "extract_on_response",
        "key": "memory.response_complete.extract",
    },
    {
        "event": HookEvent.PRE_COMPACTION.value,
        "handler": "extract_on_pre_compaction",
        "key": "memory.pre_compaction.extract",
    },
    {"event": HookEvent.SESSION_CLOSE.value, "handler": "t0_session_close", "key": "memory.session_close.t0"},
    {"event": HookEvent.SESSION_IDLE.value, "handler": "t0_session_idle", "key": "memory.session_idle.t0"},
    {"event": HookEvent.TRIGGER_END.value, "handler": "t0_trigger_end", "key": "memory.trigger_end.t0"},
    {
        "event": HookEvent.DELEGATION_END.value,
        "handler": "t0_delegation_end",
        "key": "memory.delegation_end.t0",
    },
    {
        "event": HookEvent.HEARTBEAT_TICK_END.value,
        "handler": "t0_heartbeat_tick_end",
        "key": "memory.heartbeat_tick_end.t0",
    },
    {"event": HookEvent.DREAM_END.value, "handler": "t0_dream_end", "key": "memory.dream_end.t0"},
    {
        "event": HookEvent.POST_TOOL_USE.value,
        "handler": "capture_pending_reply",
        "key": "pending_reply.post_tool_use.capture",
        "tool_names": list(OUTBOUND_TOOL_NAMES),
    },
]

_MEMORY_HOOK_REGISTRATIONS = load_registration_specs(_MEMORY_HOOK_CONFIGURATION, _MEMORY_HOOK_HANDLERS)
