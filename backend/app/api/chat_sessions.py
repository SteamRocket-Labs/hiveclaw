"""Chat session management API endpoints."""

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone as tz
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.ccplus_contracts import (
    DEFAULT_CCPLUS_PERMISSION_MODE,
    DEFAULT_CCPLUS_WRITABLE_ROOTS,
    PendingToolFrameV1,
    PermissionCheckpointV1,
    build_permission_profile,
    normalize_permission_mode,
)
from app.runtime.hooks import HookEvent, emit_hook
from app.services.chat_artifact_delivery import artifact_part_from_model
from app.services.chat_message_parts import build_session_native_event, serialize_chat_message, split_inline_tools
from app.services.chat_transcript import append_session_event
from app.services.web_chat_runtime import (
    ActiveWebChatRunExists,
    _persist_tool_call,
    broadcast_web_chat_event,
    cancel_web_chat_run,
    get_active_web_chat_run,
    start_channel_chat_run_from_saved_turn,
    start_web_chat_run,
    steer_active_web_chat_turn,
)
from app.services.web_chat_broker import web_chat_broker
from app.services.conversation_branch_service import create_conversation_branch
from app.services.session_index import read_session_index
from app.services.session_feedback import record_session_feedback
from app.services.session_control_plane import build_session_json_export, build_session_workbench

router = APIRouter(prefix="/agents", tags=["chat-sessions"])
logger = logging.getLogger(__name__)

_LEGACY_HIDDEN_CHAT_SOURCES = ("trigger", "task", "heartbeat")
_MINE_HIDDEN_CHAT_SOURCES = _LEGACY_HIDDEN_CHAT_SOURCES
_SESSION_PERMISSION_RESOLUTION_EVENT_TYPES = {
    "permission_resolved",
    "session_permission_decision",
    "session_permission_expired",
}


def _is_admin_or_creator(user: User, agent: Agent) -> bool:
    return user.role in ("platform_admin", "org_admin") or str(agent.creator_id) == str(user.id)


def _can_manage_sessions(user: User, agent: Agent, access_level: str) -> bool:
    return _is_admin_or_creator(user, agent) or access_level == "manage"


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    user_id: str
    username: Optional[str] = None  # display_name ?? username
    source_channel: str = "web"  # web / feishu / discord / slack / agent
    session_kind: str = "human_chat"
    actor_type: str = "user"
    runtime_source: str = "web_chat"
    visibility_scope: str = "direct_user"
    listed_surface: str = "chat"
    parent_session_id: Optional[str] = None
    root_session_id: Optional[str] = None
    runtime_task_id: Optional[str] = None
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value
    permission_profile: dict[str, Any] = Field(default_factory=dict)
    writable_roots: list[str] = Field(default_factory=list)
    is_current_user_session: bool = False
    read_only: bool = False
    # Agent-to-agent session fields
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type: str = "user"  # 'user' | 'agent'


def _session_view_flags(session: ChatSession, current_user: User) -> dict[str, bool]:
    is_current_user_session = str(getattr(session, "user_id", "")) == str(getattr(current_user, "id", ""))
    source_channel = str(getattr(session, "source_channel", "") or "").lower()
    participant_type = str(getattr(session, "participant_type", "") or "").lower()
    session_kind = str(getattr(session, "session_kind", "") or "").lower()
    is_agent_session = (
        source_channel == "agent" or participant_type == "agent" or session_kind in {"agent_chat", "delegation_run"}
    )
    return {
        "is_current_user_session": is_current_user_session,
        "read_only": (not is_current_user_session) or is_agent_session,
    }


def _session_contract_fields(session: ChatSession) -> dict[str, Any]:
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    return {
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "actor_type": getattr(session, "actor_type", None) or "user",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "visibility_scope": getattr(session, "visibility_scope", None) or "direct_user",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
        "parent_session_id": str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
        "root_session_id": str(session.root_session_id) if getattr(session, "root_session_id", None) else None,
        "runtime_task_id": str(session.runtime_task_id) if getattr(session, "runtime_task_id", None) else None,
        **_session_permission_metadata(
            str(session_metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value), session
        ),
    }


def _session_permission_metadata(permission_mode: str | None, session: ChatSession | None = None) -> dict[str, Any]:
    mode = normalize_permission_mode(permission_mode or DEFAULT_CCPLUS_PERMISSION_MODE.value).value
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {}) if session is not None else {}
    allowed_tools = [
        str(item) for item in (session_metadata.get("session_permission_allowed_tools") or []) if str(item).strip()
    ]
    writable_roots = list(DEFAULT_CCPLUS_WRITABLE_ROOTS)
    return {
        "permission_mode": mode,
        "writable_roots": writable_roots,
        "permission_profile": {"mode": mode, "allowed_tools": allowed_tools, "writable_roots": writable_roots},
    }


def _session_out(
    session: ChatSession,
    current_user: User,
    *,
    message_count: int = 0,
    username: str | None = None,
    peer_agent_id: str | None = None,
    peer_agent_name: str | None = None,
    participant_type: str = "user",
) -> SessionOut:
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        username=username,
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=message_count,
        **_session_view_flags(session, current_user),
        peer_agent_id=peer_agent_id,
        peer_agent_name=peer_agent_name,
        participant_type=participant_type,
        **_session_contract_fields(session),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _permission_request_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    request_payload = payload.get("permission_request")
    if isinstance(request_payload, dict):
        return request_payload

    permission_payload = payload.get("permission")
    if isinstance(permission_payload, dict):
        nested_request = permission_payload.get("permission_request")
        if isinstance(nested_request, dict):
            return nested_request
        if permission_payload.get("permission_request_id"):
            return permission_payload

    part_payload = payload.get("part")
    if isinstance(part_payload, dict):
        nested_request = _permission_request_from_payload(part_payload)
        if nested_request is not None:
            return nested_request

    parts_payload = payload.get("parts")
    if isinstance(parts_payload, list):
        for part in parts_payload:
            if isinstance(part, dict):
                nested_request = _permission_request_from_payload(part)
                if nested_request is not None:
                    return nested_request

    result_payload = _json_object(payload.get("result"))
    if result_payload:
        nested_request = _permission_request_from_payload(result_payload)
        if nested_request is not None:
            return nested_request

    if payload.get("permission_request_id") and (
        payload.get("tool_name") or isinstance(payload.get("arguments"), dict)
    ):
        return {
            key: value
            for key, value in payload.items()
            if key
            in {
                "permission_request_id",
                "session_id",
                "runtime_task_id",
                "turn_id",
                "tool_call_id",
                "tool_name",
                "tool_display_name",
                "arguments",
                "capability",
                "permission_mode",
                "decision_reason",
                "risk_class",
                "confirmation_kind",
                "allow_session_allowed",
                "destructive",
                "pending_tool_frame",
                "created_at",
                "expires_at",
            }
        }
    return None


