"""Personal WeChat (iLink) long-poll message streaming manager.

Manages persistent long-poll connections to iLink's getupdates API
for all connected wechat_personal channels. Follows the same pattern
as WeComStreamManager / FeishuWSManager.

Lifecycle:
  start_all()  → called at app startup, loads all connected channels from DB
  start_client(agent_id, bot_token, base_url) → starts poll loop for one agent
  stop_client(agent_id) → cancels poll loop
  stop_all()   → called at app shutdown
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.config import get_settings
from app.models.channel_config import ChannelConfig
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.wechat_ilink_client import (
    ERROR_BACKOFF_SECONDS,
    ILinkClient,
    ILinkSessionExpiredError,
    MAX_CONSECUTIVE_ERRORS,
    InboundMessage,
)
from app.services.wechat_personal_service import (
    get_channel_credentials,
    get_context_token,
    get_sync_buf,
    store_context_token,
    store_sync_buf,
    store_typing_ticket,
)


def _safe_upload_name(filename: str | None, fallback: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (filename or fallback)).strip("._")
    return safe or fallback


def _persist_inbound_media(agent_id: uuid.UUID, filename: str | None, content: bytes, fallback: str) -> str:
    uploads_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_name(filename, fallback)
    destination = uploads_dir / safe_name
    if destination.exists():
        destination = uploads_dir / f"{destination.stem}_{uuid.uuid4().hex[:8]}{destination.suffix}"
    destination.write_bytes(content)
    return f"workspace/uploads/{destination.name}"


def _guess_image_mime(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def _enqueue_wechat_personal_message(
    *,
    agent_id: uuid.UUID,
    provider_event_id: str,
    sender_id: str,
    user_text: str,
    delivery_target: dict,
) -> str:
    from app.services.channel_ingress_inbox import (
        accept_authenticated_channel_event,
        channel_installation_ref,
        wait_for_channel_ingress_result,
    )

    tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tenant_id) as db:
        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wechat_personal",
                )
            )
        ).scalar_one_or_none()
        if config is None:
            raise RuntimeError("WeChat Personal installation no longer exists")
        receipt = await accept_authenticated_channel_event(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            provider="wechat_personal",
            installation_ref=channel_installation_ref(config, fallback=f"wechat_personal:{agent_id}"),
            provider_event_id=provider_event_id,
            handler_key="wechat_personal.stream_message",
            body={
                "sender_id": sender_id,
                "user_text": user_text,
                "delivery_target": delivery_target,
            },
            metadata={"transport": "long_poll"},
        )
    result = await wait_for_channel_ingress_result(
        tenant_id=tenant_id,
        event_id=receipt.event_id,
    )
    return str(result.get("reply_text") or "消息已接收。")


class WeChatPersonalStreamManager:
    """Manages iLink long-poll clients for all wechat_personal channels."""

    def __init__(self):
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    async def start_all(self) -> None:
        """Load all connected wechat_personal channels and start their poll loops."""
        try:
            async with (
                async_session() as db,
                enter_rls_bypass(
                    db, reason="wechat_personal start_all — enumerate all connected channels across tenants"
                ),
            ):
                result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.channel_type == "wechat_personal",
                        ChannelConfig.is_connected.is_(True),
                    )
                )
                configs = result.scalars().all()

            started = 0
            for config in configs:
                creds = get_channel_credentials(config)
                if not creds or not creds["bot_token"]:
                    logger.warning(f"[WeChatPersonal Stream] No credentials for agent {config.agent_id}")
                    continue
                await self.start_client(
                    agent_id=config.agent_id,
                    bot_token=creds["bot_token"],
                    base_url=creds["base_url"],
                )
                started += 1

            if started:
                logger.info(f"[WeChatPersonal Stream] Started {started} client(s)")
        except Exception as e:
            logger.error(f"[WeChatPersonal Stream] start_all failed: {e}")

    async def start_client(
        self,
        agent_id: uuid.UUID,
        bot_token: str,
        base_url: str,
        stop_existing: bool = True,
    ) -> None:
        """Start the long-poll loop for a single agent."""
        if stop_existing:
            await self.stop_client(agent_id)

        task = asyncio.create_task(
            self._run_poll_loop(agent_id, bot_token, base_url),
            name=f"wechat-personal-{str(agent_id)[:8]}",
        )
        self._tasks[agent_id] = task
        logger.info(f"[WeChatPersonal Stream] Client started for agent {agent_id}")

    async def stop_client(self, agent_id: uuid.UUID) -> None:
        """Stop the poll loop for a single agent."""
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug(f"[WeChatPersonal Stream] Client cancelled for agent {agent_id}")

    async def stop_all(self) -> None:
        """Stop all active poll loops (called at shutdown)."""
        agent_ids = list(self._tasks.keys())
        for agent_id in agent_ids:
            await self.stop_client(agent_id)
        logger.info(f"[WeChatPersonal Stream] All clients stopped ({len(agent_ids)})")

    async def _run_poll_loop(
        self,
        agent_id: uuid.UUID,
        bot_token: str,
        base_url: str,
    ) -> None:
        """Main long-poll loop — runs until cancelled or session expires."""
        client = ILinkClient(base_url)
        sync_buf = await get_sync_buf(agent_id)
        consecutive_errors = 0
        total_backoff_cycles = 0
        max_backoff_cycles = 10  # after 10 × 30s backoffs, give up

        logger.info(f"[WeChatPersonal Stream] Poll loop started for agent {agent_id}")

        try:
            while True:
                try:
                    result = await client.get_updates(bot_token, sync_buf)

                    # Persist sync buffer for continuity across restarts
                    if result.sync_buf != sync_buf:
                        sync_buf = result.sync_buf
                        await store_sync_buf(agent_id, sync_buf)

                    # Process messages
                    for msg in result.messages:
                        await self._handle_message(agent_id, bot_token, base_url, msg)

                    consecutive_errors = 0

                except ILinkSessionExpiredError:
                    logger.warning(
                        f"[WeChatPersonal Stream] Session expired for agent {agent_id}, marking disconnected"
                    )
                    await self._mark_disconnected(agent_id)
                    return

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    consecutive_errors += 1
                    logger.error(
                        f"[WeChatPersonal Stream] Poll error for agent {agent_id} "
                        f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}"
                    )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        total_backoff_cycles += 1
                        if total_backoff_cycles >= max_backoff_cycles:
                            logger.error(
                                f"[WeChatPersonal Stream] Persistent errors for agent {agent_id} "
                                f"({total_backoff_cycles} backoff cycles), marking disconnected"
                            )
                            await self._mark_disconnected(agent_id)
                            return
                        logger.error(
                            f"[WeChatPersonal Stream] Backoff cycle {total_backoff_cycles}/{max_backoff_cycles} "
                            f"for agent {agent_id}, sleeping {ERROR_BACKOFF_SECONDS}s"
                        )
                        await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                        consecutive_errors = 0
                    else:
                        await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"[WeChatPersonal Stream] Poll loop cancelled for agent {agent_id}")

    async def _handle_message(
        self,
        agent_id: uuid.UUID,
        bot_token: str,
        base_url: str,
        msg: InboundMessage,
    ) -> None:
        """Process a single inbound WeChat message (text, image, file, voice, video)."""
        from_user = msg.from_user_id

        # Determine user-facing text from all possible sources
        user_text = msg.text.strip()

        # Voice messages: use transcription as text
        if not user_text and msg.voice_text:
            user_text = msg.voice_text.strip()
            if user_text:
                logger.info(f"[WeChatPersonal Stream] Voice transcription from {from_user[:12]}...: {user_text[:80]}")

        # Image: download, describe for LLM
        if msg.image_media:
            try:
                image_data = await ILinkClient(base_url).download_media(msg.image_media)
                image_path = _persist_inbound_media(
                    agent_id, None, image_data, f"wechat_image_{uuid.uuid4().hex[:8]}.jpg"
                )
                image_desc = (
                    f"[用户发送了一张图片，已保存到工作区 `{image_path}`，{len(image_data)} 字节。"
                    f"如果需要处理内容，请直接读取该路径。]"
                )
                image_marker = (
                    f"[image_data:data:{_guess_image_mime(image_data)};base64,"
                    f"{base64.b64encode(image_data).decode('ascii')}]"
                )
                image_desc = f"{image_desc}\n{image_marker}"
                user_text = f"{user_text}\n{image_desc}" if user_text else image_desc
                logger.info(f"[WeChatPersonal Stream] Image from {from_user[:12]}...: {len(image_data)}B")
            except Exception as e:
                logger.error(f"[WeChatPersonal Stream] Image download failed: {e}")
                user_text = user_text or "[用户发送了一张图片，下载失败]"

        # File: download, note filename
        if msg.file_media and msg.file_name:
            try:
                file_data = await ILinkClient(base_url).download_media(msg.file_media)
                file_path = _persist_inbound_media(
                    agent_id, msg.file_name, file_data, f"wechat_file_{uuid.uuid4().hex[:8]}"
                )
                file_desc = (
                    f"[用户发送了文件: {msg.file_name}，已保存到工作区 `{file_path}`，{len(file_data)} 字节。"
                    f"如果需要处理内容，请直接读取该路径。]"
                )
                user_text = f"{user_text}\n{file_desc}" if user_text else file_desc
                logger.info(
                    f"[WeChatPersonal Stream] File from {from_user[:12]}...: {msg.file_name} ({len(file_data)}B)"
                )
            except Exception as e:
                logger.error(f"[WeChatPersonal Stream] File download failed: {e}")
                user_text = user_text or f"[用户发送了文件: {msg.file_name}，下载失败]"

        # Video: note receipt
        if msg.video_media:
            try:
                video_data = await ILinkClient(base_url).download_media(msg.video_media)
                video_path = _persist_inbound_media(
                    agent_id, None, video_data, f"wechat_video_{uuid.uuid4().hex[:8]}.mp4"
                )
                video_desc = f"[用户发送了一段视频，已保存到工作区 `{video_path}`。如需处理内容，请直接读取该路径。]"
            except Exception as e:
                logger.error(f"[WeChatPersonal Stream] Video download failed: {e}")
                video_desc = "[用户发送了一段视频，下载失败]"
            user_text = f"{user_text}\n{video_desc}" if user_text else video_desc

        if not user_text:
            return

        logger.info(f"[WeChatPersonal Stream] Message from {from_user[:12]}...: {user_text[:80]}")

        # Cache context_token (MUST be echoed in replies)
        if msg.context_token:
            await store_context_token(agent_id, from_user, msg.context_token)

        delivery_target = {
            "channel": "wechat_personal",
            "to_user_id": from_user,
            "context_token": msg.context_token,
            "context_token_obtained_at": datetime.now(timezone.utc).isoformat(),
        }

        # Fetch and cache typing ticket on first contact
        try:
            from app.services.wechat_personal_service import get_typing_ticket

            ticket = await get_typing_ticket(agent_id, from_user)
            if not ticket:
                config = await ILinkClient(base_url).get_config(bot_token, from_user)
                if config.typing_ticket:
                    await store_typing_ticket(agent_id, from_user, config.typing_ticket)
                    ticket = config.typing_ticket
        except Exception as e:
            logger.debug(f"[WeChatPersonal Stream] typing ticket fetch failed: {e}")
            ticket = None

        # Send typing indicator
        if ticket:
            try:
                await ILinkClient(base_url).send_typing(bot_token, from_user, ticket)
            except Exception as e:
                logger.debug(f"[WeChatPersonal Stream] typing indicator failed: {e}")

        # Register channel_file_sender so send_channel_file tool goes via iLink CDN
        import mimetypes as _mt
        from pathlib import Path as _P

        from app.services.agent_tools import channel_file_sender as _cfs
        from app.services.channel_delivery_service import channel_delivery_target as _cdt
        from app.services.wechat_ilink_client import (
            MEDIA_TYPE_FILE as _MT_FILE,
            MEDIA_TYPE_IMAGE as _MT_IMG,
            MEDIA_TYPE_VIDEO as _MT_VID,
        )

        async def _wechat_file_sender(file_path, accompany_msg: str = ""):
            fp = _P(file_path)
            if not fp.exists():
                raise FileNotFoundError(f"File not found: {fp}")

            # Determine media type from MIME
            mime, _ = _mt.guess_type(str(fp))
            mime = mime or ""
            if mime.startswith("image/"):
                media_type = _MT_IMG
            elif mime.startswith("video/"):
                media_type = _MT_VID
            else:
                media_type = _MT_FILE

            # Fetch fresh context_token for this user
            ctx_token = await get_context_token(agent_id, from_user) or msg.context_token

            client = ILinkClient(base_url)
            # Optional caption as separate text message
            if accompany_msg:
                try:
                    await client.send_message(
                        bot_token=bot_token,
                        to_user_id=from_user,
                        context_token=ctx_token,
                        text=accompany_msg,
                    )
                except Exception as e:
                    logger.warning(f"[WeChatPersonal Stream] caption send failed: {e}")

            # Upload file to iLink CDN
            try:
                file_data = fp.read_bytes()
                logger.info(
                    f"[WeChatPersonal Stream] Uploading {fp.name} ({len(file_data)} bytes, type={media_type})..."
                )
                upload = await client.upload_media(
                    bot_token=bot_token,
                    to_user_id=from_user,
                    file_data=file_data,
                    media_type=media_type,
                )
                logger.info("[WeChatPersonal Stream] Upload complete, sending media message...")
                await client.send_media_message(
                    bot_token=bot_token,
                    to_user_id=from_user,
                    context_token=ctx_token,
                    upload=upload,
                    media_type=media_type,
                    file_name=fp.name,
                )
                logger.info(f"[WeChatPersonal Stream] File sent via iLink: {fp.name}")
            except Exception as e:
                logger.error(f"[WeChatPersonal Stream] File send failed for {fp.name}: {e}", exc_info=True)
                raise

        token_cfs = _cfs.set(_wechat_file_sender)
        token_cdt = _cdt.set(delivery_target)

        # Process message through LLM pipeline
        try:
            reply_text = await _enqueue_wechat_personal_message(
                agent_id=agent_id,
                provider_event_id=str(msg.message_id or ""),
                sender_id=from_user,
                user_text=user_text,
                delivery_target=delivery_target,
            )
        except Exception as e:
            logger.error(f"[WeChatPersonal Stream] LLM processing failed: {e}")
            reply_text = "抱歉，处理消息时出现错误，请稍后再试。"
        finally:
            _cfs.reset(token_cfs)
            _cdt.reset(token_cdt)

        # ILinkClient owns transport-safe chunking so every caller preserves the
        # same complete model-authored message.
        context_token = await get_context_token(agent_id, from_user) or msg.context_token
        ilink = ILinkClient(base_url)
        try:
            await ilink.send_message(
                bot_token=bot_token,
                to_user_id=from_user,
                context_token=context_token,
                text=reply_text,
            )
        except Exception as e:
            logger.error(f"[WeChatPersonal Stream] Failed to send reply: {e}")

        # Cancel typing
        if ticket:
            try:
                await ilink.send_typing(bot_token, from_user, ticket, status=2)
            except Exception as e:
                logger.debug(f"[WeChatPersonal Stream] cancel typing failed: {e}")

    async def _mark_disconnected(self, agent_id: uuid.UUID) -> None:
        """Mark channel as disconnected in DB when session expires."""
        try:
            # Stream context — no request GUC. Resolve the owning tenant so the
            # SELECT+UPDATE survives the stage-3 non-owner role flip (a bare
            # session fail-closes and would silently skip the disconnect mark).
            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "wechat_personal",
                    )
                )
                config = result.scalar_one_or_none()
                if config:
                    config.is_connected = False
                    await db.commit()
                    logger.info(f"[WeChatPersonal Stream] Marked agent {agent_id} as disconnected")
        except Exception as e:
            logger.error(f"[WeChatPersonal Stream] Failed to mark disconnected: {e}")


# ── Message processing (follows wecom_stream.py pattern) ─


async def _process_wechat_message(
    agent_id: uuid.UUID,
    sender_id: str,
    user_text: str,
    delivery_target: dict | None = None,
) -> str:
    """Process a WeChat message through the LLM pipeline and return the reply text."""
    from datetime import datetime, timezone

    from sqlalchemy import select as _select

    from app.services.channel_agent_runtime import call_agent_llm
    from app.models.agent import Agent as AgentModel
    from app.models.audit import ChatMessage
    from app.models.channel_config import ChannelConfig
    from app.services.channel_session import find_or_create_channel_session

    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        # Load agent
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            logger.warning(f"[WeChatPersonal Stream] Agent {agent_id} not found")
            return "Agent not found"

        from app.services.memory_service import compute_history_limit_for_agent

        hist_limit = await compute_history_limit_for_agent(agent_id)

        # Conversation ID
        conv_id = f"wechat_p2p_{sender_id}"

        config = (
            await db.execute(
                _select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wechat_personal",
                )
            )
        ).scalar_one_or_none()
        from app.services.channel_ingress_context import current_channel_ingress_context
        from app.services.external_principal_service import resolve_or_create_external_principal

        ingress = current_channel_ingress_context()
        installation_ref = (
            ingress.installation_ref
            if ingress is not None and ingress.provider == "wechat_personal" and ingress.installation_ref
            else str(getattr(config, "id", None) or f"wechat_personal:{agent_id}")
        )
        principal_resolution = await resolve_or_create_external_principal(
            db,
            tenant_id=agent_obj.tenant_id,
            provider="wechat_personal",
            installation_ref=installation_ref,
            channel_config_id=getattr(config, "id", None),
            subject_id=sender_id,
            display_name=f"WeChat {sender_id[:8]}",
            profile={},
        )
        runtime_actor = principal_resolution.actor
        platform_user_id = runtime_actor.id
        external_principal_id = principal_resolution.principal.id
        user_label = runtime_actor.display_name or f"WeChat {sender_id[:8]}"

        from app.core.execution_context import set_delegated_user_identity

        if platform_user_id is not None:
            set_delegated_user_identity(platform_user_id, user_label, channel="wechat_personal")

        delivery_target = dict(
            delivery_target
            or {
                "channel": "wechat_personal",
                "to_user_id": sender_id,
            }
        )
        delivery_target["external_principal_id"] = str(external_principal_id)

        # Find or create session
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            tenant_id=agent_obj.tenant_id,
            user_id=platform_user_id,
            external_principal_id=external_principal_id,
            external_conv_id=conv_id,
            source_channel="wechat_personal",
            first_message_title=user_text,
            delivery_target=delivery_target,
        )
        session_conv_id = str(sess.id)
        delivery_target["user_label"] = delivery_target.get("user_label") or user_label
        delivery_target["session_id"] = session_conv_id
        sess.delivery_target_json = delivery_target

        # Load history
        history_r = await db.execute(
            _select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(hist_limit)
        )
        history = [{"role": m.role, "content": m.content} for m in reversed(history_r.scalars().all())]

        # Save user message
        db.add(
            ChatMessage(
                agent_id=agent_id,
                tenant_id=agent_obj.tenant_id,
                user_id=platform_user_id,
                external_principal_id=external_principal_id,
                role="user",
                content=user_text,
                conversation_id=session_conv_id,
            )
        )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        from app.services.channel_delivery_service import channel_delivery_target as _cdt

        _cdt_token = _cdt.set(delivery_target)
        try:
            # Call LLM (P1-4: pass correct channel attribution)
            reply_text = await call_agent_llm(
                db,
                agent_id,
                user_text,
                history=history,
                user_id=platform_user_id,
                session_id=session_conv_id,
                session_source="wechat_personal",
                session_channel="wechat_personal",
                allow_bare_plan_confirmation=True,
                durable_run=True,
                durable_session=sess,
                durable_user=runtime_actor,
            )
        finally:
            _cdt.reset(_cdt_token)
        logger.info(f"[WeChatPersonal Stream] LLM reply: {reply_text[:100]}")

        # Save assistant reply
        db.add(
            ChatMessage(
                agent_id=agent_id,
                tenant_id=agent_obj.tenant_id,
                user_id=platform_user_id,
                external_principal_id=external_principal_id,
                role="assistant",
                content=reply_text,
                conversation_id=session_conv_id,
            )
        )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        # Log activity
        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "chat_reply",
            f"Replied to WeChat message: {reply_text[:80]}",
            detail={"channel": "wechat_personal", "user_text": user_text[:200], "reply": reply_text[:500]},
        )

    return reply_text


# ── Singleton ────────────────────────────────────────────

wechat_personal_stream_manager = WeChatPersonalStreamManager()
