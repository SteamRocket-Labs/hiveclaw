"""Background retry loop for the durable channel ingress inbox."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

from loguru import logger

from app.services.channel_ingress_inbox import ChannelIngressInboxService

_stop_event: asyncio.Event | None = None
_wakeup_event: asyncio.Event | None = None


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def notify_channel_ingress_daemon() -> None:
    if _wakeup_event is not None:
        _wakeup_event.set()


def stop_channel_ingress_daemon() -> None:
    if _stop_event is not None:
        _stop_event.set()
    if _wakeup_event is not None:
        _wakeup_event.set()


async def start_channel_ingress_daemon() -> None:
    """Drain immediately on startup, then on wakeup or bounded polling fallback."""

    global _stop_event, _wakeup_event
    _stop_event = asyncio.Event()
    _wakeup_event = asyncio.Event()
    service = ChannelIngressInboxService()
    worker_id = _worker_id()
    logger.info("[ChannelIngress] durable inbox worker started as {}", worker_id)
    try:
        while not _stop_event.is_set():
            try:
                stats = await service.drain_once(worker_id=worker_id, limit=50)
                if stats["claimed"]:
                    logger.info("[ChannelIngress] drain stats {}", stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a poison event must not kill the worker.
                logger.error("[ChannelIngress] drain failed: {}", exc, exc_info=True)
            if _stop_event.is_set():
                break
            _wakeup_event.clear()
            try:
                await asyncio.wait_for(_wakeup_event.wait(), timeout=1.0)
            except TimeoutError:
                pass
    finally:
        logger.info("[ChannelIngress] durable inbox worker stopped")
        _stop_event = None
        _wakeup_event = None


__all__ = [
    "notify_channel_ingress_daemon",
    "start_channel_ingress_daemon",
    "stop_channel_ingress_daemon",
]
