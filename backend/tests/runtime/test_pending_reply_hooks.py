from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.hooks import HookContext, HookEvent


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, session_obj):
        self._session_obj = session_obj
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return _ScalarResult(self._session_obj)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_capture_pending_reply_uses_session_delivery_target_for_originator_identity(monkeypatch):
    from app.runtime import hooks_setup

    session_obj = SimpleNamespace(
        delivery_target_json={"channel": "web", "username": "alice", "user_label": "Alice"},
        external_conv_id="web_legacy",
    )
    fake_db = _FakeDB(session_obj)
    captured: dict[str, object] = {}

    async def fake_capture_pending_reply(
        db,
        *,
        agent_id,
        tool_name,
        tool_args,
        messages,
        originator_name,
        originator_identity,
    ):
        captured["agent_id"] = agent_id
        captured["tool_name"] = tool_name
        captured["tool_args"] = tool_args
        captured["messages"] = messages
        captured["originator_name"] = originator_name
        captured["originator_identity"] = originator_identity
        return None

    monkeypatch.setattr("app.database.async_session", lambda: fake_db)
    monkeypatch.setattr(
        "app.services.pending_reply_service.extract_recipient_info",
        lambda _tool_name, _tool_args: {"channel": "feishu", "identity": "feishu:u_target", "name": "目标用户"},
    )
    monkeypatch.setattr("app.services.pending_reply_service.capture_pending_reply", fake_capture_pending_reply)

    ctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        agent_id=uuid4(),
        session_id=str(uuid4()),
        tool_name="send_feishu_message",
        tool_args={"message": "hello"},
        tool_result="✅ sent",
        messages=[{"role": "user", "content": "[发送者: Alice] 请帮我联系对方"}],
    )

    await hooks_setup._capture_pending_reply(ctx)

    assert captured["originator_name"] == "Alice"
    assert captured["originator_identity"] == "web:alice"
    assert fake_db.commits == 1
