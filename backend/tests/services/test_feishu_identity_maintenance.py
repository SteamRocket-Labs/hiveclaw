from __future__ import annotations

from datetime import datetime, timezone
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
        self.deleted = []
        self.flush_calls = 0
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self._responses:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self._responses.pop(0))

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flush_calls += 1

def test_build_feishu_p2p_conv_id_prefers_user_id():
    from app.services.feishu_identity_maintenance import build_feishu_p2p_conv_id

    assert build_feishu_p2p_conv_id("u_123", "ou_456") == "feishu_p2p_u_123"
    assert build_feishu_p2p_conv_id(None, "ou_456") == "feishu_p2p_ou_456"


def test_choose_canonical_feishu_user_prefers_stable_user_id_real_email_then_oldest():
    from app.services.feishu_identity_maintenance import choose_canonical_feishu_user

    older = datetime(2025, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2025, 2, 1, tzinfo=timezone.utc)

    duplicate_with_fake_email = SimpleNamespace(
        id="dup-fake",
        email="dup@feishu.local",
        feishu_user_id="u_123",
        created_at=older,
    )
    canonical = SimpleNamespace(
        id="canonical",
        email="real@company.com",
        feishu_user_id="u_123",
        created_at=newer,
    )
    weak_candidate = SimpleNamespace(
        id="weak",
        email="weak@company.com",
        feishu_user_id=None,
        created_at=older,
    )

    picked = choose_canonical_feishu_user([duplicate_with_fake_email, canonical, weak_candidate])

    assert picked is canonical


def test_build_feishu_session_lookup_ids_uses_canonical_then_legacy_alias():
    from app.services.feishu_identity_maintenance import build_feishu_session_lookup_ids

    conv_id, legacy_ids = build_feishu_session_lookup_ids(
        provider_user_id="u_123",
        provider_open_id="ou_456",
    )

    assert conv_id == "feishu_p2p_u_123"
    assert legacy_ids == ["feishu_p2p_ou_456"]


@pytest.mark.asyncio
async def test_find_or_create_feishu_chat_session_routes_alias_logic_through_helper(monkeypatch):
    from app.services import feishu_identity_maintenance as maintenance

    agent_id = uuid4()
    user_id = uuid4()
    session = SimpleNamespace(id=uuid4())
    captured = {}
    db = _FakeDB([None, None])

    async def fake_find_or_create_channel_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr(maintenance, "find_or_create_channel_session", fake_find_or_create_channel_session)

    result = await maintenance.find_or_create_feishu_chat_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        provider_user_id="u_123",
        provider_open_id="ou_456",
        first_message_title="你好",
        delivery_target={"channel": "feishu"},
    )

    assert result is session
    assert captured["agent_id"] == agent_id
    assert captured["user_id"] == user_id
    assert captured["external_conv_id"] == "feishu_p2p_u_123"
    assert captured["source_channel"] == "feishu"
    assert "legacy_external_conv_ids" not in captured


@pytest.mark.asyncio
async def test_find_or_create_feishu_chat_session_reuses_legacy_alias_session():
    from app.services.feishu_identity_maintenance import find_or_create_feishu_chat_session

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
        delivery_target_json=None,
    )
    db = _FakeDB([None, legacy_session])

    session = await find_or_create_feishu_chat_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        provider_user_id="u_123",
        provider_open_id="ou_legacy",
        first_message_title="新的消息",
        delivery_target={"channel": "feishu"},
    )

    assert session is legacy_session
    assert legacy_session.external_conv_id == "feishu_p2p_u_123"
    assert legacy_session.user_id == user_id
    assert legacy_session.delivery_target_json == {"channel": "feishu"}
    assert db.flush_calls == 1
