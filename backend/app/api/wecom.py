"""WeCom (企业微信) Channel API routes.

Provides Config CRUD and webhook-based message handling with AES encryption.
"""

import base64
import hashlib
import struct
import uuid
import xml.etree.ElementTree as ET

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

router = APIRouter(tags=["wecom"])


# ─── WeCom AES Crypto ──────────────────────────────────


async def _send_wecom_text_message(
    *,
    corp_id: str,
    corp_secret: str,
    agent_id: str,
    to_user: str,
    text: str,
) -> dict:
    """Send a WeCom text message through the official webhook API."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        tok_resp = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": corp_id, "corpsecret": corp_secret},
        )
        tok_resp.raise_for_status()
        token_payload = tok_resp.json()
        access_token = token_payload.get("access_token", "")
        if not access_token:
            raise ValueError(f"failed to obtain wecom access_token: {token_payload}")
        send_resp = await client.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
            json={
                "touser": to_user,
                "msgtype": "text",
                "agentid": int(agent_id),
                "text": {"content": text},
            },
        )
        send_resp.raise_for_status()
        payload = send_resp.json()
        if payload.get("errcode", 0) != 0:
            raise ValueError(f"wecom send failed: {payload}")
        return payload


def _pad(text: bytes) -> bytes:
    """PKCS7 padding for AES-CBC."""
    BLOCK_SIZE = 32
    pad_len = BLOCK_SIZE - (len(text) % BLOCK_SIZE)
    return text + bytes([pad_len] * pad_len)


def _unpad(text: bytes) -> bytes:
    """Remove PKCS7 padding."""
    pad_len = text[-1]
    return text[:-pad_len]


def _decrypt_msg(encrypt_key: str, encrypted_text: str) -> tuple[str, str]:
    """Decrypt a WeCom encrypted message.

    Returns (decrypted_xml, corp_id)
    """
    from Crypto.Cipher import AES

    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = _unpad(cipher.decrypt(base64.b64decode(encrypted_text)))
    # Skip 16 random bytes, then 4 bytes msg_length (network order)
    msg_len = struct.unpack("!I", decrypted[16:20])[0]
    msg_content = decrypted[20 : 20 + msg_len].decode("utf-8")
    corp_id = decrypted[20 + msg_len :].decode("utf-8")
    return msg_content, corp_id


def _encrypt_msg(encrypt_key: str, reply_msg: str, corp_id: str) -> str:
    """Encrypt a reply message for WeCom."""
    from Crypto.Cipher import AES
    import os

    aes_key = base64.b64decode(encrypt_key + "=")
    iv = aes_key[:16]
    msg_bytes = reply_msg.encode("utf-8")
    buf = os.urandom(16) + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(_pad(buf))
    return base64.b64encode(encrypted).decode("utf-8")


def _verify_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Generate WeCom message signature."""
    items = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_wecom_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure WeCom bot for an agent.

    Supports two modes:
    - WebSocket (AI Bot): bot_id + bot_secret (no callback URL needed)
    - Webhook (legacy): corp_id, secret, token, encoding_aes_key
    """
    agent = await require_agent_manage_access(db, current_user, agent_id)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    existing = result.scalar_one_or_none()

    # WebSocket mode fields (AI Bot)
    bot_id = data.get("bot_id", "").strip()
    bot_secret = resolve_secret_field(
        data,
        "bot_secret",
        (existing.extra_config or {}).get("bot_secret") if existing else None,
    )

    # Legacy webhook mode fields
    corp_id = data.get("corp_id", "").strip()
    wecom_agent_id = data.get("wecom_agent_id", "").strip()
    secret = resolve_secret_field(data, "secret", existing.app_secret if existing else None)
    token = resolve_secret_field(data, "token", existing.verification_token if existing else None)
    encoding_aes_key = resolve_secret_field(data, "encoding_aes_key", existing.encrypt_key if existing else None)

    # At least one mode must be configured
    has_ws_mode = bool(bot_id and bot_secret)
    has_webhook_mode = bool(corp_id and secret and token and encoding_aes_key)
    if not has_ws_mode and not has_webhook_mode:
        raise HTTPException(
            status_code=422,
            detail="Either bot_id+bot_secret (WebSocket) or corp_id+secret+token+encoding_aes_key (Webhook) required",
        )
    if has_webhook_mode and not wecom_agent_id:
        raise HTTPException(status_code=422, detail="wecom_agent_id is required for WeCom webhook mode")

    extra_config = {
        "wecom_agent_id": wecom_agent_id,
        "bot_id": bot_id,
        "bot_secret": bot_secret,
        "connection_mode": "websocket" if has_ws_mode else "webhook",
    }
    if existing:
        existing.app_id = corp_id
        existing.app_secret = secret
        existing.encrypt_key = encoding_aes_key
        existing.verification_token = token
        existing.extra_config = extra_config
        existing.is_configured = True
        await db.flush()
        config_out = ChannelConfigOut.model_validate(existing)
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            tenant_id=agent.tenant_id,
            channel_type="wecom",
            app_id=corp_id,
            app_secret=secret,
            encrypt_key=encoding_aes_key,
            verification_token=token,
            extra_config=extra_config,
            is_configured=True,
        )
        db.add(config)
        await db.flush()
        config_out = ChannelConfigOut.model_validate(config)

    # Auto-start WebSocket client if bot credentials provided
    if has_ws_mode:
        try:
            from app.services.wecom_stream import wecom_stream_manager
            import asyncio

            asyncio.create_task(wecom_stream_manager.start_client(agent_id, bot_id, bot_secret))
            logger.info(f"[WeCom] WebSocket client start triggered for agent {agent_id}")
        except Exception as e:
            logger.error(f"[WeCom] Failed to start WebSocket client: {e}")

    return config_out


@router.get("/agents/{agent_id}/wecom-channel", response_model=ChannelConfigOut)
async def get_wecom_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.get("/agents/{agent_id}/wecom-channel/webhook-url")
async def get_wecom_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
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
    return {"webhook_url": f"{public_base}/api/channel/wecom/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/wecom-channel", status_code=204)
async def delete_wecom_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await require_agent_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wecom",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WeCom not configured")
    from app.services.external_principal_service import revoke_channel_config_external_principals

    await revoke_channel_config_external_principals(
        db,
        tenant_id=agent.tenant_id,
        config=config,
        actor_user_id=current_user.id,
        reason="WeCom channel configuration deleted",
    )
    await db.delete(config)


# ─── Event Webhook ──────────────────────────────────────


@router.get("/channel/wecom/{agent_id}/webhook")
async def wecom_verify_webhook(
    agent_id: uuid.UUID,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom callback URL verification (GET request)."""
    config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="wecom")
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, echostr)
    if expected_sig != msg_signature:
        logger.warning(f"[WeCom] Signature mismatch: expected={expected_sig}, got={msg_signature}")
        return Response(status_code=403)

    # Decrypt echostr and return plaintext
    try:
        decrypted, _ = _decrypt_msg(encoding_aes_key, echostr)
        return Response(content=decrypted, media_type="text/plain")
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt echostr: {e}")
        return Response(status_code=500)


