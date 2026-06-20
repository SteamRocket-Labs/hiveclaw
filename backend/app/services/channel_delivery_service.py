"""Unified channel delivery service for live and deferred replies."""

from __future__ import annotations

import json
import mimetypes
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_config import ChannelConfig
from app.services.activity_logger import log_activity
from app.services.outbound_privacy import redact_outbound
from app.services.principal_context import PrincipalStack

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
        connected = bool(
            getattr(connected_config, "is_configured", False) and getattr(connected_config, "is_connected", True)
        )
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
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": True,
                    "deferred_text": True,
                    "deferred_file": True,
                    "on_message_current_sender": True,
                    "on_message_by_name": True,
                }
            )
        elif channel == "telegram":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": True,
                    "deferred_text": True,
                    "deferred_file": True,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("Telegram 仅支持回当前会话，不支持按人名主动寻址。")
        elif channel == "wecom":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": False,
                    "outbound_file": False,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("WeCom 当前仅承诺文本闭环；文件回发仍显式标记为 unsupported。")
        elif channel == "slack":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": True,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("Slack 延迟回投使用保存的 channel_id，仅承诺当前会话文本闭环。")
        elif channel == "dingtalk":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": False,
                    "outbound_file": False,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("DingTalk 延迟回投依赖保存的 session_webhook。")
        elif channel == "discord":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": False,
                    "outbound_file": False,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("Discord 延迟回投使用 interaction follow-up token。")
        elif channel == "microsoft_teams":
            base["official_api"] = True
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": False,
                    "outbound_file": False,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("Microsoft Teams 延迟回投使用保存的 conversation_id 和 reply activity。")
        elif channel == "wechat_personal":
            base["official_api"] = False
            base["third_party_transport"] = "ilink"
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": True,
                    "deferred_text": "conditional",
                    "deferred_file": "conditional",
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
            base["limitations"].append("个人微信延迟回投依赖近期会话 context token，有效期过后不可保证发送。")
        elif channel == "web":
            base["capabilities"].update(
                {
                    "live_text": True,
                    "inbound_file": True,
                    "outbound_file": False,
                    "deferred_text": True,
                    "deferred_file": False,
                    "on_message_current_sender": True,
                    "on_message_by_name": False,
                }
            )
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
        if channel == "teams":
            channel = "microsoft_teams"
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
        if channel == "slack":
            channel_id = str(target.get("channel_id") or "").strip()
            sender_id = str(target.get("sender_id") or "").strip()
            if channel_id and sender_id:
                return f"slack:{channel_id}:{sender_id}"
            return f"slack:{channel_id}" if channel_id else ""
        if channel == "dingtalk":
            conversation_id = str(target.get("conversation_id") or "").strip()
            sender_staff_id = str(target.get("sender_staff_id") or "").strip()
            if conversation_id and sender_staff_id:
                return f"dingtalk:{conversation_id}:{sender_staff_id}"
            webhook = str(target.get("session_webhook") or "").strip()
            return f"dingtalk:{webhook}" if webhook else ""
        if channel == "discord":
            channel_id = str(target.get("channel_id") or "").strip()
            sender_id = str(target.get("sender_id") or "").strip()
            if channel_id and sender_id:
                return f"discord:{channel_id}:{sender_id}"
            interaction_token = str(target.get("interaction_token") or "").strip()
            return f"discord:{interaction_token}" if interaction_token else ""
        if channel == "microsoft_teams":
            conversation_id = str(target.get("conversation_id") or "").strip()
            sender_id = str(target.get("sender_id") or target.get("recipient_id") or "").strip()
            if conversation_id and sender_id:
                return f"microsoft_teams:{conversation_id}:{sender_id}"
            return f"microsoft_teams:{conversation_id}" if conversation_id else ""
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
    def _failed(
        channel: str, message: str, *, status: str = "failed", retryable: bool = False, **detail: Any
    ) -> DeliveryResult:
        return DeliveryResult(
            ok=False, status=status, channel=channel, message=message, retryable=retryable, detail=detail
        )

    @staticmethod
    def _agent_relative_file_path(agent_id: uuid.UUID, path: Path) -> str:
        from app.config import get_settings

        base = (Path(get_settings().AGENT_DATA_DIR) / str(agent_id)).resolve()
        try:
            return str(path.resolve().relative_to(base))
        except ValueError:
            return path.name

    @staticmethod
    async def _send_wechat_file_fallback_link(
        *,
        agent_id: uuid.UUID,
        config: ChannelConfig | Any,
        target: dict[str, Any],
        path: Path,
        message: str,
        delivery_error: Exception,
    ) -> DeliveryResult:
        from app.services.file_download_tokens import build_channel_file_download_url
        from app.services.wechat_ilink_client import ILinkClient, TEXT_MESSAGE_MAX_LEN
        from app.services.wechat_personal_service import get_channel_credentials, get_context_token

        creds = get_channel_credentials(config) or {}
        base_url = creds.get("base_url")
        bot_token = creds.get("bot_token")
        to_user_id = str(target.get("to_user_id") or "").strip()
        context_token = target.get("context_token") or await get_context_token(agent_id, to_user_id)
        if not to_user_id:
            raise ValueError("missing to_user_id")
        if not context_token:
            raise ValueError("missing context_token for WeChat fallback link")
        if not bot_token:
            raise ValueError("missing bot_token for WeChat fallback link")

        rel_path = ChannelDeliveryService._agent_relative_file_path(agent_id, path)
        download_url = build_channel_file_download_url(
            agent_id=agent_id,
            path=rel_path,
            expires_delta=timedelta(hours=24),
        )
        parts = []
        if message:
            parts.append(message)
        parts.extend(
            [
                "微信文件直传失败，已生成备用下载链接。",
                f"文件：{path.name}",
                f"下载链接（24小时有效）：{download_url}",
                "直传错误：微信文件通道暂时不可用。",
            ]
        )
        await ILinkClient(base_url).send_message(
            bot_token=bot_token,
            to_user_id=to_user_id,
            context_token=context_token,
            text="\n".join(parts)[:TEXT_MESSAGE_MAX_LEN],
        )
        return ChannelDeliveryService._success(
            "wechat_personal",
            "WeChat personal file fallback link delivered.",
            file_name=path.name,
            to_user_id=to_user_id,
            fallback_used=True,
        )

    @staticmethod
    async def send_text(
        *,
        db: AsyncSession,
        agent_id: uuid.UUID,
        reply_target: dict[str, Any] | None,
        text: str,
        delivery_mode: str = "live",
        extra_detail: dict[str, Any] | None = None,
        principal_stack: PrincipalStack | None = None,
    ) -> DeliveryResult:
        target = ChannelDeliveryService.normalize_reply_target(reply_target)
        if not target:
            result = ChannelDeliveryService._failed(
                "unknown", "No reply target available for channel delivery.", status="unavailable"
            )
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=reply_target, extra_detail=extra_detail
            )
            return result

        channel = target["channel"]

        redact_decision = redact_outbound(text, channel=channel, principal_stack=principal_stack)
        redact_detail = {
            "outbound_sensitivity": redact_decision.sensitivity.value,
            "outbound_redact_reason": redact_decision.reason,
        }
        if redact_decision.rejected:
            result = ChannelDeliveryService._failed(
                channel,
                redact_decision.reason or "Outbound content blocked by privacy gate.",
                status="denied",
                **redact_detail,
            )
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
            )
            return result
        text = redact_decision.text

        config = None if channel == "web" else await ChannelDeliveryService._load_config(db, agent_id, channel)
        if channel != "web" and (not config or not getattr(config, "is_configured", False)):
            result = ChannelDeliveryService._failed(
                channel, f"{channel} channel is not configured for this agent.", status="unavailable"
            )
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
            )
            return result

        try:
            if channel == "feishu":
                from app.services.feishu_service import FeishuService

                receive_id = target.get("receive_id") or target.get("open_id") or target.get("chat_id")
                receive_id_type = target.get("receive_id_type") or (
                    "chat_id" if target.get("chat_type") == "group" else "open_id"
                )
                if not receive_id:
                    raise ValueError("missing receive_id")
                payload = json.dumps({"text": text})
                service = FeishuService()
                await service.send_message(
                    config.app_id,
                    config.app_secret,
                    receive_id,
                    "text",
                    payload,
                    receive_id_type=receive_id_type,
                    extra_config=config.extra_config,
                )
                result = ChannelDeliveryService._success(
                    channel, "Feishu message delivered.", receive_id=receive_id, receive_id_type=receive_id_type
                )
            elif channel == "telegram":
                from app.api.telegram import _send_telegram_message

                chat_id = target.get("chat_id")
                if chat_id in (None, ""):
                    raise ValueError("missing chat_id")
                await _send_telegram_message(config.app_secret, chat_id, text)
                result = ChannelDeliveryService._success(channel, "Telegram message delivered.", chat_id=chat_id)
            elif channel == "slack":
                from app.api.slack import _send_slack_messages

                channel_id = str(target.get("channel_id") or "").strip()
                if not channel_id:
                    raise ValueError("missing channel_id")
                await _send_slack_messages(str(config.app_secret or ""), channel_id, text)
                result = ChannelDeliveryService._success(channel, "Slack message delivered.", channel_id=channel_id)
            elif channel == "dingtalk":
                import httpx

                session_webhook = str(target.get("session_webhook") or "").strip()
                if not session_webhook:
                    raise ValueError("missing session_webhook")
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        session_webhook,
                        json={"msgtype": "markdown", "markdown": {"title": "AI Reply", "text": text}},
                    )
                result = ChannelDeliveryService._success(
                    channel, "DingTalk message delivered.", session_webhook=session_webhook
                )
            elif channel == "discord":
                from app.api.discord_bot import _send_discord_followup

                interaction_token = str(target.get("interaction_token") or "").strip()
                if not interaction_token:
                    raise ValueError("missing interaction_token")
                await _send_discord_followup(
                    str(config.app_id or ""), str(config.app_secret or ""), interaction_token, text
                )
                result = ChannelDeliveryService._success(
                    channel, "Discord message delivered.", interaction_token=interaction_token
                )
            elif channel == "microsoft_teams":
                from app.api.teams import _send_teams_message

                conversation_id = str(target.get("conversation_id") or "").strip()
                if not conversation_id:
                    raise ValueError("missing conversation_id")
                reply_to_id = str(target.get("reply_to_id") or target.get("activity_id") or "").strip()
                recipient_id = str(target.get("recipient_id") or target.get("sender_id") or "").strip()
                recipient_name = str(target.get("recipient_name") or target.get("user_label") or "").strip()
                bot_id = str(target.get("bot_id") or target.get("recipient_bot_id") or config.app_id or "").strip()
                activity = {
                    "type": "message",
                    "from": {"id": bot_id} if bot_id else {},
                    "conversation": {"id": conversation_id},
                    "recipient": {"id": recipient_id, "name": recipient_name} if recipient_id else {},
                    "text": text,
                }
                if reply_to_id:
                    activity["replyToId"] = reply_to_id
                await _send_teams_message(config, conversation_id, activity)
                result = ChannelDeliveryService._success(
                    channel, "Microsoft Teams message delivered.", conversation_id=conversation_id
                )
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
                    await ChannelDeliveryService._log_result(
                        agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
                    )
                    return result
                client = ILinkClient(base_url)
                await client.send_message(
                    bot_token=bot_token, to_user_id=to_user_id, context_token=context_token, text=text
                )
                result = ChannelDeliveryService._success(
                    channel, "WeChat personal message delivered.", to_user_id=to_user_id
                )
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
                result = ChannelDeliveryService._success(
                    channel, "WeCom message delivered.", user_id=to_user, agent_id=wecom_agent_id
                )
            elif channel == "web":
                from app.api.websocket import manager as ws_manager
                from app.models.agent import Agent as AgentModel
                from app.models.audit import ChatMessage
                from app.models.chat_session import ChatSession
                from app.models.user import User as UserModel
                from app.services.web_session_contract import apply_web_session_contract

                username = str(target.get("username") or "").strip()
                if not username:
                    raise ValueError("missing username")

                user_result = await db.execute(select(UserModel).where(UserModel.username == username))
                target_user = user_result.scalar_one_or_none()
                if not target_user:
                    raise ValueError(f"unknown web user: {username}")

                session_result = await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == agent_id,
                        ChatSession.user_id == target_user.id,
                        ChatSession.source_channel == "web",
                    )
                    .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
                    .limit(1)
                )
                session = session_result.scalar_one_or_none()
                # RLS 阶段2b: chat_sessions + chat_messages are USING-only — stamp
                # tenant_id so these delivery rows aren't globally visible under
                # the non-owner role. The recipient shares the agent's tenant.
                _web_tenant_id = target_user.tenant_id
                if not session:
                    session = ChatSession(
                        agent_id=agent_id,
                        tenant_id=_web_tenant_id,
                        user_id=target_user.id,
                        title=f"[Web Message] {username}",
                        source_channel="web",
                    )
                    db.add(session)
                    await db.flush()
                await apply_web_session_contract(db, session=session, agent_id=agent_id, user=target_user)
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=_web_tenant_id,
                        user_id=target_user.id,
                        role="assistant",
                        content=text,
                        conversation_id=str(session.id),
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
                            logger.debug(f"[ChannelDelivery] Web push suppressed: {exc}")
                result = ChannelDeliveryService._success(
                    channel,
                    "Web message delivered.",
                    username=username,
                    session_id=str(session.id),
                )
            else:
                result = ChannelDeliveryService._failed(
                    channel, f"Channel '{channel}' is not supported by unified delivery.", status="denied"
                )
        except Exception as exc:
            logger.warning(f"[ChannelDelivery] Text delivery failed via {channel}: {exc}")
            result = ChannelDeliveryService._failed(channel, str(exc), retryable=True)

        await ChannelDeliveryService._log_result(
            agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
        )
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
            result = ChannelDeliveryService._failed(
                "unknown", "No reply target available for channel file delivery.", status="unavailable"
            )
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=reply_target, extra_detail=extra_detail
            )
            return result

        channel = target["channel"]
        config = await ChannelDeliveryService._load_config(db, agent_id, channel)
        if not config or not getattr(config, "is_configured", False):
            result = ChannelDeliveryService._failed(
                channel, f"{channel} channel is not configured for this agent.", status="unavailable"
            )
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
            )
            return result

        path = Path(file_path)
        if not path.exists():
            result = ChannelDeliveryService._failed(channel, f"File not found: {path}", status="failed")
            await ChannelDeliveryService._log_result(
                agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
            )
            return result

        try:
            if channel == "feishu":
                from app.services.feishu_service import FeishuService

                receive_id = target.get("receive_id") or target.get("open_id") or target.get("chat_id")
                receive_id_type = target.get("receive_id_type") or (
                    "chat_id" if target.get("chat_type") == "group" else "open_id"
                )
                if not receive_id:
                    raise ValueError("missing receive_id")
                await FeishuService().upload_and_send_file(
                    config.app_id,
                    config.app_secret,
                    receive_id,
                    path,
                    receive_id_type=receive_id_type,
                    accompany_msg=message,
                    extra_config=config.extra_config,
                )
                result = ChannelDeliveryService._success(channel, "Feishu file delivered.", file_name=path.name)
            elif channel == "telegram":
                from app.api.telegram import _send_telegram_file

                chat_id = target.get("chat_id")
                if chat_id in (None, ""):
                    raise ValueError("missing chat_id")
                await _send_telegram_file(config.app_secret, chat_id, path, message)
                result = ChannelDeliveryService._success(
                    channel, "Telegram file delivered.", file_name=path.name, chat_id=chat_id
                )
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
                    await ChannelDeliveryService._log_result(
                        agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
                    )
                    return result
                mime = mimetypes.guess_type(str(path))[0] or ""
                media_type = (
                    MEDIA_TYPE_IMAGE
                    if mime.startswith("image/")
                    else MEDIA_TYPE_VIDEO
                    if mime.startswith("video/")
                    else MEDIA_TYPE_FILE
                )
                client = ILinkClient(base_url)
                if message:
                    await client.send_message(
                        bot_token=bot_token, to_user_id=to_user_id, context_token=context_token, text=message
                    )
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
                result = ChannelDeliveryService._success(
                    channel, "WeChat personal file delivered.", file_name=path.name, to_user_id=to_user_id
                )
            else:
                result = ChannelDeliveryService._failed(
                    channel, f"Channel '{channel}' is not supported by unified file delivery.", status="denied"
                )
        except Exception as exc:
            logger.warning(f"[ChannelDelivery] File delivery failed via {channel}: {exc}")
            if channel == "wechat_personal":
                try:
                    result = await ChannelDeliveryService._send_wechat_file_fallback_link(
                        agent_id=agent_id,
                        config=config,
                        target=target,
                        path=path,
                        message=message,
                        delivery_error=exc,
                    )
                except Exception as fallback_exc:
                    logger.warning(f"[ChannelDelivery] WeChat file fallback failed: {fallback_exc}")
                    result = ChannelDeliveryService._failed(
                        channel,
                        f"{exc}; fallback failed: {fallback_exc}",
                        retryable=True,
                        file_name=path.name,
                    )
            else:
                result = ChannelDeliveryService._failed(channel, str(exc), retryable=True, file_name=path.name)

        await ChannelDeliveryService._log_result(
            agent_id, result, delivery_mode=delivery_mode, reply_target=target, extra_detail=extra_detail
        )
        return result
