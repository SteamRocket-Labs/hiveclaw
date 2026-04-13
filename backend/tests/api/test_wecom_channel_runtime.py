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


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if "gettoken" in url:
            return _FakeHttpResponse({"access_token": "tok"})
        return _FakeHttpResponse({"errcode": 0, "name": "张三"})


@pytest.mark.asyncio
async def test_process_wecom_text_sets_delivery_target_session_and_execution_identity(monkeypatch):
    import app.api.wecom as wecom_api
    from app.core.execution_context import clear_execution_identity
    from app.services.channel_delivery_service import channel_delivery_target

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    session_id = uuid4()
    config = SimpleNamespace(
        agent_id=agent_id,
        app_id="corp-id",
        app_secret="corp-secret",
        extra_config={"wecom_agent_id": "1000002"},
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    platform_user = SimpleNamespace(id=platform_user_id, username="wecom_zhangsan", display_name="张三")
    captured: dict[str, object] = {}
    db = _SequenceSession([
        _ScalarResult(agent),
        _ScalarResult(platform_user),
        _RowsResult([]),
    ])

    async def fake_find_or_create_channel_session(*, delivery_target=None, external_conv_id=None, **_kwargs):
        captured["external_conv_id"] = external_conv_id
        captured["session_delivery_target"] = dict(delivery_target or {})
        return SimpleNamespace(id=session_id, last_message_at=None, delivery_target_json=delivery_target)

    async def fake_call_agent_llm(_db, _agent_id, _user_text, **kwargs):
        from app.core.execution_context import get_execution_identity

        captured["llm_kwargs"] = kwargs
        captured["runtime_delivery_target"] = channel_delivery_target.get()
        captured["execution_identity"] = get_execution_identity()
        return "WeCom reply"

    async def fake_send_wecom_text_message(**kwargs):
        captured["send_kwargs"] = kwargs
        return {"errcode": 0}

    async def fake_compute_history_limit_for_agent(_agent_id):
        return 10

    monkeypatch.setattr("app.database.async_session", lambda: db)
    monkeypatch.setattr("app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent)
    monkeypatch.setattr("app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session)
    monkeypatch.setattr("app.api.feishu._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(wecom_api, "_send_wecom_text_message", fake_send_wecom_text_message)
    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=5: _FakeHttpClient())

    clear_execution_identity()
    await wecom_api._process_wecom_text(db, agent_id, config, "zhangsan", "你好")

    assert captured["external_conv_id"] == "wecom_p2p_zhangsan"
    assert captured["session_delivery_target"] == {
        "channel": "wecom",
        "user_id": "zhangsan",
        "user_label": "张三",
    }
    assert captured["runtime_delivery_target"] == {
        "channel": "wecom",
        "user_id": "zhangsan",
        "user_label": "张三",
        "session_id": str(session_id),
    }
    assert captured["llm_kwargs"]["session_id"] == str(session_id)
    assert captured["llm_kwargs"]["session_source"] == "wecom"
    assert captured["llm_kwargs"]["session_channel"] == "wecom"
    assert captured["execution_identity"].identity_type == "delegated_user"
    assert captured["execution_identity"].identity_id == platform_user_id
    assert captured["execution_identity"].label == "张三 via wecom"
    assert captured["send_kwargs"]["to_user"] == "zhangsan"
