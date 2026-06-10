from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_process_wechat_message_sets_sender_scoped_identity_and_session_contract(monkeypatch):
    import app.services.wechat_personal_stream as wechat_stream
    from app.core.execution_context import clear_execution_identity
    from app.services.channel_delivery_service import channel_delivery_target

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    creator_id = uuid4()
    session_id = uuid4()
    captured: dict[str, object] = {}

    db = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=creator_id)),
            _ScalarResult(
                SimpleNamespace(id=platform_user_id, username="wechat_wxid_abc", display_name="WeChat wxid_abc")
            ),
            _RowsResult([]),
        ]
    )

    async def fake_find_or_create_channel_session(*, delivery_target=None, external_conv_id=None, **_kwargs):
        captured["external_conv_id"] = external_conv_id
        captured["session_delivery_target"] = dict(delivery_target or {})
        return SimpleNamespace(id=session_id, last_message_at=None, delivery_target_json=delivery_target)

    async def fake_call_agent_llm(_db, _agent_id, _user_text, **kwargs):
        from app.core.execution_context import get_execution_identity

        captured["llm_kwargs"] = kwargs
        captured["runtime_delivery_target"] = channel_delivery_target.get()
        captured["execution_identity"] = get_execution_identity()
        return "wechat reply"

    async def fake_compute_history_limit_for_agent(_agent_id):
        return 10

    async def fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr("app.database.async_session", lambda: db)
    monkeypatch.setattr("app.services.wechat_personal_stream.tenant_scoped_session", lambda *a, **k: db)
    monkeypatch.setattr("app.services.wechat_personal_stream.resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(
        "app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent
    )
    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session
    )
    monkeypatch.setattr("app.api.feishu._call_agent_llm", fake_call_agent_llm)

    clear_execution_identity()
    token = channel_delivery_target.set(
        {
            "channel": "wechat_personal",
            "to_user_id": "wxid_abc",
            "context_token": "ctx-1",
            "user_label": "WeChat wxid_abc",
        }
    )
    try:
        reply = await wechat_stream._process_wechat_message(
            agent_id=agent_id,
            sender_id="wxid_abc",
            user_text="你好",
            delivery_target={
                "channel": "wechat_personal",
                "to_user_id": "wxid_abc",
                "context_token": "ctx-1",
                "user_label": "WeChat wxid_abc",
            },
        )
    finally:
        channel_delivery_target.reset(token)

    assert reply == "wechat reply"
    assert captured["external_conv_id"] == "wechat_p2p_wxid_abc"
    assert captured["session_delivery_target"] == {
        "channel": "wechat_personal",
        "to_user_id": "wxid_abc",
        "context_token": "ctx-1",
        "user_label": "WeChat wxid_abc",
    }
    assert captured["runtime_delivery_target"] == {
        "channel": "wechat_personal",
        "to_user_id": "wxid_abc",
        "context_token": "ctx-1",
        "user_label": "WeChat wxid_abc",
        "session_id": str(session_id),
    }
    assert captured["llm_kwargs"]["session_id"] == str(session_id)
    assert captured["llm_kwargs"]["session_source"] == "wechat_personal"
    assert captured["llm_kwargs"]["session_channel"] == "wechat_personal"
    assert captured["llm_kwargs"]["allow_bare_plan_confirmation"] is True
    assert captured["execution_identity"].identity_type == "delegated_user"
    assert captured["execution_identity"].identity_id == platform_user_id
    assert captured["execution_identity"].label == "WeChat wxid_abc via wechat_personal"
    assert captured["execution_identity"].identity_id != creator_id
