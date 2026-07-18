"""Personal WeChat channel service — QR login session management and connection lifecycle.

Manages the QR-code-based login flow for personal WeChat (iLink) channels:
1. start_qr_login → fetch QR from iLink → store session in Redis
2. wait_qr_login → long-poll iLink for scan confirmation
3. connect → persist credentials to ChannelConfig → start stream
4. disconnect → stop stream → remove config
"""

from __future__ import annotations

import json
import time
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_config import ChannelConfig
from app.services.secrets_provider import get_secrets_provider
from app.services.wechat_ilink_client import (
    DEFAULT_ILINK_BASE_URL,
    ILinkClient,
    MAX_QR_REFRESH,
    QR_LOGIN_TTL_SECONDS,
    QRStatusResult,
)

# ── Redis session keys ───────────────────────────────────

_QR_SESSION_PREFIX = "wechat_qr:"
_CONTEXT_TOKEN_PREFIX = "wechat_ctx:"
_SYNC_BUF_PREFIX = "wechat_sync:"
_TYPING_TICKET_PREFIX = "wechat_ticket:"

CONTEXT_TOKEN_TTL = 86400  # 24h
SYNC_BUF_TTL = 604800  # 7 days


# ── Encryption helpers ───────────────────────────────────


def _encrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return get_secrets_provider().encrypt(value)
    except Exception as e:
        logger.error(f"[WeChatPersonal] CRITICAL: encryption failed, refusing to store plaintext: {e}")
        raise


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return get_secrets_provider().decrypt(value)
    except Exception as e:
        logger.error(f"[WeChatPersonal] Decryption failed for stored credential: {e}")
        return None


# ── QR Login Session ─────────────────────────────────────


async def _get_redis():
    from app.core.events import get_redis

    return await get_redis()


async def _store_qr_session(session_key: str, data: dict) -> None:
    """Store QR login session in Redis with TTL."""
    key = f"{_QR_SESSION_PREFIX}{session_key}"
    try:
        r = await _get_redis()
        await r.set(key, json.dumps(data), ex=QR_LOGIN_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"[WeChatPersonal] Redis store failed: {e}")


async def _get_qr_session(session_key: str) -> dict | None:
    """Retrieve QR login session from Redis."""
    key = f"{_QR_SESSION_PREFIX}{session_key}"
    try:
        r = await _get_redis()
        raw = await r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[WeChatPersonal] Redis get failed: {e}")
    return None


async def _delete_qr_session(session_key: str) -> None:
    key = f"{_QR_SESSION_PREFIX}{session_key}"
    try:
        r = await _get_redis()
        await r.delete(key)
    except Exception as e:
        logger.warning(f"[WeChatPersonal] Redis delete failed for {key}: {e}")


# ── Context Token Cache ──────────────────────────────────


async def store_context_token(agent_id: uuid.UUID, user_id: str, token: str) -> None:
    """Cache the latest context_token for an agent+user pair (needed for replies)."""
    key = f"{_CONTEXT_TOKEN_PREFIX}{agent_id}:{user_id}"
    try:
        r = await _get_redis()
        await r.set(key, token, ex=CONTEXT_TOKEN_TTL)
    except Exception as e:
        logger.debug(f"[WeChatPersonal] context_token cache failed: {e}")


async def get_context_token(agent_id: uuid.UUID, user_id: str) -> str | None:
    """Retrieve cached context_token for sending replies."""
    key = f"{_CONTEXT_TOKEN_PREFIX}{agent_id}:{user_id}"
    try:
        r = await _get_redis()
        return await r.get(key)
    except Exception as e:
        logger.debug(f"[WeChatPersonal] context_token get failed: {e}")
        return None


# ── Sync Buffer Persistence ──────────────────────────────


async def store_sync_buf(agent_id: uuid.UUID, buf: str) -> None:
    """Persist the getupdates sync buffer for message continuity across restarts."""
    key = f"{_SYNC_BUF_PREFIX}{agent_id}"
    try:
        r = await _get_redis()
        await r.set(key, buf, ex=SYNC_BUF_TTL)
    except Exception as e:
        logger.debug(f"[WeChatPersonal] sync_buf store failed: {e}")


async def get_sync_buf(agent_id: uuid.UUID) -> str:
    """Retrieve persisted sync buffer."""
    key = f"{_SYNC_BUF_PREFIX}{agent_id}"
    try:
        r = await _get_redis()
        return await r.get(key) or ""
    except Exception as e:
        logger.debug(f"[WeChatPersonal] sync_buf get failed: {e}")
        return ""


