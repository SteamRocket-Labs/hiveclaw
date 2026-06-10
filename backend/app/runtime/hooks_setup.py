"""Memory system hook handler registration.

Phase 0: logging-only for SESSION_START, POST_COMPACTION, MEMORY_EXTRACTED.
Phase 1: T0 cursor-based log writers for SESSION_CLOSE/IDLE, TRIGGER_END,
         DELEGATION_END, HEARTBEAT_TICK_END, DREAM_END.
         Chat T0 uses cursor to write only new messages — safe across reconnects.
Phase 2: Extractor for RESPONSE_COMPLETE, PRE_COMPACTION, SESSION_CLOSE drain.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

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
_DEFAULT_HOOK_REGISTRY = hook_registry


# ── Logging-only handlers (Phase 0, kept for events without active handler) ──


async def _log_session_start(ctx: HookContext) -> None:
    model = ctx.metadata.get("model", "?")
    logger.info(
        "[Hooks] SESSION_START: agent=%s source=%s model=%s",
        ctx.agent_id,
        ctx.source,
        model,
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
        ctx.agent_id,
        trigger,
        summary_len,
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


def _agent_data_root() -> Path:
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR)


def schedule_fast_reflection_candidate(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    messages: list[dict],
    metadata: dict,
) -> dict[str, str]:
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    async def _run() -> None:
        try:
            await asyncio.to_thread(
                create_fast_reflection_candidate,
                data_root=data_root,
                agent_id=agent_id,
                session_id=session_id,
                messages=messages,
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("[FastReflection] skipped for %s: %s", agent_id, exc)

    asyncio.create_task(_run())
    return {"status": "scheduled"}


async def _fast_reflection_on_response(ctx: HookContext) -> None:
    """RESPONSE_COMPLETE → non-blocking candidate creation for strong correction signals."""
    agent_id = _parse_agent_id(ctx)
    if not agent_id:
        return
    messages = ctx.messages or []
    if not messages:
        return
    schedule_fast_reflection_candidate(
        data_root=_agent_data_root(),
        agent_id=agent_id,
        session_id=str(ctx.session_id or ""),
        messages=messages,
        metadata=ctx.metadata,
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


def _is_reportable_session(messages: list[dict], metadata: dict) -> bool:
    if metadata.get("loop_guard_triggered") or metadata.get("failed") or metadata.get("partial_failure"):
        return True
    if metadata.get("commit") or metadata.get("deployment") or metadata.get("external_action"):
        return True
    if len(messages) >= int(metadata.get("reportable_message_threshold") or 12):
        return True
    text = "\n".join(str(msg.get("content") or "") for msg in messages[-6:])
    return any(marker in text.lower() for marker in ("wrong", "错了", "不是", "failed", "失败", "loop guard"))


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
    # 切口④: settle the session's verified ledger findings into durable T2 memory.
    # Runs through the same write gate as all extractions (PL4 rejected, sensitivity
    # classified) — see extract_agent.consolidate_ledger_findings_to_t2. Best-effort:
    # a consolidation failure must not abort SESSION_CLOSE T0 logging below.
    try:
        from app.services.extract_agent import consolidate_ledger_findings_to_t2

        written = consolidate_ledger_findings_to_t2(
            agent_id,
            session_id=ctx.session_id,
            source="work_ledger",
        )
        if written:
            logger.info("[Hooks] SESSION_CLOSE: agent=%s ledger→T2 settled %d entries", agent_id, written)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Hooks] SESSION_CLOSE: ledger→T2 consolidation skipped for %s: %s", agent_id, exc)
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
    if _is_reportable_session(messages, ctx.metadata):
        try:
            from app.config import get_settings
            from app.services.reflection_service import create_reportable_reflection

            create_reportable_reflection(
                data_root=Path(get_settings().AGENT_DATA_DIR),
                agent_id=agent_id,
                session_id=str(ctx.session_id or ""),
                reason=str(ctx.metadata.get("reason") or "session_close_reportable"),
                messages=messages,
                metadata=ctx.metadata,
            )
        except Exception as exc:
            logger.debug("[Hooks] reportable reflection skipped for %s: %s", agent_id, exc)
    _t0_cursors[session_key] = len(messages)


async def _t0_session_idle(ctx: HookContext) -> None:
    """SESSION_IDLE → write incremental T0 log (cursor-based, no duplication).

    Extraction is NOT triggered here — RESPONSE_COMPLETE already extracts
    after every agent response (cursor-based, no duplicates). SESSION_IDLE
    only writes the T0 snapshot and marks the session for dream gate counting.
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
    logger.info(
        "[Hooks] SESSION_IDLE: agent=%s idle=%ss new_msgs=%d (cursor %d→%d)",
        ctx.agent_id,
        idle_s,
        len(new_messages),
        cursor,
        len(messages),
    )
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

    from app.channel_message_contracts import extract_sender_label_from_message

    # Extract originator info from conversation messages
    originator_name = ""
    originator_identity = ""
    messages = ctx.messages or []
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                originator_name = extract_sender_label_from_message(content) or originator_name
                # No fallback to agent_name — that's the bot, not the human originator

    try:
        from app.database import tenant_scoped_session
        from app.models.chat_session import ChatSession
        from app.services.pending_reply_service import sender_identity_from_session
        from app.services.tenant_resolver import resolve_tenant_for_agent
        from sqlalchemy import select

        # POST_TOOL_USE may fire from a daemon/background path with no request GUC.
        # Resolve the owning tenant so the SELECT and the pending_reply_contexts
        # INSERT survive the stage-3 non-owner role flip (a bare session
        # fail-closes → silently drops the captured reply context).
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            if ctx.session_id:
                try:
                    session_result = await db.execute(
                        select(ChatSession).where(ChatSession.id == uuid.UUID(str(ctx.session_id)))
                    )
                    session_obj = session_result.scalar_one_or_none()
                except Exception:
                    session_obj = None
                if session_obj:
                    if not originator_name:
                        delivery_target = getattr(session_obj, "delivery_target_json", None) or {}
                        originator_name = (
                            str(delivery_target.get("user_label") or delivery_target.get("username") or "").strip()
                            or originator_name
                        )
                    originator_identity = sender_identity_from_session(session_obj)
            await capture_pending_reply(
                db,
                agent_id=agent_id,
                tenant_id=tenant_id,
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
    from app.runtime import hooks as hooks_mod

    registry = hook_registry
    if registry is _DEFAULT_HOOK_REGISTRY and hooks_mod.hook_registry is not _DEFAULT_HOOK_REGISTRY:
        registry = hooks_mod.hook_registry

    registry.register_many(_MEMORY_HOOK_REGISTRATIONS)

    logger.info(
        "[Hooks] Memory hooks registered: %d handlers (3 log + 2 extract + 1 fast_reflection + 6 T0 + 1 pending_reply)",
        len(_MEMORY_HOOK_REGISTRATIONS),
    )


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
    "fast_reflection_on_response": _fast_reflection_on_response,
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
        "event": HookEvent.RESPONSE_COMPLETE.value,
        "handler": "fast_reflection_on_response",
        "key": "memory.response_complete.fast_reflection",
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
