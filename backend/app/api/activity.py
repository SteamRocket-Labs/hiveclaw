"""Activity log API — view agent work history."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_message_contracts import extract_sender_label_from_message, strip_sender_label_prefix
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.user import User
from app.services.tool_telemetry import collect_agent_tool_failure_summary

router = APIRouter(tags=["activity"])


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


def _coerce_limit(limit, default: int = 100) -> int:
    """FastAPI Query defaults are not plain ints when endpoint functions are called directly in tests."""
    return limit if isinstance(limit, int) else default


async def _get_session_message_stats(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> tuple[int, object | None]:
    result = await db.execute(
        select(func.count(ChatMessage.id), func.max(ChatMessage.created_at)).where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
    )
    row = result.fetchone()
    if not row:
        return 0, None
    return int(row[0] or 0), row[1]


async def _get_last_session_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> str:
    result = await db.execute(
        select(ChatMessage.content)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(1)
    )
    content = result.scalar_one_or_none() or ""
    return strip_sender_label_prefix(content)


async def _get_first_user_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> str:
    result = await db.execute(
        select(ChatMessage.content)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "user",
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none() or ""


async def _get_user_display_name(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if not user_id:
        return None
    result = await db.execute(select(User.display_name).where(User.id == user_id))
    return result.scalar_one_or_none()


def _delivery_label(session: ChatSession) -> str | None:
    target = getattr(session, "delivery_target_json", None) or {}
    label = target.get("user_label") or target.get("username") or target.get("sender_label")
    return str(label).strip() if label else None


async def _format_channel_partner_name(db: AsyncSession, session: ChatSession, last_message: str) -> str:
    source = getattr(session, "source_channel", "") or "web"
    label = _delivery_label(session)

    if source == "web":
        label = label or await _get_user_display_name(db, getattr(session, "user_id", None)) or "未知用户"
        return f"👤 {label}"
    if source == "feishu":
        first_user_message = await _get_first_user_message(
            db,
            agent_id=session.agent_id,
            conversation_id=str(session.id),
        )
        sender_label = extract_sender_label_from_message(first_user_message)
        if sender_label:
            label = sender_label
        if not label:
            sender_label = extract_sender_label_from_message(last_message)
            label = sender_label or await _get_user_display_name(db, getattr(session, "user_id", None))
        if label:
            return f"📱 {label}"
        external_conv_id = getattr(session, "external_conv_id", "") or ""
        return "👥 飞书群聊" if "group" in external_conv_id else "📱 飞书用户"
    if source == "telegram":
        label = label or await _get_user_display_name(db, getattr(session, "user_id", None))
        return f"✈️ Telegram {label}" if label else "✈️ Telegram"
    if source == "microsoft_teams":
        label = label or await _get_user_display_name(db, getattr(session, "user_id", None))
        return f"🪟 Teams {label}" if label else "🪟 Teams"
    if source == "slack":
        label = label or getattr(session, "title", None) or getattr(session, "external_conv_id", None)
        return f"💬 Slack {label}" if label else "💬 Slack"
    if source == "discord":
        label = label or getattr(session, "title", None) or getattr(session, "external_conv_id", None)
        return f"🎮 Discord {label}" if label else "🎮 Discord"

    label = label or getattr(session, "title", None) or source
    return f"{source} {label}".strip()


async def _append_legacy_prefix_conversations(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversations: list[dict],
) -> None:
    legacy_prefixes = [
        ("web_", "user", "👤", "Web"),
        ("feishu_", "feishu", "📱", "飞书"),
        ("slack_", "slack", "💬", "Slack"),
        ("discord_", "discord", "🎮", "Discord"),
    ]
    for prefix, partner_type, icon, label in legacy_prefixes:
        rows_result = await db.execute(
            select(
                ChatMessage.conversation_id,
                func.max(ChatMessage.created_at).label("last_at"),
                func.count(ChatMessage.id).label("cnt"),
            )
            .where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id.like(f"{prefix}%"),
            )
            .group_by(ChatMessage.conversation_id)
        )
        for conv_id, last_at, count in rows_result.fetchall():
            last_message = await _get_last_session_message(db, agent_id=agent_id, conversation_id=conv_id)
            partner_name = f"{icon} {label}"
            if partner_type == "feishu":
                sender_label = extract_sender_label_from_message(last_message)
                partner_name = f"📱 {sender_label}" if sender_label else "📱 飞书用户"
            conversations.append(
                {
                    "conv_id": conv_id,
                    "partner_type": partner_type,
                    "partner_id": conv_id,
                    "partner_name": partner_name,
                    "last_message": last_message[:80],
                    "message_count": count,
                    "last_at": last_at.isoformat() if last_at else None,
                }
            )


@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all conversation partners for this agent using ChatSession as the canonical entrypoint."""
    await check_agent_access(db, current_user, agent_id)

    conversations = []

    channel_sessions_q = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.source_channel != "agent",
        )
        .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
    )
    channel_sessions = channel_sessions_q.scalars().all()
    for session in channel_sessions:
        conv_id = str(session.id)
        count, last_at = await _get_session_message_stats(db, agent_id=session.agent_id, conversation_id=conv_id)
        last_message = await _get_last_session_message(db, agent_id=session.agent_id, conversation_id=conv_id)
        source = getattr(session, "source_channel", "") or "web"
        partner_type = "user" if source == "web" else source
        partner_id = str(getattr(session, "user_id", "") or getattr(session, "external_conv_id", "") or session.id)
        if source != "web":
            partner_id = str(getattr(session, "external_conv_id", None) or getattr(session, "user_id", "") or session.id)
        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": partner_type,
                "partner_id": partner_id,
                "partner_name": await _format_channel_partner_name(db, session, last_message),
                "last_message": last_message[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    agent_sessions_q = await db.execute(
        select(ChatSession).where(
            ChatSession.source_channel == "agent",
            or_(ChatSession.agent_id == agent_id, ChatSession.peer_agent_id == agent_id),
        )
    )
    agent_sessions = agent_sessions_q.scalars().all()
    for session in agent_sessions:
        partner_id = session.peer_agent_id if session.agent_id == agent_id else session.agent_id
        agent_r = await db.execute(select(Agent.name).where(Agent.id == partner_id))
        partner_name = agent_r.scalar_one_or_none() or "未知数字员工"

        conv_id = str(session.id)
        count, last_at = await _get_session_message_stats(db, agent_id=session.agent_id, conversation_id=conv_id)
        last_message = await _get_last_session_message(db, agent_id=session.agent_id, conversation_id=conv_id)
        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": "agent",
                "partner_id": str(partner_id),
                "partner_name": f"🤖 {partner_name}",
                "last_message": last_message[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    if not conversations:
        await _append_legacy_prefix_conversations(db, agent_id=agent_id, conversations=conversations)

    conversations.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return conversations


async def _load_accessible_session(db: AsyncSession, *, agent_id: uuid.UUID, conv_id: str) -> ChatSession | None:
    try:
        session_uuid = uuid.UUID(str(conv_id))
    except (ValueError, TypeError):
        return None

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            or_(ChatSession.agent_id == agent_id, ChatSession.peer_agent_id == agent_id),
        )
    )
    return result.scalar_one_or_none()