@router.post("/channel/wecom/{agent_id}/webhook")
async def wecom_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle WeCom message callback (POST request with encrypted XML)."""
    body_bytes = await request.body()

    # Get channel config and pin tenant RLS for this public webhook.
    config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="wecom")
    if not config:
        return Response(status_code=404)

    token = config.verification_token or ""
    encoding_aes_key = config.encrypt_key or ""

    # Parse encrypted XML body
    try:
        root = ET.fromstring(body_bytes)
        encrypt_text = root.findtext("Encrypt", "")
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse XML body: {e}")
        return Response(content="success", media_type="text/plain")

    # Verify signature
    expected_sig = _verify_signature(token, timestamp, nonce, encrypt_text)
    if expected_sig != msg_signature:
        logger.warning("[WeCom] Signature mismatch on POST")
        return Response(status_code=403)

    # Decrypt message
    try:
        decrypted_xml, recv_corp_id = _decrypt_msg(encoding_aes_key, encrypt_text)
    except Exception as e:
        logger.error(f"[WeCom] Failed to decrypt message: {e}")
        return Response(content="success", media_type="text/plain")

    logger.info(f"[WeCom] Decrypted event for {agent_id}")

    # Parse decrypted message XML
    try:
        msg_root = ET.fromstring(decrypted_xml)
    except Exception as e:
        logger.error(f"[WeCom] Failed to parse decrypted XML: {e}")
        return Response(content="success", media_type="text/plain")

    msg_type = msg_root.findtext("MsgType", "")
    from_user = msg_root.findtext("FromUserName", "")  # WeCom userid
    msg_id = msg_root.findtext("MsgId", "")

    logger.info(f"[WeCom] Message type={msg_type}, from={from_user}, msg_id={msg_id}")

    if msg_type == "text":
        user_text = msg_root.findtext("Content", "").strip()
        if not user_text:
            return Response(content="success", media_type="text/plain")

        from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref

        await accept_authenticated_channel_event(
            db,
            tenant_id=config.tenant_id,
            agent_id=agent_id,
            provider="wecom",
            installation_ref=channel_installation_ref(config, fallback=f"wecom:{agent_id}"),
            provider_event_id=str(msg_id or ""),
            handler_key="wecom.webhook",
            body={"from_user": from_user, "user_text": user_text},
            metadata={"transport": "encrypted_webhook", "corp_id": recv_corp_id},
        )

    elif msg_type in ("image", "file"):
        # TODO: Handle image/file messages in future
        logger.info(f"[WeCom] Received {msg_type} message (not yet handled)")

    return Response(content="success", media_type="text/plain")


async def _process_wecom_text(
    db: AsyncSession,
    agent_id: uuid.UUID,
    config: ChannelConfig,
    from_user: str,
    user_text: str,
):
    """Process an incoming WeCom text message and reply."""
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select as _select
    from app.database import tenant_scoped_session
    from app.services.tenant_resolver import resolve_tenant_for_agent
    from app.models.agent import Agent as AgentModel
    from app.models.audit import ChatMessage
    from app.services.channel_agent_runtime import call_agent_llm, should_persist_channel_reply_as_assistant
    from app.services.channel_session import find_or_create_channel_session
    from app.services.channel_delivery_service import channel_delivery_target as _cdt

    # Webhook bg path has no TenantMiddleware GUC. Resolve the tenant from the
    # path agent_id (narrow audited bypass single-row read) and pin the session.
    _wecom_tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(_wecom_tenant_id) as db:
        # Load agent
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            logger.warning(f"[WeCom] Agent {agent_id} not found")
            return
        from app.services.memory_service import compute_history_limit_for_agent

        _hist_limit = await compute_history_limit_for_agent(agent_id)

        conv_id = f"wecom_p2p_{from_user}"

        # Try to resolve display name from WeCom API
        display_name = f"WeCom {from_user[:8]}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                tok_resp = await client.get(
                    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                    params={"corpid": config.app_id, "corpsecret": config.app_secret},
                )
                access_token = tok_resp.json().get("access_token", "")
                if access_token:
                    user_resp = await client.get(
                        "https://qyapi.weixin.qq.com/cgi-bin/user/get",
                        params={"access_token": access_token, "userid": from_user},
                    )
                    user_data = user_resp.json()
                    if user_data.get("errcode") == 0:
                        display_name = user_data.get("name", display_name)
        except Exception as e:
            logger.error(f"[WeCom] Failed to resolve user info: {e}")

        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.external_principal_service import resolve_or_create_external_principal

        principal_resolution = await resolve_or_create_external_principal(
            db,
            tenant_id=agent_obj.tenant_id,
            provider="wecom",
            installation_ref=channel_installation_ref(config, fallback=f"wecom:{agent_id}"),
            channel_config_id=getattr(config, "id", None),
            subject_id=from_user,
            display_name=display_name,
            profile={},
        )
        runtime_actor = principal_resolution.actor
        platform_user_id = runtime_actor.id
        external_principal_id = principal_resolution.principal.id
        user_label = runtime_actor.display_name or display_name or f"WeCom {from_user[:8]}"

        from app.core.execution_context import set_delegated_user_identity

        if platform_user_id is not None:
            set_delegated_user_identity(platform_user_id, user_label, channel="wecom")

        delivery_target = {
            "channel": "wecom",
            "user_id": from_user,
            "user_label": user_label,
            "external_principal_id": str(external_principal_id),
        }

        # Find or create session
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            tenant_id=agent_obj.tenant_id if agent_obj else None,
            user_id=platform_user_id,
            external_principal_id=external_principal_id,
            external_conv_id=conv_id,
            source_channel="wecom",
            first_message_title=user_text,
            delivery_target=delivery_target,
        )
        session_conv_id = str(sess.id)
        delivery_target["session_id"] = session_conv_id
        sess.delivery_target_json = delivery_target

        # Load history
        history_r = await db.execute(
            _select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(_hist_limit)
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

        # Call LLM
        _cdt_token = _cdt.set(delivery_target)
        try:
            reply_text = await call_agent_llm(
                db,
                agent_id,
                user_text,
                history=history,
                user_id=platform_user_id,
                session_id=session_conv_id,
                session_source="wecom",
                session_channel="wecom",
                allow_bare_plan_confirmation=True,
                durable_run=True,
                durable_session=sess,
                durable_user=runtime_actor,
            )
        finally:
            _cdt.reset(_cdt_token)
        logger.info(f"[WeCom] LLM reply: {reply_text[:100]}")

        if should_persist_channel_reply_as_assistant(reply_text):
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

        # Send reply via unified WeCom helper after assistant reply is persisted
        try:
            await _send_wecom_text_message(
                corp_id=str(config.app_id or "").strip(),
                corp_secret=str(config.app_secret or "").strip(),
                agent_id=str((config.extra_config or {}).get("wecom_agent_id") or "").strip(),
                to_user=from_user,
                text=reply_text,
            )
        except Exception as e:
            logger.error(f"[WeCom] Failed to send reply: {e}")

        # Log activity
        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "chat_reply",
            f"Replied to WeCom message: {reply_text[:80]}",
            detail={"channel": "wecom", "user_text": user_text[:200], "reply": reply_text[:500]},
        )
