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
        # tenant_scoped_session emits `SET LOCAL app.current_tenant_id` before the
        # business query; the GUC statement must not be mistaken for the session row.
        if "app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
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

    agent_uuid = uuid4()
    tenant_uuid = uuid4()

    async def fake_capture_pending_reply(
        db,
        *,
        agent_id,
        tool_name,
        tool_args,
        messages,
        originator_name,
        originator_identity,
        tenant_id=None,
    ):
        captured["agent_id"] = agent_id
        captured["tenant_id"] = tenant_id
        captured["tool_name"] = tool_name
        captured["tool_args"] = tool_args
        captured["messages"] = messages
        captured["originator_name"] = originator_name
        captured["originator_identity"] = originator_identity
        return None

    async def fake_resolve_tenant_for_agent(_agent_id, **_kwargs):
        return tenant_uuid

    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *a, **k: fake_db)
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(
        "app.services.pending_reply_service.extract_recipient_info",
        lambda _tool_name, _tool_args: {"channel": "feishu", "identity": "feishu:u_target", "name": "目标用户"},
    )
    monkeypatch.setattr("app.services.pending_reply_service.capture_pending_reply", fake_capture_pending_reply)

    ctx = HookContext(
        event=HookEvent.POST_TOOL_USE,
        agent_id=agent_uuid,
        session_id=str(uuid4()),
        tool_name="send_feishu_message",
        tool_args={"message": "hello"},
        tool_result="✅ sent",
        messages=[{"role": "user", "content": "[发送者: Alice] 请帮我联系对方"}],
    )

    await hooks_setup._capture_pending_reply(ctx)

    assert captured["originator_name"] == "Alice"
    assert captured["originator_identity"] == "web:alice"
    # tenant resolved from the agent and threaded into the INSERT path (stage-2b)
    assert captured["tenant_id"] == tenant_uuid
    assert fake_db.commits == 1
