"""Activity log API — view agent work history."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_message_contracts import extract_sender_label_from_message, strip_sender_label_prefix
from app.core.permissions import check_agent_access, check_agent_operator_reachability
from app.core.resource_authority import (
    OWNED_AUTHORITY_STATE,
    ResourceAuthorityDecision,
    authorize_resource_action,
    filter_authorized_resources,
    load_explicit_resource_grant_ids,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.activity_log import AgentActivityLog
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.user import User
from app.services.tool_telemetry import summarize_tool_failure_logs

router = APIRouter(tags=["activity"])


async def _load_authorized_activity_rows(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    query,
    limit: int,
    operator_view: bool,
    operator_reason: str | None,
    agent_access,
):
    """Apply authority inside SQL and execute one limited activity scan."""

    bounded_limit = max(1, int(limit))
    if operator_view:
        rows = (await db.execute(query.limit(bounded_limit))).scalars().all()
        return await filter_authorized_resources(
            db,
            current_user,
            agent_id=agent_id,
            resource_kind="agent_activity",
            action="read",
            resources=rows,
            operator_view=True,
            operator_reason=operator_reason,
            agent_access=agent_access,
        )

    visible = await load_visible_activity_rows(
        db,
        current_user,
        query=query,
        limit=bounded_limit,
    )
    agent, access_level = agent_access
    return [
        (
            row,
            ResourceAuthorityDecision(
                agent=agent,
                access_level=access_level,
                resource_kind="agent_activity",
                resource_id=row.id,
                action="read",
                authority_source=authority_source,
            ),
        )
        for row, authority_source in visible
    ]


def activity_visibility_clause(current_user: User, explicit_grant_ids: set[uuid.UUID]):
    """SQL predicate matching the canonical owner/session/grant read contract."""

    owner_visible = AgentActivityLog.owner_user_id == current_user.id
    root_session_visible = exists(
        select(ChatSession.id).where(
            ChatSession.id == AgentActivityLog.root_session_id,
            ChatSession.agent_id == AgentActivityLog.agent_id,
            ChatSession.user_id == current_user.id,
        )
    )
    grant_visible = AgentActivityLog.id.in_(explicit_grant_ids) if explicit_grant_ids else false()
    return and_(
        AgentActivityLog.authority_state == OWNED_AUTHORITY_STATE,
        or_(owner_visible, root_session_visible, grant_visible),
    )


async def load_visible_activity_rows(
    db: AsyncSession,
    current_user: User,
    *,
    query,
    limit: int,
    explicit_grant_ids: set[uuid.UUID] | None = None,
) -> list[tuple[AgentActivityLog, str]]:
    """Return bounded visible rows and their authority source without N+1 IO."""

    grant_ids = (
        explicit_grant_ids
        if explicit_grant_ids is not None
        else await load_explicit_resource_grant_ids(
            db,
            user=current_user,
            resource_kind="agent_activity",
            action="read",
        )
    )
    statement = query.where(activity_visibility_clause(current_user, grant_ids)).limit(max(1, int(limit)))
    rows = list((await db.execute(statement)).scalars().all())

    root_candidates = {
        (row.root_session_id, row.agent_id)
        for row in rows
        if getattr(row, "root_session_id", None) is not None
        and str(getattr(row, "owner_user_id", "")) != str(current_user.id)
    }
    owned_root_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    if root_candidates:
        root_ids = {root_id for root_id, _agent_id in root_candidates}
        result = await db.execute(
            select(ChatSession.id, ChatSession.agent_id).where(
                ChatSession.id.in_(root_ids),
                ChatSession.user_id == current_user.id,
            )
        )
        owned_root_pairs = {(root_id, root_agent_id) for root_id, root_agent_id in result.all()}

    visible: list[tuple[AgentActivityLog, str]] = []
    for row in rows:
        if str(getattr(row, "owner_user_id", "")) == str(current_user.id):
            authority_source = "resource_owner"
        elif (getattr(row, "root_session_id", None), row.agent_id) in owned_root_pairs:
            authority_source = "root_session_owner"
        elif row.id in grant_ids:
            authority_source = "resource_grant"
        else:  # The SQL predicate is authoritative; this guards test doubles/drift.
            continue
        visible.append((row, authority_source))
    return visible


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: uuid.UUID,
    limit: int = Query(50, le=200),
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get recent activity logs for an agent."""
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )

    query = (
        select(AgentActivityLog)
        .where(AgentActivityLog.agent_id == agent_id)
        .order_by(AgentActivityLog.created_at.desc())
    )
    authorized = await _load_authorized_activity_rows(
        db,
        current_user,
        agent_id=agent_id,
        query=query,
        limit=limit,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )

    return [
        {
            "id": str(log.id),
            "action_type": log.action_type,
            "summary": log.summary,
            "detail": log.detail_json,
            "related_id": str(log.related_id) if log.related_id else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "authority_source": decision.authority_source,
            "operator_view": decision.operator_view,
        }
        for log, decision in authorized
    ]


