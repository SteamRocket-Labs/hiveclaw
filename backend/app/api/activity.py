"""Activity log API — view agent work history."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_message_contracts import (
    extract_sender_label_from_message,
    strip_sender_label_prefix,
)
from app.core.security import get_current_user
from app.core.permissions import check_agent_access
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.user import User
from app.session_identifiers import parse_feishu_p2p_conv_id
from app.services.tool_telemetry import collect_agent_tool_failure_summary

router = APIRouter(tags=["activity"])
_SESSION_BACKED_CHANNELS = (
    "web",
    "feishu",
    "slack",
    "discord",
    "telegram",
    "wecom",
    "dingtalk",
    "wechat_personal",
    "microsoft_teams",
)
_DIRECT_LABEL_CHANNELS: dict[str, tuple[str, str]] = {
    "telegram": ("✈️", "Telegram"),
    "wecom": ("🏢", "企业微信"),
    "dingtalk": ("🔔", "钉钉"),
    "wechat_personal": ("🟢", "微信"),
    "microsoft_teams": ("🪟", "Teams"),
}


def _feishu_conversation_partner_name(conv_id: str, first_user_message: str | None = None) -> str:
    identifier = parse_feishu_p2p_conv_id(conv_id)
    if not identifier:
        return "👥 飞书群聊"

    sender_label = extract_sender_label_from_message(first_user_message)
    return f"📱 {sender_label}" if sender_label else "📱 飞书用户"


def _coerce_limit(limit: Any, *, default: int) -> int:
    try:
        return int(limit)
    except (TypeError, ValueError):
        return default


def _parse_conv_uuid(conv_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(conv_id))
    except (TypeError, ValueError):
        return None


def _channel_partner_type(session: ChatSession | Any) -> str:
    return "user" if getattr(session, "source_channel", None) == "web" else str(getattr(session, "source_channel", ""))


def _channel_partner_id(session: ChatSession | Any) -> str:
    if getattr(session, "source_channel", None) == "web" and getattr(session, "user_id", None):
        return str(session.user_id)
    return str(getattr(session, "external_conv_id", None) or getattr(session, "id", ""))


def _delivery_target_label(delivery_target: dict[str, Any] | None) -> str:
    target = delivery_target or {}
    for key in ("user_label", "username", "member_name", "display_name", "name", "user_id", "to_user_id", "open_id", "receive_id", "sender_id", "chat_id"):
        value = str(target.get(key) or "").strip()
        if value:
            return value
    return ""


async def _channel_user_display_name(db: AsyncSession, session: ChatSession | Any) -> str:
    user_id = getattr(session, "user_id", None)
    if not user_id:
        return ""
    user_r = await db.execute(select(User.display_name).where(User.id == user_id))
    return str(user_r.scalar_one_or_none() or "").strip()


async def _channel_partner_name(
    db: AsyncSession,
    session: ChatSession | Any,
    *,
    first_user_message: str | None = None,
) -> str:
    source_channel = str(getattr(session, "source_channel", "") or "")
    delivery_target = getattr(session, "delivery_target_json", None) or {}
    external_conv_id = str(getattr(session, "external_conv_id", "") or "")

    if source_channel == "web":
        user_label = str(delivery_target.get("user_label") or delivery_target.get("username") or "").strip()
        return f"👤 {user_label}" if user_label else "👤 未知用户"

    if source_channel == "feishu":
        return _feishu_conversation_partner_name(external_conv_id, first_user_message)

    if source_channel in {"slack", "discord"}:
        icon = "💬" if source_channel == "slack" else "🎮"
        label = "Slack" if source_channel == "slack" else "Discord"
        conv_ref = external_conv_id or str(getattr(session, "id", ""))
        parts = conv_ref.split("_", 2)
        channel_part = parts[1] if len(parts) > 1 else conv_ref
        return f"{icon} {label} DM" if channel_part == "dm" else f"{icon} {label} #{channel_part}"

    if source_channel in _DIRECT_LABEL_CHANNELS:
        icon, label = _DIRECT_LABEL_CHANNELS[source_channel]
        target_label = _delivery_target_label(delivery_target) or await _channel_user_display_name(db, session)
        return f"{icon} {label} {target_label}".strip() if target_label else f"{icon} {label}"

    title = str(getattr(session, "title", "") or "").strip()
    return title or "未知会话"


async def _get_session_message_stats(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> tuple[int, Any]:
    stats_q = await db.execute(
        select(func.count(ChatMessage.id), func.max(ChatMessage.created_at))
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
    )
    stats = stats_q.fetchone()
    if not stats:
        return 0, None
    return int(stats[0] or 0), stats[1]


async def _get_last_session_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> str:
    last_msg_r = await db.execute(
        select(ChatMessage.content)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(1)
    )
    return strip_sender_label_prefix(last_msg_r.scalar_one_or_none() or "")


async def _get_first_user_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> str:
    first_msg_r = await db.execute(
        select(ChatMessage.content)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "user",
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(1)
    )
    return first_msg_r.scalar_one_or_none() or ""


async def _list_session_backed_conversations(db: AsyncSession, *, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    session_rows = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.source_channel.in_(_SESSION_BACKED_CHANNELS),
        )
        .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
    )

    for session in session_rows.scalars().all():
        conversation_id = str(session.id)
        count, last_at = await _get_session_message_stats(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        last_message = await _get_last_session_message(
            db,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        first_user_message = ""
        if getattr(session, "source_channel", None) == "feishu":
            first_user_message = await _get_first_user_message(
                db,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )

        conversations.append(
            {
                "conv_id": conversation_id,
                "partner_type": _channel_partner_type(session),
                "partner_id": _channel_partner_id(session),
                "partner_name": await _channel_partner_name(db, session, first_user_message=first_user_message),
                "last_message": last_message[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    return conversations


async def _list_agent_conversations(db: AsyncSession, *, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    agent_sessions_q = await db.execute(
        select(ChatSession).where(
            ChatSession.source_channel == "agent",
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )

    for sess in agent_sessions_q.scalars().all():
        partner_id = sess.peer_agent_id if sess.agent_id == agent_id else sess.agent_id
        agent_r = await db.execute(select(Agent.name).where(Agent.id == partner_id))
        partner_name = agent_r.scalar_one_or_none() or "未知数字员工"
        canonical_agent_id = sess.agent_id
        count, last_at = await _get_session_message_stats(
            db,
            agent_id=canonical_agent_id,
            conversation_id=str(sess.id),
        )
        last_message = await _get_last_session_message(
            db,
            agent_id=canonical_agent_id,
            conversation_id=str(sess.id),
        )
        conversations.append(
            {
                "conv_id": str(sess.id),
                "partner_type": "agent",
                "partner_id": str(partner_id),
                "partner_name": f"🤖 {partner_name}",
                "last_message": last_message[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    return conversations


async def _load_channel_session_messages(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
    )
    return [
        {
            "id": str(message.id),
            "role": message.role,
            "content": strip_sender_label_prefix(message.content),
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
        for message in result.scalars().all()
    ]


async def _load_agent_session_messages(
    db: AsyncSession,
    *,
    conversation_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
    )
    messages: list[dict[str, Any]] = []
    name_cache: dict[str, str] = {}

    for message in result.scalars().all():
        sender_name = "未知"
        if getattr(message, "participant_id", None):
            participant_id = str(message.participant_id)
            if participant_id not in name_cache:
                participant_result = await db.execute(
                    select(Participant.display_name).where(Participant.id == message.participant_id)
                )
                name_cache[participant_id] = participant_result.scalar_one_or_none() or "未知"
            sender_name = name_cache[participant_id]
        messages.append(
            {
                "id": str(message.id),
                "role": message.role,
                "sender_name": sender_name,
                "content": message.content,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
        )

    return messages


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: uuid.UUID,
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recent activity logs for an agent."""
    await check_agent_access(db, current_user, agent_id)

    result = await db.execute(
        select(AgentActivityLog)
        .where(AgentActivityLog.agent_id == agent_id)
        .order_by(AgentActivityLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    return [
        {
            "id": str(log.id),
            "action_type": log.action_type,
            "summary": log.summary,
            "detail": log.detail_json,
            "related_id": str(log.related_id) if log.related_id else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


@router.get("/agents/{agent_id}/activity/tool-failures")
async def get_agent_tool_failure_summary(
    agent_id: uuid.UUID,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(500, ge=10, le=2000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregated tool failure telemetry for an agent."""
    await check_agent_access(db, current_user, agent_id)
    return await collect_agent_tool_failure_summary(
        db,
        agent_id=agent_id,
        hours=hours,
        limit=limit,
    )


# ─── Chat History (per-agent) ─────────────────────────────────

@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all conversation partners for this agent (web users + other agents)."""
    await check_agent_access(db, current_user, agent_id)
    conversations = await _list_session_backed_conversations(db, agent_id=agent_id)
    conversations.extend(await _list_agent_conversations(db, agent_id=agent_id))

    # Sort by last_at desc
    conversations.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return conversations


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a specific conversation."""
    await check_agent_access(db, current_user, agent_id)
    message_limit = _coerce_limit(limit, default=100)
    session_uuid = _parse_conv_uuid(conv_id)

    if session_uuid is not None:
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_uuid,
                (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
            )
        )
        session = session_result.scalar_one_or_none()
        if session is not None:
            if session.source_channel == "agent":
                return await _load_agent_session_messages(
                    db,
                    conversation_id=str(session.id),
                    limit=message_limit,
                )
            return await _load_channel_session_messages(
                db,
                agent_id=agent_id,
                conversation_id=str(session.id),
                limit=message_limit,
            )

    if conv_id.startswith(("web_", "feishu_", "slack_", "discord_")):
        return await _load_channel_session_messages(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            limit=message_limit,
        )

    if conv_id.startswith("agent_") or session_uuid is not None:
        return await _load_agent_session_messages(
            db,
            conversation_id=conv_id,
            limit=message_limit,
        )

    return []