async def _format_session_message(message: ChatMessage, *, include_sender: bool, db: AsyncSession) -> dict:
    payload = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
    if include_sender:
        sender_name = "未知"
        participant_id = getattr(message, "participant_id", None)
        if participant_id:
            result = await db.execute(select(Participant.display_name).where(Participant.id == participant_id))
            sender_name = result.scalar_one_or_none() or "未知"
        payload["sender_name"] = sender_name
    else:
        payload["content"] = strip_sender_label_prefix(message.content)
    return payload


async def _list_messages_by_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    agent_id: uuid.UUID | None,
    limit: int,
) -> list[ChatMessage]:
    filters = [ChatMessage.conversation_id == conversation_id]
    if agent_id is not None:
        filters.append(ChatMessage.agent_id == agent_id)
    result = await db.execute(
        select(ChatMessage)
        .where(*filters)
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
    )
    return result.scalars().all()


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a canonical ChatSession or a legacy channel conversation id."""
    await check_agent_access(db, current_user, agent_id)
    limit_value = _coerce_limit(limit)

    session = await _load_accessible_session(db, agent_id=agent_id, conv_id=conv_id)
    if session is not None:
        include_sender = getattr(session, "source_channel", None) == "agent"
        messages = await _list_messages_by_conversation(
            db,
            conversation_id=str(session.id),
            agent_id=session.agent_id,
            limit=limit_value,
        )
        return [
            await _format_session_message(message, include_sender=include_sender, db=db)
            for message in messages
        ]

    legacy_prefixes = ("web_", "feishu_", "slack_", "discord_")
    if conv_id.startswith(legacy_prefixes):
        messages = await _list_messages_by_conversation(
            db,
            conversation_id=conv_id,
            agent_id=agent_id,
            limit=limit_value,
        )
        return [
            await _format_session_message(message, include_sender=False, db=db)
            for message in messages
        ]

    return []
