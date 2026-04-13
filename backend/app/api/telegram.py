"""Telegram Bot Channel API routes."""

import hashlib
import hmac
import json
import os
import re
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_secrets import resolve_secret_field
from app.config import get_settings
from app.core.events import get_redis
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut

router = APIRouter(tags=["telegram"])

TG_API = "https://api.telegram.org"
TG_MSG_LIMIT = 4096  # Telegram message char limit
_TG_DEDUP_TTL = 3600  # Redis TTL for update dedup (1 hour)
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


# ─── Helpers ────────────────────────────────────────────


def _compute_webhook_secret(bot_token: str) -> str:
    """Derive a deterministic secret_token for Telegram webhook verification.

    Uses HMAC-SHA256(SECRET_KEY, bot_token) so we never need to store
    the secret separately — it can be recomputed from the config.
    """
    settings = get_settings()
    return hmac.new(
        settings.SECRET_KEY.encode(), bot_token.encode(), hashlib.sha256
    ).hexdigest()[:64]


async def _resolve_public_base_url(
    db: AsyncSession | None = None, request: Request | None = None
) -> str:
    """Resolve the public base URL: env → DB → request fallback."""
    from app.models.system_settings import SystemSetting

    public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if public_base:
        return public_base

    try:
        if db:
            result = await db.execute(
                select(SystemSetting).where(SystemSetting.key == "platform")
            )
            setting = result.scalar_one_or_none()
            if setting and setting.value.get("public_base_url"):
                return setting.value["public_base_url"].rstrip("/")
        else:
            from app.database import async_session

            async with async_session() as session:
                result = await session.execute(
                    select(SystemSetting).where(SystemSetting.key == "platform")
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value.get("public_base_url"):
                    return setting.value["public_base_url"].rstrip("/")
    except Exception as exc:
        logger.debug("[Telegram] Could not read PUBLIC_BASE_URL from DB: %s", exc)

    if request:
        return str(request.base_url).rstrip("/")

    return ""


def _validate_bot_token(token: str) -> None:
    """Validate Telegram bot token format: <numeric_id>:<alphanumeric_secret>."""
    if not _BOT_TOKEN_RE.match(token):
        raise HTTPException(
            status_code=422,
            detail="Invalid bot_token format. Expected: <numeric_id>:<secret>",
        )


async def _register_telegram_webhook(bot_token: str, agent_id: uuid.UUID) -> None:
    """Register webhook URL with Telegram Bot API, including secret_token."""
    public_base = await _resolve_public_base_url()
    if not public_base:
        logger.warning("[Telegram] No PUBLIC_BASE_URL set, cannot register webhook")
        return

    webhook_url = f"{public_base}/api/channel/telegram/{agent_id}/webhook"
    secret_token = _compute_webhook_secret(bot_token)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{TG_API}/bot{bot_token}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message"],
                    "secret_token": secret_token,
                },
            )
            resp_data = resp.json()
            if resp_data.get("ok"):
                logger.info("[Telegram] Webhook registered: %s", webhook_url)
            else:
                logger.error("[Telegram] Webhook registration failed: %s", resp_data)
    except Exception as e:
        logger.error("[Telegram] Failed to register webhook: %s", e)