def _permission_request_payload_from_event(event: ChatTranscriptEvent) -> tuple[dict[str, Any], dict[str, Any]] | None:
    metadata = dict(getattr(event, "metadata_json", None) or {})
    tool_payload = _json_object(getattr(event, "content", None))
    request_payload = _permission_request_from_payload(metadata) or _permission_request_from_payload(tool_payload)
    if not isinstance(request_payload, dict):
        return None
    if not tool_payload and metadata:
        tool_payload = metadata
    return request_payload, tool_payload if isinstance(tool_payload, dict) else {}


def _event_payloads(event: ChatTranscriptEvent) -> tuple[dict[str, Any], dict[str, Any]]:
    return dict(getattr(event, "metadata_json", None) or {}), _json_object(getattr(event, "content", None))


def _event_type_from_payload(event: ChatTranscriptEvent, metadata: dict[str, Any], content: dict[str, Any]) -> str:
    return str(
        getattr(event, "event_type", None)
        or metadata.get("event_type")
        or metadata.get("runtime_event_type")
        or content.get("event_type")
        or content.get("type")
        or ""
    )


def _permission_resolution_payload_from_event(event: ChatTranscriptEvent) -> dict[str, Any] | None:
    metadata, content = _event_payloads(event)
    event_type = _event_type_from_payload(event, metadata, content)
    if event_type not in _SESSION_PERMISSION_RESOLUTION_EVENT_TYPES:
        return None
    request_payload = _permission_request_from_payload(metadata) or _permission_request_from_payload(content) or {}
    permission_request_id = (
        metadata.get("permission_request_id")
        or content.get("permission_request_id")
        or request_payload.get("permission_request_id")
    )
    if not permission_request_id:
        return None
    merged = {**content, **metadata}
    merged["event_type"] = event_type
    merged["permission_request_id"] = str(permission_request_id)
    if request_payload:
        merged["permission_request"] = request_payload
    return merged


def _parse_permission_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz.utc)
    return parsed.astimezone(tz.utc)


def _pending_tool_frame_is_expired(pending_frame: PendingToolFrameV1, *, now: datetime | None = None) -> bool:
    expires_at = _parse_permission_datetime(pending_frame.expires_at)
    if expires_at is None:
        return False
    current = now or datetime.now(tz.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz.utc)
    return expires_at <= current.astimezone(tz.utc)


def _session_permission_expired_payload(
    *,
    permission_request_id: str,
    request_payload: dict[str, Any],
    pending_frame: PendingToolFrameV1,
    source_event_id: str | None,
) -> dict[str, Any]:
    return {
        "type": "permission_resolved",
        "event_type": "session_permission_expired",
        "permission_request_id": permission_request_id,
        "permission_request": request_payload,
        "permission_checkpoint": _permission_checkpoint_payload(
            permission_request_id=permission_request_id,
            decision="expired",
            pending_frame=pending_frame,
            resolver_user_id=None,
            resolution_channel="system",
        ),
        "decision": "expired",
        "status": "expired",
        "tool_name": pending_frame.tool_name or request_payload.get("tool_name"),
        "tool_call_id": pending_frame.tool_call_id,
        "source_event_id": source_event_id,
        "reason": "Permission request expired before resolution",
        "message": "Permission request expired before resolution.",
    }


async def _append_session_permission_expired_event(
    *,
    db: AsyncSession,
    event: ChatTranscriptEvent,
    request_payload: dict[str, Any],
    pending_frame: PendingToolFrameV1,
) -> dict[str, Any]:
    permission_request_id = str(request_payload.get("permission_request_id") or pending_frame.permission_request_id)
    payload = _session_permission_expired_payload(
        permission_request_id=permission_request_id,
        request_payload=request_payload,
        pending_frame=pending_frame,
        source_event_id=str(getattr(event, "id", "")) if getattr(event, "id", None) else None,
    )
    await append_session_event(
        db=db,
        agent_id=getattr(event, "agent_id", None),
        tenant_id=getattr(event, "tenant_id", None),
        session_id=getattr(event, "session_id", None),
        run_id=getattr(event, "run_id", None),
        actor_type="system",
        event_type="session_permission_expired",
        role="system",
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        source="system",
        metadata=payload,
        materialize_chat_message=False,
    )
    return payload


