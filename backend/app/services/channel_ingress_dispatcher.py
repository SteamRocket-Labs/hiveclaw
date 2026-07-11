"""Explicit dispatch table for already-authenticated durable channel events."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
import uuid

from fastapi import Response
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.channel_ingress_context import use_channel_ingress_context
from app.services.channel_ingress_inbox import ClaimedChannelIngressEvent

SUPPORTED_CHANNEL_INGRESS_HANDLERS = (
    "slack.event_callback",
    "feishu.event",
    "feishu.card_action",
    "telegram.update",
    "discord.interaction",
    "teams.activity",
    "wecom.webhook",
    "dingtalk.stream_message",
    "wecom.stream_message",
    "wechat_personal.stream_message",
)


class ChannelIngressReplayRequest:
    """Minimal internal request shape; it cannot carry untrusted auth headers."""

    def __init__(self, *, event_id: uuid.UUID, body: dict[str, Any]) -> None:
        self._body = dict(body)
        self.state = SimpleNamespace(channel_ingress_event_id=event_id)
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}

    async def body(self) -> bytes:
        return json.dumps(self._body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


def _response_failed(result: Any) -> bool:
    return isinstance(result, Response) and int(result.status_code) >= 400


async def _resume_materialized_user_message(
    db,
    item: ClaimedChannelIngressEvent,
) -> dict[str, Any] | None:
    """Close the crash window after user-message commit but before run staging."""

    message = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.source_ingress_event_id == item.id,
                ChatMessage.tenant_id == item.tenant_id,
                ChatMessage.agent_id == item.agent_id,
                ChatMessage.role == "user",
            )
        )
    ).scalar_one_or_none()
    if message is None:
        return None
    try:
        session_uuid = uuid.UUID(str(message.conversation_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("materialized channel ingress message has no durable session") from exc
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_uuid,
                ChatSession.tenant_id == item.tenant_id,
                ChatSession.agent_id == item.agent_id,
            )
        )
    ).scalar_one_or_none()
    agent = (
        await db.execute(select(Agent).where(Agent.id == item.agent_id, Agent.tenant_id == item.tenant_id))
    ).scalar_one_or_none()
    user = (
        await db.execute(select(User).where(User.id == message.user_id, User.tenant_id == item.tenant_id))
    ).scalar_one_or_none()
    if session is None or agent is None or user is None:
        raise RuntimeError("materialized channel ingress authority cannot be restored")

    from app.services.web_chat_runtime import start_channel_chat_run_from_saved_turn

    run = await start_channel_chat_run_from_saved_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=message.content,
        source_channel=session.source_channel or item.provider,
        extra_metadata={"channel_ingress_recovered_from_message_id": str(message.id)},
    )
    return {
        "status": "accepted",
        "runtime_task_id": run.get("run_id"),
        "session_id": str(session.id),
        "recovered_from_materialized_user_message": True,
    }


async def _dispatch_verified_payload(db, item: ClaimedChannelIngressEvent) -> dict[str, Any]:
    key = item.handler_key
    body = dict(item.payload.get("body") or item.payload)
    replay_request = ChannelIngressReplayRequest(event_id=item.id, body=body)

    if key == "slack.event_callback":
        from app.api.slack import slack_event_webhook

        result = await slack_event_webhook(item.agent_id, replay_request, db)
    elif key == "feishu.event":
        from app.api.feishu import process_feishu_event

        tenant_channel_config = None
        tenant_config_id = item.metadata.get("tenant_channel_config_id")
        if tenant_config_id:
            from app.models.tenant_channel_config import TenantChannelConfig

            tenant_channel_config = (
                await db.execute(
                    select(TenantChannelConfig).where(
                        TenantChannelConfig.id == uuid.UUID(str(tenant_config_id)),
                        TenantChannelConfig.tenant_id == item.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if tenant_channel_config is None:
                raise RuntimeError("Feishu tenant installation no longer exists")
        result = await process_feishu_event(
            item.agent_id,
            body,
            db,
            tenant_channel_config=tenant_channel_config,
        )
    elif key == "feishu.card_action":
        from app.api.feishu import feishu_card_callback

        result = await feishu_card_callback(replay_request, db)
    elif key == "telegram.update":
        from app.api.telegram import telegram_webhook

        result = await telegram_webhook(item.agent_id, replay_request, db)
    elif key == "discord.interaction":
        from app.api.discord_bot import discord_interaction_webhook

        result = await discord_interaction_webhook(item.agent_id, replay_request, db)
    elif key == "teams.activity":
        from app.api.teams import teams_event_webhook

        result = await teams_event_webhook(item.agent_id, replay_request, db)
    elif key == "wecom.webhook":
        from app.api.wecom import _process_wecom_text
        from app.models.channel_config import ChannelConfig

        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == item.agent_id,
                    ChannelConfig.tenant_id == item.tenant_id,
                    ChannelConfig.channel_type == "wecom",
                )
            )
        ).scalar_one_or_none()
        if config is None:
            raise RuntimeError("WeCom installation no longer exists")
        result = await _process_wecom_text(
            db,
            item.agent_id,
            config,
            str(body.get("from_user") or ""),
            str(body.get("user_text") or ""),
        )
    elif key == "dingtalk.stream_message":
        from app.api.dingtalk import process_dingtalk_message

        result = await process_dingtalk_message(
            agent_id=item.agent_id,
            sender_staff_id=str(body.get("sender_staff_id") or ""),
            user_text=str(body.get("user_text") or ""),
            conversation_id=str(body.get("conversation_id") or ""),
            conversation_type=str(body.get("conversation_type") or "1"),
            session_webhook=str(body.get("session_webhook") or ""),
        )
    elif key == "wecom.stream_message":
        from app.services.wecom_stream import _process_wecom_stream_message

        result = await _process_wecom_stream_message(
            agent_id=item.agent_id,
            sender_id=str(body.get("sender_id") or ""),
            user_text=str(body.get("user_text") or ""),
            chat_id=str(body.get("chat_id") or ""),
            chat_type=str(body.get("chat_type") or "single"),
        )
    elif key == "wechat_personal.stream_message":
        from app.services.wechat_personal_stream import _process_wechat_message

        result = await _process_wechat_message(
            agent_id=item.agent_id,
            sender_id=str(body.get("sender_id") or ""),
            user_text=str(body.get("user_text") or ""),
            delivery_target=dict(body.get("delivery_target") or {}),
        )
    else:  # pragma: no cover - guarded before the tenant session opens.
        raise ValueError(f"unsupported channel ingress handler: {key}")

    if _response_failed(result):
        raise RuntimeError(f"channel ingress processor returned HTTP {result.status_code}")
    return {"status": "processed", **({"reply_text": result} if isinstance(result, str) else {})}


async def dispatch_channel_ingress_event(item: ClaimedChannelIngressEvent) -> dict[str, Any]:
    """Resume or dispatch one claimed event under its immutable tenant authority."""

    if item.handler_key not in SUPPORTED_CHANNEL_INGRESS_HANDLERS:
        raise ValueError(f"unsupported channel ingress handler: {item.handler_key}")
    with use_channel_ingress_context(
        event_id=item.id,
        tenant_id=item.tenant_id,
        agent_id=item.agent_id,
    ) as context:
        async with tenant_scoped_session(
            item.tenant_id,
            require_tenant=True,
            source="channel_ingress_dispatch",
        ) as db:
            recovered = await _resume_materialized_user_message(db, item)
            result = recovered or await _dispatch_verified_payload(db, item)

        if context.runtime_task_id is not None:
            result = {
                **result,
                "runtime_task_id": str(context.runtime_task_id),
                "session_id": str(context.session_id) if context.session_id else None,
            }
        return result


__all__ = [
    "ChannelIngressReplayRequest",
    "SUPPORTED_CHANNEL_INGRESS_HANDLERS",
    "dispatch_channel_ingress_event",
]
