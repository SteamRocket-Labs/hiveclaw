"""Discord Bot Channel API routes (slash command interactions)."""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_rls import load_public_agent_channel_config
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import get_db
from app.api.channel_secrets import resolve_secret_field
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut

router = APIRouter(tags=["discord"])

DISCORD_MSG_LIMIT = 2000  # Discord message char limit


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_discord_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Discord bot for an agent. Fields: application_id, bot_token, public_key."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    existing = result.scalar_one_or_none()
    application_id = data.get("application_id", "").strip()
    bot_token = resolve_secret_field(data, "bot_token", existing.app_secret if existing else None)
    public_key = resolve_secret_field(data, "public_key", existing.encrypt_key if existing else None)
    if not application_id or not bot_token or not public_key:
        raise HTTPException(status_code=422, detail="application_id, bot_token, and public_key are required")
    if existing:
        existing.app_id = application_id
        existing.app_secret = bot_token
        existing.encrypt_key = public_key
        existing.is_configured = True
        await db.flush()
        try:
            reg = await _register_slash_commands(application_id, bot_token)
            logger.info(f"[Discord] Slash command registration (update): {reg['status']}")
        except Exception as e:
            logger.warning(f"[Discord] Could not register slash commands: {e}")
        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        channel_type="discord",
        app_id=application_id,
        app_secret=bot_token,
        encrypt_key=public_key,
        is_configured=True,
    )
    db.add(config)
    await db.flush()
    try:
        reg = await _register_slash_commands(application_id, bot_token)
        logger.info(f"[Discord] Slash command registration (new): {reg['status']}")
    except Exception as e:
        logger.warning(f"[Discord] Could not register slash commands: {e}")
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/discord-channel", response_model=ChannelConfigOut)
async def get_discord_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.get("/agents/{agent_id}/discord-channel/webhook-url")
async def get_discord_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
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
    return {"webhook_url": f"{public_base}/api/channel/discord/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/discord-channel", status_code=204)
async def delete_discord_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_agent_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "discord",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Discord not configured")
    await db.delete(config)


# ─── Slash Command Registration ─────────────────────────


async def _register_slash_commands(application_id: str, bot_token: str) -> dict:
    """Register /ask global slash command with Discord API."""
    import httpx
    import os

    command = {
        "name": "ask",
        "description": "Ask the AI agent a question",
        "options": [
            {
                "name": "message",
                "description": "Your question or message to the agent",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    }
    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=15, proxy=proxy) as client:
        resp = await client.put(
            url,
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json=[command],
        )
        return {"status": resp.status_code, "body": resp.text}


# ─── Interactions Webhook ───────────────────────────────


def _verify_discord_signature(public_key: str, body: bytes, headers: dict) -> bool:
    """Verify Discord ed25519 signature."""
    try:
        from nacl.signing import VerifyKey

        timestamp = headers.get("x-signature-timestamp", "")
        signature = headers.get("x-signature-ed25519", "")
        if not timestamp or not signature:
            return False

        verify_key = VerifyKey(bytes.fromhex(public_key))
        verify_key.verify(f"{timestamp}".encode() + body, bytes.fromhex(signature))
        return True
    except Exception:
        return False


async def _send_discord_followup(application_id: str, bot_token: str, interaction_token: str, text: str) -> None:
    """Send follow-up message(s) to Discord Interactions, chunked at 2000 chars."""
    import httpx

    chunks = [text[i : i + DISCORD_MSG_LIMIT] for i in range(0, len(text), DISCORD_MSG_LIMIT)]
    proxy = os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY") or None
    async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
        for i, chunk in enumerate(chunks):
            if i == 0:
                # Edit the original deferred response
                await client.patch(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )
            else:
                # Additional chunks as follow-up messages
                await client.post(
                    f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}",
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                    json={"content": chunk},
                )


