"""Continuation controls for long-lived Agent-to-Agent sessions.

The transcript event is the durable mailbox truth. Active runs consume the same
message through the existing mid-run drain; inactive open sessions start a new
durable turn; terminal sessions reject the continuation and record that fact.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_transcript import append_session_event
from app.services.web_chat_runtime import (
    WEB_CHAT_TURN_TASK_TYPE,
    _find_active_run,
    _queue_saved_mid_run_user_message,
    start_web_chat_run,
)

_TERMINAL_SESSION_STATES = {"completed", "failed", "killed", "closed", "skipped", "cancelled"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _session_metadata(session: ChatSession) -> dict[str, Any]:
    return dict(getattr(session, "transcript_metadata_json", None) or {})


def _session_state(session: ChatSession) -> str:
    metadata = _session_metadata(session)
    return _text(metadata.get("session_state") or metadata.get("state") or "open").lower() or "open"


def _runtime_task_type_for_session(session: ChatSession, explicit: str | None = None) -> str:
    if _text(explicit):
        return _text(explicit)
    if _text(getattr(session, "session_kind", None)) == "team_member":
        return "team_member"
    if _text(getattr(session, "runtime_source", None)) == "team_member":
        return "team_member"
    return WEB_CHAT_TURN_TASK_TYPE


async def _append_mailbox_event(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    event_type: str,
    message: str,
    parent_session_id: str | uuid.UUID | None,
    metadata: dict[str, Any],
    role: str | None = "user",
    run_id: Any = None,
    materialize_chat_message: bool = True,
) -> None:
    await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(session, "tenant_id", None) or getattr(agent, "tenant_id", None),
        session_id=session.id,
        actor_type="agent",
        event_type=event_type,
        content=message,
        role=role,
        user_id=getattr(session, "user_id", None) or getattr(user, "id", None),
        run_id=run_id,
        runtime_task_id=run_id,
        root_session_id=getattr(session, "root_session_id", None) or _uuid_or_none(parent_session_id),
        parent_session_id=getattr(session, "parent_session_id", None) or _uuid_or_none(parent_session_id),
        metadata=metadata,
        visibility_scope=getattr(session, "visibility_scope", None) or "team",
        listed_surface=getattr(session, "listed_surface", None) or "parent",
        materialize_chat_message=materialize_chat_message,
        source="agent_session_mailbox",
    )


async def continue_agent_session_from_mailbox(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    message: str,
    parent_session_id: str | uuid.UUID | None = None,
    interrupt_requested: bool = False,
    runtime_task_type: str | None = None,
    display_content: str = "",
) -> dict[str, Any]:
    """Append and consume a follow-up message for an Agent-Agent session."""

    clean_message = _text(message)
    if not clean_message:
        return {"ok": False, "status": "rejected", "reason": "empty_message"}

    state = _session_state(session)
    metadata_base = {
        **_session_metadata(session),
        "mailbox_kind": "followup",
        "parent_session_id": _text(parent_session_id or getattr(session, "parent_session_id", None)) or None,
        "interrupt_requested": bool(interrupt_requested),
        "session_state": state,
    }
    if state in _TERMINAL_SESSION_STATES:
        await _append_mailbox_event(
            db=db,
            agent=agent,
            user=user,
            session=session,
            event_type="agent_session_message_rejected",
            message=clean_message,
            parent_session_id=parent_session_id,
            role="system",
            metadata={
                **metadata_base,
                "reason": "terminal_agent_session",
                # CCPlus V1 D-16 ruling (Hive-native non-parity, see
                # docs/ccplus-v1-subagent-resume-ruling-2026-06-24.md): a completed/terminal
                # subagent session is SEALED audit truth and is not reopened in place
                # (unlike CC resumeAgentBackground). Continuation is a NEW durable child
                # session linked by parent/root refs. Surface the redirect explicitly so the
                # caller spawns a fresh session instead of dead-ending.
                "resumable": False,
                "redirect": "spawn_new_session",
            },
            materialize_chat_message=False,
        )
        await db.commit()
        return {
            "ok": False,
            "status": "rejected",
            "reason": "terminal_agent_session",
            "child_session_id": str(session.id),
            "session_state": state,
            "resumable": False,
            "redirect": "spawn_new_session",
        }

    active_run = await _find_active_run(db=db, agent_id=agent.id, session_id=session.id)
    await _append_mailbox_event(
        db=db,
        agent=agent,
        user=user,
        session=session,
        event_type="agent_session_message",
        message=clean_message,
        parent_session_id=parent_session_id,
        run_id=getattr(active_run, "id", None) if active_run is not None else None,
        metadata={
            **metadata_base,
            "consumer": "mid_run_message_drain" if active_run is not None else "continuation_turn",
        },
    )

    if active_run is not None:
        payload = await _queue_saved_mid_run_user_message(
            db=db,
            active_run=active_run,
            agent=agent,
            user=user,
            session=session,
            content=clean_message,
            display_content=display_content,
            source_channel="agent_session_mailbox",
            message_already_in_t0=True,
        )
        return {
            **payload,
            "ok": True,
            "status": "queued",
            "consumer": "mid_run_message_drain",
            "child_session_id": str(session.id),
        }

    run = await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=clean_message,
        display_content=display_content,
        append_user_message=False,
        runtime_task_type=_runtime_task_type_for_session(session, runtime_task_type),
        extra_metadata={
            "source": "agent_session_mailbox",
            "agent_session_message": True,
            "parent_session_id": _text(parent_session_id or getattr(session, "parent_session_id", None)) or None,
            "latest_user_prompt_overrides_history": True,
        },
    )
    return {
        **run,
        "ok": True,
        "status": "started",
        "consumer": "continuation_turn",
        "child_session_id": str(session.id),
    }