# ── Typing Ticket Cache ──────────────────────────────────


async def store_typing_ticket(agent_id: uuid.UUID, user_id: str, ticket: str) -> None:
    key = f"{_TYPING_TICKET_PREFIX}{agent_id}:{user_id}"
    try:
        r = await _get_redis()
        await r.set(key, ticket, ex=CONTEXT_TOKEN_TTL)
    except Exception as e:
        logger.debug(f"[WeChatPersonal] typing_ticket store failed: {e}")


async def get_typing_ticket(agent_id: uuid.UUID, user_id: str) -> str | None:
    key = f"{_TYPING_TICKET_PREFIX}{agent_id}:{user_id}"
    try:
        r = await _get_redis()
        return await r.get(key)
    except Exception as e:
        logger.debug(f"[WeChatPersonal] typing_ticket get failed: {e}")
        return None


# ── Public API ───────────────────────────────────────────


async def start_qr_login(agent_id: uuid.UUID) -> dict:
    """Initiate a new QR login session for personal WeChat.

    Returns: {session_key, qr_image_url, message}
    """
    session_key = str(uuid.uuid4())
    client = ILinkClient()

    try:
        qr = await client.fetch_qr_code()
    except Exception as e:
        logger.error(f"[WeChatPersonal] QR fetch failed: {e}")
        return {
            "session_key": session_key,
            "qr_image_url": None,
            "message": f"获取微信二维码失败: {e}",
        }

    await _store_qr_session(
        session_key,
        {
            "qrcode": qr.qrcode,
            "qr_image_url": qr.qrcode_img_url,
            "agent_id": str(agent_id),
            "started_at": time.time(),
            "refresh_count": 1,
        },
    )

    logger.info(f"[WeChatPersonal] QR login started for agent {agent_id}, session={session_key[:8]}...")
    return {
        "session_key": session_key,
        "qr_image_url": qr.qrcode_img_url,
        "message": "请使用微信扫描二维码完成连接",
    }


async def wait_qr_login(agent_id: uuid.UUID, session_key: str) -> dict:
    """Long-poll for QR scan result.

    Returns: {connected, account_id?, message}
    """
    session = await _get_qr_session(session_key)
    if not session:
        return {"connected": False, "message": "登录会话已过期，请重新扫码"}

    client = ILinkClient()
    qrcode = session["qrcode"]

    result: QRStatusResult = await client.poll_qr_status(qrcode)

    if result.status == "wait":
        return {"connected": False, "message": "等待扫码中..."}

    if result.status == "scaned":
        return {"connected": False, "message": "已扫码，请在微信上确认"}

    if result.status == "expired":
        refresh_count = session.get("refresh_count", 1) + 1
        if refresh_count > MAX_QR_REFRESH:
            await _delete_qr_session(session_key)
            return {"connected": False, "message": "二维码多次过期，请重新开始"}

        # Auto-refresh QR
        try:
            new_qr = await client.fetch_qr_code()
            session["qrcode"] = new_qr.qrcode
            session["qr_image_url"] = new_qr.qrcode_img_url
            session["refresh_count"] = refresh_count
            session["started_at"] = time.time()
            await _store_qr_session(session_key, session)
            return {
                "connected": False,
                "qr_image_url": new_qr.qrcode_img_url,
                "message": f"二维码已刷新 ({refresh_count}/{MAX_QR_REFRESH})，请重新扫码",
            }
        except Exception as e:
            await _delete_qr_session(session_key)
            return {"connected": False, "message": f"刷新二维码失败: {e}"}

    if result.status == "confirmed":
        if not result.ilink_bot_id:
            await _delete_qr_session(session_key)
            return {"connected": False, "message": "登录失败：服务器未返回 bot ID"}
        if not result.ilink_user_id:
            await _delete_qr_session(session_key)
            return {"connected": False, "message": "登录失败：服务器未返回可验证的微信用户身份"}

        # Store credentials server-side — bot_token NEVER sent to frontend
        await _store_qr_session(
            session_key,
            {
                **session,
                "confirmed": True,
                "bot_token": result.bot_token,
                "account_id": result.ilink_bot_id,
                "ilink_base_url": result.base_url,
                "ilink_user_id": result.ilink_user_id,
            },
        )
        logger.info(f"[WeChatPersonal] QR confirmed for agent {agent_id}, bot_id={result.ilink_bot_id}")
        return {
            "connected": True,
            "session_key": session_key,
            "account_id": result.ilink_bot_id,
            "message": "微信连接成功",
        }

    # Unknown status
    return {"connected": False, "message": f"未知状态: {result.status}"}


