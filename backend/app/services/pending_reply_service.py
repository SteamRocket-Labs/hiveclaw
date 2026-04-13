"""Pending reply context service — automatic cross-session context propagation.

Captures outbound message context when an agent sends a message via any
messaging tool, and injects it when the recipient replies in a separate session.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_reply import PendingReplyContext
from app.session_identifiers import parse_feishu_p2p_conv_id

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_HOURS = 48

# ── Tool → recipient arg mapping ──

_TOOL_RECIPIENT_MAP: dict[str, dict[str, str | list[str]]] = {
    "send_feishu_message": {"channel": "feishu", "name_key": "member_name", "id_keys": ["user_id", "open_id"]},
    "send_web_message": {"channel": "web", "name_key": "username", "id_keys": ["username"]},
    "send_slack_message": {"channel": "slack", "name_key": "username", "id_keys": ["user_id", "username"]},
    "send_dingtalk_message": {"channel": "dingtalk", "name_key": "member_name", "id_keys": ["user_id"]},
    "send_wecom_message": {"channel": "wecom", "name_key": "member_name", "id_keys": ["user_id"]},
    "send_channel_message": {"channel": "dynamic", "name_key": "message", "id_keys": []},
}

OUTBOUND_TOOL_NAMES = tuple(_TOOL_RECIPIENT_MAP.keys())


def normalize_identity(channel: str, identifier: str) -> str:
    """Normalize recipient identity for cross-session matching.

    Format: "{channel}:{identifier}" — e.g. "feishu:u_abc123", "web:simon".
    """
    return f"{channel}:{identifier.strip()}"


def extract_recipient_info(tool_name: str, tool_args: dict) -> dict | None:
    """Extract recipient channel, name, and identity from tool args.

    Returns None if tool is not a messaging tool or required args are missing.
    """
    spec = _TOOL_RECIPIENT_MAP.get(tool_name)
    if not spec:
        return None

    if tool_name == "send_channel_message":
        from app.services.channel_delivery_service import ChannelDeliveryService, channel_delivery_target

        target = channel_delivery_target.get(None)
        normalized_target = ChannelDeliveryService.normalize_reply_target(target)
        identity = ChannelDeliveryService.identity_from_delivery_target(normalized_target)
        if not normalized_target or not identity:
            return None
        return {
            "channel": normalized_target["channel"],
            "name": (
                normalized_target.get("user_label")
                or normalized_target.get("username")
                or normalized_target.get("user_id")
                or normalized_target.get("to_user_id")
                or normalized_target.get("open_id")
                or normalized_target.get("receive_id")
                or ""
            ),
            "identity": identity,
        }

    channel = str(spec["channel"])
    name_key = str(spec["name_key"])
    name = (tool_args.get(name_key) or "").strip()

    identifier = ""
    for key in spec["id_keys"]:
        val = (tool_args.get(key) or "").strip()
        if val:
            identifier = val
            break

    if not identifier and not name:
        return None

    return {
        "channel": channel,
        "name": name,
        "identity": normalize_identity(channel, identifier or name),
    }


def sender_identity_from_external_conv_id(external_conv_id: str) -> str:
    """Infer sender identity from canonical external conversation ids."""
    ext = (external_conv_id or "").strip()
    if not ext:
        return ""
    if identifier := parse_feishu_p2p_conv_id(ext):
        return normalize_identity("feishu", identifier)
    if ext.startswith("web_"):
        return normalize_identity("web", ext[len("web_"):])
    if ext.startswith("slack_"):
        return normalize_identity("slack", ext[len("slack_"):])
    if ext.startswith("tg_"):
        parts = ext.split("_", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return f"telegram:{parts[1]}:{parts[2]}"
    if ext.startswith("wecom_p2p_"):
        return normalize_identity("wecom", ext[len("wecom_p2p_"):])
    if ext.startswith("wecom_group_"):
        payload = ext[len("wecom_group_"):]
        if "_" in payload:
            _chat_id, user_id = payload.rsplit("_", 1)
            if user_id:
                return normalize_identity("wecom", user_id)
    if ext.startswith("wechat_p2p_"):
        return normalize_identity("wechat_personal", ext[len("wechat_p2p_"):])
    return ""


def sender_identity_from_session(session: object | None) -> str:
    """Resolve canonical sender identity from a ChatSession-like object."""
    if session is None:
        return ""
    delivery_target = getattr(session, "delivery_target_json", None)
    if delivery_target:
        from app.services.channel_delivery_service import ChannelDeliveryService

        identity = ChannelDeliveryService.identity_from_delivery_target(delivery_target)
        if identity:
            return identity
    return sender_identity_from_external_conv_id(getattr(session, "external_conv_id", "") or "")


def build_task_context(messages: list[dict], tool_args: dict) -> dict:
    """Build structured task context from recent conversation messages.

    Extracts: last user message (originator's request), last assistant message
    (agent's reasoning), and the outbound message. Model-agnostic — no LLM call.
    """
    originator_message = ""
    agent_reasoning = ""

    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user" and not originator_message:
            originator_message = content[:500]
        elif role == "assistant" and not agent_reasoning and not msg.get("tool_calls"):
            agent_reasoning = content[:500]
        if originator_message and agent_reasoning:
            break

    return {
        "originator_message": originator_message,
        "agent_reasoning": agent_reasoning,
        "outbound_message": (tool_args.get("message") or "")[:500],
    }


async def capture_pending_reply(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    tool_name: str,
    tool_args: dict,
    messages: list[dict],
    originator_name: str = "",
    originator_identity: str = "",
) -> PendingReplyContext | None:
    """Capture pending reply context after a successful outbound message.

    Called by the POST_TOOL_USE hook. Returns the created record, or None if
    the tool is not a messaging tool or required args are missing.
    """
    recipient = extract_recipient_info(tool_name, tool_args)
    if not recipient:
        return None

    context = build_task_context(messages, tool_args)
    message_text = (tool_args.get("message") or "").strip()

    # Build expected_action from originator's request
    expected_action = ""
    if context["originator_message"]:
        expected_action = (
            f"Fulfill the originator's request: {context['originator_message'][:300]}. "
            f"After handling the reply, notify the originator ({originator_name or 'the requester'}) of the result."
        )

    now = datetime.now(timezone.utc)
    record = PendingReplyContext(
        agent_id=agent_id,
        recipient_channel=recipient["channel"],
        recipient_identity=recipient["identity"],
        outbound_message=message_text[:2000],
        task_summary=context["originator_message"][:1000] or f"Agent sent: {message_text[:200]}",
        originator_name=originator_name[:100] if originator_name else None,
        originator_identity=originator_identity[:200] if originator_identity else None,
        expected_action=expected_action[:2000] if expected_action else None,
        full_context_json=context,
        status="pending",
        expires_at=now + timedelta(hours=DEFAULT_EXPIRY_HOURS),
    )
    db.add(record)
    await db.flush()
    logger.info(
        "[PendingReply] Captured for agent %s → %s (%s), expires %s",
        agent_id, recipient["name"], recipient["identity"], record.expires_at,
    )
    return record


async def lookup_pending_replies(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    sender_identity: str,
) -> list[PendingReplyContext]:
    """Find all pending (non-expired, non-fulfilled) reply contexts for a sender.

    Called at message reception time in channel handlers.
    Uses read-only SELECT — call claim_and_fulfill_pending_replies for atomic
    lookup+fulfill when you need to consume them.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PendingReplyContext)
        .where(
            PendingReplyContext.agent_id == agent_id,
            PendingReplyContext.recipient_identity == sender_identity,
            PendingReplyContext.status == "pending",
            PendingReplyContext.expires_at > now,
        )
        .order_by(PendingReplyContext.created_at.desc())
        .limit(3)
    )
    return list(result.scalars().all())


