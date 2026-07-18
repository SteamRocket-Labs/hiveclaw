"""Personal WeChat (iLink) channel API routes.

Provides QR-code-based login flow and channel lifecycle management:
- POST /agents/{agent_id}/wechat-personal/qr-start  → initiate QR login
- POST /agents/{agent_id}/wechat-personal/qr-wait   → poll scan status
- POST /agents/{agent_id}/wechat-personal/connect    → finalize connection
- GET  /agents/{agent_id}/wechat-personal/status     → connection status
- DELETE /agents/{agent_id}/wechat-personal          → disconnect
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.wechat_personal_service import (
    connect_channel,
    disconnect_channel,
    disconnect_duplicate_account_bindings,
    get_channel_credentials,
    get_channel_identity_status,
    retrieve_confirmed_credentials,
    start_qr_login,
    wait_qr_login,
)

router = APIRouter(tags=["wechat-personal"])


# ── Request / Response schemas ───────────────────────────


class QrStartResponse(BaseModel):
    session_key: str
    qr_image_url: str | None = None
    message: str


class QrWaitRequest(BaseModel):
    session_key: str


class QrWaitResponse(BaseModel):
    connected: bool
    account_id: str | None = None
    session_key: str | None = None
    qr_image_url: str | None = None
    message: str


class ConnectRequest(BaseModel):
    session_key: str  # credentials retrieved server-side, never from frontend


class StatusResponse(BaseModel):
    connected: bool
    account_id: str | None = None
    channel_type: str = "wechat_personal"
    transport_connected: bool = False
    identity_status: Literal["disconnected", "verified", "rebind_required", "access_denied"] = "disconnected"
    requires_rebind: bool = False
    requires_access_recovery: bool = False


# ── Routes ───────────────────────────────────────────────


@router.post("/agents/{agent_id}/wechat-personal/qr-start", response_model=QrStartResponse)
async def qr_start(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start WeChat QR login — fetches a fresh QR code from iLink."""
    await require_agent_manage_access(db, current_user, agent_id)

    # P1-6: Reject if already connected — must disconnect first
    existing = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wechat_personal",
        )
    )
    existing_config = existing.scalar_one_or_none()
    if existing_config and existing_config.is_connected:
        from app.services.wechat_personal_stream import wechat_personal_stream_manager

        identity = await get_channel_identity_status(
            db,
            existing_config,
            transport_running=wechat_personal_stream_manager.is_client_running(agent_id),
        )
        if identity["connected"]:
            raise HTTPException(status_code=409, detail="WeChat already connected. Disconnect first.")

    result = await start_qr_login(agent_id)
    return QrStartResponse(
        session_key=result["session_key"],
        qr_image_url=result.get("qr_image_url"),
        message=result["message"],
    )


@router.post("/agents/{agent_id}/wechat-personal/qr-wait", response_model=QrWaitResponse)
async def qr_wait(
    agent_id: uuid.UUID,
    body: QrWaitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Long-poll for WeChat QR scan status. Call repeatedly until connected=true."""
    await check_agent_access(db, current_user, agent_id)

    result = await wait_qr_login(agent_id, body.session_key)
    return QrWaitResponse(
        connected=result.get("connected", False),
        account_id=result.get("account_id"),
        session_key=result.get("session_key"),
        qr_image_url=result.get("qr_image_url"),
        message=result.get("message", ""),
    )


@router.post("/agents/{agent_id}/wechat-personal/connect", response_model=StatusResponse)
async def connect(
    agent_id: uuid.UUID,
    body: ConnectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Finalize WeChat connection — retrieve credentials from server-side session."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    # Retrieve credentials server-side — bot_token never touches the frontend
    creds = await retrieve_confirmed_credentials(body.session_key)
    if not creds or not creds["bot_token"]:
        raise HTTPException(status_code=400, detail="No confirmed QR session found. Please scan again.")
    if not str(creds.get("user_id") or "").strip():
        raise HTTPException(status_code=400, detail="WeChat did not return a verifiable user identity. Please scan again.")

    stale_agent_ids = await disconnect_duplicate_account_bindings(
        db=db,
        agent_id=agent_id,
        account_id=creds["account_id"],
        user_id=creds["user_id"],
        tenant_id=agent.tenant_id,
        actor_user_id=current_user.id,
    )

    config = await connect_channel(
        db=db,
        agent_id=agent_id,
        account_id=creds["account_id"],
        bot_token=creds["bot_token"],
        base_url=creds["base_url"],
        user_id=creds["user_id"],
        tenant_id=agent.tenant_id,
        actor_user_id=current_user.id,
    )
    await db.commit()

    from app.services.wechat_personal_stream import wechat_personal_stream_manager

    for stale_agent_id in stale_agent_ids:
        try:
            await wechat_personal_stream_manager.stop_client(stale_agent_id)
        except Exception as e:
            logger.warning(f"[WeChatPersonal] Failed to stop stale stream {stale_agent_id}: {e}")

    stream_running = False
    try:
        decrypted = get_channel_credentials(config)
        if decrypted and decrypted["bot_token"]:
            await wechat_personal_stream_manager.start_client(
                agent_id=agent_id,
                bot_token=decrypted["bot_token"],
                base_url=decrypted["base_url"],
            )
            stream_running = wechat_personal_stream_manager.is_client_running(agent_id)
    except Exception as e:
        logger.error(f"[WeChatPersonal] Failed to start stream after connect: {e}")

    if not stream_running:
        config.is_connected = False
        await db.commit()

    identity = await get_channel_identity_status(
        db,
        config,
        transport_running=stream_running,
    )
    return StatusResponse(account_id=creds["account_id"], **identity)


@router.get("/agents/{agent_id}/wechat-personal/status", response_model=StatusResponse)
async def status(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check whether personal WeChat is connected for this agent."""
    await check_agent_access(db, current_user, agent_id)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "wechat_personal",
        )
    )
    config = result.scalar_one_or_none()

    from app.services.wechat_personal_stream import wechat_personal_stream_manager

    identity = await get_channel_identity_status(
        db,
        config,
        transport_running=wechat_personal_stream_manager.is_client_running(agent_id),
    )
    creds = get_channel_credentials(config) if config is not None else None
    account_id = creds["bot_id"] if creds else None
    return StatusResponse(account_id=account_id, **identity)


@router.delete("/agents/{agent_id}/wechat-personal", response_model=StatusResponse)
async def disconnect(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect personal WeChat channel and stop message stream."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    # Stop the streaming client first
    try:
        from app.services.wechat_personal_stream import wechat_personal_stream_manager

        await wechat_personal_stream_manager.stop_client(agent_id)
    except Exception as e:
        logger.warning(f"[WeChatPersonal] Failed to stop stream on disconnect: {e}")

    removed = await disconnect_channel(
        db,
        agent_id,
        tenant_id=agent.tenant_id,
        actor_user_id=current_user.id,
    )
    await db.commit()

    if not removed:
        raise HTTPException(status_code=404, detail="No WeChat Personal channel configured")

    return StatusResponse(connected=False)