async def retrieve_confirmed_credentials(session_key: str) -> dict | None:
    """Retrieve confirmed QR credentials from Redis (server-side only).

    Returns {account_id, bot_token, base_url, user_id} or None if not found/confirmed.
    """
    session = await _get_qr_session(session_key)
    if not session or not session.get("confirmed"):
        return None

    result = {
        "account_id": session.get("account_id", ""),
        "bot_token": session.get("bot_token", ""),
        "base_url": session.get("ilink_base_url"),
        "user_id": session.get("ilink_user_id"),
    }
    # Clean up session after retrieval
    await _delete_qr_session(session_key)
    return result


async def connect_channel(
    db: AsyncSession,
    agent_id: uuid.UUID,
    account_id: str,
    bot_token: str,
    base_url: str | None = None,
    user_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> ChannelConfig:
    """Persist WeChat credentials to ChannelConfig after successful QR login.

    Creates or updates the wechat_personal ChannelConfig for the agent.
    """
    if tenant_id is None or actor_user_id is None:
        raise ValueError("WeChat connection requires authenticated tenant and user authority")
    provider_user_id = str(user_id or "").strip()
    if not provider_user_id:
        raise ValueError("WeChat connection requires a verified provider user identity")

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wechat_personal",
        )
    )
    config = result.scalar_one_or_none()

    extra = {
        "ilink_bot_id": account_id,
        "ilink_bot_token": _encrypt(bot_token),
        "ilink_base_url": base_url or DEFAULT_ILINK_BASE_URL,
        "ilink_user_id": provider_user_id,
        "bot_type": "3",
    }

    if config is not None:
        from app.services.external_principal_service import revoke_channel_config_external_principals

        installation_tenant_id = tenant_id or config.tenant_id
        if installation_tenant_id is None:
            raise ValueError("WeChat reconnect requires tenant authority")
        await revoke_channel_config_external_principals(
            db,
            tenant_id=installation_tenant_id,
            config=config,
            actor_user_id=actor_user_id,
            reason="WeChat Personal channel reconnected with a new installation",
        )
        await db.delete(config)
        await db.flush()

    config = ChannelConfig(
        agent_id=agent_id,
        tenant_id=tenant_id,
        channel_type="wechat_personal",
        is_configured=True,
        is_connected=True,
        extra_config=extra,
    )
    db.add(config)

    await db.flush()
    from app.services.external_principal_service import bind_authenticated_self_channel_principal

    await bind_authenticated_self_channel_principal(
        db,
        tenant_id=tenant_id,
        config=config,
        provider_subject_id=provider_user_id,
        user_id=actor_user_id,
        actor_user_id=actor_user_id,
    )
    logger.info(f"[WeChatPersonal] Channel connected for agent {agent_id}")
    return config


async def get_channel_identity_status(
    db: AsyncSession,
    config: ChannelConfig | None,
    *,
    transport_running: bool | None = None,
) -> dict[str, object]:
    """Return independently verified transport, identity, and Agent authority."""

    if config is None or not config.is_configured:
        return {
            "connected": False,
            "transport_connected": False,
            "identity_status": "disconnected",
            "requires_rebind": False,
            "requires_access_recovery": False,
        }

    transport_connected = bool(
        config.is_connected
        and (transport_running if transport_running is not None else True)
    )

    subject_id = str((config.extra_config or {}).get("ilink_user_id") or "").strip()
    bound_user_id = getattr(config, "self_identity_user_id", None)
    if not subject_id or bound_user_id is None or getattr(config, "self_identity_verified_at", None) is None:
        return {
            "connected": False,
            "transport_connected": transport_connected,
            "identity_status": "rebind_required",
            "requires_rebind": True,
            "requires_access_recovery": False,
        }

    from app.models.external_principal import ExternalPrincipal
    from app.models.user import User

    principal = await db.scalar(
        select(ExternalPrincipal)
        .where(
            ExternalPrincipal.channel_config_id == config.id,
            ExternalPrincipal.tenant_id == config.tenant_id,
            ExternalPrincipal.provider == "wechat_personal",
            ExternalPrincipal.subject_id == subject_id,
            ExternalPrincipal.status == "active",
            ExternalPrincipal.linked_user_id == bound_user_id,
        )
        .order_by(ExternalPrincipal.updated_at.desc())
        .limit(1)
    )
    user = await db.scalar(
        select(User).where(
            User.id == bound_user_id,
            User.tenant_id == config.tenant_id,
            User.is_active.is_(True),
        )
    )
    identity_verified = principal is not None and user is not None
    authority_verified = False
    if identity_verified:
        from fastapi import HTTPException

        from app.core.permissions import check_agent_access

        try:
            await check_agent_access(db, user, config.agent_id)
            authority_verified = True
        except (HTTPException, PermissionError):
            authority_verified = False

    if identity_verified and not authority_verified:
        return {
            "connected": False,
            "transport_connected": transport_connected,
            "identity_status": "access_denied",
            "requires_rebind": False,
            "requires_access_recovery": True,
        }

    return {
        "connected": bool(identity_verified and transport_connected),
        "transport_connected": transport_connected,
        "identity_status": "verified" if identity_verified else "rebind_required",
        "requires_rebind": bool(not identity_verified or not transport_connected),
        "requires_access_recovery": False,
    }


