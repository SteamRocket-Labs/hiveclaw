from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


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


@pytest.mark.asyncio
async def test_find_or_create_feishu_chat_session_uses_canonical_user_id_and_legacy_open_id(monkeypatch):
    from app.services import feishu_identity_maintenance

    agent_id = uuid4()
    user_id = uuid4()
    db = object()
    session = SimpleNamespace(id=uuid4())
    captured = {}

    async def _fake_find_or_create_channel_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session",
        _fake_find_or_create_channel_session,
    )

    result = await feishu_identity_maintenance.find_or_create_feishu_chat_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        provider_user_id=" u_staff_123 ",
        provider_open_id=" ou_app_scoped ",
        first_message_title="[Agent → 王天怡]",
    )

    assert result is session
    assert captured["db"] is db
    assert captured["agent_id"] == agent_id
    assert captured["user_id"] == user_id
    assert captured["source_channel"] == "feishu"
    assert captured["external_conv_id"] == "feishu_p2p_u_staff_123"
    assert captured["legacy_external_conv_ids"] == ["feishu_p2p_ou_app_scoped"]
    assert captured["first_message_title"] == "[Agent → 王天怡]"
