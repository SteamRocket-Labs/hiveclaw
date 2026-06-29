"""Continuation controls for long-lived Agent-to-Agent sessions.

The transcript event is the durable mailbox truth. Active runs consume the same
message through the existing mid-run drain; inactive open sessions start a new
durable turn; terminal sessions reject the continuation and record that fact.
"""

from __future__ import annotations

from html import escape
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
    parts: list[dict[str, Any]] | None = None,
    materialize_chat_message: bool = True,
    source: str = "agent_session_mailbox",
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
        parts=parts,
        metadata=metadata,
        visibility_scope=getattr(session, "visibility_scope", None) or "team",
        listed_surface=getattr(session, "listed_surface", None) or "parent",
        materialize_chat_message=materialize_chat_message,
        source=source,
    )


def _task_notification_xml_field(tag: str, value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return f"<{tag}>{escape(text, quote=False)}</{tag}>"


def _task_notification_artifacts_xml(artifacts: list[dict[str, Any]] | None) -> str:
    if not artifacts:
        return ""
    rows: list[str] = []
    for artifact in artifacts:
        path = _text(artifact.get("path"))
        if not path:
            continue
        fields = [
            _task_notification_xml_field("artifact-id", artifact.get("artifact_id") or artifact.get("id")),
            _task_notification_xml_field("artifact-path", path),
            _task_notification_xml_field("artifact-name", artifact.get("name")),
            _task_notification_xml_field("source-agent-id", artifact.get("source_agent_id")),
            _task_notification_xml_field("owner-agent-id", artifact.get("owner_agent_id")),
            _task_notification_xml_field("download-agent-id", artifact.get("download_agent_id")),
        ]
        rows.append("<artifact>\n" + "\n".join(field for field in fields if field) + "\n</artifact>")
    if not rows:
        return ""
    return "<artifacts>\n" + "\n".join(rows) + "\n</artifacts>"


def build_task_notification_message(
    *,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    source: str = "task_notification",
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    """Build the CC-compatible task notification envelope consumed by parent turns."""

    fields = [
        _task_notification_xml_field("task-id", task_id),
        _task_notification_xml_field("task-type", task_type),
        _task_notification_xml_field("source", source),
        _task_notification_xml_field("child-agent", child_agent_name),
        _task_notification_xml_field("child-session-id", child_session_id),
        _task_notification_xml_field("status", status),
        _task_notification_xml_field("summary", summary),
        _task_notification_xml_field("result", summary),
        _task_notification_artifacts_xml(artifacts),
    ]
    body = "\n".join(field for field in fields if field)
    return f"<task-notification>\n{body}\n</task-notification>"


def _task_notification_display_content(*, child_agent_name: str | None, status: str, summary: str) -> str:
    actor = _text(child_agent_name) or "Background task"
    status_text = _text(status) or "completed"
    clean_summary = _text(summary)
    if len(clean_summary) > 240:
        clean_summary = clean_summary[:237].rstrip() + "..."
    return f"{actor} {status_text}: {clean_summary}" if clean_summary else f"{actor} {status_text}"


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
    event_type: str = "agent_session_message",
    mailbox_kind: str = "followup",
    materialize_chat_message: bool = True,
    source_channel: str = "agent_session_mailbox",
    extra_metadata: dict[str, Any] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append and consume a follow-up message for an Agent-Agent session."""

    clean_message = _text(message)
    if not clean_message:
        return {"ok": False, "status": "rejected", "reason": "empty_message"}

    state = _session_state(session)
    metadata_base = {
        **_session_metadata(session),
        **(extra_metadata or {}),
        "mailbox_kind": mailbox_kind,
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
            source=source_channel,
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
        event_type=event_type,
        message=clean_message,
        parent_session_id=parent_session_id,
        run_id=getattr(active_run, "id", None) if active_run is not None else None,
        metadata={
            **metadata_base,
            "consumer": "mid_run_message_drain" if active_run is not None else "continuation_turn",
        },
        parts=parts,
        materialize_chat_message=materialize_chat_message,
        source=source_channel,
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
            source_channel=source_channel,
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
            "source": source_channel,
            "agent_session_message": True,
            "parent_session_id": _text(parent_session_id or getattr(session, "parent_session_id", None)) or None,
            "latest_user_prompt_overrides_history": True,
            **(extra_metadata or {}),
        },
    )
    return {
        **run,
        "ok": True,
        "status": "started",
        "consumer": "continuation_turn",
        "child_session_id": str(session.id),
    }


async def continue_parent_session_with_task_notification(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    source: str = "task_notification",
    metadata: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message = build_task_notification_message(
        task_id=task_id,
        task_type=task_type,
        status=status,
        summary=summary,
        child_session_id=child_session_id,
        child_agent_name=child_agent_name,
        source=source,
        artifacts=artifacts,
    )
    artifact_paths = [str(part.get("path")) for part in artifacts or [] if part.get("path")]
    artifact_ids = [str(part.get("artifact_id") or part.get("id")) for part in artifacts or [] if part.get("artifact_id") or part.get("id")]
    task_metadata = {
        "task_notification": True,
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
        "summary": summary,
        "child_session_id": _text(child_session_id) or None,
        "child_agent_name": _text(child_agent_name) or None,
        "source": "task_notification",
        "notification_source": source,
        "artifacts": artifacts or [],
        "artifact_paths": artifact_paths,
        "artifact_ids": artifact_ids,
        **(metadata or {}),
    }
    return await continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message=message,
        parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
        display_content=_task_notification_display_content(
            child_agent_name=child_agent_name,
            status=status,
            summary=summary,
        ),
        event_type="agent_task_notification",
        mailbox_kind="task_notification",
        materialize_chat_message=False,
        source_channel="task_notification",
        extra_metadata=task_metadata,
        parts=artifacts,
    )