async def disconnect_duplicate_account_bindings(
    db: AsyncSession,
    agent_id: uuid.UUID,
    account_id: str,
    user_id: str | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Disconnect stale personal-WeChat bindings for the same iLink account.

    iLink long-polling is account-scoped. If the same WeChat account remains
    connected to an older agent, that older poll loop can consume inbound
    messages before the newly bound agent sees them.
    """
    if not account_id and not user_id:
        return []

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.channel_type == "wechat_personal",
            ChannelConfig.is_connected.is_(True),
        )
    )
    configs = list(result.scalars().all())
    stale_agent_ids: list[uuid.UUID] = []

    for config in configs:
        config_agent_id = getattr(config, "agent_id", None)
        if config_agent_id == agent_id:
            continue

        config_tenant_id = getattr(config, "tenant_id", None)
        if tenant_id is not None and config_tenant_id is not None and config_tenant_id != tenant_id:
            continue

        extra = getattr(config, "extra_config", None) or {}
        same_account = bool(account_id and extra.get("ilink_bot_id") == account_id)
        same_user = bool(user_id and extra.get("ilink_user_id") == user_id)
        if not (same_account or same_user):
            continue

        installation_tenant_id = config_tenant_id or tenant_id
        if installation_tenant_id is None:
            raise ValueError("stale WeChat installation has no tenant authority")
        from app.services.external_principal_service import revoke_channel_config_external_principals

        await revoke_channel_config_external_principals(
            db,
            tenant_id=installation_tenant_id,
            config=config,
            actor_user_id=actor_user_id,
            reason="WeChat account moved to another agent installation",
        )
        await db.delete(config)
        if config_agent_id:
            stale_agent_ids.append(config_agent_id)

    if stale_agent_ids:
        await db.flush()
        logger.info(
            "[WeChatPersonal] Disconnected {} stale binding(s) for iLink account {}",
            len(stale_agent_ids),
            account_id,
        )

    return stale_agent_ids


async def disconnect_channel(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> bool:
    """Disconnect and remove wechat_personal config for an agent."""
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wechat_personal",
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        return False

    from app.services.external_principal_service import revoke_channel_config_external_principals

    await revoke_channel_config_external_principals(
        db,
        tenant_id=tenant_id,
        config=config,
        actor_user_id=actor_user_id,
        reason="WeChat Personal channel disconnected",
    )

    await db.delete(config)
    await db.flush()

    # Clean up Redis state
    try:
        r = await _get_redis()
        await r.delete(f"{_SYNC_BUF_PREFIX}{agent_id}")
    except Exception as e:
        logger.warning(f"[WeChatPersonal] Redis cleanup failed on disconnect: {e}")

    logger.info(f"[WeChatPersonal] Channel disconnected for agent {agent_id}")
    return True


def get_channel_credentials(config: ChannelConfig) -> dict | None:
    """Extract and decrypt credentials from a ChannelConfig's extra_config.

    Returns: {bot_token, base_url, bot_id, user_id} or None if not configured.
    """
    extra = config.extra_config or {}
    encrypted_token = extra.get("ilink_bot_token")
    if not encrypted_token:
        return None

    return {
        "bot_token": _decrypt(encrypted_token),
        "base_url": extra.get("ilink_base_url", DEFAULT_ILINK_BASE_URL),
        "bot_id": extra.get("ilink_bot_id", ""),
        "user_id": extra.get("ilink_user_id", ""),
    }
