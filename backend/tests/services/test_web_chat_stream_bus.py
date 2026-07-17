from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest


def _canonical_content_event(
    sequence: int,
    *,
    item_kind: str,
    audience: str,
    content: str,
    redaction_paths: list[str] | None = None,
) -> dict:
    session_id = "session-live-continuity"
    run_id = "run-live-continuity"
    round_id = "round-live-continuity"
    phase = {
        "assistant_commentary": "commentary",
        "assistant_reasoning_private": "reasoning_private",
    }[item_kind]
    return {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": f"event-{sequence}",
        "sequence": sequence,
        "ordinal": sequence - 1,
        "tenant_id": "tenant-live-continuity",
        "scope": {
            "level": "round",
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": "turn-live-continuity",
            "run_id": run_id,
            "round_id": round_id,
        },
        "run_id": run_id,
        "item_id": f"item-{sequence}",
        "item_kind": item_kind,
        "kind": f"{item_kind}.delta",
        "lifecycle": "delta",
        "payload_schema": f"hive.session.payload.{item_kind}.delta.v2",
        "actor": {"type": "assistant"},
        "visibility": {
            "audience": audience,
            **({"redaction_paths": redaction_paths} if redaction_paths else {}),
        },
        "payload": {"content": content, "phase": phase},
        "occurred_at": "2026-07-18T00:00:00Z",
        "persisted_at": "2026-07-18T00:00:00Z",
    }


def _canonical_delivery(event: dict) -> dict:
    from app.services.execution_receipts import canonical_payload_hash

    return {
        "schema": "hive.session_event.delivery",
        "schema_version": 1,
        "event_id": event["event_id"],
        "agent_id": "agent-live-continuity",
        "session_id": event["scope"]["session_id"],
        "sequence": event["sequence"],
        "envelope_ref": f"session-event:{event['event_id']}",
        "envelope_sha256": canonical_payload_hash(event),
        "envelope": event,
    }


def _user_projected_delivery(event: dict) -> dict:
    from app.services.execution_receipts import canonical_payload_hash
    from app.services.session_event_contract import serialize_session_event

    delivery = _canonical_delivery(event)
    projected = serialize_session_event(event, audience="user")
    return {
        **delivery,
        "projection_audience": "user",
        "source_envelope_sha256": delivery["envelope_sha256"],
        "envelope_sha256": canonical_payload_hash(projected),
        "envelope": projected,
    }


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


@pytest.mark.asyncio
async def test_canonical_live_publish_redacts_private_content_before_redis(monkeypatch):
    import app.services.web_chat_stream_bus as bus
    from app.services.execution_receipts import canonical_payload_hash

    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    event = _canonical_content_event(
        2,
        item_kind="assistant_reasoning_private",
        audience="private_provider",
        content="provider-private-reasoning-secret",
        redaction_paths=["/payload/content"],
    )
    delivery = _canonical_delivery(event)

    await bus.publish_canonical_session_event(delivery)

    channel, encoded = redis.published[0]
    published = json.loads(encoded)
    projected = published["envelope"]
    assert channel == bus.SESSION_EVENT_LIVE_CHANNEL
    assert published["projection_audience"] == "user"
    assert published["source_envelope_sha256"] == canonical_payload_hash(event)
    assert published["envelope_sha256"] == canonical_payload_hash(projected)
    assert projected["event_id"] == event["event_id"]
    assert projected["sequence"] == 2
    assert projected["visibility"]["audience"] == "private_provider"
    assert projected["visibility"]["redacted_fields"] == ["/payload/content"]
    assert "content" not in projected["payload"]
    assert "provider-private-reasoning-secret" not in encoded


@pytest.mark.asyncio
async def test_canonical_live_forwarder_preserves_redacted_sequence_continuity(monkeypatch):
    import app.services.web_chat_broker as broker_module
    import app.services.web_chat_stream_bus as bus

    events = [
        _canonical_content_event(
            1,
            item_kind="assistant_commentary",
            audience="direct_user",
            content="public progress one",
        ),
        _canonical_content_event(
            2,
            item_kind="assistant_reasoning_private",
            audience="private_provider",
            content="provider-private-reasoning-secret",
            redaction_paths=["/payload/content"],
        ),
        _canonical_content_event(
            3,
            item_kind="assistant_commentary",
            audience="direct_user",
            content="public progress two",
        ),
        _canonical_content_event(
            4,
            item_kind="assistant_reasoning_private",
            audience="private_provider",
            content="new-publisher-private-secret",
            redaction_paths=["/payload/content"],
        ),
        _canonical_content_event(
            5,
            item_kind="assistant_commentary",
            audience="direct_user",
            content="public progress three",
        ),
    ]

    class FinitePubSub:
        async def subscribe(self, *_args):
            return None

        async def listen(self):
            for event in events:
                delivery = _user_projected_delivery(event) if event["sequence"] == 4 else _canonical_delivery(event)
                yield {
                    "type": "message",
                    "data": json.dumps(delivery),
                }

    class FakeRedisWithPubSub:
        def pubsub(self):
            return FinitePubSub()

    forwarded: list[dict] = []

    async def fake_get_redis():
        return FakeRedisWithPubSub()

    async def fake_send_session_message(_agent_id, _session_id, payload):
        forwarded.append(payload)

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    monkeypatch.setattr(broker_module.web_chat_broker, "send_session_message", fake_send_session_message)

    await bus._listen_web_chat_stream_once()

    assert [event["sequence"] for event in forwarded] == [1, 2, 3, 4, 5]
    assert forwarded[0]["payload"]["content"] == "public progress one"
    assert "content" not in forwarded[1]["payload"]
    assert forwarded[1]["visibility"]["redacted_fields"] == ["/payload/content"]
    assert forwarded[2]["payload"]["content"] == "public progress two"
    assert "content" not in forwarded[3]["payload"]
    assert forwarded[3]["visibility"]["redacted_fields"] == ["/payload/content"]
    assert forwarded[4]["payload"]["content"] == "public progress three"
    assert "provider-private-reasoning-secret" not in repr(forwarded)
    assert "new-publisher-private-secret" not in repr(forwarded)


@pytest.mark.asyncio
async def test_web_chat_stream_forwarder_reconnects_after_pubsub_error(monkeypatch):
    import app.services.web_chat_stream_bus as bus

    class FailingPubSub:
        async def subscribe(self, *_args):
            return None

        async def listen(self):
            raise RuntimeError("redis connection lost")
            yield  # pragma: no cover

    class BlockingPubSub:
        async def subscribe(self, *_args):
            return None

        async def listen(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    class FakeRedisWithPubSub:
        def __init__(self, pubsub):
            self._pubsub = pubsub

        def pubsub(self):
            return self._pubsub

    calls = 0
    ready = asyncio.Event()

    async def fake_get_redis():
        nonlocal calls
        calls += 1
        if calls >= 2:
            ready.set()
            return FakeRedisWithPubSub(BlockingPubSub())
        return FakeRedisWithPubSub(FailingPubSub())

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    task = asyncio.create_task(bus.start_web_chat_stream_forwarder(reconnect_delay_seconds=0))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert calls >= 2
        assert bus.web_chat_stream_forwarder_snapshot()["restart_count"] >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
