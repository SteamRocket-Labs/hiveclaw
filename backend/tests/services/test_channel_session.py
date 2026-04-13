from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, responses):
        self._responses = list(responses)
        self.added = []
        self.flush_calls = 0

    async def execute(self, _stmt):
        if not self._responses:
            raise AssertionError("No more queued execute responses")
        return _ScalarResult(self._responses.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_find_or_create_channel_session_reattributes_existing_session_without_legacy_aliases():
    from app.services.channel_session import find_or_create_channel_session

    agent_id = uuid4()
    user_id = uuid4()
    existing_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        user_id=uuid4(),
        external_conv_id="slack_dm_123",
        title="Existing Session",
        source_channel="slack",
        last_message_at=None,
        delivery_target_json=None,
    )
    db = _FakeDB([existing_session])

    session = await find_or_create_channel_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="slack_dm_123",
        source_channel="slack",
        first_message_title="新的消息",
        delivery_target={"channel": "slack", "thread_ts": "123"},
    )

    assert session is existing_session
    assert existing_session.user_id == user_id
    assert existing_session.delivery_target_json == {"channel": "slack", "thread_ts": "123"}
    assert db.flush_calls == 0
