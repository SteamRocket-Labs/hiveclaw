"""Unified channel delivery service for live and deferred replies."""

from __future__ import annotations

import json
import mimetypes
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_config import ChannelConfig
from app.services.activity_logger import log_activity

channel_delivery_target: ContextVar[dict[str, Any] | None] = ContextVar("channel_delivery_target", default=None)


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    status: str
    channel: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChannelDeliveryService:
    """Unified text/file delivery across supported channels."""

    @staticmethod
    def resolve_capabilities(channel: str, connected_config: ChannelConfig | Any | None) -> dict[str, Any]:
        connected = bool(getattr(connected_config, "is_configured", False) and getattr(connected_config, "is_connected", True))
        base: dict[str, Any] = {
            "channel": channel,
            "connected": connected,
            "official_api": False,
            "third_party_transport": None,
            "capabilities": {
                "live_text": False,
                "inbound_file": False,
                "outbound_file": False,
                "deferred_text": False,
                "deferred_file": False,
                "on_message_current_sender": False,
                "on_message_by_name": False,
            },
            "limitations": [],
        }

        if channel == "feishu":
            base["official_api"] = True
            base["capabilities"].update({
                "live_text": True,
                "inbound_file": True,
                "outbound_file": True,
                "deferred_text": True,
                "deferred_file": True,
                "on_message_current_sender": True,
                "on_message_by_name": True,
            })
        elif channel == "telegram":
            base["official_api"] = True
            base["capabilities"].update({
                "live_text": True,
                "inbound_file": True,
                "outbound_file": True,
                "deferred_text": True,
                "deferred_file": True,
                "on_message_current_sender": True,
                "on_message_by_name": False,
            })
            base["limitations"].append("Telegram 仅支持回当前会话，不支持按人名主动寻址。")
        elif channel == "wecom":
            base["official_api"] = True
            base["capabilities"].update({
                "live_text": True,
                "inbound_file": False,
                "outbound_file": False,
                "deferred_text": True,
                "deferred_file": False,
                "on_message_current_sender": True,
                "on_message_by_name": False,
            })
            base["limitations"].append("WeCom 当前仅承诺文本闭环；文件回发仍显式标记为 unsupported。")
        elif channel == "wechat_personal":
            base["official_api"] = False
            base["third_party_transport"] = "ilink"
            base["capabilities"].update({
                "live_text": True,
                "inbound_file": True,
                "outbound_file": True,
                "deferred_text": "conditional",
                "deferred_file": "conditional",
                "on_message_current_sender": True,
                "on_message_by_name": False,
            })
            base["limitations"].append("个人微信延迟回投依赖近期会话 context token，有效期过后不可保证发送。")
        elif channel == "web":
            base["capabilities"].update({
                "live_text": True,
                "inbound_file": True,
                "outbound_file": False,
                "deferred_text": True,
                "deferred_file": False,
                "on_message_current_sender": True,
                "on_message_by_name": False,
            })
            base["limitations"].append("Web 通过站内会话与在线 WebSocket 推送闭环，不依赖外部渠道配置。")
        return base

    @staticmethod
    async def resolve_agent_capabilities(*, db: AsyncSession, agent_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.agent_id == agent_id).order_by(ChannelConfig.channel_type.asc())
        )
        configs = result.scalars().all()
        return [ChannelDeliveryService.resolve_capabilities(config.channel_type, config) for config in configs]

    @staticmethod
    def normalize_reply_target(reply_target: dict[str, Any] | None) -> dict[str, Any] | None:
        if not reply_target:
            return None
        channel = str(reply_target.get("channel") or "").strip()
        if not channel:
            return None
        normalized = dict(reply_target)
        normalized["channel"] = channel
        return normalized

    @staticmethod
    def identity_from_delivery_target(reply_target: dict[str, Any] | None) -> str:
        target = ChannelDeliveryService.normalize_reply_target(reply_target) or {}
        channel = target.get("channel")
        if channel == "feishu":
            receive_id = target.get("user_id") or target.get("open_id") or target.get("receive_id") or ""
            return f"feishu:{str(receive_id).strip()}" if receive_id else ""
        if channel == "telegram":
            chat_id = str(target.get("chat_id") or "").strip()
            sender_id = str(target.get("sender_id") or "").strip()
            if chat_id and sender_id:
                return f"telegram:{chat_id}:{sender_id}"
            return f"telegram:{chat_id}" if chat_id else ""
        if channel == "wecom":
            user_id = str(target.get("user_id") or "").strip()
            return f"wecom:{user_id}" if user_id else ""
        if channel == "dingtalk":
            user_id = str(target.get("user_id") or "").strip()
            return f"dingtalk:{user_id}" if user_id else ""
        if channel == "microsoft_teams":
            sender_id = str(target.get("sender_id") or "").strip()
            return f"microsoft_teams:{sender_id}" if sender_id else ""
        if channel == "wechat_personal":
            to_user_id = str(target.get("to_user_id") or "").strip()
            return f"wechat_personal:{to_user_id}" if to_user_id else ""
        if channel == "web":
            username = str(target.get("username") or "").strip()
            return f"web:{username}" if username else ""
        return ""

    @staticmethod
    async def _load_config(db: AsyncSession, agent_id: uuid.UUID, channel: str) -> ChannelConfig | None:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == channel,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _log_result(
        agent_id: uuid.UUID,
        result: DeliveryResult,
        *,
        delivery_mode: str,
        reply_target: dict[str, Any] | None,
        extra_detail: dict[str, Any] | None = None,
    ) -> None:
        action_type = {
            "success": "channel_delivery_success",
            "failed": "channel_delivery_failed",
            "unavailable": "channel_delivery_unavailable",
            "denied": "channel_capability_denied",
        }.get(result.status, "channel_delivery_failed")
        detail = {
            "channel": result.channel,
            "delivery_mode": delivery_mode,
            "target_type": (reply_target or {}).get("channel"),
            "failure_reason": None if result.ok else result.message,
            "reply_target": reply_target,
            **result.detail,
        }
        if extra_detail:
            detail.update(extra_detail)
        await log_activity(agent_id, action_type, result.message[:200], detail=detail)

    @staticmethod
    def _success(channel: str, message: str, **detail: Any) -> DeliveryResult:
        return DeliveryResult(ok=True, status="success", channel=channel, message=message, detail=detail)

    @staticmethod
    def _failed(channel: str, message: str, *, status: str = "failed", retryable: bool = False, **detail: Any) -> DeliveryResult:
        return DeliveryResult(ok=False, status=status, channel=channel, message=message, retryable=retryable, detail=detail)

    @staticmethod
    async def send_text(
        *,
        db: AsyncSession,
        agent_id: uuid.UUID,
        reply_target: dict[str, Any] | None,
        text: str,
        delivery_mode: str = "live",
        extra_detail: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        target = ChannelDeliveryService.normalize_reply_target(reply_target)
        if not target:
            result = ChannelDeliveryService._failed("unknown", "No reply target available for channel delivery.", status="unavailable")
            await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=reply_target, extra_detail=extra_detail)
            return result

        channel = target["channel"]
        config = None if channel == "web" else await ChannelDeliveryService._load_config(db, agent_id, channel)
        if channel != "web" and (not config or not getattr(config, "is_configured", False)):
            result = ChannelDeliveryService._failed(channel, f"{channel} channel is not configured for this agent.", status="unavailable")
            await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
            return result

        try:
            if channel == "feishu":
                from app.services.feishu_service import FeishuService

                receive_id = target.get("receive_id") or target.get("open_id") or target.get("chat_id")
                receive_id_type = target.get("receive_id_type") or ("chat_id" if target.get("chat_type") == "group" else "open_id")
                if not receive_id:
                    raise ValueError("missing receive_id")
                payload = json.dumps({"text": text})
                service = FeishuService()
                await service.send_message(config.app_id, config.app_secret, receive_id, "text", payload, receive_id_type=receive_id_type)
                result = ChannelDeliveryService._success(channel, "Feishu message delivered.", receive_id=receive_id, receive_id_type=receive_id_type)
            elif channel == "telegram":
                from app.api.telegram import _send_telegram_message

                chat_id = target.get("chat_id")
                if chat_id in (None, ""):
                    raise ValueError("missing chat_id")
                await _send_telegram_message(config.app_secret, chat_id, text)
                result = ChannelDeliveryService._success(channel, "Telegram message delivered.", chat_id=chat_id)
            elif channel == "wechat_personal":
                from app.services.wechat_ilink_client import ILinkClient
                from app.services.wechat_personal_service import get_channel_credentials, get_context_token

                creds = get_channel_credentials(config) or {}
                base_url = creds.get("base_url")
                bot_token = creds.get("bot_token")
                to_user_id = str(target.get("to_user_id") or "").strip()
                context_token = target.get("context_token") or await get_context_token(agent_id, to_user_id)
                if not to_user_id:
                    raise ValueError("missing to_user_id")
                if not context_token:
                    result = ChannelDeliveryService._failed(
                        channel,
                        "WeChat personal deferred delivery requires a valid context token.",
                        status="unavailable",
                        to_user_id=to_user_id,
                    )
                    await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
                    return result
                client = ILinkClient(base_url)
                await client.send_message(bot_token=bot_token, to_user_id=to_user_id, context_token=context_token, text=text)
                result = ChannelDeliveryService._success(channel, "WeChat personal message delivered.", to_user_id=to_user_id)
            elif channel == "wecom":
                from app.api.wecom import _send_wecom_text_message

                to_user = str(target.get("user_id") or "").strip()
                wecom_agent_id = str((getattr(config, "extra_config", {}) or {}).get("wecom_agent_id") or "").strip()
                if not to_user:
                    raise ValueError("missing user_id")
                if not wecom_agent_id:
                    raise ValueError("missing wecom_agent_id")
                await _send_wecom_text_message(
                    corp_id=str(config.app_id or "").strip(),
                    corp_secret=str(config.app_secret or "").strip(),
                    agent_id=wecom_agent_id,
                    to_user=to_user,
                    text=text,
                )
                result = ChannelDeliveryService._success(channel, "WeCom message delivered.", user_id=to_user, agent_id=wecom_agent_id)
            elif channel == "web":
                from app.api.websocket import manager as ws_manager
                from app.models.agent import Agent as AgentModel
                from app.models.audit import ChatMessage
                from app.models.user import User as UserModel
                from app.services.session_service import find_or_create_web_chat_session, session_conversation_id

                username = str(target.get("username") or "").strip()
                if not username:
                    raise ValueError("missing username")

                user_result = await db.execute(select(UserModel).where(UserModel.username == username))
                target_user = user_result.scalar_one_or_none()
                if not target_user:
                    raise ValueError(f"unknown web user: {username}")

                session = await find_or_create_web_chat_session(
                    db,
                    agent_id=agent_id,
                    user=target_user,
                    default_title=f"[Web Message] {username}",
                )
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        user_id=target_user.id,
                        role="assistant",
                        content=text,
                        conversation_id=session_conversation_id(session),
                    )
                )
                session.last_message_at = datetime.now(timezone.utc)
                await db.commit()

                agent_result = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                agent_obj = agent_result.scalar_one_or_none()
                agent_id_str = str(agent_id)
                if agent_id_str in ws_manager.active_connections:
                    for ws, _sid in list(ws_manager.active_connections[agent_id_str]):
                        try:
                            await ws.send_json(
                                {
                                    "type": "trigger_notification",
                                    "content": text,
                                    "triggers": ["web_message"],
                                    "agent_name": getattr(agent_obj, "name", None),
                                }
                            )
                        except Exception as exc:
                            logger.debug("[ChannelDelivery] Web push suppressed: %s", exc)
                result = ChannelDeliveryService._success(
                    channel,
                    "Web message delivered.",
                    username=username,
                    session_id=str(session.id),
                )
            else:
                result = ChannelDeliveryService._failed(channel, f"Channel '{channel}' is not supported by unified delivery.", status="denied")
        except Exception as exc:
            logger.warning("[ChannelDelivery] Text delivery failed via %s: %s", channel, exc)
            result = ChannelDeliveryService._failed(channel, str(exc), retryable=True)

        await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
        return result

    @staticmethod
    async def send_file(
        *,
        db: AsyncSession,
        agent_id: uuid.UUID,
        reply_target: dict[str, Any] | None,
        file_path: str | Path,
        message: str = "",
        delivery_mode: str = "live",
        extra_detail: dict[str, Any] | None = None,
    ) -> DeliveryResult:
        target = ChannelDeliveryService.normalize_reply_target(reply_target)
        if not target:
            result = ChannelDeliveryService._failed("unknown", "No reply target available for channel file delivery.", status="unavailable")
            await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=reply_target, extra_detail=extra_detail)
            return result

        channel = target["channel"]
        config = await ChannelDeliveryService._load_config(db, agent_id, channel)
        if not config or not getattr(config, "is_configured", False):
            result = ChannelDeliveryService._failed(channel, f"{channel} channel is not configured for this agent.", status="unavailable")
            await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
            return result

        path = Path(file_path)
        if not path.exists():
            result = ChannelDeliveryService._failed(channel, f"File not found: {path}", status="failed")
            await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
            return result

        try:
            if channel == "feishu":
                from app.services.feishu_service import FeishuService

                receive_id = target.get("receive_id") or target.get("open_id") or target.get("chat_id")
                receive_id_type = target.get("receive_id_type") or ("chat_id" if target.get("chat_type") == "group" else "open_id")
                if not receive_id:
                    raise ValueError("missing receive_id")
                await FeishuService().upload_and_send_file(
                    config.app_id,
                    config.app_secret,
                    receive_id,
                    path,
                    receive_id_type=receive_id_type,
                    accompany_msg=message,
                )
                result = ChannelDeliveryService._success(channel, "Feishu file delivered.", file_name=path.name)
            elif channel == "telegram":
                from app.api.telegram import _send_telegram_file

                chat_id = target.get("chat_id")
                if chat_id in (None, ""):
                    raise ValueError("missing chat_id")
                await _send_telegram_file(config.app_secret, chat_id, path, message)
                result = ChannelDeliveryService._success(channel, "Telegram file delivered.", file_name=path.name, chat_id=chat_id)
            elif channel == "wechat_personal":
                from app.services.wechat_ilink_client import (
                    ILinkClient,
                    MEDIA_TYPE_FILE,
                    MEDIA_TYPE_IMAGE,
                    MEDIA_TYPE_VIDEO,
                )
                from app.services.wechat_personal_service import get_channel_credentials, get_context_token

                creds = get_channel_credentials(config) or {}
                base_url = creds.get("base_url")
                bot_token = creds.get("bot_token")
                to_user_id = str(target.get("to_user_id") or "").strip()
                context_token = target.get("context_token") or await get_context_token(agent_id, to_user_id)
                if not to_user_id:
                    raise ValueError("missing to_user_id")
                if not context_token:
                    result = ChannelDeliveryService._failed(
                        channel,
                        "WeChat personal deferred file delivery requires a valid context token.",
                        status="unavailable",
                        to_user_id=to_user_id,
                    )
                    await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
                    return result
                mime = mimetypes.guess_type(str(path))[0] or ""
                media_type = MEDIA_TYPE_IMAGE if mime.startswith("image/") else MEDIA_TYPE_VIDEO if mime.startswith("video/") else MEDIA_TYPE_FILE
                client = ILinkClient(base_url)
                if message:
                    await client.send_message(bot_token=bot_token, to_user_id=to_user_id, context_token=context_token, text=message)
                upload = await client.upload_media(
                    bot_token=bot_token,
                    to_user_id=to_user_id,
                    file_data=path.read_bytes(),
                    media_type=media_type,
                )
                await client.send_media_message(
                    bot_token=bot_token,
                    to_user_id=to_user_id,
                    context_token=context_token,
                    upload=upload,
                    media_type=media_type,
                    file_name=path.name,
                )
                result = ChannelDeliveryService._success(channel, "WeChat personal file delivered.", file_name=path.name, to_user_id=to_user_id)
            else:
                result = ChannelDeliveryService._failed(channel, f"Channel '{channel}' is not supported by unified file delivery.", status="denied")
        except Exception as exc:
            logger.warning("[ChannelDelivery] File delivery failed via %s: %s", channel, exc)
            result = ChannelDeliveryService._failed(channel, str(exc), retryable=True, file_name=path.name)

        await ChannelDeliveryService._log_result(agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail)
        return result