async def _send_telegram_message(bot_token: str, chat_id: int | str, text: str) -> None:
    """Send text to Telegram, splitting into TG_MSG_LIMIT chunks if needed."""
    chunks = [text[i : i + TG_MSG_LIMIT] for i in range(0, len(text), TG_MSG_LIMIT)]
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            await client.post(
                f"{TG_API}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            )


async def _is_duplicate_update(update_id: int) -> bool:
    """Check and mark a Telegram update_id using Redis for cross-worker dedup."""
    if not update_id:
        return False
    key = f"tg:dedup:{update_id}"
    try:
        r = await get_redis()
        was_set = await r.set(key, "1", ex=_TG_DEDUP_TTL, nx=True)
        return was_set is None  # None → key already existed → duplicate
    except Exception as exc:
        logger.debug("[Telegram] Redis dedup unavailable, allowing through: %s", exc)
        return False


# ─── Config CRUD ────────────────────────────────────────


@router.post(
    "/agents/{agent_id}/telegram-channel",
    response_model=ChannelConfigOut,
    status_code=201,
)
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
    bot_token = resolve_secret_field(
        data, "bot_token", existing.app_secret if existing else None
    )
    if not bot_token:
        raise HTTPException(status_code=422, detail="bot_token is required")
    _validate_bot_token(bot_token)

    if existing:
        existing.app_secret = bot_token
        existing.is_configured = True
        await db.flush()
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
    await _register_telegram_webhook(bot_token, agent_id)
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
async def get_telegram_webhook_url(
    agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    public_base = await _resolve_public_base_url(db=db, request=request)
    if not public_base:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL not configured")
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
    if config.app_secret:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{TG_API}/bot{config.app_secret}/deleteWebhook"
                )
        except Exception as e:
            logger.warning("[Telegram] Failed to delete webhook: %s", e)
    await db.delete(config)


# ─── Webhook Handler ───────────────────────────────────


@router.post("/channel/telegram/{agent_id}/webhook")
async def telegram_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Telegram Bot API webhook updates."""
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(status_code=400)

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

    bot_token = config.app_secret
    if not bot_token:
        logger.warning(
            "[Telegram] Webhook called but bot_token is empty for agent %s", agent_id
        )
        return Response(status_code=403)

    # Verify webhook secret (if header present — absent means pre-upgrade webhook)
    received_secret = request.headers.get("x-telegram-bot-api-secret-token")
    if received_secret is not None:
        expected_secret = _compute_webhook_secret(bot_token)
        if not hmac.compare_digest(expected_secret, received_secret):
            logger.warning(
                "[Telegram] Webhook secret mismatch for agent %s", agent_id
            )
            return Response(status_code=403)
    else:
        logger.warning(
            "[Telegram] Webhook without secret_token for agent %s "
            "— re-save Telegram config to enable verification",
            agent_id,
        )

    # Dedup by update_id (Redis-backed, multi-worker safe)
    update_id = body.get("update_id", 0)
    if await _is_duplicate_update(update_id):
        return {"ok": True}

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

    if not user_text:
        return {"ok": True}

    # Log metadata at INFO (no PII), content + name at DEBUG only
    logger.info(
        "[Telegram] Message sender_id=%s chat=%s len=%d",
        sender_id,
        chat_id,
        len(user_text),
    )
    logger.debug(
        "[Telegram] sender_name=%s content_preview=%s",
        sender_name,
        user_text[:80],
    )

    # Handle /start command
    if user_text == "/start":
        from app.models.agent import Agent as AgentModel

        agent_r = await db.execute(
            select(AgentModel).where(AgentModel.id == agent_id)
        )
        agent_obj = agent_r.scalar_one_or_none()
        welcome = agent_obj.welcome_message if agent_obj else None
        await _send_telegram_message(
            bot_token,
            chat_id,
            welcome
            or f"Hi! I'm {agent_obj.name if agent_obj else 'your assistant'}. "
            "Send me a message to get started.",
        )
        return {"ok": True}

    # Strip /ask prefix if present
    if user_text.startswith("/ask "):
        user_text = user_text[5:].strip()

    conv_id = f"tg_{chat_id}_{sender_id}"

    # Find-or-create platform user
    from app.core.security import hash_password
    from app.models.agent import Agent as AgentModel
    from app.models.user import User as UserModel

    tg_username = f"tg_{sender_id}"
    user_result = await db.execute(
        select(UserModel).where(UserModel.username == tg_username)
    )
    platform_user = user_result.scalar_one_or_none()
    if not platform_user:
        agent_result = await db.execute(
            select(AgentModel).where(AgentModel.id == agent_id)
        )
        agent_obj = agent_result.scalar_one_or_none()
        if not agent_obj:
            logger.error(
                "[Telegram] Agent %s not found during user creation", agent_id
            )
            return Response(status_code=404)
        platform_user = UserModel(
            username=tg_username,
            email=f"{tg_username}@telegram.local",
            password_hash=hash_password(uuid.uuid4().hex),
            display_name=sender_name,
            tenant_id=agent_obj.tenant_id,
            role="member",
        )
        db.add(platform_user)
        await db.flush()
    elif sender_name and platform_user.display_name != sender_name:
        platform_user.display_name = sender_name
        await db.flush()

    # Find or create chat session
    from app.models.audit import ChatMessage
    from app.services.channel_session import find_or_create_channel_session

    session = await find_or_create_channel_session(
        db,
        agent_id,
        platform_user.id,
        conv_id,
        "telegram",
        first_message_title=f"Telegram: {sender_name}",
    )

    # Save user message
    db.add(
        ChatMessage(
            agent_id=agent_id,
            conversation_id=str(session.id),
            role="user",
            content=user_text,
            user_id=platform_user.id,
        )
    )
    await db.commit()

    # Load history
    from app.services.memory_service import compute_history_limit_for_agent

    hist_limit = await compute_history_limit_for_agent(agent_id)
    hist_r = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session.id))
        .order_by(ChatMessage.created_at.desc())
        .limit(hist_limit)
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in reversed(hist_r.scalars().all())
    ]

    # Call agent LLM (same function used by Feishu/Slack/DingTalk channels)
    from app.api.feishu import _call_agent_llm

    try:
        reply = await _call_agent_llm(
            db, agent_id, user_text, history=history,
            user_id=platform_user.id,
            session_source="telegram", session_channel="telegram",
        )
    except Exception as e:
        logger.error("[Telegram] LLM error for %s: %s", agent_id, e)
        reply = "Sorry, I encountered an error processing your message. Please try again."

    # Save assistant reply
    db.add(
        ChatMessage(
            agent_id=agent_id,
            conversation_id=str(session.id),
            role="assistant",
            content=reply,
            user_id=platform_user.id,
        )
    )
    await db.commit()

    await _send_telegram_message(bot_token, chat_id, reply)

    return {"ok": True}
