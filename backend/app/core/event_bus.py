"""Redis Streams event bus — durable, replay-capable event delivery.

Replaces Redis pub/sub for agent events, channel messages, knowledge updates,
and inter-agent collaboration (replacing file-based inbox).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Coroutine

from app.core.events import get_redis

logger = logging.getLogger(__name__)


class EventBus:
    """Redis Streams-backed event bus with consumer group support."""

    async def publish(
        self,
        stream: str,
        event_type: str,
        payload: dict[str, Any],
        tenant_id: str | None = None,
    ) -> str:
        """Publish an event to a stream. Returns the message ID."""
        r = await get_redis()
        data = {
            "event_type": event_type,
            "payload": json.dumps(payload, default=str),
            "tenant_id": tenant_id or "",
            "timestamp": str(time.time()),
            "event_id": str(uuid.uuid4()),
        }
        msg_id = await r.xadd(stream, data, maxlen=10000)
        logger.debug("Published %s to %s: %s", event_type, stream, msg_id)
        return msg_id

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[str, dict], Coroutine],
        *,
        block_ms: int = 5000,
        count: int = 10,
    ) -> None:
        """Subscribe to a stream via consumer group. Blocks until messages arrive.

        handler receives (event_type, payload_dict).
        Call in a loop from a background task.
        """
        r = await get_redis()

        # Create consumer group if it doesn't exist
        try:
            await r.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception:
            pass  # Group already exists

        # Read pending + new messages
        messages = await r.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )

        for stream_name, entries in messages:
            for msg_id, data in entries:
                event_type = data.get("event_type", "unknown")
                try:
                    payload = json.loads(data.get("payload", "{}"))
                    await handler(event_type, payload)
                    await r.xack(stream_name, group, msg_id)
                except Exception as e:
                    logger.error("Event handler failed for %s/%s: %s", stream_name, msg_id, e)

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message (mark as processed)."""
        r = await get_redis()
        await r.xack(stream, group, message_id)


# Stream name patterns (tenant-scoped)
def agent_stream(tenant_id: str) -> str:
    return f"events:{tenant_id}:agent"


def channel_stream(tenant_id: str) -> str:
    return f"events:{tenant_id}:channel"


def knowledge_stream(tenant_id: str) -> str:
    return f"events:{tenant_id}:knowledge"


def collab_stream(tenant_id: str) -> str:
    return f"events:{tenant_id}:collab"


# Global singleton
event_bus = EventBus()
