from __future__ import annotations

import asyncio
import json
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
    await bus.handle_runtime_control_message(
        {
            "schema": bus.RUNTIME_CONTROL_SCHEMA,
            "type": "session_lifecycle_hook",
            "event": "session_idle",
            "agent_id": str(uuid4()),
            "session_id": "session-1",
            "messages": [{"role": "user", "content": "hi"}],
            "source": "websocket",
            "metadata": {"idle_seconds": 180},
        }
    )

    assert calls
    assert str(calls[0][0]) == "session_idle"
    assert calls[0][1]["session_id"] == "session-1"
