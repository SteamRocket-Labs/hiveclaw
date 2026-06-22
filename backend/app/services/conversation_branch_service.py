from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.user import User
from app.services.chat_transcript import append_session_event

ConversationBranchMode = Literal["fork", "edit", "insert_before", "insert_after", "reply", "regenerate", "rewind"]

_CONTENT_REQUIRED_MODES = {"edit", "insert_before", "insert_after", "reply"}
_VALID_MODES = {*_CONTENT_REQUIRED_MODES, "fork", "regenerate", "rewind"}


@dataclass(frozen=True, slots=True)
class BranchRunRequest:
    content: str
    display_content: str
    file_name: str = ""
    append_user_message: bool = True
    attachments: list[dict[str, Any]] | None = None
    parts: list[dict[str, Any]] | None = None
    extra_metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConversationBranchResult:
    session: ChatSession
    branch: dict[str, Any]
    run_request: BranchRunRequest | None


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _event_role(event: ChatTranscriptEvent) -> str | None:
    metadata = getattr(event, "metadata_json", None) or {}
    role = metadata.get("role")
    if isinstance(role, str) and role:
        return role
    event_type = getattr(event, "event_type", None)
    if event_type == "user_message":
        return "user"
    if event_type == "assistant_message":
        return "assistant"
    if event_type in {"tool_call", "tool_result"}:
        return "tool_call"
    return None


def _event_t0_role(event: ChatTranscriptEvent, role: str | None) -> str | None:
    metadata = getattr(event, "metadata_json", None) or {}
    t0_role = metadata.get("t0_role")
    return t0_role if isinstance(t0_role, str) and t0_role else role


def _prefix_includes_anchor(mode: str) -> bool:
    return mode in {"fork", "insert_after", "reply"}


def _branch_title(source_session: ChatSession, mode: str, title: str | None) -> str:
    if title and title.strip():
        return title.strip()[:200]
    source_title = (getattr(source_session, "title", None) or "Conversation").strip()
    suffix = {
        "fork": "fork",
        "edit": "edit",
        "insert_before": "insert",
        "insert_after": "insert",
        "reply": "reply",
        "regenerate": "regenerate",
        "rewind": "rewind",
    }.get(mode, "branch")
    return f"{source_title} ({suffix})"[:200]


