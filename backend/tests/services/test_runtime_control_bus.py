from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeRedis:
    def __init__(self):
        self.published = []

    async def publish(self, channel: str, payload: str):
        self.published.append((channel, payload))
        return 1


@pytest.mark.asyncio
async def test_publish_web_chat_cancel_uses_runtime_control_channel(monkeypatch):
    import app.services.runtime_control_bus as bus

    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()

    await bus.publish_web_chat_cancel(run_id=run_id, agent_id=agent_id, session_id=session_id, user_id=uuid4())

    assert redis.published[0][0] == bus.RUNTIME_CONTROL_CHANNEL
    payload = json.loads(redis.published[0][1])
    assert payload["schema"] == bus.RUNTIME_CONTROL_SCHEMA
    assert payload["type"] == "web_chat_cancel"
    assert payload["run_id"] == run_id.hex
    assert payload["agent_id"] == str(agent_id)
    assert payload["session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_publish_transcript_t0_bridge_uses_runtime_control_channel(monkeypatch):
    import app.services.runtime_control_bus as bus

    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)
    transcript_event_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()

    await bus.publish_transcript_t0_bridge(
        transcript_event_id=transcript_event_id,
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
    )

    assert redis.published[0][0] == bus.RUNTIME_CONTROL_CHANNEL
    payload = json.loads(redis.published[0][1])
    assert payload["schema"] == bus.RUNTIME_CONTROL_SCHEMA
    assert payload["type"] == "transcript_t0_bridge"
    assert payload["transcript_event_id"] == str(transcript_event_id)
    assert payload["agent_id"] == str(agent_id)
    assert payload["session_id"] == str(session_id)
    assert payload["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_publish_session_lifecycle_hook_uses_reference_payload(monkeypatch):
    import app.services.runtime_control_bus as bus

    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(bus, "get_redis", fake_get_redis)

    await bus.publish_session_lifecycle_hook(
        event="session_idle",
        agent_id=uuid4(),
        session_id="session-1",
        messages=[{"role": "user", "content": "large payload that must not ride redis"}],
        source="websocket",
        metadata={"idle_seconds": 180},
    )

    payload = json.loads(redis.published[0][1])
    assert payload["type"] == "session_lifecycle_hook"
    assert "messages" not in payload
    assert payload["metadata"]["idle_seconds"] == 180
    assert payload["metadata"]["message_count"] == 1


@pytest.mark.asyncio
async def test_runtime_control_web_chat_cancel_sets_local_cancel_event():
    import app.services.runtime_control_bus as bus
    from app.services.web_chat_runtime import (
        register_web_chat_run_for_test,
        unregister_web_chat_run_for_test,
    )

    run_id = uuid4().hex
    cancel_event = asyncio.Event()
    register_web_chat_run_for_test(run_id, cancel_event=cancel_event)

    try:
        await bus.handle_runtime_control_message({"schema": bus.RUNTIME_CONTROL_SCHEMA, "type": "web_chat_cancel", "run_id": run_id})
        assert cancel_event.is_set() is True
    finally:
        unregister_web_chat_run_for_test(run_id)


@pytest.mark.asyncio
async def test_runtime_control_session_lifecycle_emits_hook(monkeypatch):
    import app.services.runtime_control_bus as bus

    calls = []

    async def fake_emit_hook(event, **kwargs):
        calls.append((event, kwargs))

    monkeypatch.setattr(bus, "emit_hook", fake_emit_hook)
    monkeypatch.setattr(
        bus,
        "_load_session_lifecycle_messages",
        lambda **_kwargs: [{"role": "user", "content": "loaded from db"}],
    )
    await bus.handle_runtime_control_message(
        {
            "schema": bus.RUNTIME_CONTROL_SCHEMA,
            "type": "session_lifecycle_hook",
            "event": "session_idle",
            "agent_id": str(uuid4()),
            "session_id": "session-1",
            "source": "websocket",
            "metadata": {"idle_seconds": 180},
        }
    )

    assert calls
    assert str(calls[0][0]) == "session_idle"
    assert calls[0][1]["session_id"] == "session-1"
    assert calls[0][1]["messages"] == [{"role": "user", "content": "loaded from db"}]


@pytest.mark.asyncio
async def test_runtime_control_transcript_t0_bridge_dispatches_to_bridge(monkeypatch):
    import app.services.runtime_control_bus as bus

    calls = []

    async def fake_bridge(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(bus, "bridge_transcript_event_to_t0", fake_bridge)

    transcript_event_id = uuid4()
    handled = await bus.handle_runtime_control_message(
        {
            "schema": bus.RUNTIME_CONTROL_SCHEMA,
            "type": "transcript_t0_bridge",
            "transcript_event_id": str(transcript_event_id),
        }
    )

    assert handled is True
    assert calls == [{"transcript_event_id": str(transcript_event_id)}]


@pytest.mark.asyncio
async def test_bridge_transcript_event_to_t0_writes_ledger_and_marks_metadata(monkeypatch, tmp_path):
    import app.services.runtime_control_bus as bus
    from app.memory.t0.ledger import replay_t0_session_events

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    message_id = uuid4()
    transcript_event_id = uuid4()
    transcript_event = SimpleNamespace(
        id=transcript_event_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        message_id=message_id,
        event_type="assistant_message",
        content="bridged answer",
        created_at=datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc),
        metadata_json={"role": "assistant", "source": "web", "transcript_event_id": str(transcript_event_id)},
    )
    chat_message = SimpleNamespace(id=message_id, user_id=user_id)

    class ScalarResult:
        def scalar_one_or_none(self):
            return transcript_event

    class FakeSession:
        def __init__(self):
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _stmt):
            return ScalarResult()

        async def get(self, _model, key):
            return chat_message if key == message_id else None

        async def commit(self):
            self.commits += 1

    class Bypass:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return None

    fake_session = FakeSession()
    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr("app.database.enter_rls_bypass", lambda session, **_kwargs: Bypass(session))
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=transcript_event_id) is True
    assert fake_session.commits == 1
    assert transcript_event.metadata_json["t0_bridge_pending"] is False
    assert transcript_event.metadata_json["t0_bridge_relay_source"] == "runtime_control_bus"

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content, event.actor_id) for event in events] == [
        ("assistant_message", "assistant", "bridged answer", user_id.hex)
    ]

    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=transcript_event_id) is True
    assert len(replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)) == 1

    transcript_event.metadata_json = {"role": "assistant", "source": "web", "transcript_event_id": str(transcript_event_id)}
    assert await bus.bridge_transcript_event_to_t0(transcript_event_id=transcript_event_id) is True
    assert len(replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)) == 1


@pytest.mark.asyncio
async def test_runtime_control_listener_reconnects_after_pubsub_error(monkeypatch):
    import app.services.runtime_control_bus as bus

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
    task = asyncio.create_task(bus.start_runtime_control_listener(reconnect_delay_seconds=0))
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert calls >= 2
        assert bus.runtime_control_bus_snapshot()["restart_count"] >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