@router.post("/channel/discord/{agent_id}/webhook")
async def discord_interaction_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Discord Interaction webhooks (PING + slash commands)."""
    body_bytes = await request.body()
    replay_event_id = getattr(getattr(request, "state", None), "channel_ingress_event_id", None)

    # Get channel config and pin tenant RLS for this public webhook.
    config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="discord")
    if not config:
        return Response(status_code=404)

    # Verify Discord signature
    public_key = config.encrypt_key or ""
    if (
        replay_event_id is None
        and public_key
        and not _verify_discord_signature(public_key, body_bytes, dict(request.headers))
    ):
        return Response(content="Invalid signature", status_code=401)

    import json

    body = json.loads(body_bytes)
    interaction_type = body.get("type", 0)

    # Type 1: PING — Discord URL verification
    if interaction_type == 1:
        return {"type": 1}

    # Type 2: APPLICATION_COMMAND (slash command)
    if interaction_type == 2:
        data_obj = body.get("data", {})
        command_name = data_obj.get("name", "")
        options = data_obj.get("options", [])
        user_text = ""
        for opt in options:
            if opt.get("name") == "message":
                user_text = opt.get("value", "").strip()
                break

        if not user_text:
            return {"type": 4, "data": {"content": "⚠️ 请提供消息内容。Usage: `/ask message:<你的问题>`"}}

        if replay_event_id is None:
            from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref

            await accept_authenticated_channel_event(
                db,
                tenant_id=config.tenant_id,
                agent_id=agent_id,
                provider="discord",
                installation_ref=channel_installation_ref(config, fallback=f"discord:{agent_id}"),
                provider_event_id=str(body.get("id") or ""),
                handler_key="discord.interaction",
                body=body,
                metadata={"transport": "interaction_webhook"},
            )
            return {"type": 5}

        interaction_token = body.get("token", "")
        sender_id = body.get("member", {}).get("user", {}).get("id") or body.get("user", {}).get("id", "")
        channel_id = body.get("channel_id", "")
        conv_id = f"discord_{channel_id}_{sender_id}" if channel_id else f"discord_dm_{sender_id}"

        logger.info(f"[Discord] /{command_name} from {sender_id}: {user_text[:80]}")

        # Defer response immediately (Discord requires response within 3 seconds)
        # We return type 5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE) and reply later
        async def handle_in_background():
            from app.models.audit import ChatMessage
            from app.models.agent import Agent as AgentModel
            from app.services.channel_agent_runtime import call_agent_llm
            from app.services.channel_session import find_or_create_channel_session
            from app.database import tenant_scoped_session
            from app.services.tenant_resolver import resolve_tenant_for_agent
            from datetime import datetime, timezone

            # Webhook bg path has no TenantMiddleware GUC. Resolve the tenant from
            # the path agent_id (narrow audited bypass single-row read) and pin it.
            _discord_tenant_id = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(_discord_tenant_id) as bg_db:
                # Load agent
                agent_r = await bg_db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                agent_obj = agent_r.scalar_one_or_none()
                from app.services.memory_service import compute_history_limit_for_agent

                _hist_limit = await compute_history_limit_for_agent(agent_id)

                # Find-or-create platform user for this Discord sender
                from app.models.user import User as _User
                from app.core.security import hash_password as _hp
                import uuid as _uuid

                _username = f"discord_{sender_id}"
                _u_r = await bg_db.execute(select(_User).where(_User.username == _username))
                _platform_user = _u_r.scalar_one_or_none()
                if not _platform_user:
                    _discord_username = body.get("member", {}).get("user", {}).get("username") or body.get(
                        "user", {}
                    ).get("username", "")
                    _display = _discord_username or f"Discord User {sender_id[:8]}"
                    _platform_user = _User(
                        username=_username,
                        email=f"{_username}@discord.local",
                        password_hash=_hp(_uuid.uuid4().hex),
                        display_name=_display,
                        role="member",
                        tenant_id=agent_obj.tenant_id if agent_obj else None,
                    )
                    bg_db.add(_platform_user)
                    await bg_db.flush()
                platform_user_id = _platform_user.id
                delivery_target = {
                    "channel": "discord",
                    "interaction_token": interaction_token,
                    "channel_id": channel_id,
                    "sender_id": sender_id,
                    "user_label": _platform_user.display_name or f"Discord User {sender_id[:8]}",
                }

                # Find-or-create ChatSession for this Discord conversation
                sess = await find_or_create_channel_session(
                    db=bg_db,
                    agent_id=agent_id,
                    tenant_id=agent_obj.tenant_id if agent_obj else None,
                    user_id=platform_user_id,
                    external_conv_id=conv_id,
                    source_channel="discord",
                    first_message_title=user_text,
                    delivery_target=delivery_target,
                )
                session_conv_id = str(sess.id)
                delivery_target["session_id"] = session_conv_id
                sess.delivery_target_json = delivery_target

                # Load history from session
                history_r = await bg_db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
                    .order_by(ChatMessage.created_at.desc())
                    .limit(_hist_limit)
                )
                history = [{"role": m.role, "content": m.content} for m in reversed(history_r.scalars().all())]

                # Save user message
                bg_db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=agent_obj.tenant_id,
                        user_id=platform_user_id,
                        role="user",
                        content=user_text,
                        conversation_id=session_conv_id,
                    )
                )
                sess.last_message_at = datetime.now(timezone.utc)
                await bg_db.commit()

                # Call LLM
                from app.services.channel_delivery_service import channel_delivery_target as _cdt

                _cdt_token = _cdt.set(delivery_target)
                try:
                    reply_text = await call_agent_llm(
                        bg_db,
                        agent_id,
                        user_text,
                        history=history,
                        user_id=platform_user_id,
                        session_id=session_conv_id,
                        session_source="discord",
                        session_channel="discord",
                        allow_bare_plan_confirmation=True,
                        durable_run=True,
                        durable_session=sess,
                        durable_user=_platform_user,
                    )
                finally:
                    _cdt.reset(_cdt_token)
                logger.info(f"[Discord] LLM reply: {reply_text[:80]}")

                # Save reply
                bg_db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=agent_obj.tenant_id,
                        user_id=platform_user_id,
                        role="assistant",
                        content=reply_text,
                        conversation_id=session_conv_id,
                    )
                )
                sess.last_message_at = datetime.now(timezone.utc)
                await bg_db.commit()

                # Bot token stored in config — read from DB to avoid detached ORM issues
                from sqlalchemy import select as _sel

                cfg_r = await bg_db.execute(
                    _sel(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "discord",
                    )
                )
                cfg = cfg_r.scalar_one_or_none()
                bot_token_bg = cfg.app_secret if cfg else ""
                app_id_bg = cfg.app_id if cfg else ""

                # Send chunked reply via Discord follow-up
                if bot_token_bg and interaction_token and app_id_bg:
                    try:
                        await _send_discord_followup(app_id_bg, bot_token_bg, interaction_token, reply_text)
                    except Exception as e:
                        logger.error(f"[Discord] Failed to send follow-up: {e}")

        await handle_in_background()
        # Return DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE — shows "thinking..." to user
        return {"type": 5}

    # Unsupported interaction type
    return {"type": 1}
