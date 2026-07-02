from __future__ import annotations

from uuid import uuid4

import pytest


class _FakeRedis:
    def __init__(self):
        self.increments: dict[str, int] = {}
        self.streams = []
        self.published = []

    async def incr(self, key: str):
        self.increments[key] = self.increments.get(key, 0) + 1
        return self.increments[key]

    async def xadd(self, stream: str, data: dict, maxlen=None, approximate=True):
        self.streams.append((stream, data, maxlen, approximate))
        return "1-0"

    async def publish(self, channel: str, payload: str):
        self.published.append((channel, payload))
        return 1


@pytest.mark.asyncio
async def test_publish_web_chat_stream_event_writes_ordered_redis_stream(monkeypatch):
    import app.services.web_chat_stream_bus as bus

    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    run_id = uuid4()
    envelope = await bus.publish_web_chat_stream_event(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=uuid4(),
        run_id=run_id,
        event_type="chunk",
        payload={"type": "chunk", "content": "hi"},
    )

    assert envelope["schema"] == "hive.web_chat.stream.v1"
    assert envelope["sequence"] == 1
    assert envelope["run_id"] == str(run_id)
    assert redis.streams[0][0] == f"hive:web_chat:stream:{run_id}"
    assert redis.published[0][0] == "hive:web_chat:stream:live"
