"""Channel adapter base class and message types (deer-flow MessageBus pattern).

Each IM platform (Feishu, Slack, Discord, etc.) implements ChannelAdapter.
Messages are normalized to InboundMessage/OutboundMessage for unified processing.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter


@dataclass
class InboundMessage:
    """Normalized incoming message from any channel."""

    channel_type: str           # feishu, slack, discord, dingtalk, wecom, teams, web
    channel_id: str             # Platform-specific channel/conversation ID
    thread_id: str | None       # Thread/topic within channel (if threaded)
    sender_id: str              # Platform-specific sender identifier
    sender_name: str
    content: str
    agent_id: uuid.UUID | None = None  # Resolved target agent
    tenant_id: uuid.UUID | None = None
    attachments: list[dict] = field(default_factory=list)
    raw_event: dict = field(default_factory=dict)  # Original platform event
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_id: str = ""


@dataclass
class OutboundMessage:
    """Normalized outgoing message to any channel."""

    channel_type: str
    channel_id: str
    thread_id: str | None
    content: str
    agent_id: uuid.UUID | None = None
    attachments: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(ABC):
    """Abstract base for IM platform adapters."""

    name: str  # "feishu", "slack", etc.

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform (WebSocket, polling, etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""
        ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Deliver a response message to the platform."""
        ...

    def create_router(self) -> APIRouter | None:
        """Return a FastAPI router for webhook endpoints (optional)."""
        return None
