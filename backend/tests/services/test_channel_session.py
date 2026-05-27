from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or [])


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
async def test_find_or_create_channel_session_reuses_legacy_feishu_open_id_session():
    from app.services.channel_session import find_or_create_channel_session

    agent_id = uuid4()
    user_id = uuid4()
    legacy_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        user_id=uuid4(),
        external_conv_id="feishu_p2p_ou_legacy",
        title="Old Session",
        source_channel="feishu",
        last_message_at=None,
    )
    db = _FakeDB([None, legacy_session])

    session = await find_or_create_channel_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_user_123",
        legacy_external_conv_ids=["feishu_p2p_ou_legacy"],
        source_channel="feishu",
        first_message_title="新的消息",
    )

    assert session is legacy_session
    assert legacy_session.external_conv_id == "feishu_p2p_user_123"
    assert legacy_session.user_id == user_id
    assert db.flush_calls == 1


@pytest.mark.asyncio
async def test_start_new_channel_session_archives_active_external_conversation():
    from app.services.channel_session import start_new_channel_session

    agent_id = uuid4()
    user_id = uuid4()
    old_session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_user_123",
        title="Old Session",
        source_channel="feishu",
        last_message_at=None,
    )
    delivery_target = {"channel": "feishu", "receive_id": "ou_123"}
    db = _FakeDB([[old_session]])

    new_session = await start_new_channel_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id="feishu_p2p_user_123",
        source_channel="feishu",
        title="New Session",
        delivery_target=delivery_target,
    )

    assert old_session.external_conv_id.startswith("feishu_p2p_user_123#archived:")
    assert new_session.external_conv_id == "feishu_p2p_user_123"
    assert new_session.delivery_target_json["session_id"] == str(new_session.id)
    assert db.added == [new_session]
    assert db.flush_calls == 1