async def expire_stale_session_permission_requests(
    *,
    db: AsyncSession,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Best-effort startup/maintenance scanner for stale CCPlus permission frames."""
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.event_type.in_(
                [
                    "permission",
                    "tool_result",
                    "permission_resolved",
                    "session_permission_decision",
                    "session_permission_expired",
                ]
            )
        )
        .order_by(ChatTranscriptEvent.created_at.desc())
        .limit(limit)
    )
    resolved_request_ids: set[str] = set()
    expired_count = 0
    for event in result.scalars().all():
        resolution_payload = _permission_resolution_payload_from_event(event)
        if resolution_payload is not None:
            resolved_request_ids.add(str(resolution_payload["permission_request_id"]))
            continue
        parsed = _permission_request_payload_from_event(event)
        if parsed is None:
            continue
        request_payload, tool_payload = parsed
        permission_request_id = str(request_payload.get("permission_request_id") or "")
        if not permission_request_id or permission_request_id in resolved_request_ids:
            continue
        pending_frame = _pending_tool_frame_from_payload(
            request_payload,
            tool_payload,
            session_id=str(getattr(event, "session_id", "") or ""),
        )
        if not _pending_tool_frame_is_expired(pending_frame, now=now):
            continue
        await _append_session_permission_expired_event(
            db=db,
            event=event,
            request_payload=request_payload,
            pending_frame=pending_frame,
        )
        resolved_request_ids.add(permission_request_id)
        expired_count += 1
    if expired_count:
        await db.commit()
    return expired_count


def _pending_tool_frame_from_payload(
    request_payload: dict[str, Any],
    tool_payload: dict[str, Any],
    *,
    session_id: str,
) -> PendingToolFrameV1:
    pending_payload = request_payload.get("pending_tool_frame")
    if not isinstance(pending_payload, dict):
        pending_payload = {}
    arguments = pending_payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = request_payload.get("arguments") if isinstance(request_payload.get("arguments"), dict) else {}
    profile_payload = pending_payload.get("permission_profile")
    permission_request_id = str(
        pending_payload.get("permission_request_id") or request_payload.get("permission_request_id") or ""
    )
    return PendingToolFrameV1(
        permission_request_id=permission_request_id,
        session_id=str(pending_payload.get("session_id") or request_payload.get("session_id") or session_id),
        turn_id=str(pending_payload.get("turn_id")) if pending_payload.get("turn_id") else None,
        runtime_task_id=str(
            pending_payload.get("runtime_task_id")
            or request_payload.get("runtime_task_id")
            or tool_payload.get("runtime_task_id")
            or ""
        )
        or None,
        tool_call_id=str(
            pending_payload.get("tool_call_id")
            or request_payload.get("tool_call_id")
            or tool_payload.get("tool_call_id")
            or ""
        ),
        tool_name=str(pending_payload.get("tool_name") or request_payload.get("tool_name") or ""),
        arguments=dict(arguments),
        origin_channel=str(pending_payload.get("origin_channel")) if pending_payload.get("origin_channel") else None,
        permission_profile=build_permission_profile(profile_payload if isinstance(profile_payload, dict) else None),
        round_state=dict(pending_payload.get("round_state") or {})
        if isinstance(pending_payload.get("round_state"), dict)
        else {},
        knowledge_refs=_string_tuple(pending_payload.get("knowledge_refs")),
        hook_refs=_string_tuple(pending_payload.get("hook_refs")),
        t0_refs=_string_tuple(pending_payload.get("t0_refs")),
        created_at=str(pending_payload.get("created_at") or request_payload.get("created_at"))
        if pending_payload.get("created_at") or request_payload.get("created_at")
        else None,
        expires_at=str(pending_payload.get("expires_at") or request_payload.get("expires_at"))
        if pending_payload.get("expires_at") or request_payload.get("expires_at")
        else None,
        status=str(pending_payload.get("status") or "pending"),
    )


def _permission_checkpoint_payload(
    *,
    permission_request_id: str,
    decision: str,
    pending_frame: PendingToolFrameV1,
    resolver_user_id: str | None,
    resolution_channel: str,
    continuation_runtime_task_id: str | None = None,
    denial_tool_result_ref: str | None = None,
) -> dict[str, Any]:
    checkpoint = PermissionCheckpointV1(
        permission_request_id=permission_request_id,
        decision=decision,
        pending_frame=pending_frame,
        resolver_user_id=resolver_user_id,
        resolved_at=datetime.now(tz.utc).isoformat(),
        resolution_channel=resolution_channel,
        continuation_runtime_task_id=continuation_runtime_task_id,
        denial_tool_result_ref=denial_tool_result_ref,
    )
    return _json_ready(asdict(checkpoint))


def _session_permission_exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("detail")
            return str(message) if message else json.dumps(detail, ensure_ascii=False, sort_keys=True)
        return str(detail)
    return str(exc) or exc.__class__.__name__


def _session_permission_origin_channel(session: ChatSession, pending_frame: PendingToolFrameV1) -> str:
    return str(pending_frame.origin_channel or getattr(session, "source_channel", None) or "web").strip() or "web"


async def _start_session_permission_continuation_run(
    *,
    db: Any,
    agent: Any,
    user: User,
    session: ChatSession,
    content: str,
    extra_metadata: dict[str, Any],
    pending_frame: PendingToolFrameV1,
) -> dict[str, Any]:
    active_run = await get_active_web_chat_run(db=db, agent_id=agent.id, session_id=session.id)
    if active_run:
        return active_run

    source_channel = _session_permission_origin_channel(session, pending_frame)
    metadata = {
        **extra_metadata,
        "origin_channel": pending_frame.origin_channel or source_channel,
        "channel": source_channel,
        "delivery_target_json": getattr(session, "delivery_target_json", None),
    }
    if source_channel != "web":
        return await start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=content,
            source_channel=source_channel,
            display_content="",
            extra_metadata=metadata,
        )
    return await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=content,
        display_content="",
        append_user_message=False,
        extra_metadata=metadata,
    )


async def _broadcast_session_permission_event(
    *,
    db: Any,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    session: ChatSession,
    payload: dict[str, Any],
    channel_text: str | None = None,
) -> None:
    await broadcast_web_chat_event(agent_id, session_id, payload)
    source_channel = str(getattr(session, "source_channel", None) or "web").strip().lower() or "web"
    target = getattr(session, "delivery_target_json", None)
    delivery_text = channel_text
    if (
        str(payload.get("event_type") or payload.get("type") or "") == "tool_result"
        and payload.get("result") is not None
    ):
        delivery_text = _session_permission_tool_result_channel_text(
            str(payload.get("name") or payload.get("tool_name") or "unknown_tool"),
            payload.get("result"),
        )
    if source_channel == "web" or not isinstance(target, dict) or not delivery_text:
        return
    try:
        from app.services.channel_delivery_service import ChannelDeliveryService

        await ChannelDeliveryService.send_text(
            db=db,
            agent_id=agent_id,
            reply_target=target,
            text=delivery_text,
            delivery_mode="live",
            extra_detail={
                "source": "session_permission_event",
                "event_type": str(payload.get("event_type") or payload.get("type") or ""),
                "session_id": str(session_id),
            },
        )
    except Exception as exc:
        logger.warning(
            "Session permission channel event delivery failed: agent_id=%s session_id=%s error=%s",
            agent_id,
            session_id,
            exc,
        )


def _session_permission_tool_result_channel_text(tool_name: str, tool_result: Any, *, limit: int = 1800) -> str:
    """Render the IM live copy for an approved permission tool result.

    Web already receives the full structured event payload. Channel users need
    the same result signal, but bounded so a large file/search result does not
    flood the IM thread.
    """
    name = str(tool_name or "unknown_tool").strip() or "unknown_tool"
    text = str(tool_result or "").strip()
    if not text:
        return f"Tool `{name}` completed after permission approval."
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n\n[Result truncated for live channel delivery.]"
    return f"Tool `{name}` completed after permission approval.\n\n{text}"


def _permission_request_allows_session_scope(request_payload: dict[str, Any]) -> bool:
    if request_payload.get("allow_session_allowed") is False:
        return False
    if request_payload.get("risk_class") == "destructive_delete":
        return False
    if request_payload.get("confirmation_kind") == "destructive_once":
        return False
    return True


class CreateSessionIn(BaseModel):
    title: Optional[str] = None


class PatchSessionIn(BaseModel):
    title: str


class UpdateSessionPermissionProfileIn(BaseModel):
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value


class StartSessionRunIn(BaseModel):
    content: str
    display_content: str = ""
    file_name: str = ""
    plan_mode_requested: bool = False
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []


class CreateSessionRunIn(StartSessionRunIn):
    title: Optional[str] = None


class BranchSessionIn(BaseModel):
    mode: Literal[
        "fork",
        "branch",
        "edit",
        "insert_before",
        "insert_after",
        "reply",
        "regenerate",
        "rewind",
        "side_question",
    ]
    anchor_event_id: uuid.UUID
    content: str = ""
    display_content: str = ""
    file_name: str = ""
    title: Optional[str] = None
    start_run: bool = True
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []


class SteerSessionTurnIn(BaseModel):
    content: str
    display_content: str = ""
    file_name: str = ""
    expected_turn_id: Optional[str] = None
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []


class ResolveSessionPermissionIn(BaseModel):
    action: Literal["allow_once", "allow_session", "deny"]
    feedback: str = ""


class SessionRunOut(BaseModel):
    run_id: str
    status: str
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Optional[str] = None


class BranchSessionOut(BaseModel):
    session: SessionOut
    branch: dict[str, Any]
    run: Optional[dict[str, Any]] = None


class CreateSessionRunOut(BaseModel):
    session: SessionOut
    run: dict[str, Any]


class RecordSessionFeedbackIn(BaseModel):
    label: Literal["useful", "misleading"]
    reason: str = ""
    message_id: Optional[uuid.UUID] = None
    decision_id: Optional[str] = None


def _transcript_role_for_event(event: ChatTranscriptEvent) -> str:
    metadata = event.metadata_json or {}
    role = metadata.get("role")
    if isinstance(role, str) and role:
        return role
    if event.event_type == "user_message":
        return "user"
    if event.event_type == "assistant_message":
        return "assistant"
    if event.event_type in {"tool_result", "tool_call"}:
        return "tool_call"
    return "event"


def _serialize_transcript_event(event: ChatTranscriptEvent) -> dict:
    return {
        "id": str(event.id),
        "sequence": event.sequence,
        "session_id": str(event.session_id),
        "run_id": str(event.run_id) if event.run_id else None,
        "message_id": str(event.message_id) if event.message_id else None,
        "actor_type": event.actor_type,
        "event_type": event.event_type,
        "type": event.event_type,
        "role": _transcript_role_for_event(event),
        "visibility_scope": event.visibility_scope,
        "listed_surface": event.listed_surface,
        "content": event.content or "",
        "parts": event.parts_json or [],
        "metadata": event.metadata_json or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


async def _get_run_session_and_agent(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User,
) -> tuple[ChatSession, Agent, str]:
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized to use this session")
    return session, agent, access_level


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for an agent. 'all' requires admin or creator role."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)

    if scope == "all":
        if not _can_manage_sessions(current_user, agent, access_level):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        # Fetch all sessions (including agent-to-agent where this agent is peer)
        result = await db.execute(
            select(ChatSession)
            .where(
                (ChatSession.agent_id == agent_id)
                | ((ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent")),
                ChatSession.listed_surface == "chat",
                ChatSession.source_channel.notin_(_LEGACY_HIDDEN_CHAT_SOURCES),
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        out = []
        for session in sessions:
            count_result = await db.execute(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.conversation_id == str(session.id),
                )
            )
            count = count_result.scalar() or 0
            if count == 0:
                continue  # hide empty sessions

            # Determine display name based on session type
            display = None
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"

            if session.source_channel == "agent" and session.peer_agent_id:
                # Agent-to-agent session
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                # Get both agent names
                a1_r = await db.execute(select(Agent.name).where(Agent.id == session.agent_id))
                a2_r = await db.execute(select(Agent.name).where(Agent.id == session.peer_agent_id))
                a1_name = a1_r.scalar_one_or_none() or "Agent"
                a2_name = a2_r.scalar_one_or_none() or "Agent"
                peer_agent_name = a2_name
                display = f"🤖 {a1_name} ↔ {a2_name}"
            else:
                # Human session — resolve username
                user_r = await db.execute(
                    select(func.coalesce(User.display_name, User.username)).where(User.id == session.user_id)
                )
                display = user_r.scalar_one_or_none() or "Unknown"

            out.append(
                SessionOut(
                    id=str(session.id),
                    agent_id=str(session.agent_id),
                    user_id=str(session.user_id),
                    username=display,
                    source_channel=session.source_channel,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                    message_count=count,
                    **_session_view_flags(session, current_user),
                    peer_agent_id=peer_agent_id,
                    peer_agent_name=peer_agent_name,
                    participant_type=participant_type,
                    **_session_contract_fields(session),
                )
            )
        return out

    else:  # scope == "mine"
        # For agent creator/admin: also show inbound channel sessions
        # (Telegram, Feishu, Slack, etc.) so they can monitor all conversations
        _channel_types = (
            "feishu",
            "telegram",
            "slack",
            "discord",
            "dingtalk",
            "wecom",
            "microsoft_teams",
            "wechat_personal",
        )
        if _is_admin_or_creator(current_user, agent):
            ownership_filter = or_(
                ChatSession.user_id == current_user.id,
                ChatSession.source_channel.in_(_channel_types),
            )
        else:
            ownership_filter = ChatSession.user_id == current_user.id
        direct_session_filter = (ChatSession.agent_id == agent_id) & ownership_filter
        a2a_peer_session_filter = (
            (ChatSession.peer_agent_id == agent_id)
            & (ChatSession.source_channel == "agent")
            & (ChatSession.user_id == current_user.id)
        )

        result = await db.execute(
            select(ChatSession)
            .where(
                direct_session_filter | a2a_peer_session_filter,
                ChatSession.listed_surface == "chat",
                ChatSession.source_channel.notin_(_MINE_HIDDEN_CHAT_SOURCES),
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        out = []
        for session in sessions:
            # Count only — skip sessions with no user messages (orphan assistant-only records)
            if session.source_channel == "agent":
                count_result = await db.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.role == "user",
                    )
                )
            else:
                count_result = await db.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.agent_id == agent_id,
                        ChatMessage.role == "user",
                    )
                )
            user_msg_count = count_result.scalar() or 0
            is_owned_direct_web_session = (
                str(session.user_id) == str(current_user.id)
                and session.source_channel == "web"
                and (getattr(session, "session_kind", None) in (None, "human_chat"))
            )
            if user_msg_count == 0 and not is_owned_direct_web_session:
                continue  # hide empty channel/A2A/orphan sessions, but keep newly created user web sessions writable.
            # Total message count for display
            if session.source_channel == "agent":
                total_result = await db.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.conversation_id == str(session.id),
                    )
                )
            else:
                total_result = await db.execute(
                    select(func.count(ChatMessage.id)).where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.agent_id == agent_id,
                    )
                )
            count = total_result.scalar() or 0
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"
            username = None
            if session.source_channel == "agent" and session.peer_agent_id:
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                peer_agent_name = session.title
                username = session.title
            out.append(
                SessionOut(
                    id=str(session.id),
                    agent_id=str(session.agent_id),
                    user_id=str(session.user_id),
                    username=username,
                    source_channel=session.source_channel,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                    message_count=count,
                    **_session_view_flags(session, current_user),
                    peer_agent_id=peer_agent_id,
                    peer_agent_name=peer_agent_name,
                    participant_type=participant_type,
                    **_session_contract_fields(session),
                )
            )
        return out


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID,
    body: CreateSessionIn = CreateSessionIn(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session for the current user."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)

    now = datetime.now(tz.utc)
    new_id = uuid.uuid4()
    session = ChatSession(
        id=new_id,
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", getattr(current_user, "tenant_id", None)),
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        created_at=now,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _session_out(session, current_user, message_count=0)


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename a session. Only owner, admin, or creator can rename."""
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized")

    session.title = body.title
    await db.commit()
    return {"id": str(session.id), "title": session.title}


@router.patch("/{agent_id}/sessions/{session_id}/permissions/profile")
async def update_session_permission_profile(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: UpdateSessionPermissionProfileIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current CCPlus session permission mode immediately."""
    session, _agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    permission_metadata = _session_permission_metadata(body.permission_mode, session)
    session_metadata = dict(session.transcript_metadata_json or {})
    session_metadata.update(permission_metadata)
    session.transcript_metadata_json = session_metadata

    active_result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(("pending", "running")),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(1)
    )
    active_run = active_result.scalar_one_or_none()
    if active_run is not None:
        active_metadata = dict(getattr(active_run, "metadata_json", None) or {})
        active_metadata.update(permission_metadata)
        active_run.metadata_json = active_metadata

    await db.commit()

    runtime_session_context = await web_chat_broker.get_or_create_runtime_session(str(agent_id), str(session_id))
    runtime_session_context.metadata.update(permission_metadata)
    await broadcast_web_chat_event(
        agent_id,
        session_id,
        {
            "type": "permission_profile_updated",
            "event_type": "permission_profile_updated",
            **permission_metadata,
        },
    )
    return permission_metadata


@router.post("/{agent_id}/sessions/{session_id}/feedback")
async def record_feedback_for_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: RecordSessionFeedbackIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record owner/user Useful or Misleading feedback for one session."""
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    result = await record_session_feedback(
        db=db,
        agent=agent,
        session=session,
        current_user=current_user,
        label=body.label,
        reason=body.reason,
        message_id=body.message_id,
        decision_id=body.decision_id,
    )
    await db.commit()
    return result


@router.post("/{agent_id}/sessions/runs", status_code=201, response_model=CreateSessionRunOut)
async def create_session_run(
    agent_id: uuid.UUID,
    body: CreateSessionRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new human web session and start its first durable run atomically."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)

    now = datetime.now(tz.utc)
    session = ChatSession(
        id=uuid.uuid4(),
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", getattr(current_user, "tenant_id", None)),
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        created_at=now,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    db.add(session)
    await db.flush()
    try:
        run = await start_web_chat_run(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content=body.content,
            display_content=body.display_content,
            file_name=body.file_name,
            plan_mode_requested=body.plan_mode_requested,
            extra_metadata=_session_permission_metadata(body.permission_mode, session),
            attachments=body.attachments,
            parts=body.parts,
        )
    except ActiveWebChatRunExists as exc:
        return JSONResponse(status_code=202, content={"session": _session_out(session, current_user).model_dump(), "run": exc.run})

    await db.refresh(session)
    return CreateSessionRunOut(session=_session_out(session, current_user, message_count=1), run=run)


@router.post("/{agent_id}/sessions/{session_id}/runs", status_code=201)
async def start_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: StartSessionRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a durable in-process web chat run for a session."""
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    try:
        return await start_web_chat_run(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content=body.content,
            display_content=body.display_content,
            file_name=body.file_name,
            plan_mode_requested=body.plan_mode_requested,
            extra_metadata=_session_permission_metadata(body.permission_mode, session),
            attachments=body.attachments,
            parts=body.parts,
        )
    except ActiveWebChatRunExists as exc:
        return JSONResponse(status_code=202, content={"status": "queued", **exc.run})


@router.post("/{agent_id}/sessions/{session_id}/branches", status_code=201, response_model=BranchSessionOut)
async def branch_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: BranchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a non-destructive conversation branch from a transcript event."""
    source_session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    branch_result = await create_conversation_branch(
        db=db,
        agent=agent,
        user=current_user,
        source_session=source_session,
        mode=body.mode,
        anchor_event_id=body.anchor_event_id,
        content=body.content,
        display_content=body.display_content,
        file_name=body.file_name,
        title=body.title,
        attachments=body.attachments,
        parts=body.parts,
    )
    run_payload: dict[str, Any] | None = None
    if body.start_run and branch_result.run_request is not None:
        request = branch_result.run_request
        try:
            run_payload = await start_web_chat_run(
                db=db,
                agent=agent,
                user=current_user,
                session=branch_result.session,
                content=request.content,
                display_content=request.display_content,
                file_name=request.file_name,
                attachments=getattr(request, "attachments", None) or [],
                parts=getattr(request, "parts", None) or [],
                append_user_message=request.append_user_message,
                extra_metadata={
                    **(getattr(request, "extra_metadata", None) or {}),
                    **_session_permission_metadata(body.permission_mode, branch_result.session),
                },
            )
        except ActiveWebChatRunExists as exc:
            run_payload = {"status": "queued", **exc.run}
    else:
        await db.commit()

    session = branch_result.session
    session_out = SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=0,
        **_session_view_flags(session, current_user),
        **_session_contract_fields(session),
    )
    return BranchSessionOut(session=session_out, branch=branch_result.branch, run=run_payload)


@router.get("/{agent_id}/sessions/{session_id}/branches")
async def list_session_branches(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List direct branches created from a session."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.parent_session_id == session_id,
            ChatSession.listed_surface == "chat",
        )
        .order_by(ChatSession.created_at.desc())
    )
    sessions = result.scalars().all()
    return [
        SessionOut(
            id=str(session.id),
            agent_id=str(session.agent_id),
            user_id=str(session.user_id),
            source_channel=session.source_channel,
            title=session.title,
            created_at=session.created_at.isoformat(),
            last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
            message_count=0,
            **_session_view_flags(session, current_user),
            **_session_contract_fields(session),
        )
        for session in sessions
    ]


@router.get("/{agent_id}/sessions/{session_id}/lineage")
async def get_session_lineage(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the branch family for the selected session."""
    session, _agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    root_id = session.root_session_id or session.id
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.listed_surface == "chat",
            (ChatSession.id == root_id) | (ChatSession.root_session_id == root_id),
        )
        .order_by(ChatSession.created_at.asc())
    )
    sessions = result.scalars().all()
    return [
        {
            "id": str(item.id),
            "parent_session_id": str(item.parent_session_id) if item.parent_session_id else None,
            "root_session_id": str(item.root_session_id) if item.root_session_id else None,
            "title": item.title,
            "branch": item.transcript_metadata_json or {},
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in sessions
    ]


@router.get("/{agent_id}/sessions/{session_id}/index")
async def get_session_index(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await check_agent_access(db, current_user, agent_id)
    index = await read_session_index(db, agent_id=agent_id, session_id=session_id)
    if index is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return index


@router.get("/{agent_id}/sessions/{session_id}/workbench")
async def get_session_workbench(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await build_session_workbench(db, agent=agent, session=session)


@router.get("/{agent_id}/sessions/{session_id}/export")
async def export_session_json(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await build_session_json_export(db, agent=agent, session=session)


@router.get("/{agent_id}/sessions/{session_id}/runs/active")
async def get_active_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the active durable web chat run for a session, if one exists."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await get_active_web_chat_run(db=db, agent_id=agent_id, session_id=session_id)


@router.post("/{agent_id}/sessions/{session_id}/turns/steer")
async def steer_session_turn(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: SteerSessionTurnIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue an additional user message into the currently active turn."""
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await steer_active_web_chat_turn(
        db=db,
        agent=agent,
        user=current_user,
        session=session,
        content=body.content,
        display_content=body.display_content,
        file_name=body.file_name,
        expected_turn_id=body.expected_turn_id,
        attachments=body.attachments,
        parts=body.parts,
        extra_metadata=_session_permission_metadata(body.permission_mode, session),
    )


@router.post("/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve")
async def resolve_session_permission(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    permission_request_id: uuid.UUID,
    body: ResolveSessionPermissionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a CCPlus session-local permission request inside the same chat session."""
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.agent_id == agent_id,
            ChatTranscriptEvent.session_id == session_id,
        )
        .order_by(ChatTranscriptEvent.created_at.desc())
        .limit(300)
    )
    pending_event: ChatTranscriptEvent | None = None
    request_payload: dict[str, Any] | None = None
    tool_payload: dict[str, Any] | None = None
    for event in result.scalars().all():
        resolved_payload = _permission_resolution_payload_from_event(event)
        if resolved_payload is not None and str(resolved_payload.get("permission_request_id")) == str(
            permission_request_id
        ):
            raise HTTPException(status_code=409, detail="Session permission request already resolved")
        parsed = _permission_request_payload_from_event(event)
        if parsed is None:
            continue
        candidate_request, candidate_tool = parsed
        if str(candidate_request.get("permission_request_id")) == str(permission_request_id):
            pending_event = event
            request_payload = candidate_request
            tool_payload = candidate_tool
            break
    if pending_event is None or request_payload is None or tool_payload is None:
        raise HTTPException(status_code=404, detail="Pending session permission request not found")

    pending_frame = _pending_tool_frame_from_payload(request_payload, tool_payload, session_id=str(session_id))
    if _pending_tool_frame_is_expired(pending_frame):
        expired_payload = _session_permission_expired_payload(
            permission_request_id=str(permission_request_id),
            request_payload=request_payload,
            pending_frame=pending_frame,
            source_event_id=str(pending_event.id),
        )
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=session_id,
            run_id=pending_event.run_id,
            actor_type="system",
            event_type="session_permission_expired",
            role="system",
            user_id=current_user.id,
            content=json.dumps(expired_payload, ensure_ascii=False, sort_keys=True),
            source="web",
            metadata=expired_payload,
            materialize_chat_message=False,
        )
        await db.commit()
        await _broadcast_session_permission_event(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            session=session,
            payload=expired_payload,
            channel_text="The pending permission request expired. Please send the request again if you still want to proceed.",
        )
        raise HTTPException(status_code=410, detail="Session permission request expired")
    tool_call_id = pending_frame.tool_call_id or str(tool_payload.get("tool_call_id") or "")
    permission_checkpoint = _permission_checkpoint_payload(
        permission_request_id=str(permission_request_id),
        decision=body.action,
        pending_frame=pending_frame,
        resolver_user_id=str(current_user.id),
        resolution_channel=pending_frame.origin_channel or getattr(session, "source_channel", None) or "web",
    )
    resolution_metadata = {
        "permission_request_id": str(permission_request_id),
        "permission_request": request_payload,
        "permission_checkpoint": permission_checkpoint,
        "decision": body.action,
        "feedback": body.feedback,
        "tool_name": request_payload.get("tool_name"),
        "tool_call_id": tool_call_id,
        "source_event_id": str(pending_event.id),
    }
    tool_name = str(request_payload.get("tool_name") or "")
    arguments = request_payload.get("arguments") if isinstance(request_payload.get("arguments"), dict) else {}

    if body.action == "allow_session" and not _permission_request_allows_session_scope(request_payload):
        raise HTTPException(status_code=400, detail="Destructive permissions can only be allowed once")

    if body.action == "allow_session":
        session_metadata = dict(session.transcript_metadata_json or {})
        allowed_tools = [str(item) for item in (session_metadata.get("session_permission_allowed_tools") or [])]
        if tool_name and tool_name not in allowed_tools:
            allowed_tools.append(tool_name)
        session_metadata["session_permission_allowed_tools"] = allowed_tools
        session.transcript_metadata_json = session_metadata
        await db.flush()

    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", None),
        session_id=session_id,
        run_id=pending_event.run_id,
        actor_type="user",
        event_type="session_permission_decision",
        user_id=current_user.id,
        content=json.dumps(resolution_metadata, ensure_ascii=False, sort_keys=True),
        source="web",
        metadata=resolution_metadata,
        materialize_chat_message=False,
    )
    await db.commit()

    if body.action == "deny":
        await emit_hook(
            HookEvent.PERMISSION_DENIED,
            agent_id=agent_id,
            session_id=str(session_id),
            tool_name=tool_name,
            tool_args=arguments,
            source="session_permission_resolve",
            metadata=resolution_metadata,
        )
        try:
            run_payload = await _start_session_permission_continuation_run(
                db=db,
                agent=agent,
                user=current_user,
                session=session,
                content=(
                    f"The user denied the session permission request for tool {tool_name or 'unknown_tool'}. "
                    "Do not retry the denied tool in this continuation. Explain the denial and offer a safe "
                    "alternative path that stays within the current permissions."
                ),
                pending_frame=pending_frame,
                extra_metadata={
                    **_session_permission_metadata(
                        str(request_payload.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value), session
                    ),
                    "source": "session_permission_denied_resume",
                    "latest_user_prompt_overrides_history": True,
                    "resumed_from_permission_request_id": str(permission_request_id),
                    "denied_tool_name": tool_name,
                    "denied_tool_call_id": tool_call_id,
                    "resumed_turn_id": pending_frame.turn_id,
                    "resumed_runtime_task_id": pending_frame.runtime_task_id,
                    "round_state": dict(pending_frame.round_state or {}),
                    "t0_refs": list(pending_frame.t0_refs or ()),
                },
            )
        except ActiveWebChatRunExists as exc:
            run_payload = {"status": "queued", **exc.run}
        await _broadcast_session_permission_event(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            session=session,
            payload={
                "type": "permission_resolved",
                "event_type": "permission_resolved",
                **resolution_metadata,
                "status": "denied",
                "run": run_payload,
            },
            channel_text=f"Permission request for tool `{tool_name or 'unknown_tool'}` was denied.",
        )
        return {"status": "denied", "permission_request_id": str(permission_request_id), "run": run_payload}

    if not tool_name:
        raise HTTPException(status_code=400, detail="Permission request is missing tool_name")

    from app.services.agent_tools import execute_session_permission_tool

    try:
        tool_result = await execute_session_permission_tool(
            tool_name,
            arguments,
            agent_id=agent_id,
            user_id=current_user.id,
            session_id=str(session_id),
            permission_profile={
                "mode": "bypassPermissions",
                "allowed_tools": [tool_name],
                "writable_roots": list(DEFAULT_CCPLUS_WRITABLE_ROOTS),
            },
            tool_call_id=tool_call_id or None,
            turn_id=pending_frame.turn_id,
            runtime_task_id=pending_frame.runtime_task_id,
            origin_channel=pending_frame.origin_channel,
            round_state=dict(pending_frame.round_state or {}),
            t0_refs=tuple(pending_frame.t0_refs or ()),
        )
        persisted_tool_event = await _persist_tool_call(
            agent_id=agent_id,
            user_id=current_user.id,
            session_id=str(session_id),
            data={
                "name": tool_name,
                "args": arguments,
                "status": "done",
                "result": str(tool_result),
                "tool_call_id": tool_call_id,
                "run_id": str(pending_event.run_id) if pending_event.run_id else None,
                "runtime_task_id": str(pending_event.run_id) if pending_event.run_id else None,
                "visibility": "expanded",
            },
        )
        await _broadcast_session_permission_event(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            session=session,
            payload={
                "type": "tool_call",
                "event_type": "tool_result",
                "role": "tool_call",
                "name": tool_name,
                "args": arguments,
                "status": "done",
                "result": str(tool_result),
                "tool_call_id": tool_call_id,
                "transcript_event_id": str(persisted_tool_event.event_id) if persisted_tool_event else None,
                "sequence": persisted_tool_event.sequence if persisted_tool_event else None,
                "metadata": persisted_tool_event.transcript_event.metadata_json if persisted_tool_event else {},
            },
            channel_text=_session_permission_tool_result_channel_text(tool_name, tool_result),
        )

        try:
            run_payload = await _start_session_permission_continuation_run(
                db=db,
                agent=agent,
                user=current_user,
                session=session,
                content="Continue after the approved session permission tool result.",
                pending_frame=pending_frame,
                extra_metadata={
                    **_session_permission_metadata(
                        str(request_payload.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value), session
                    ),
                    "source": "session_permission_resume",
                    "resumed_from_permission_request_id": str(permission_request_id),
                    "resumed_turn_id": pending_frame.turn_id,
                    "resumed_runtime_task_id": pending_frame.runtime_task_id,
                    "round_state": dict(pending_frame.round_state or {}),
                    "t0_refs": list(pending_frame.t0_refs or ()),
                },
            )
        except ActiveWebChatRunExists as exc:
            run_payload = {"status": "queued", **exc.run}
    except Exception as exc:
        error_message = _session_permission_exception_message(exc)
        error_type = exc.__class__.__name__
        failure_payload = {
            "type": "permission_resolved",
            "event_type": "permission_resolved",
            **resolution_metadata,
            "status": "failed",
            "error": error_message,
            "error_type": error_type,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "capability": request_payload.get("capability"),
            "reason": error_message,
            "message": f"Permission request could not be completed: {error_message}",
            "retryable": True,
        }
        logger.exception(
            "Session permission resolve failed: agent_id=%s session_id=%s permission_request_id=%s tool=%s",
            agent_id,
            session_id,
            permission_request_id,
            tool_name,
        )
        event_payload = build_session_native_event(failure_payload)
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=session_id,
            run_id=pending_event.run_id,
            actor_type="system",
            event_type="permission_resolved",
            role="system",
            user_id=current_user.id,
            content=json.dumps(failure_payload, ensure_ascii=False, sort_keys=True),
            source="web",
            parts=[event_payload["part"]] if isinstance(event_payload.get("part"), dict) else None,
            metadata=failure_payload,
        )
        await db.commit()
        await _broadcast_session_permission_event(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            session=session,
            payload=failure_payload,
            channel_text=failure_payload["message"],
        )
        return {
            "status": "failed",
            "permission_request_id": str(permission_request_id),
            "error": error_message,
            "error_type": error_type,
        }

    await _broadcast_session_permission_event(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        session=session,
        payload={
            "type": "permission_resolved",
            "event_type": "permission_resolved",
            **resolution_metadata,
            "status": "allowed",
            "run": run_payload,
        },
    )
    return {"status": "allowed", "permission_request_id": str(permission_request_id), "run": run_payload}


@router.get("/{agent_id}/threads/{session_id}/read")
async def read_thread(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Thread-style alias for reading a durable session JSON export."""
    session, agent, _access_level = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await build_session_json_export(db, agent=agent, session=session)


@router.get("/{agent_id}/sessions/{session_id}/transcript")
async def get_session_transcript(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    after_sequence: int = 0,
    limit: int = 500,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get replayable transcript events for a session.

    This is the durable UI replay surface. `chat_messages` remains a read model
    for compatibility, while transcript events are the ordered event stream.
    """
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    events_result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.session_id == session_id,
            ChatTranscriptEvent.sequence > after_sequence,
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
        .limit(limit)
    )
    return [_serialize_transcript_event(event) for event in events_result.scalars().all()]


@router.post("/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")
async def cancel_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly stop an active durable web chat run."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await cancel_web_chat_run(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        user_id=current_user.id,
    )


@router.post("/{agent_id}/threads/{session_id}/turns/{run_id}/interrupt")
async def interrupt_thread_turn(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Thread-style alias for interrupting an active durable turn."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    return await cancel_web_chat_run(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        user_id=current_user.id,
    )


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a chat session and its messages. Owner, admin, or creator only."""
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete associated messages first
    from sqlalchemy import delete as sql_delete

    await db.execute(sql_delete(ChatArtifact).where(ChatArtifact.session_id == session_id))
    await db.execute(sql_delete(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id))
    await db.execute(sql_delete(ChatMessage).where(ChatMessage.conversation_id == str(session_id)))
    await db.delete(session)
    await db.commit()
    return None


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get chat messages for a specific session."""
    # Allow looking up sessions where agent_id OR peer_agent_id matches
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if str(session.user_id) != str(current_user.id) and not _can_manage_sessions(current_user, agent, access_level):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    # Query messages by conversation_id only (agent-to-agent uses session_agent_id)
    msgs_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session_id))
        .order_by(ChatMessage.created_at.asc())
        .limit(500)
    )
    messages = msgs_result.scalars().all()
    message_ids = [m.id for m in messages]
    artifacts_by_message: dict[uuid.UUID, list[dict]] = {}
    if message_ids:
        artifacts_result = await db.execute(
            select(ChatArtifact).where(ChatArtifact.message_id.in_(message_ids)).order_by(ChatArtifact.created_at.asc())
        )
        for artifact in artifacts_result.scalars().all():
            artifacts_by_message.setdefault(artifact.message_id, []).append(artifact_part_from_model(artifact))

    # Resolve sender names for agent sessions
    sender_cache: dict = {}
    if session.source_channel == "agent":
        from app.models.participant import Participant

        for m in messages:
            if m.participant_id and str(m.participant_id) not in sender_cache:
                p_r = await db.execute(select(Participant.display_name).where(Participant.id == m.participant_id))
                sender_cache[str(m.participant_id)] = p_r.scalar_one_or_none() or "Unknown"

    out = []
    for m in messages:
        sender_name = sender_cache.get(str(m.participant_id)) if m.participant_id else None
        artifacts = artifacts_by_message.get(m.id, [])

        if m.role == "tool_call":
            out.append(serialize_chat_message(m, sender_name=sender_name, artifacts=artifacts))
            continue

        # For agent sessions, parse inline tool_code blocks from assistant messages
        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = split_inline_tools(m.content, sender_name=sender_name)
            for part in parts:
                out.append(part)
        else:
            out.append(serialize_chat_message(m, sender_name=sender_name, artifacts=artifacts))

    return out