@router.get("/agents/{agent_id}/activity/tool-failures")
async def get_agent_tool_failure_summary(
    agent_id: uuid.UUID,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(500, ge=10, le=2000),
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregated tool failure telemetry for an agent."""
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )
    from datetime import UTC, datetime, timedelta

    query = (
        select(AgentActivityLog)
        .where(AgentActivityLog.agent_id == agent_id)
        .where(AgentActivityLog.action_type == "error")
        .where(AgentActivityLog.created_at >= datetime.now(UTC) - timedelta(hours=max(hours, 1)))
        .order_by(AgentActivityLog.created_at.desc())
    )
    authorized = await _load_authorized_activity_rows(
        db,
        current_user,
        agent_id=agent_id,
        query=query,
        limit=limit,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
    return summarize_tool_failure_logs([log for log, _decision in authorized])


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


def _session_authority_state(session: ChatSession) -> str:
    return "owned" if getattr(session, "user_id", None) else "quarantined"


async def _filter_authorized_sessions(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    sessions: list[ChatSession],
    operator_view: bool,
    operator_reason: str | None,
    agent_access,
):
    return await filter_authorized_resources(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="chat_session",
        action="read",
        resources=sessions,
        owner_user_id_of=lambda session: getattr(session, "user_id", None),
        root_session_id_of=lambda session: getattr(session, "root_session_id", None),
        authority_state_of=_session_authority_state,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )


@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(
    agent_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all conversation partners for this agent using ChatSession as the canonical entrypoint."""
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )

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
    authorized_channel_sessions = await _filter_authorized_sessions(
        db,
        current_user,
        agent_id=agent_id,
        sessions=channel_sessions,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
    for session, decision in authorized_channel_sessions:
        conv_id = str(session.id)
        count, last_at = await _get_session_message_stats(db, agent_id=session.agent_id, conversation_id=conv_id)
        last_message = await _get_last_session_message(db, agent_id=session.agent_id, conversation_id=conv_id)
        source = getattr(session, "source_channel", "") or "web"
        partner_type = "user" if source == "web" else source
        partner_id = str(getattr(session, "user_id", "") or getattr(session, "external_conv_id", "") or session.id)
        if source != "web":
            partner_id = str(
                getattr(session, "external_conv_id", None) or getattr(session, "user_id", "") or session.id
            )
        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": partner_type,
                "partner_id": partner_id,
                "partner_name": await _format_channel_partner_name(db, session, last_message),
                "last_message": last_message[:80],
                "message_count": count,
                "last_at": last_at.isoformat() if last_at else None,
                "authority_source": decision.authority_source,
                "operator_view": decision.operator_view,
            }
        )

    agent_sessions_q = await db.execute(
        select(ChatSession).where(
            ChatSession.source_channel == "agent",
            or_(ChatSession.agent_id == agent_id, ChatSession.peer_agent_id == agent_id),
        )
    )
    agent_sessions = agent_sessions_q.scalars().all()
    authorized_agent_sessions = await _filter_authorized_sessions(
        db,
        current_user,
        agent_id=agent_id,
        sessions=agent_sessions,
        operator_view=operator_view,
        operator_reason=operator_reason,
        agent_access=agent_access,
    )
    for session, decision in authorized_agent_sessions:
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
                "authority_source": decision.authority_source,
                "operator_view": decision.operator_view,
            }
        )

    if not conversations and operator_view:
        legacy_decision = await authorize_resource_action(
            db,
            current_user,
            agent_id=agent_id,
            resource_kind="legacy_chat_history",
            resource_id=uuid.uuid5(agent_id, "legacy-chat-history"),
            action="read",
            authority_state="quarantined",
            allow_manager_override=True,
            manager_override_reason=operator_reason,
            agent_access=agent_access,
        )
        await _append_legacy_prefix_conversations(db, agent_id=agent_id, conversations=conversations)
        for conversation in conversations:
            conversation["authority_source"] = legacy_decision.authority_source
            conversation["operator_view"] = legacy_decision.operator_view

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
    result = await db.execute(select(ChatMessage).where(*filters).order_by(ChatMessage.created_at.asc()).limit(limit))
    return result.scalars().all()


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a canonical ChatSession or a legacy channel conversation id."""
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )
    limit_value = _coerce_limit(limit)

    session = await _load_accessible_session(db, agent_id=agent_id, conv_id=conv_id)
    if session is not None:
        decision = await authorize_resource_action(
            db,
            current_user,
            agent_id=agent_id,
            resource_kind="chat_session",
            resource_id=session.id,
            action="read",
            owner_user_id=getattr(session, "user_id", None),
            root_session_id=getattr(session, "root_session_id", None),
            authority_state=_session_authority_state(session),
            allow_manager_override=operator_view,
            manager_override_reason=operator_reason,
            agent_access=agent_access,
        )
        include_sender = getattr(session, "source_channel", None) == "agent"
        messages = await _list_messages_by_conversation(
            db,
            conversation_id=str(session.id),
            agent_id=session.agent_id,
            limit=limit_value,
        )
        payload = [await _format_session_message(message, include_sender=include_sender, db=db) for message in messages]
        for item in payload:
            item["authority_source"] = decision.authority_source
            item["operator_view"] = decision.operator_view
        return payload

    legacy_prefixes = ("web_", "feishu_", "slack_", "discord_")
    if conv_id.startswith(legacy_prefixes):
        try:
            decision = await authorize_resource_action(
                db,
                current_user,
                agent_id=agent_id,
                resource_kind="legacy_chat_history",
                resource_id=uuid.uuid5(agent_id, f"legacy-chat-history:{conv_id}"),
                action="read",
                authority_state="quarantined",
                allow_manager_override=operator_view,
                manager_override_reason=operator_reason,
                agent_access=agent_access,
            )
        except HTTPException:
            return []
        messages = await _list_messages_by_conversation(
            db,
            conversation_id=conv_id,
            agent_id=agent_id,
            limit=limit_value,
        )
        payload = [await _format_session_message(message, include_sender=False, db=db) for message in messages]
        for item in payload:
            item["authority_source"] = decision.authority_source
            item["operator_view"] = decision.operator_view
        return payload

    return []