async def claim_and_fulfill_pending_replies(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    sender_identity: str,
) -> list[PendingReplyContext]:
    """Atomically claim and fulfill pending replies via UPDATE ... RETURNING.

    Solves the TOCTOU race: concurrent requests both see pending rows, but only
    the winner of the UPDATE gets rows back. The loser gets zero and skips injection.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(PendingReplyContext)
        .where(
            PendingReplyContext.agent_id == agent_id,
            PendingReplyContext.recipient_identity == sender_identity,
            PendingReplyContext.status == "pending",
            PendingReplyContext.expires_at > now,
        )
        .values(status="fulfilled", fulfilled_at=now)
        .returning(PendingReplyContext)
    )
    rows = list(result.scalars().all())
    if rows:
        logger.info("[PendingReply] Claimed %d pending reply(ies) for agent %s, sender %s", len(rows), agent_id, sender_identity)
    return rows


def format_pending_reply_context(pending_replies: list[PendingReplyContext]) -> str:
    """Format pending reply records into a system prompt suffix block.

    This is the structured context injected into the agent's prompt so it
    knows WHY it previously messaged this user and WHAT to do with the reply.
    """
    if not pending_replies:
        return ""

    parts = [
        "===== 待回复任务上下文 =====",
        "你之前代他人向当前用户发送了消息。收到回复后，根据以下上下文执行对应操作。",
        "",
    ]
    for i, pr in enumerate(pending_replies, 1):
        parts.append(f"[任务 {i}]")
        if pr.originator_name:
            parts.append(f"- 发起人：{pr.originator_name}")
        if getattr(pr, "originator_identity", None):
            parts.append(f"- 发起人标识：{pr.originator_identity[:200]}")
        if getattr(pr, "recipient_identity", None):
            parts.append(f"- 收件人标识：{pr.recipient_identity[:200]}")
        created_at = getattr(pr, "created_at", None)
        if created_at:
            try:
                parts.append(f"- 创建时间：{created_at.astimezone(timezone.utc).isoformat()}")
            except Exception:
                parts.append(f"- 创建时间：{created_at}")
        parts.append(f"- 已发送内容：\"{pr.outbound_message[:300]}\"")
        if pr.task_summary:
            parts.append(f"- 原始请求：{pr.task_summary[:300]}")
        if pr.expected_action:
            parts.append(f"- 期望操作：{pr.expected_action[:300]}")
        parts.append("")

    parts.extend([
        "重要：将用户回复与上述任务匹配。如果相关，执行期望操作。",
        "不要重复询问上述已有信息。",
        "=============================",
    ])
    return "\n".join(parts)


async def fulfill_pending_replies(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    sender_identity: str,
) -> int:
    """Mark all matching pending replies as fulfilled.

    Called after the agent processes the reply (first reply fulfills all pending
    contexts for that sender — pragmatic for one-shot confirmation patterns).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(PendingReplyContext)
        .where(
            PendingReplyContext.agent_id == agent_id,
            PendingReplyContext.recipient_identity == sender_identity,
            PendingReplyContext.status == "pending",
            PendingReplyContext.expires_at > now,
        )
        .values(status="fulfilled", fulfilled_at=now)
    )
    count = result.rowcount
    if count:
        logger.info("[PendingReply] Fulfilled %d pending replies for agent %s, sender %s", count, agent_id, sender_identity)
    return count


async def cleanup_expired_replies(db: AsyncSession) -> int:
    """Mark expired pending replies. Called periodically from trigger daemon."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(PendingReplyContext)
        .where(
            PendingReplyContext.status == "pending",
            PendingReplyContext.expires_at <= now,
        )
        .values(status="expired")
    )
    count = result.rowcount
    if count:
        logger.info("[PendingReply] Expired %d stale pending replies", count)
    return count