async def _load_anchor_event(
    *,
    db: AsyncSession,
    source_session: ChatSession,
    agent_id: uuid.UUID,
    anchor_event_id: uuid.UUID,
) -> ChatTranscriptEvent:
    result = await db.execute(
        select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.id == anchor_event_id,
            ChatTranscriptEvent.session_id == source_session.id,
            ChatTranscriptEvent.agent_id == agent_id,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Anchor transcript event not found")
    return event


async def _load_prefix_events(
    *,
    db: AsyncSession,
    source_session_id: uuid.UUID,
    anchor: ChatTranscriptEvent,
    include_anchor: bool,
) -> list[ChatTranscriptEvent]:
    predicate = (
        ChatTranscriptEvent.sequence <= anchor.sequence
        if include_anchor
        else ChatTranscriptEvent.sequence < anchor.sequence
    )
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.session_id == source_session_id,
            predicate,
            ChatTranscriptEvent.listed_surface == "chat",
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    return list(result.scalars().all())


async def _copy_prefix_events(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    branch_session: ChatSession,
    source_session: ChatSession,
    mode: str,
    events: list[ChatTranscriptEvent],
) -> list[str]:
    copied_event_ids: list[str] = []
    previous_new_event_id: uuid.UUID | None = None
    for event in events:
        role = _event_role(event)
        result = await append_session_event(
            db=db,
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", getattr(source_session, "tenant_id", None)),
            session_id=branch_session.id,
            actor_type=getattr(event, "actor_type", None) or "system",
            event_type=getattr(event, "event_type", None) or "event",
            content=getattr(event, "content", None) or "",
            role=role,
            t0_role=_event_t0_role(event, role),
            user_id=getattr(user, "id", None),
            run_id=None,
            parent_event_id=previous_new_event_id,
            root_session_id=branch_session.root_session_id,
            parent_session_id=source_session.id,
            parts=getattr(event, "parts_json", None) or None,
            metadata={
                **(getattr(event, "metadata_json", None) or {}),
                "source": "conversation_branch",
                "branch_mode": mode,
                "projection_only": True,
                "projection_source": "conversation_branch_prefix",
                "semantic_memory_eligible": False,
                "source_session_id": str(source_session.id),
                "copied_from_event_id": str(event.id),
                "copied_from_sequence": getattr(event, "sequence", None),
            },
            visibility_scope=getattr(event, "visibility_scope", None) or "direct_user",
            listed_surface=getattr(event, "listed_surface", None) or "chat",
            source="conversation_branch",
            bridge_to_t0=False,
        )
        previous_new_event_id = result.event_id
        copied_event_ids.append(str(event.id))
    return copied_event_ids


def _last_user_event(events: list[ChatTranscriptEvent]) -> ChatTranscriptEvent | None:
    for event in reversed(events):
        if _event_role(event) == "user":
            return event
    return None


def _validate_branch_request(*, mode: str, anchor: ChatTranscriptEvent, content: str) -> None:
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported branch mode: {mode}")
    if mode in _CONTENT_REQUIRED_MODES and not content.strip():
        raise HTTPException(status_code=400, detail=f"content is required for {mode}")
    role = _event_role(anchor)
    if mode == "edit" and role != "user":
        raise HTTPException(status_code=400, detail="edit requires a user message anchor")
    if mode == "regenerate" and role != "assistant":
        raise HTTPException(status_code=400, detail="regenerate requires an assistant message anchor")
    if mode == "rewind" and role != "user":
        raise HTTPException(status_code=400, detail="rewind requires a user message checkpoint")


async def create_conversation_branch(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    source_session: ChatSession,
    mode: ConversationBranchMode | str,
    anchor_event_id: uuid.UUID | str,
    content: str = "",
    display_content: str = "",
    file_name: str = "",
    title: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> ConversationBranchResult:
    mode_text = str(mode)
    anchor_uuid = _uuid(anchor_event_id)
    anchor = await _load_anchor_event(
        db=db,
        source_session=source_session,
        agent_id=agent.id,
        anchor_event_id=anchor_uuid,
    )
    _validate_branch_request(mode=mode_text, anchor=anchor, content=content)

    prefix_events = await _load_prefix_events(
        db=db,
        source_session_id=source_session.id,
        anchor=anchor,
        include_anchor=_prefix_includes_anchor(mode_text),
    )
    root_session_id = getattr(source_session, "root_session_id", None) or source_session.id
    now = datetime.now(timezone.utc)
    branch_session = ChatSession(
        id=uuid.uuid4(),
        agent_id=agent.id,
        tenant_id=getattr(agent, "tenant_id", getattr(source_session, "tenant_id", None)),
        user_id=getattr(user, "id", getattr(source_session, "user_id", None)),
        title=_branch_title(source_session, mode_text, title),
        source_channel=getattr(source_session, "source_channel", None) or "web",
        participant_id=getattr(source_session, "participant_id", None),
        peer_agent_id=getattr(source_session, "peer_agent_id", None),
        delivery_target_json=getattr(source_session, "delivery_target_json", None),
        session_kind=getattr(source_session, "session_kind", None) or "human_chat",
        actor_type=getattr(source_session, "actor_type", None) or "user",
        runtime_source=getattr(source_session, "runtime_source", None) or "web_chat",
        visibility_scope=getattr(source_session, "visibility_scope", None) or "direct_user",
        listed_surface=getattr(source_session, "listed_surface", None) or "chat",
        parent_session_id=source_session.id,
        root_session_id=root_session_id,
        transcript_metadata_json={
            "branch_mode": mode_text,
            "source_session_id": str(source_session.id),
            "root_session_id": str(root_session_id),
            "anchor_event_id": str(anchor.id),
            "anchor_sequence": anchor.sequence,
            "created_by_user_id": str(getattr(user, "id", "")),
        },
        created_at=now,
        last_message_at=now if mode_text != "fork" else getattr(source_session, "last_message_at", None),
    )
    db.add(branch_session)
    if hasattr(db, "flush"):
        await db.flush()

    copied_event_ids = await _copy_prefix_events(
        db=db,
        agent=agent,
        user=user,
        branch_session=branch_session,
        source_session=source_session,
        mode=mode_text,
        events=prefix_events,
    )

    run_request: BranchRunRequest | None = None
    if mode_text in _CONTENT_REQUIRED_MODES:
        visible_content = display_content if display_content else content
        run_request = BranchRunRequest(
            content=content,
            display_content=visible_content,
            file_name=file_name,
            append_user_message=True,
            attachments=attachments or [],
            parts=parts or [],
            extra_metadata={
                "branch_mode": mode_text,
                "source_session_id": str(source_session.id),
                "branch_session_id": str(branch_session.id),
                "anchor_event_id": str(anchor.id),
            },
        )
    elif mode_text == "regenerate":
        last_user = _last_user_event(prefix_events)
        if last_user is None or not (last_user.content or "").strip():
            raise HTTPException(status_code=400, detail="regenerate requires a prior user message")
        prompt = last_user.content or ""
        run_request = BranchRunRequest(
            content=prompt,
            display_content=prompt,
            append_user_message=False,
            extra_metadata={
                "branch_mode": mode_text,
                "source_session_id": str(source_session.id),
                "branch_session_id": str(branch_session.id),
                "anchor_event_id": str(anchor.id),
                "regenerate_from_event_id": str(anchor.id),
                "regenerate_prompt_source_event_id": str(last_user.id),
                "regenerate_prompt": prompt,
                "semantic_source_refs": [
                    {
                        "session_id": str(source_session.id),
                        "event_id": str(last_user.id),
                        "role": "user",
                        "kind": "regenerate_prompt",
                    }
                ],
            },
        )

    return ConversationBranchResult(
        session=branch_session,
        branch={
            "mode": mode_text,
            "source_session_id": str(source_session.id),
            "root_session_id": str(root_session_id),
            "session_id": str(branch_session.id),
            "anchor_event_id": str(anchor.id),
            "copied_event_ids": copied_event_ids,
        },
        run_request=run_request,
    )
