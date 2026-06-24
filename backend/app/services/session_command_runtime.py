"""Session command execution for CC/Codex parity.

The command layer is a control surface over existing transcript truth. It never
rewrites prior transcript events; branch/rewind create new ChatSession indexes
whose transcript prefix points back to copied source evidence.
"""

from __future__ import annotations

import uuid
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.user import User
from app.memory.t0.ledger import T0SessionEvent, replay_t0_session_events
from app.runtime.hooks import HookEvent, emit_hook
from app.services.chat_transcript import append_session_event
from app.services.conversation_branch_service import create_conversation_branch
from app.services.web_chat_runtime import cancel_web_chat_run, get_active_web_chat_run, steer_active_web_chat_turn

SESSION_COMMAND_NAMES = frozenset(
    {
        "resume",
        "checkpoints",
        "rewind",
        "rollback",
        "branch",
        "btw",
        "interrupt",
        "turn_steer",
        "steer",
        "rename",
        "tag",
        "export",
        "copy",
        "clear",
        "compact",
    }
)

_REPLAYABLE_TURN_EVENT_TYPES = {"user_message", "assistant_message", "tool_call", "tool_result", "assistant_delta"}
_INTERRUPTED_TAIL_EVENT_TYPES = {"user_message", "tool_call", "tool_result", "assistant_delta", "run_started"}
_FENCED_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _can_manage_sessions(user: User, agent: Agent, access_level: str) -> bool:
    return (
        getattr(user, "role", None) in ("platform_admin", "org_admin")
        or str(getattr(agent, "creator_id", "")) == str(getattr(user, "id", ""))
        or access_level == "manage"
    )


