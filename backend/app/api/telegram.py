"""Telegram Bot Channel API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.api.channel_secrets import resolve_secret_field
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut

router = APIRouter(tags=["telegram"])

TG_API = "https://api.telegram.org"
TG_MSG_LIMIT = 4096  # Telegram message char limit


# ─── Config CRUD ────────────────────────────────────────

@router.post("/agents/{agent_id}/telegram-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_telegram_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Telegram bot for an agent. Fields: bot_token."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "telegram",
        )
    )
    existing = result.scalar_one_or_none()
    bot_token = resolve_secret_field(data, "bot_token", existing.app_secret if existing else None)
    if not bot_token:
        raise HTTPException(status_code=422, detail="bot_token is required")

    if existing:
        existing.app_secret = bot_token
        existing.is_configured = True
        await db.flush()
        # Register webhook with Telegram
        await _register_telegram_webhook(bot_token, agent_id)
        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="telegram",
        app_id="telegram",
        app_secret=bot_token,
        is_configured=True,
    )
    db.add(config)
    await db.flush()
    await _register_telegram_webhook(bot_token, agent_id, data.get("_request"))
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/telegram-channel", response_model=ChannelConfigOut)
async def get_telegram_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "telegram",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Telegram not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.get("/agents/{agent_id}/telegram-channel/webhook-url")
async def get_telegram_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    import os
    from app.models.system_settings import SystemSetting
    public_base = ""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
    setting = result.scalar_one_or_none()
    if setting and setting.value.get("public_base_url"):
        public_base = setting.value["public_base_url"].rstrip("/")
    if not public_base:
        public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        public_base = str(request.base_url).rstrip("/")
    return {"webhook_url": f"{public_base}/api/channel/telegram/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/telegram-channel", status_code=204)
async def delete_telegram_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "telegram",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Telegram not configured")
    # Remove webhook from Telegram
    if config.app_secret:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{TG_API}/bot{config.app_secret}/deleteWebhook")
        except Exception as e:
            logger.warning(f"[Telegram] Failed to delete webhook: {e}")
    await db.delete(config)


# ─── Helpers ────────────────────────────────────────────

async def _register_telegram_webhook(bot_token: str, agent_id: uuid.UUID) -> None:
    """Register webhook URL with Telegram Bot API."""
    import os
    import httpx
    from app.database import async_session
    from app.models.system_settings import SystemSetting

    public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        try:
            async with async_session() as db:
                result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
                setting = result.scalar_one_or_none()
                if setting and setting.value.get("public_base_url"):
                    public_base = setting.value["public_base_url"].rstrip("/")
        except Exception as exc:
            logger.debug("[Telegram] Could not read PUBLIC_BASE_URL from DB: %s", exc)
    if not public_base:
        logger.warning("[Telegram] No PUBLIC_BASE_URL set, cannot register webhook")
        return

    webhook_url = f"{public_base}/api/channel/telegram/{agent_id}/webhook"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{TG_API}/bot{bot_token}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["message"]},
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"[Telegram] Webhook registered: {webhook_url}")
            else:
                logger.error(f"[Telegram] Webhook registration failed: {data}")
    except Exception as e:
        logger.error(f"[Telegram] Failed to register webhook: {e}")


async def _send_telegram_message(bot_token: str, chat_id: int | str, text: str) -> None:
    """Send text to Telegram, splitting into TG_MSG_LIMIT chunks if needed."""
    import httpx
    chunks = [text[i:i + TG_MSG_LIMIT] for i in range(0, len(text), TG_MSG_LIMIT)]
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            await client.post(
                f"{TG_API}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            )


_processed_tg_updates: set[int] = set()


# ─── Webhook Handler ───────────────────────────────────

@router.post("/channel/telegram/{agent_id}/webhook")
async def telegram_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Telegram Bot API webhook updates."""
    import json
    body_bytes = await request.body()
    body = json.loads(body_bytes)

    # Get channel config
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "telegram",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    bot_token = config.app_secret or ""

    # Dedup by update_id
    update_id = body.get("update_id", 0)
    if update_id in _processed_tg_updates:
        return {"ok": True}
    if update_id:
        _processed_tg_updates.add(update_id)
        if len(_processed_tg_updates) > 2000:
            _processed_tg_updates.clear()

    # Extract message
    message = body.get("message")
    if not message:
        return {"ok": True}

    user_text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")
    sender = message.get("from", {})
    sender_id = str(sender.get("id", ""))
    sender_name = (
        sender.get("first_name", "")
        + (" " + sender.get("last_name", "") if sender.get("last_name") else "")
    ).strip() or f"tg_{sender_id}"

    # Ignore empty messages and commands other than /start
    if not user_text:
        return {"ok": True}
    # Handle /start command
    if user_text == "/start":
        from app.models.agent import Agent as AgentModel
        agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        welcome = agent_obj.welcome_message if agent_obj else None
        await _send_telegram_message(
            bot_token, chat_id,
            welcome or f"Hi! I'm {agent_obj.name if agent_obj else 'your assistant'}. Send me a message to get started.",
        )
        return {"ok": True}

    # Strip /ask prefix if present
    if user_text.startswith("/ask "):
        user_text = user_text[5:].strip()

    conv_id = f"tg_{chat_id}_{sender_id}"
    logger.info(f"[Telegram] Message from={sender_name}({sender_id}), chat={chat_id}: {user_text[:80]}")

    # Find-or-create platform user
    from app.models.user import User as _User
    from app.core.security import hash_password as _hp
    import uuid as _uuid2
    _tg_username = f"tg_{sender_id}"
    _u_r = await db.execute(select(_User).where(_User.username == _tg_username))
    _platform_user = _u_r.scalar_one_or_none()
    if not _platform_user:
        from app.models.agent import Agent as AgentModel
        _ag_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
        _ag = _ag_r.scalar_one_or_none()
        _platform_user = _User(
            username=_tg_username,
            email=f"{_tg_username}@telegram.local",
            password_hash=_hp(_uuid2.uuid4().hex),
            display_name=sender_name,
            tenant_id=_ag.tenant_id if _ag else None,
            role="member",
        )
        db.add(_platform_user)
        await db.flush()
    elif sender_name and _platform_user.display_name != sender_name:
        _platform_user.display_name = sender_name
        await db.flush()

    # Find or create chat session
    from app.services.channel_session import find_or_create_channel_session
    from app.models.audit import ChatMessage
    from app.models.agent import Agent as AgentModel

    session = await find_or_create_channel_session(
        db, agent_id, _platform_user.id, conv_id, "telegram",
        first_message_title=f"Telegram: {sender_name}",
    )

    # Save user message
    db.add(ChatMessage(
        agent_id=agent_id,
        conversation_id=str(session.id),
        role="user",
        content=user_text,
        user_id=_platform_user.id,
    ))
    await db.commit()

    # Load history
    from app.services.memory_service import compute_history_limit_for_agent
    _hist_limit = await compute_history_limit_for_agent(agent_id)
    hist_r = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session.id))
        .order_by(ChatMessage.created_at.desc())
        .limit(_hist_limit)
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(hist_r.scalars().all())]

    # Call agent LLM (same function used by Feishu/Slack/DingTalk channels)
    from app.api.feishu import _call_agent_llm

    try:
        reply = await _call_agent_llm(db, agent_id, user_text, history=history)
    except Exception as e:
        logger.error(f"[Telegram] LLM error for {agent_id}: {e}")
        reply = "Sorry, I encountered an error processing your message. Please try again."

    # Save assistant reply
    db.add(ChatMessage(
        agent_id=agent_id,
        conversation_id=str(session.id),
        role="assistant",
        content=reply,
    ))
    await db.commit()

    # Send reply to Telegram
    await _send_telegram_message(bot_token, chat_id, reply)

    return {"ok": True}
