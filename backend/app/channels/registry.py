"""Channel registry — manages adapter lifecycle and message routing."""

from __future__ import annotations

import logging
from typing import Any

from app.channels.base import ChannelAdapter, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Central registry for all channel adapters.

    Usage:
        registry = ChannelRegistry()
        registry.register(FeishuAdapter())
        registry.register(SlackAdapter())
        await registry.start_all()
        ...
        await registry.stop_all()
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        self._handlers: list[Any] = []  # inbound message handlers

    def register(self, adapter: ChannelAdapter) -> None:
        """Register a channel adapter."""
        self._adapters[adapter.name] = adapter
        logger.info("Registered channel adapter: %s", adapter.name)

    def get(self, name: str) -> ChannelAdapter | None:
        """Get an adapter by name."""
        return self._adapters.get(name)

    def on_message(self, handler) -> None:
        """Register a handler for inbound messages."""
        self._handlers.append(handler)

    async def dispatch_inbound(self, msg: InboundMessage) -> None:
        """Dispatch an inbound message to all registered handlers."""
        for handler in self._handlers:
            try:
                await handler(msg)
            except Exception as e:
                logger.error("Inbound handler failed for %s message: %s", msg.channel_type, e)

    async def send(self, msg: OutboundMessage) -> None:
        """Route an outbound message to the correct adapter."""
        adapter = self._adapters.get(msg.channel_type)
        if not adapter:
            logger.warning("No adapter registered for channel type: %s", msg.channel_type)
            return
        try:
            await adapter.send(msg)
        except Exception as e:
            logger.error("Failed to send message via %s: %s", msg.channel_type, e)

    async def start_all(self) -> None:
        """Start all registered adapters."""
        for name, adapter in self._adapters.items():
            try:
                await adapter.start()
                logger.info("Started channel adapter: %s", name)
            except Exception as e:
                logger.error("Failed to start channel adapter %s: %s", name, e)

    async def stop_all(self) -> None:
        """Stop all registered adapters."""
        for name, adapter in self._adapters.items():
            try:
                await adapter.stop()
                logger.info("Stopped channel adapter: %s", name)
            except Exception as e:
                logger.error("Failed to stop channel adapter %s: %s", name, e)

    @property
    def adapter_names(self) -> list[str]:
        return list(self._adapters.keys())


# Global singleton
channel_registry = ChannelRegistry()
