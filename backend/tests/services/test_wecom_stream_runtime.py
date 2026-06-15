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
async def test_process_wecom_stream_message_binds_group_sender_context(monkeypatch):
    import app.services.wecom_stream as wecom_stream
    from app.core.execution_context import clear_execution_identity
    from app.services.channel_delivery_service import channel_delivery_target

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    platform_user = SimpleNamespace(id=platform_user_id, username="wecom_zhangsan", display_name="张三")
    captured: dict[str, object] = {}
    db = _SequenceSession(
        [
            _ScalarResult(agent),
            _ScalarResult(platform_user),
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
        return "stream reply"

    async def fake_compute_history_limit_for_agent(_agent_id):
        return 10

    async def fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr("app.database.async_session", lambda: db)
    monkeypatch.setattr("app.services.wecom_stream.tenant_scoped_session", lambda *a, **k: db)
    monkeypatch.setattr("app.services.wecom_stream.resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(
        "app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent
    )
    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session
    )
    monkeypatch.setattr("app.services.channel_agent_runtime.call_agent_llm", fake_call_agent_llm)

    clear_execution_identity()
    reply = await wecom_stream._process_wecom_stream_message(
        agent_id=agent_id,
        sender_id="zhangsan",
        user_text="会议安排好了",
        chat_id="sales-room",
        chat_type="group",
    )

    assert reply == "stream reply"
    assert captured["external_conv_id"] == "wecom_group_sales-room_zhangsan"
    assert captured["session_delivery_target"] == {
        "channel": "wecom",
        "user_id": "zhangsan",
        "chat_id": "sales-room",
        "user_label": "张三",
    }
    assert captured["runtime_delivery_target"] == {
        "channel": "wecom",
        "user_id": "zhangsan",
        "chat_id": "sales-room",
        "user_label": "张三",
        "session_id": str(session_id),
    }
    assert captured["llm_kwargs"]["session_id"] == str(session_id)
    assert captured["llm_kwargs"]["session_source"] == "wecom"
    assert captured["llm_kwargs"]["session_channel"] == "wecom"
    assert captured["execution_identity"].identity_type == "delegated_user"
    assert captured["execution_identity"].identity_id == platform_user_id
    assert captured["execution_identity"].label == "张三 via wecom"
