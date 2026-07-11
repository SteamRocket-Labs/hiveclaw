from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _RowsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._values))

    def fetchall(self):
        return list(self._values)

    def fetchone(self):
        return self._values[0] if self._values else None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SequenceDB:
    def __init__(self, responses):
        self._responses = list(responses)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self._responses:
            raise AssertionError(f"Unexpected execute call: {stmt}")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_list_conversations_returns_canonical_uuid_for_web_session(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member")
    web_session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        external_conv_id="web_alice",
        delivery_target_json={"channel": "web", "username": "alice", "user_label": "Alice"},
        peer_agent_id=None,
    )
    db = _SequenceDB(
        [
            _RowsResult([web_session]),
            _RowsResult([(3, datetime(2026, 4, 14, 12, 0, tzinfo=UTC))]),
            _ScalarResult("hello from web"),
            _RowsResult([]),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.list_conversations(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "conv_id": str(session_id),
            "partner_type": "user",
            "partner_id": str(user_id),
            "partner_name": "👤 Alice",
            "last_message": "hello from web",
            "message_count": 3,
            "last_at": "2026-04-14T12:00:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_conversation_messages_uses_channel_session_uuid_and_strips_sender_prefix(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=current_user.id,
        source_channel="feishu",
        peer_agent_id=None,
    )
    message = SimpleNamespace(
        id=uuid4(),
        role="user",
        content="[发送者: 张三 (ID: u_123)] 你好",
        created_at=datetime(2026, 4, 14, 12, 30, tzinfo=UTC),
    )
    db = _SequenceDB(
        [
            _ScalarResult(session),
            _RowsResult([message]),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.get_conversation_messages(
        agent_id=agent_id,
        conv_id=str(session_id),
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "id": str(message.id),
            "role": "user",
            "content": "你好",
            "created_at": "2026-04-14T12:30:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_get_conversation_messages_keeps_agent_sender_names_for_agent_session(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    session_id = uuid4()
    participant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=current_user.id,
        source_channel="agent",
        peer_agent_id=uuid4(),
    )
    message = SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="agent reply",
        participant_id=participant_id,
        created_at=datetime(2026, 4, 14, 13, 0, tzinfo=UTC),
    )
    db = _SequenceDB(
        [
            _ScalarResult(session),
            _RowsResult([message]),
            _ScalarResult("执行助手"),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.get_conversation_messages(
        agent_id=agent_id,
        conv_id=str(session_id),
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "id": str(message.id),
            "role": "assistant",
            "sender_name": "执行助手",
            "content": "agent reply",
            "created_at": "2026-04-14T13:00:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_list_conversations_includes_telegram_session_with_delivery_target_label(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member")
    telegram_session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="telegram",
        external_conv_id="tg_123456_789",
        delivery_target_json={"channel": "telegram", "chat_id": 123456, "sender_id": 789, "user_label": "Luna"},
        peer_agent_id=None,
        title="Telegram Luna",
    )
    db = _SequenceDB(
        [
            _RowsResult([telegram_session]),
            _RowsResult([(2, datetime(2026, 4, 14, 14, 0, tzinfo=UTC))]),
            _ScalarResult("ping from telegram"),
            _RowsResult([]),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.list_conversations(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "conv_id": str(session_id),
            "partner_type": "telegram",
            "partner_id": "tg_123456_789",
            "partner_name": "✈️ Telegram Luna",
            "last_message": "ping from telegram",
            "message_count": 2,
            "last_at": "2026-04-14T14:00:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_list_conversations_uses_feishu_delivery_target_label_when_sender_prefix_missing(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member")
    feishu_session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="feishu",
        external_conv_id="feishu_p2p_u_123",
        delivery_target_json={"channel": "feishu", "user_label": "王天怡", "user_id": "u_123"},
        peer_agent_id=None,
        title="Feishu session",
    )
    db = _SequenceDB(
        [
            _RowsResult([feishu_session]),
            _RowsResult([(2, datetime(2026, 4, 14, 14, 30, tzinfo=UTC))]),
            _ScalarResult("普通消息"),
            _ScalarResult("普通消息"),
            _RowsResult([]),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.list_conversations(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "conv_id": str(session_id),
            "partner_type": "feishu",
            "partner_id": "feishu_p2p_u_123",
            "partner_name": "📱 王天怡",
            "last_message": "普通消息",
            "message_count": 2,
            "last_at": "2026-04-14T14:30:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_list_conversations_falls_back_to_user_display_name_for_teams_session(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="member")
    teams_session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="microsoft_teams",
        external_conv_id="19:teams-conversation-id",
        delivery_target_json=None,
        peer_agent_id=None,
        title="Incoming teams thread",
    )
    db = _SequenceDB(
        [
            _RowsResult([teams_session]),
            _RowsResult([(1, datetime(2026, 4, 14, 15, 0, tzinfo=UTC))]),
            _ScalarResult("hello from teams"),
            _ScalarResult("Ava"),
            _RowsResult([]),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)

    payload = await activity_api.list_conversations(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "conv_id": str(session_id),
            "partner_type": "microsoft_teams",
            "partner_id": "19:teams-conversation-id",
            "partner_name": "🪟 Teams Ava",
            "last_message": "hello from teams",
            "message_count": 1,
            "last_at": "2026-04-14T15:00:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]


@pytest.mark.asyncio
async def test_list_conversations_reads_agent_session_stats_from_canonical_session_agent(monkeypatch):
    import app.api.activity as activity_api

    requested_agent_id = uuid4()
    source_agent_id = uuid4()
    session_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member")
    agent_session = SimpleNamespace(
        id=session_id,
        agent_id=source_agent_id,
        user_id=current_user.id,
        source_channel="agent",
        external_conv_id=None,
        delivery_target_json=None,
        peer_agent_id=requested_agent_id,
        title="Agent Pair Session",
    )
    db = _SequenceDB(
        [
            _RowsResult([]),
            _RowsResult([agent_session]),
            _ScalarResult("Source Agent"),
        ]
    )

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id_arg):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id_arg == requested_agent_id
        return SimpleNamespace(id=requested_agent_id), "use"

    async def fake_get_session_message_stats(db_arg, *, agent_id, conversation_id):
        assert db_arg is db
        assert agent_id == source_agent_id
        assert conversation_id == str(session_id)
        return 4, datetime(2026, 4, 14, 16, 0, tzinfo=UTC)

    async def fake_get_last_session_message(db_arg, *, agent_id, conversation_id):
        assert db_arg is db
        assert agent_id == source_agent_id
        assert conversation_id == str(session_id)
        return "agent reply"

    monkeypatch.setattr(activity_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(activity_api, "_get_session_message_stats", fake_get_session_message_stats)
    monkeypatch.setattr(activity_api, "_get_last_session_message", fake_get_last_session_message)

    payload = await activity_api.list_conversations(
        agent_id=requested_agent_id,
        current_user=current_user,
        db=db,
    )

    assert payload == [
        {
            "conv_id": str(session_id),
            "partner_type": "agent",
            "partner_id": str(source_agent_id),
            "partner_name": "🤖 Source Agent",
            "last_message": "agent reply",
            "message_count": 4,
            "last_at": "2026-04-14T16:00:00+00:00",
            "authority_source": "resource_owner",
            "operator_view": False,
        }
    ]