async def _load_session(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session_id: uuid.UUID | str | None,
    access_level: str,
) -> ChatSession:
    if session_id is None:
        raise HTTPException(status_code=400, detail="session_id is required for this command")
    session_uuid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.agent_id == agent.id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(session.user_id) != str(user.id) and not _can_manage_sessions(user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized to use this session")
    return session


def _event_metadata(event: ChatTranscriptEvent | T0SessionEvent) -> dict[str, Any]:
    metadata = getattr(event, "metadata_json", None)
    if isinstance(metadata, dict):
        return metadata
    metadata = getattr(event, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _event_anchor_id(event: ChatTranscriptEvent | T0SessionEvent) -> str:
    metadata = _event_metadata(event)
    transcript_event_id = str(metadata.get("transcript_event_id") or "").strip()
    if transcript_event_id:
        return transcript_event_id
    raw_id = getattr(event, "id", None)
    if raw_id is not None:
        return str(raw_id)
    return str(getattr(event, "event_id", "") or "")


def _event_ledger_id(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    raw_id = getattr(event, "event_id", None)
    return str(raw_id) if raw_id else None


def _branch_anchor_event_id(event: ChatTranscriptEvent | T0SessionEvent) -> uuid.UUID:
    raw = _event_anchor_id(event)
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="This checkpoint has no transcript_event_id read-model anchor for branch projection.",
        ) from exc


def _parse_uuid_argument(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a UUID") from exc


def _created_at_value(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    created_at = getattr(event, "created_at", None)
    if hasattr(created_at, "isoformat"):
        return created_at.isoformat()
    return str(created_at) if created_at else None


def _event_payload(event: ChatTranscriptEvent | T0SessionEvent) -> dict[str, Any]:
    return {
        "id": _event_anchor_id(event),
        "ledger_event_id": _event_ledger_id(event),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "actor_type": getattr(event, "actor_type", None) or _event_metadata(event).get("actor_type"),
        "role": _event_metadata(event).get("role") or getattr(event, "role", None),
        "content": event.content or "",
        "metadata": _event_metadata(event),
        "created_at": _created_at_value(event),
        "truth_path": str(getattr(event, "truth_path", None) or "") or None,
        "projection_path": str(getattr(event, "path", None) or "") or None,
        "event_hash": getattr(event, "event_hash", None),
    }


def _session_payload(session: ChatSession) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title,
        "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
        "root_session_id": str(session.root_session_id) if session.root_session_id else None,
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
    }


def _run_request_payload(run_request: Any | None) -> dict[str, Any] | None:
    if run_request is None:
        return None
    extra_metadata = dict(getattr(run_request, "extra_metadata", None) or {})
    return {
        "content": getattr(run_request, "content", ""),
        "display_content": getattr(run_request, "display_content", ""),
        "file_name": getattr(run_request, "file_name", ""),
        "append_user_message": bool(getattr(run_request, "append_user_message", True)),
        "attachments": getattr(run_request, "attachments", None) or [],
        "parts": getattr(run_request, "parts", None) or [],
        **extra_metadata,
    }


def _event_role(event: ChatTranscriptEvent | T0SessionEvent) -> str | None:
    metadata = _event_metadata(event)
    role = metadata.get("role")
    if isinstance(role, str) and role:
        return role
    event_type = getattr(event, "event_type", None)
    if event_type == "user_message":
        return "user"
    if event_type == "assistant_message":
        return "assistant"
    if event_type in {"tool_call", "tool_result"}:
        return "tool"
    return None


def _is_replayable_turn_event(event: ChatTranscriptEvent | T0SessionEvent) -> bool:
    event_type = getattr(event, "event_type", None)
    if event_type not in _REPLAYABLE_TURN_EVENT_TYPES:
        return False
    if event_type == "assistant_message" and not (getattr(event, "content", None) or "").strip():
        return False
    return True


def _last_replayable_turn_event(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> ChatTranscriptEvent | T0SessionEvent | None:
    for event in reversed(events):
        if _is_replayable_turn_event(event):
            return event
    return None


def _user_checkpoint_events(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> list[ChatTranscriptEvent | T0SessionEvent]:
    return [
        event for event in events if _event_role(event) == "user" and (getattr(event, "content", None) or "").strip()
    ]


def _checkpoint_payload(event: ChatTranscriptEvent | T0SessionEvent, *, turn_index: int) -> dict[str, Any]:
    payload = _event_payload(event)
    payload["checkpoint_event_id"] = _event_anchor_id(event)
    payload["turn_index"] = turn_index
    payload["checkpoint_type"] = "user_message"
    return payload


def _checkpoint_payloads(events: list[ChatTranscriptEvent | T0SessionEvent]) -> list[dict[str, Any]]:
    return [
        _checkpoint_payload(event, turn_index=index) for index, event in enumerate(_user_checkpoint_events(events), 1)
    ]


def _assistant_copy_candidates(
    events: list[ChatTranscriptEvent | T0SessionEvent],
) -> list[ChatTranscriptEvent | T0SessionEvent]:
    candidates: list[ChatTranscriptEvent | T0SessionEvent] = []
    for event in reversed(events):
        metadata = _event_metadata(event)
        if _event_role(event) != "assistant":
            continue
        if metadata.get("is_api_error_message") or metadata.get("api_error"):
            continue
        if not (getattr(event, "content", None) or "").strip():
            continue
        candidates.append(event)
    return candidates


def _extract_code_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index, match in enumerate(_FENCED_CODE_BLOCK_RE.finditer(markdown)):
        lang = re.sub(r"[^A-Za-z0-9_+.-]", "", match.group(1).strip())
        blocks.append({"index": index, "lang": lang or None, "code": match.group(2)})
    return blocks


def _positive_int(value: Any, *, default: int, field: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a positive integer") from exc
    if parsed < 1:
        raise HTTPException(status_code=400, detail=f"{field} must be a positive integer")
    return parsed


async def _load_db_events(db: AsyncSession, *, session: ChatSession, limit: int = 500) -> list[ChatTranscriptEvent]:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.listed_surface == "chat")
        .order_by(ChatTranscriptEvent.sequence.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _load_events(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    limit: int = 500,
) -> tuple[list[ChatTranscriptEvent | T0SessionEvent], str]:
    try:
        t0_events = replay_t0_session_events(agent_id=agent.id, session_id=session.id)
    except Exception:  # noqa: BLE001 - command surface must fall back to DB read model if files are unavailable.
        t0_events = []
    if t0_events:
        return list(t0_events[:limit]), "t0_events_jsonl"
    return await _load_db_events(db, session=session, limit=limit), "chat_transcript_events_read_model"


async def _last_event(db: AsyncSession, *, session: ChatSession) -> ChatTranscriptEvent | None:
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.session_id == session.id, ChatTranscriptEvent.listed_surface == "chat")
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_anchor_event_id(
    db: AsyncSession,
    *,
    agent: Agent,
    session: ChatSession,
    arguments: dict[str, Any],
) -> uuid.UUID:
    raw = arguments.get("anchor_event_id") or arguments.get("event_id")
    if raw:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    events, _truth_source = await _load_events(db, agent=agent, session=session, limit=1000)
    event = events[-1] if events else await _last_event(db, session=session)
    if event is None:
        raise HTTPException(status_code=400, detail="Cannot branch or rewind an empty transcript")
    return _branch_anchor_event_id(event)


def _select_user_checkpoint(
    events: list[ChatTranscriptEvent | T0SessionEvent],
    *,
    arguments: dict[str, Any],
    default_num_turns: int = 1,
) -> tuple[ChatTranscriptEvent | T0SessionEvent, int]:
    raw_checkpoint = (
        arguments.get("checkpoint_event_id") or arguments.get("anchor_event_id") or arguments.get("event_id")
    )
    checkpoints = _user_checkpoint_events(events)
    if raw_checkpoint:
        for index, event in enumerate(checkpoints, 1):
            if _event_anchor_id(event) == str(raw_checkpoint) or _event_ledger_id(event) == str(raw_checkpoint):
                return event, index
        raise HTTPException(status_code=404, detail="User-message checkpoint not found in this transcript")

    num_turns = _positive_int(arguments.get("num_turns"), default=default_num_turns, field="num_turns")
    if len(checkpoints) < num_turns:
        raise HTTPException(status_code=400, detail="Not enough user-message checkpoints to roll back that many turns")
    target = checkpoints[-num_turns]
    return target, len(checkpoints) - num_turns + 1


async def _export_session(db: AsyncSession, *, agent: Agent, session: ChatSession) -> dict[str, Any]:
    events, truth_source = await _load_events(db, agent=agent, session=session)
    db_events = (
        events if truth_source == "chat_transcript_events_read_model" else await _load_db_events(db, session=session)
    )
    messages_result = await db.execute(
        select(ChatMessage).where(ChatMessage.conversation_id == str(session.id)).order_by(ChatMessage.created_at.asc())
    )
    messages = list(messages_result.scalars().all())
    artifact_result = await db.execute(
        select(ChatArtifact).where(ChatArtifact.session_id == session.id).order_by(ChatArtifact.created_at.asc())
    )
    artifacts = list(artifact_result.scalars().all())
    return {
        "session": {
            "id": str(session.id),
            "agent_id": str(session.agent_id),
            "title": session.title,
            "source_channel": session.source_channel,
            "parent_session_id": str(session.parent_session_id) if session.parent_session_id else None,
            "root_session_id": str(session.root_session_id) if session.root_session_id else None,
            "metadata": session.transcript_metadata_json or {},
        },
        "truth_source": truth_source,
        "t0_events": [_event_payload(event) for event in events] if truth_source == "t0_events_jsonl" else [],
        "transcript_events": [_event_payload(event) for event in db_events],
        "messages": [
            {
                "id": str(message.id),
                "role": message.role,
                "content": message.content or "",
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in messages
        ],
        "artifacts": [
            {
                "id": str(artifact.id),
                "path": artifact.path,
                "name": artifact.name,
                "mime_type": artifact.mime_type,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
            }
            for artifact in artifacts
        ],
        "truth_surface": "t0_events_jsonl_plus_markdown_projection"
        if truth_source == "t0_events_jsonl"
        else "chat_transcript_events_read_model_with_t0_fallback",
    }


async def execute_session_command(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    access_level: str,
    command_name: str,
    session_id: uuid.UUID | str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if command_name not in SESSION_COMMAND_NAMES:
        raise HTTPException(status_code=501, detail=f"Unsupported session command {command_name!r}")
    session = await _load_session(db, agent=agent, user=user, session_id=session_id, access_level=access_level)
    metadata = dict(session.transcript_metadata_json or {})

    if command_name == "resume":
        events, truth_source = await _load_events(db, agent=agent, session=session)
        raw_last_event_type = events[-1].event_type if events else None
        last_turn_event = _last_replayable_turn_event(events)
        last_turn_event_type = last_turn_event.event_type if last_turn_event else None
        checkpoints = _user_checkpoint_events(events)
        interrupted = last_turn_event_type in _INTERRUPTED_TAIL_EVENT_TYPES
        resume_checkpoint = checkpoints[-1] if interrupted and checkpoints else None
        return {
            "ok": True,
            "command": "resume",
            "session_id": str(session.id),
            "truth_source": truth_source,
            "event_count": len(events),
            "checkpoint_count": len(checkpoints),
            "interrupted": interrupted,
            "repair_strategy": "transcript_replay_chain_repair",
            "raw_last_event_type": raw_last_event_type,
            "last_replayable_event": _event_payload(last_turn_event) if last_turn_event else None,
            "resume_from_checkpoint_event_id": _event_anchor_id(resume_checkpoint) if resume_checkpoint else None,
            "repair_actions": [
                "ignore_non_turn_tail_events",
                "ignore_empty_assistant_messages",
                "continue_if_tail_is_user_or_tool_turn",
            ],
            "next_query": "Continue from where you left off." if interrupted else None,
        }

    if command_name == "checkpoints":
        events, truth_source = await _load_events(
            db,
            agent=agent,
            session=session,
            limit=_positive_int(arguments.get("limit"), default=500, field="limit"),
        )
        checkpoints = _checkpoint_payloads(events)
        return {
            "ok": True,
            "command": "checkpoints",
            "session_id": str(session.id),
            "truth_source": truth_source,
            "event_count": len(events),
            "checkpoint_count": len(checkpoints),
            "checkpoints": checkpoints,
            "checkpoint_strategy": "user_message_turn_boundary",
        }

    if command_name == "copy":
        events, truth_source = await _load_events(
            db,
            agent=agent,
            session=session,
            limit=_positive_int(arguments.get("limit"), default=500, field="limit"),
        )
        candidates = _assistant_copy_candidates(events)
        if not candidates:
            raise HTTPException(status_code=404, detail="No assistant message to copy")
        copy_index = arguments["n"] if "n" in arguments else arguments.get("index")
        n = _positive_int(copy_index, default=1, field="n")
        if n > len(candidates):
            noun = "message" if len(candidates) == 1 else "messages"
            raise HTTPException(status_code=400, detail=f"Only {len(candidates)} assistant {noun} available to copy")
        event = candidates[n - 1]
        content = event.content or ""
        line_count = content.count("\n") + 1 if content else 0
        return {
            "ok": True,
            "command": "copy",
            "session_id": str(session.id),
            "truth_source": truth_source,
            "source_event_id": _event_anchor_id(event),
            "ledger_event_id": _event_ledger_id(event),
            "source_sequence": event.sequence,
            "message_age": n - 1,
            "available_assistant_messages": len(candidates),
            "content": content,
            "char_count": len(content),
            "line_count": line_count,
            "code_blocks": _extract_code_blocks(content),
            "copy_strategy": "client_clipboard_or_file",
        }

    if command_name == "rename":
        title = str(arguments.get("title") or arguments.get("name") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        session.title = title[:200]
        await db.flush()
        return {"ok": True, "command": "rename", "session_id": str(session.id), "title": session.title}

    if command_name == "tag":
        tags = arguments.get("tags")
        if isinstance(tags, str):
            tags = [tags]
        clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        metadata["tags"] = sorted(set([*(metadata.get("tags") or []), *clean_tags]))
        session.transcript_metadata_json = metadata
        await db.flush()
        return {"ok": True, "command": "tag", "session_id": str(session.id), "tags": metadata["tags"]}

    if command_name == "export":
        return {"ok": True, "command": "export", **await _export_session(db, agent=agent, session=session)}

    if command_name == "clear":
        new_session = ChatSession(
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", getattr(user, "tenant_id", None)),
            user_id=user.id,
            title=str(arguments.get("title") or f"{session.title} (clear)")[:200],
            source_channel=session.source_channel,
            session_kind=session.session_kind,
            actor_type=session.actor_type,
            runtime_source=session.runtime_source,
            visibility_scope=session.visibility_scope,
            listed_surface=session.listed_surface,
            parent_session_id=session.id,
            root_session_id=session.root_session_id or session.id,
            transcript_metadata_json={
                "command": "clear",
                "source_session_id": str(session.id),
                "keeps_evidence": True,
            },
        )
        db.add(new_session)
        await db.flush()
        return {
            "ok": True,
            "command": "clear",
            "source_session_id": str(session.id),
            "session": {"id": str(new_session.id), "title": new_session.title},
        }

    if command_name == "branch":
        anchor_event_id = await _resolve_anchor_event_id(db, agent=agent, session=session, arguments=arguments)
        branch_result = await create_conversation_branch(
            db=db,
            agent=agent,
            user=user,
            source_session=session,
            mode="fork",
            anchor_event_id=anchor_event_id,
            title=str(arguments.get("title") or f"{session.title} ({command_name})"),
        )
        branch_metadata = dict(branch_result.branch)
        branch_metadata["command"] = command_name
        return {
            "ok": True,
            "command": command_name,
            "source_session_id": str(session.id),
            "session": {
                "id": str(branch_result.session.id),
                "title": branch_result.session.title,
                "parent_session_id": str(branch_result.session.parent_session_id)
                if branch_result.session.parent_session_id
                else None,
                "root_session_id": str(branch_result.session.root_session_id)
                if branch_result.session.root_session_id
                else None,
            },
            "branch": branch_metadata,
        }

    if command_name == "btw":
        question = str(
            arguments.get("question") or arguments.get("content") or arguments.get("message") or arguments.get("prompt") or ""
        ).strip()
        if not question:
            raise HTTPException(status_code=400, detail="question or content is required")
        anchor_event_id = await _resolve_anchor_event_id(db, agent=agent, session=session, arguments=arguments)
        branch_result = await create_conversation_branch(
            db=db,
            agent=agent,
            user=user,
            source_session=session,
            mode="side_question",
            anchor_event_id=anchor_event_id,
            content=question,
            display_content=str(arguments.get("display_content") or f"btw: {question}"),
            title=str(arguments.get("title") or f"{session.title} (btw)"),
        )
        branch_metadata = dict(branch_result.branch)
        branch_metadata["command"] = "btw"
        return {
            "ok": True,
            "command": "btw",
            "source_session_id": str(session.id),
            "session": _session_payload(branch_result.session),
            "branch": branch_metadata,
            "run_request": _run_request_payload(branch_result.run_request),
        }

    if command_name in {"turn_steer", "steer"}:
        content = str(arguments.get("content") or arguments.get("message") or arguments.get("input") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        result = await steer_active_web_chat_turn(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=content,
            display_content=str(arguments.get("display_content") or ""),
            file_name=str(arguments.get("file_name") or ""),
            expected_turn_id=str(arguments.get("expected_turn_id") or "") or None,
            attachments=arguments.get("attachments") if isinstance(arguments.get("attachments"), list) else None,
            parts=arguments.get("parts") if isinstance(arguments.get("parts"), list) else None,
        )
        return {
            "ok": True,
            "command": command_name,
            "session_id": str(session.id),
            **result,
        }

    if command_name == "interrupt":
        active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
        run_id = arguments.get("run_id") or (active_run or {}).get("run_id")
        if not run_id:
            raise HTTPException(status_code=404, detail="No active turn to interrupt")
        result = await cancel_web_chat_run(
            db=db,
            agent_id=agent.id,
            session_id=session.id,
            run_id=_parse_uuid_argument(run_id, field="run_id"),
            user_id=user.id,
        )
        return {
            "ok": True,
            "command": "interrupt",
            "session_id": str(session.id),
            "interrupt_strategy": "cancel_active_web_chat_run",
            **result,
        }

    if command_name in {"rewind", "rollback"}:
        events, truth_source = await _load_events(
            db,
            agent=agent,
            session=session,
            limit=_positive_int(arguments.get("limit"), default=1000, field="limit"),
        )
        target_checkpoint, turn_index = _select_user_checkpoint(events, arguments=arguments)
        branch_result = await create_conversation_branch(
            db=db,
            agent=agent,
            user=user,
            source_session=session,
            mode="rewind",
            anchor_event_id=_branch_anchor_event_id(target_checkpoint),
            title=str(arguments.get("title") or f"{session.title} ({command_name})"),
        )
        branch_metadata = dict(branch_result.branch)
        branch_metadata["command"] = command_name
        checkpoint = _checkpoint_payload(target_checkpoint, turn_index=turn_index)
        return {
            "ok": True,
            "command": command_name,
            "source_session_id": str(session.id),
            "truth_source": truth_source,
            "session": {
                "id": str(branch_result.session.id),
                "title": branch_result.session.title,
                "parent_session_id": str(branch_result.session.parent_session_id)
                if branch_result.session.parent_session_id
                else None,
                "root_session_id": str(branch_result.session.root_session_id)
                if branch_result.session.root_session_id
                else None,
            },
            "checkpoint": checkpoint,
            "rollback": {
                "strategy": "non_destructive_branch_before_user_checkpoint",
                "num_turns": _positive_int(arguments.get("num_turns"), default=1, field="num_turns")
                if command_name == "rollback"
                else 1,
            },
            "branch": branch_metadata,
        }

    if command_name == "compact":
        reason = str(arguments.get("reason") or "manual compact command").strip()
        await emit_hook(
            HookEvent.PRE_COMPACTION,
            agent_id=agent.id,
            session_id=str(session.id),
            source="command",
            metadata={"tenant_id": str(getattr(agent, "tenant_id", "") or ""), "reason": reason},
        )
        event = await append_session_event(
            db=db,
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", getattr(session, "tenant_id", None)),
            session_id=session.id,
            actor_type="system",
            event_type="session_compact_command",
            content=reason,
            role="system",
            user_id=user.id,
            root_session_id=session.root_session_id or session.id,
            parent_session_id=session.parent_session_id,
            metadata={"command": "compact", "manual": True, "reason": reason},
            source="command",
        )
        await emit_hook(
            HookEvent.POST_COMPACTION,
            agent_id=agent.id,
            session_id=str(session.id),
            source="command",
            metadata={
                "tenant_id": str(getattr(agent, "tenant_id", "") or ""),
                "reason": reason,
                "transcript_event_id": str(event.event_id),
            },
        )
        return {
            "ok": True,
            "command": "compact",
            "session_id": str(session.id),
            "transcript_event_id": str(event.event_id),
            "hook_events": [HookEvent.PRE_COMPACTION.value, HookEvent.POST_COMPACTION.value],
        }

    raise HTTPException(status_code=501, detail=f"Unsupported session command {command_name!r}")
