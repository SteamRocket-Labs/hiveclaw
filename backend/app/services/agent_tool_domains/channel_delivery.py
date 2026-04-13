"""Channel delivery domain for live channel replies and file return paths."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Awaitable, Callable

from app.config import get_settings
from app.database import async_session
from app.services.channel_delivery_service import ChannelDeliveryService, channel_delivery_target

logger = logging.getLogger(__name__)

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.AGENT_DATA_DIR)

ChannelFileSender = Callable[[Path, str], Awaitable[None]]

# ContextVar set by each channel handler so send_channel_file knows where to send.
# Value: async callable(file_path: Path, accompany_msg: str) -> None.
channel_file_sender: ContextVar[ChannelFileSender | None] = ContextVar("channel_file_sender", default=None)
# For web chat: agent_id needed to build download URL.
channel_web_agent_id: ContextVar[str | None] = ContextVar("channel_web_agent_id", default=None)
# Set by Feishu channel handler so downstream tools can infer the requester.
channel_feishu_sender_open_id: ContextVar[str | None] = ContextVar("channel_feishu_sender_open_id", default=None)


async def _send_channel_file(agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Send a file to the current requester via unified channel delivery."""
    rel_path = arguments.get("file_path", "").strip()
    accompany_msg = arguments.get("message", "")
    if not rel_path:
        return "❌ file_path is required"

    file_path = (ws / rel_path).resolve()
    ws_resolved = ws.resolve()
    if not str(file_path).startswith(str(ws_resolved)):
        file_path = (WORKSPACE_ROOT / str(agent_id) / rel_path).resolve()
        if not file_path.exists():
            return f"❌ File not found: {rel_path}"
    if not file_path.exists():
        return f"❌ File not found: {rel_path}"

    reply_target = channel_delivery_target.get()
    if reply_target is not None:
        try:
            async with async_session() as db:
                result = await ChannelDeliveryService.send_file(
                    db=db,
                    agent_id=agent_id,
                    reply_target=reply_target,
                    file_path=file_path,
                    message=accompany_msg,
                    delivery_mode="live",
                    extra_detail={"tool_name": "send_channel_file"},
                )
            if result.ok:
                return f"✅ File '{file_path.name}' sent to user via {result.channel}."
            logger.warning("[ChannelFile] Unified delivery failed: %s", result.message)
        except Exception as exc:
            logger.warning("[ChannelFile] Unified delivery error: %s", exc)

    sender = channel_file_sender.get()
    if sender is not None:
        try:
            await sender(file_path, accompany_msg)
            return f"✅ File '{file_path.name}' sent to user via channel."
        except Exception as exc:
            return f"❌ Failed to send file: {exc}"

    aid = channel_web_agent_id.get() or str(agent_id)
    base_abs = (WORKSPACE_ROOT / str(agent_id)).resolve()
    try:
        file_rel = str(file_path.resolve().relative_to(base_abs))
    except ValueError:
        file_rel = rel_path

    base_url = getattr(get_settings(), "BASE_URL", "").rstrip("/") or ""
    download_url = f"{base_url}/api/agents/{aid}/files/download?path={file_rel}"
    msg = f"✅ File ready: [{file_path.name}]({download_url})"
    if accompany_msg:
        msg = accompany_msg + "\n\n" + msg
    return msg


async def _send_channel_message(agent_id: uuid.UUID, arguments: dict) -> str:
    """Send a text message to the current requester / persisted reply target."""
    message = str(arguments.get("message", "") or "").strip()
    if not message:
        return "❌ message is required"

    reply_target = channel_delivery_target.get()
    if reply_target is None:
        return "❌ No current channel delivery target is available."

    async with async_session() as db:
        result = await ChannelDeliveryService.send_text(
            db=db,
            agent_id=agent_id,
            reply_target=reply_target,
            text=message,
            delivery_mode="live",
            extra_detail={"tool_name": "send_channel_message"},
        )

    if result.ok:
        return f"✅ {result.message}"
    return f"❌ {result.message}"
